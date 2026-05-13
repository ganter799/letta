"""Integration tests for the LETTA_OAUTH_ORG_REASSIGN feature.

Coverage
--------
(a) Reassignment path end-to-end via ``get_or_provision_user_for_claims``:
    - First login provisions a user in org_a.
    - Second login with the same (iss, sub) but org_reassign=False leaves the
      user in org_a (default / backward-compatible behaviour).
    - Second login with org_reassign=True and a claim pointing to org_b moves
      the user to org_b.
    - A third call (same org_b) is a no-op (idempotent).

(b) User-data relocation: agents and blocks created under org_a follow the
    user to org_b after reassignment.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from letta.constants import DEFAULT_ORG_ID, DEFAULT_ORG_NAME
from letta.orm import Base, OAuthIdentity
from letta.orm.agent import Agent as AgentModel
from letta.orm.block import Block as BlockModel
from letta.orm.organization import Organization as OrganizationModel
from letta.orm.source import Source as SourceModel
from letta.orm.user import User as UserModel
from letta.schemas.organization import Organization as PydanticOrganization
from letta.schemas.user import User as PydanticUser
from letta.server.db import db_registry
from letta.server.rest_api.oauth_user import OrgResolverConfig, get_or_provision_user_for_claims


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clear_tables():
    """Wipe all tables before each test (mirrors test_managers.py pattern)."""
    async with db_registry.async_session() as session:
        engine_name = session.bind.dialect.name
        if engine_name == "sqlite":
            await session.execute(text("PRAGMA foreign_keys = OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            if table.name == "block_history":
                continue
            await session.execute(table.delete())
        await session.commit()
        if engine_name == "sqlite":
            await session.execute(text("PRAGMA foreign_keys = ON"))


@pytest.fixture
async def org_a() -> PydanticOrganization:
    """Create and return org_a."""
    async with db_registry.async_session() as session:
        org = OrganizationModel(name="org_alpha", privileged_tools=False)
        session.add(org)
        await session.commit()
        await session.refresh(org)
        return org.to_pydantic()


@pytest.fixture
async def org_b() -> PydanticOrganization:
    """Create and return org_b."""
    async with db_registry.async_session() as session:
        org = OrganizationModel(name="org_beta", privileged_tools=False)
        session.add(org)
        await session.commit()
        await session.refresh(org)
        return org.to_pydantic()


@pytest.fixture
def claims_a(org_a: PydanticOrganization) -> dict:
    """JWT claims for a user whose org claim maps to org_a."""
    return {
        "iss": "https://idp.example.com",
        "sub": "user-42",
        "preferred_username": "alice",
        "org": "alpha",
    }


def _config(org_map: dict, org_reassign: bool) -> OrgResolverConfig:
    return OrgResolverConfig(
        org_claim="org",
        org_map=org_map,
        autocreate=False,
        org_reassign=org_reassign,
    )


# ---------------------------------------------------------------------------
# (a) Reassignment path end-to-end
# ---------------------------------------------------------------------------


async def test_first_login_provisions_user(org_a: PydanticOrganization, claims_a: dict):
    """First login creates the user in org_a and writes an OAuthIdentity row."""
    cfg = _config({"alpha": org_a.id}, org_reassign=False)

    user = await get_or_provision_user_for_claims(claims_a, config=cfg, default_org_id=org_a.id)

    assert user.name == "alice"
    assert user.organization_id == org_a.id

    async with db_registry.async_session() as session:
        identity_rows = (
            await session.execute(
                select(OAuthIdentity).where(
                    OAuthIdentity.iss == claims_a["iss"],
                    OAuthIdentity.sub == claims_a["sub"],
                )
            )
        ).scalars().all()
    assert len(identity_rows) == 1
    assert identity_rows[0].user_id == user.id


async def test_second_login_no_reassign_stays_in_same_org(org_a: PydanticOrganization, org_b: PydanticOrganization, claims_a: dict):
    """Second login with org_reassign=False must not change the user's org."""
    cfg_off = _config({"alpha": org_a.id}, org_reassign=False)
    cfg_b = _config({"alpha": org_b.id}, org_reassign=False)

    user_first = await get_or_provision_user_for_claims(claims_a, config=cfg_off, default_org_id=org_a.id)

    # Same claims but config now points to org_b; reassign is OFF → no change.
    user_second = await get_or_provision_user_for_claims(claims_a, config=cfg_b, default_org_id=org_b.id)

    assert user_second.id == user_first.id
    assert user_second.organization_id == org_a.id, "org must not change when org_reassign=False"


async def test_second_login_with_reassign_moves_user(org_a: PydanticOrganization, org_b: PydanticOrganization, claims_a: dict):
    """Second login with org_reassign=True and a new org claim moves the user."""
    cfg_a = _config({"alpha": org_a.id}, org_reassign=False)
    cfg_b_reassign = _config({"alpha": org_b.id}, org_reassign=True)

    user_first = await get_or_provision_user_for_claims(claims_a, config=cfg_a, default_org_id=org_a.id)
    assert user_first.organization_id == org_a.id

    user_second = await get_or_provision_user_for_claims(claims_a, config=cfg_b_reassign, default_org_id=org_b.id)

    assert user_second.id == user_first.id, "must be same Letta user"
    assert user_second.organization_id == org_b.id, "org must be updated to org_b"

    # Verify database state.
    async with db_registry.async_session() as session:
        db_user = await session.get(UserModel, user_first.id)
    assert db_user.organization_id == org_b.id


