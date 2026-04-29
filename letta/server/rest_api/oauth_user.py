import hashlib
import json
import os
import uuid
from typing import Iterable, Mapping, Optional, Sequence

from letta.log import get_logger

logger = get_logger(__name__)


def _deterministic_uuid4(seed: str) -> uuid.UUID:
    """Derive a stable UUID-shaped value with v4 version/variant bits set."""
    digest = bytearray(hashlib.sha256(seed.encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(digest))


def derive_user_id(issuer: str, subject: str) -> str:
    """Map an OIDC (iss, sub) pair to a Letta user id deterministically."""
    return f"user-{_deterministic_uuid4(f'{issuer}|{subject}')}"


def derive_org_id(issuer: str, claim_value: str) -> str:
    """Map an OIDC org claim value to a Letta org id deterministically."""
    return f"org-{_deterministic_uuid4(f'{issuer}|org|{claim_value}')}"


def _display_name_for_claims(claims: Mapping[str, object]) -> str:
    for key in ("preferred_username", "email", "name", "sub"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "oauth-user"


def _claim_values(claim: object) -> Sequence[str]:
    if isinstance(claim, str):
        return [claim] if claim.strip() else []
    if isinstance(claim, Iterable):
        return [v for v in claim if isinstance(v, str) and v.strip()]
    return []


class OrgResolverConfig:
    """Configuration for mapping an OIDC claim onto a Letta organization.

    Attributes:
        claim_name: Name of the claim to read (e.g. ``"groups"``, ``"org"``).
            If empty/unset, no mapping is performed and the default org is used.
        explicit_map: Mapping of claim value -> Letta org id. Takes precedence.
        autocreate: When True, claim values without an explicit mapping are
            materialized as new orgs (id derived from the (iss, value) pair).
    """

    __slots__ = ("claim_name", "explicit_map", "autocreate")

    def __init__(
        self,
        claim_name: Optional[str] = None,
        explicit_map: Optional[Mapping[str, str]] = None,
        autocreate: bool = False,
    ):
        self.claim_name = (claim_name or "").strip() or None
        self.explicit_map = dict(explicit_map or {})
        self.autocreate = autocreate

    @classmethod
    def from_env(cls) -> "OrgResolverConfig":
        raw_map = os.getenv("LETTA_OAUTH_ORG_MAP", "").strip()
        explicit_map: dict = {}
        if raw_map:
            try:
                parsed = json.loads(raw_map)
                if isinstance(parsed, dict):
                    explicit_map = {str(k): str(v) for k, v in parsed.items()}
                else:
                    logger.warning("LETTA_OAUTH_ORG_MAP must be a JSON object; ignoring")
            except json.JSONDecodeError as exc:
                logger.warning("LETTA_OAUTH_ORG_MAP is not valid JSON: %s", exc)

        return cls(
            claim_name=os.getenv("LETTA_OAUTH_ORG_CLAIM"),
            explicit_map=explicit_map,
            autocreate=os.getenv("LETTA_OAUTH_ORG_AUTOCREATE", "").lower() in {"1", "true", "yes"},
        )


async def resolve_org_for_claims(
    claims: Mapping[str, object],
    config: OrgResolverConfig,
) -> str:
    """Resolve a claim payload to a Letta organization id.

    Falls back to ``DEFAULT_ORG_ID`` when no org claim is configured, the claim
    is missing, or the value is not in the explicit map and autocreate is off.
    """
    from letta.constants import DEFAULT_ORG_ID
    from letta.orm.errors import NoResultFound
    from letta.schemas.organization import Organization as PydanticOrganization
    from letta.services.organization_manager import OrganizationManager

    if not config.claim_name:
        return DEFAULT_ORG_ID

    issuer = claims.get("iss")
    if not isinstance(issuer, str):
        return DEFAULT_ORG_ID

    values = _claim_values(claims.get(config.claim_name))
    if not values:
        return DEFAULT_ORG_ID

    manager = OrganizationManager()

    for value in values:
        mapped = config.explicit_map.get(value)
        if mapped:
            return mapped

    if not config.autocreate:
        return DEFAULT_ORG_ID

    target = values[0]
    org_id = derive_org_id(issuer, target)
    try:
        await manager.get_organization_by_id_async(org_id)
        return org_id
    except NoResultFound:
        pass

    try:
        await manager.create_organization_async(
            PydanticOrganization(id=org_id, name=target)
        )
    except Exception as exc:
        # Concurrent autocreate; re-read.
        try:
            await manager.get_organization_by_id_async(org_id)
        except NoResultFound:
            logger.error("OAuth: failed to autocreate org %s: %s", org_id, exc)
            return DEFAULT_ORG_ID
    return org_id


async def get_or_provision_user_for_claims(
    claims: Mapping[str, object],
    org_resolver: Optional[OrgResolverConfig] = None,
) -> Optional[str]:
    """Resolve an OIDC subject to a Letta user id, creating the user on first
    sight and refreshing the display name when it has changed in the IdP.
    """
    from letta.orm.errors import NoResultFound
    from letta.schemas.user import User as PydanticUser, UserUpdate
    from letta.services.user_manager import UserManager

    issuer = claims.get("iss")
    subject = claims.get("sub")
    if not isinstance(issuer, str) or not isinstance(subject, str):
        return None

    user_id = derive_user_id(issuer, subject)
    desired_name = _display_name_for_claims(claims)
    org_id = await resolve_org_for_claims(claims, org_resolver or OrgResolverConfig())

    manager = UserManager()
    try:
        existing = await manager.get_actor_by_id_async(user_id)
    except NoResultFound:
        existing = None

    if existing is not None:
        if existing.name != desired_name:
            try:
                await manager.update_actor_async(
                    UserUpdate(id=user_id, name=desired_name)
                )
            except Exception:
                logger.exception("OAuth: failed to refresh display name for %s", user_id)
        return user_id

    new_user = PydanticUser(id=user_id, organization_id=org_id, name=desired_name)
    try:
        await manager.create_actor_async(new_user)
    except Exception as exc:
        try:
            await manager.get_actor_by_id_async(user_id)
        except NoResultFound:
            logger.error("OAuth: failed to provision user %s: %s", user_id, exc)
            raise
    return user_id
