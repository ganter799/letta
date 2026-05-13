"""OIDC user provisioning and org resolution for Letta.

Flow
----
1. Middleware decodes the JWT and calls ``get_or_provision_user_for_claims``.
2. On first login a Letta user (and optionally an org) is created and an
   ``OAuthIdentity`` row ties the OIDC ``(iss, sub)`` pair to that user.
3. On subsequent logins the existing user is returned immediately.
4. When ``LETTA_OAUTH_ORG_REASSIGN=true`` and the resolved org differs from
   the stored ``organization_id``, the user row is updated and all rows that
   belong to that user are bulk-moved to the new org in a single transaction
   (see :func:`_reassign_user_org` for the table list and locking notes).

Environment variables (all optional)
-------------------------------------
LETTA_OAUTH_ORG_CLAIM        JWT claim name that carries the org identifier
                              (e.g. ``groups``, ``org``, ``tenant``).
LETTA_OAUTH_ORG_MAP_JSON     JSON mapping claim-value -> org_id for explicit
                              routing.  Unrecognised values fall through to
                              autocreate if enabled.
LETTA_OAUTH_ORG_AUTOCREATE   ``true`` to create an org from the claim value
                              when it has no map entry.
LETTA_OAUTH_ORG_REASSIGN     ``true`` to move an existing user to a different
                              org when the resolved org changes on login.
                              **Default off** — omitting this flag preserves
                              existing behaviour exactly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, update
from sqlalchemy.ext.asyncio import AsyncSession

from letta.orm.oauth_identity import OAuthIdentity as OAuthIdentityModel
from letta.orm.organization import Organization as OrganizationModel
from letta.orm.user import User as UserModel
from letta.schemas.organization import Organization as PydanticOrganization
from letta.schemas.user import User as PydanticUser
from letta.server.db import db_registry
from letta.settings import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OrgResolverConfig
# ---------------------------------------------------------------------------


@dataclass
class OrgResolverConfig:
    """Holds all env-derived config for org resolution.

    Populated once at startup via :meth:`from_env`; never mutated at runtime.
    """

    org_claim: Optional[str] = None
    org_map: Dict[str, str] = field(default_factory=dict)
    autocreate: bool = False
    org_reassign: bool = False

    @classmethod
    def from_env(cls) -> "OrgResolverConfig":
        """Read config from :mod:`letta.settings`.

        All four knobs honour the corresponding ``LETTA_OAUTH_*`` env vars via
        pydantic-settings in :class:`letta.settings.Settings`.
        """
        org_map: Dict[str, str] = {}
        if settings.oauth_org_map_json:
            try:
                raw = json.loads(settings.oauth_org_map_json)
                if not isinstance(raw, dict):
                    raise ValueError("LETTA_OAUTH_ORG_MAP_JSON must be a JSON object")
                org_map = {str(k): str(v) for k, v in raw.items()}
            except (json.JSONDecodeError, ValueError) as exc:
                logger.error("Failed to parse LETTA_OAUTH_ORG_MAP_JSON: %s", exc)

        return cls(
            org_claim=settings.oauth_org_claim or None,
            org_map=org_map,
            autocreate=settings.oauth_org_autocreate,
            org_reassign=settings.oauth_org_reassign,
        )


# ---------------------------------------------------------------------------
# derive_org_id
# ---------------------------------------------------------------------------


async def derive_org_id(
    config: OrgResolverConfig,
    claims: Dict[str, Any],
    db_session: AsyncSession,
) -> Optional[str]:
    """Resolve an org_id from OIDC ``claims`` according to *config*.

    Returns
    -------
    str | None
        The resolved org_id, or ``None`` when ``config.org_claim`` is unset or
        the claim is absent and autocreate is disabled.

    Resolution order
    ----------------
    1. Read ``config.org_claim`` from *claims*.
    2. Look up the value in ``config.org_map``; return immediately on a hit.
    3. If ``config.autocreate`` is ``True``, create a new org named after the
       claim value and return its id.
    4. Return ``None``.
    """
    if not config.org_claim:
        return None

    # Claims may be a list (e.g. "groups") or a scalar string.
    raw = claims.get(config.org_claim)
    if raw is None:
        return None

    # When the claim is a list, use the first element.
    claim_value: str = raw[0] if isinstance(raw, list) else str(raw)

    # Explicit map hit.
    if claim_value in config.org_map:
        return config.org_map[claim_value]

    # Autocreate.
    if config.autocreate:
        org = OrganizationModel(name=claim_value)
        db_session.add(org)
        await db_session.flush()  # populate id without committing
        logger.info("Auto-created org %s (name=%r) from claim %r", org.id, claim_value, config.org_claim)
        return org.id

    logger.debug(
        "Claim %r value %r has no org_map entry and autocreate is disabled; returning None",
        config.org_claim,
        claim_value,
    )
    return None


# ---------------------------------------------------------------------------
# _reassign_user_org  (internal)
# ---------------------------------------------------------------------------

# Tables that carry organization_id and can be re-parented by filtering on
# _created_by_id.  These are bounded per-user so a single transaction is safe.
#
# Large fan-out tables (messages, passages, conversation_messages, run_metrics,
# provider_trace_metadata, llm_batch_items, files, files_agents, passage_tags)
# are intentionally excluded here; see the PR description for the background-
# job design that handles them asynchronously.

_DIRECT_ORG_TABLES: List[str] = [
    "agents",
    "blocks",
    "identities",
    "sources",
    "tools",
    "providers",
    "groups",
    "conversations",
    "archives",
    "runs",
    "sandbox_configs",
    "mcp_server",
    "llm_batch_jobs",
    "provider_traces",
]

# Tables that are children of other re-parented tables and need a nested UPDATE.
# Keyed by parent table; value is (child_table, child_fk_column, parent_id_column).
_CHILD_ORG_TABLES: List[tuple] = [
    # (child_table, child_fk_col, parent_table, parent_id_col)
    ("sandbox_environment_variables", "sandbox_config_id", "sandbox_configs", "id"),
    ("agent_environment_variables", "agent_id", "agents", "id"),
    ("mcp_tools", "mcp_server_id", "mcp_server", "id"),
]


async def _reassign_user_org(
    session: AsyncSession,
    user_id: str,
    old_org_id: str,
    new_org_id: str,
) -> None:
    """Move all rows owned by *user_id* from *old_org_id* to *new_org_id*.

    Called inside an already-open transaction; does NOT commit.

    Locking note
    ------------
    Each ``UPDATE … WHERE organization_id = :old AND _created_by_id = :uid``
    acquires row-level locks.  In PostgreSQL these are released on commit (not
    immediately), so concurrent requests for the same user that arrive during
    reassignment will queue behind this transaction.  This is acceptable for
    typical login rates but could cause latency spikes in burst scenarios.

    Idempotency
    -----------
    If *old_org_id == new_org_id* the caller must not invoke this function
    (see :func:`get_or_provision_user_for_claims`).  If called twice with the
    same arguments the WHERE clauses simply match zero rows on the second run.
    """
    from sqlalchemy import text

    # 1. Re-parent direct tables by _created_by_id.
    for table in _DIRECT_ORG_TABLES:
        stmt = text(
            f"UPDATE {table} SET organization_id = :new_org "  # noqa: S608 — table name is from a closed constant list
            f"WHERE organization_id = :old_org AND _created_by_id = :uid"
        )
        await session.execute(stmt, {"new_org": new_org_id, "old_org": old_org_id, "uid": user_id})

    # 2. Re-parent child tables using a correlated sub-select.
    for child_table, child_fk, parent_table, parent_id in _CHILD_ORG_TABLES:
        stmt = text(
            f"UPDATE {child_table} SET organization_id = :new_org "  # noqa: S608
            f"WHERE organization_id = :old_org "
            f"AND {child_fk} IN ("
            f"  SELECT {parent_id} FROM {parent_table} "
            f"  WHERE organization_id = :new_org AND _created_by_id = :uid"
            f")"
        )
        await session.execute(stmt, {"new_org": new_org_id, "old_org": old_org_id, "uid": user_id})

    # 3. mcp_oauth is keyed by user_id directly.
    stmt = text(
        "UPDATE mcp_oauth SET organization_id = :new_org "  # noqa: S608
        "WHERE organization_id = :old_org AND user_id = :uid"
    )
    await session.execute(stmt, {"new_org": new_org_id, "old_org": old_org_id, "uid": user_id})

    # 4. Update the user row itself last (FK from all child tables has already
    #    been updated above so this won't orphan anything).
    stmt = text("UPDATE users SET organization_id = :new_org WHERE id = :uid")
    await session.execute(stmt, {"new_org": new_org_id, "uid": user_id})

    logger.info(
        "Reassigned user %s from org %s to org %s (direct tables: %s)",
        user_id,
        old_org_id,
        new_org_id,
        ", ".join(_DIRECT_ORG_TABLES),
    )


# ---------------------------------------------------------------------------
# get_or_provision_user_for_claims  (public API)
# ---------------------------------------------------------------------------


async def get_or_provision_user_for_claims(
    claims: Dict[str, Any],
    config: Optional[OrgResolverConfig] = None,
    default_org_id: Optional[str] = None,
) -> PydanticUser:
    """Return (or create) the Letta user for the OIDC ``(iss, sub)`` pair.

    Parameters
    ----------
    claims:
        Decoded JWT claims dict.  Must contain ``"iss"`` and ``"sub"``.
    config:
        Resolver configuration.  When ``None``, :meth:`OrgResolverConfig.from_env`
        is called to read settings from environment.
    default_org_id:
        Org to assign when ``derive_org_id`` returns ``None``.  Falls back to
        ``letta.constants.DEFAULT_ORG_ID`` when not provided.

    Returns
    -------
    :class:`letta.schemas.user.User`
        The provisioned or retrieved user.

    Idempotency
    -----------
    Calling this function twice with identical inputs is a no-op: the second
    call finds the existing ``OAuthIdentity`` row and returns the same user.
    When ``org_reassign`` is enabled and the resolved org matches what is
    already stored, no UPDATE is issued.
    """
    from letta.constants import DEFAULT_ORG_ID

    iss: str = claims["iss"]
    sub: str = claims["sub"]

    if config is None:
        config = OrgResolverConfig.from_env()

    if default_org_id is None:
        default_org_id = DEFAULT_ORG_ID

    async with db_registry.async_session() as session:
        async with session.begin():
            # ------------------------------------------------------------------
            # Look up existing identity mapping.
            # ------------------------------------------------------------------
            from sqlalchemy import select

            stmt = select(OAuthIdentityModel).where(
                and_(
                    OAuthIdentityModel.iss == iss,
                    OAuthIdentityModel.sub == sub,
                )
            )
            result = await session.execute(stmt)
            identity_row = result.scalar_one_or_none()

            if identity_row is not None:
                # Existing user — possibly reassign org.
                user_row = await session.get(UserModel, identity_row.user_id)
                if user_row is None:
                    raise RuntimeError(
                        f"OAuthIdentity {iss}/{sub} points to missing user {identity_row.user_id}"
                    )

                if config.org_reassign:
                    resolved_org_id = await derive_org_id(config, claims, session) or default_org_id
                    if user_row.organization_id != resolved_org_id:
                        logger.info(
                            "LETTA_OAUTH_ORG_REASSIGN: moving user %s from org %s -> %s",
                            user_row.id,
                            user_row.organization_id,
                            resolved_org_id,
                        )
                        await _reassign_user_org(
                            session=session,
                            user_id=user_row.id,
                            old_org_id=user_row.organization_id,
                            new_org_id=resolved_org_id,
                        )
                        # Reflect the change on the in-memory object so the
                        # returned Pydantic model has the new org.
                        await session.refresh(user_row)

                return user_row.to_pydantic()

            # ------------------------------------------------------------------
            # First login — provision user (and optionally org).
            # ------------------------------------------------------------------
            resolved_org_id = await derive_org_id(config, claims, session) or default_org_id

            # Ensure the org exists (it might be a well-known id like DEFAULT_ORG_ID).
            org = await session.get(OrganizationModel, resolved_org_id)
            if org is None:
                raise ValueError(
                    f"Resolved org_id={resolved_org_id!r} does not exist in the database. "
                    "Create it first or enable LETTA_OAUTH_ORG_AUTOCREATE."
                )

            # Derive a display name from claims (preferred_username > email > sub).
            display_name: str = (
                claims.get("preferred_username")
                or claims.get("email")
                or claims.get("name")
                or sub
            )

            user_row = UserModel(name=display_name, organization_id=resolved_org_id)
            session.add(user_row)
            await session.flush()  # populate user_row.id

            oidc_row = OAuthIdentityModel(iss=iss, sub=sub, user_id=user_row.id)
            session.add(oidc_row)

            logger.info(
                "Provisioned user %s (name=%r) for iss=%r sub=%r org=%s",
                user_row.id,
                display_name,
                iss,
                sub,
                resolved_org_id,
            )

            return user_row.to_pydantic()