async def test_reassign_is_idempotent(org_a: PydanticOrganization, org_b: PydanticOrganization, claims_a: dict):
    """Calling reassignment twice with the same target org is a no-op."""
    cfg_a = _config({"alpha": org_a.id}, org_reassign=False)
    cfg_b = _config({"alpha": org_b.id}, org_reassign=True)

    await get_or_provision_user_for_claims(claims_a, config=cfg_a, default_org_id=org_a.id)
    user_after_first_reassign = await get_or_provision_user_for_claims(claims_a, config=cfg_b, default_org_id=org_b.id)
    user_after_second_call = await get_or_provision_user_for_claims(claims_a, config=cfg_b, default_org_id=org_b.id)

    assert user_after_second_call.organization_id == org_b.id
    assert user_after_second_call.id == user_after_first_reassign.id


async def test_no_claim_falls_back_to_default_org(org_a: PydanticOrganization):
    """When org_claim is None, user is provisioned into default_org_id."""
    cfg = OrgResolverConfig(org_claim=None, org_map={}, autocreate=False, org_reassign=False)
    claims = {"iss": "https://idp.example.com", "sub": "user-99", "preferred_username": "bob"}

    user = await get_or_provision_user_for_claims(claims, config=cfg, default_org_id=org_a.id)

    assert user.organization_id == org_a.id


# ---------------------------------------------------------------------------
# (b) User-data relocation: rows follow the user after reassignment
# ---------------------------------------------------------------------------


async def _create_agent(session, user: PydanticUser, org_id: str, name: str) -> str:
    """Insert a minimal Agent row owned by *user* in *org_id*; return its id."""
    from letta.orm.agent import Agent

    # Build the minimal set of required columns for Agent.
    agent = Agent(
        name=name,
        agent_type="memgpt_agent",
        organization_id=org_id,
    )
    agent._created_by_id = user.id
    session.add(agent)
    await session.flush()
    return agent.id


async def _create_block(session, user: PydanticUser, org_id: str, label: str) -> str:
    """Insert a minimal Block row owned by *user* in *org_id*; return its id."""
    from letta.orm.block import Block

    block = Block(
        label=label,
        value="test content",
        organization_id=org_id,
    )
    block._created_by_id = user.id
    session.add(block)
    await session.flush()
    return block.id


async def test_agent_and_block_follow_user_on_reassign(
    org_a: PydanticOrganization,
    org_b: PydanticOrganization,
    claims_a: dict,
):
    """After org reassignment, agent and block rows owned by the user appear in org_b."""
    cfg_a = _config({"alpha": org_a.id}, org_reassign=False)
    cfg_b = _config({"alpha": org_b.id}, org_reassign=True)

    # Provision user in org_a.
    user = await get_or_provision_user_for_claims(claims_a, config=cfg_a, default_org_id=org_a.id)

    # Create one agent and one block in org_a, attributed to this user.
    async with db_registry.async_session() as session:
        async with session.begin():
            agent_id = await _create_agent(session, user, org_a.id, "agent-for-relocation")
            block_id = await _create_block(session, user, org_a.id, "human")

    # Confirm rows are in org_a before reassignment.
    async with db_registry.async_session() as session:
        agent_row = await session.get(AgentModel, agent_id)
        block_row = await session.get(BlockModel, block_id)
    assert agent_row.organization_id == org_a.id
    assert block_row.organization_id == org_a.id

    # Perform reassignment via second login.
    user_after = await get_or_provision_user_for_claims(claims_a, config=cfg_b, default_org_id=org_b.id)
    assert user_after.organization_id == org_b.id

    # Rows must have followed the user.
    async with db_registry.async_session() as session:
        agent_row = await session.get(AgentModel, agent_id)
        block_row = await session.get(BlockModel, block_id)

    assert agent_row.organization_id == org_b.id, "agent must be in org_b after reassignment"
    assert block_row.organization_id == org_b.id, "block must be in org_b after reassignment"


async def test_other_user_data_not_moved(
    org_a: PydanticOrganization,
    org_b: PydanticOrganization,
    claims_a: dict,
):
    """Rows owned by a *different* user in org_a must not be moved to org_b."""
    cfg_a = _config({"alpha": org_a.id}, org_reassign=False)
    cfg_b = _config({"alpha": org_b.id}, org_reassign=True)

    # Provision the primary user.
    primary_user = await get_or_provision_user_for_claims(claims_a, config=cfg_a, default_org_id=org_a.id)

    # Create a second user in org_a.
    async with db_registry.async_session() as session:
        async with session.begin():
            other_user_orm = UserModel(name="carol", organization_id=org_a.id)
            session.add(other_user_orm)
            await session.flush()
            other_user_id = other_user_orm.id

            # Block owned by the other user.
            other_user_pydantic = other_user_orm.to_pydantic()
            other_block_id = await _create_block(session, other_user_pydantic, org_a.id, "persona")

    # Reassign primary_user to org_b.
    await get_or_provision_user_for_claims(claims_a, config=cfg_b, default_org_id=org_b.id)

    # The other user's block must still be in org_a.
    async with db_registry.async_session() as session:
        other_block = await session.get(BlockModel, other_block_id)

    assert other_block.organization_id == org_a.id, "other user's block must remain in org_a"
