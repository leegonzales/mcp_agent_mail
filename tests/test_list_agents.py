"""Test the list_agents diagnostic MCP tool.

Added as part of fix/step-2-shadow-create-root-cause so the shadow-audit
follow-up can enumerate per-project agents + flag names that also appear
in other projects (shadow candidates).
"""
from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, Project


@pytest.mark.asyncio
async def test_list_agents_returns_local_and_shadow_candidates(isolated_env):
    """list_agents(project_key) returns the local project's agents AND flags
    any agents whose names also exist in other projects (shadow candidates)."""
    await ensure_schema()
    async with get_session() as s:
        p_a = Project(slug="fake-a", human_key="Fake-A")
        p_b = Project(slug="fake-b", human_key="Fake-B")
        s.add_all([p_a, p_b])
        await s.commit()
        await s.refresh(p_a)
        await s.refresh(p_b)
        s.add_all([
            Agent(project_id=p_a.id, name="Alice",
                  program="claude-code", model="opus", task_description=""),
            Agent(project_id=p_a.id, name="Bob",    # shadow candidate
                  program="claude-code", model="opus", task_description=""),
            Agent(project_id=p_b.id, name="Bob",    # "real" Bob
                  program="claude-code", model="opus", task_description=""),
            Agent(project_id=p_b.id, name="Carol",
                  program="claude-code", model="opus", task_description=""),
        ])
        await s.commit()

    server = build_mcp_server()
    async with Client(server) as client:
        result = await client.call_tool("list_agents", {"project_key": "fake-a"})

    payload = result.data if hasattr(result, "data") else result
    # Local agents for fake-a
    assert {a["name"] for a in payload["local"]} == {"Alice", "Bob"}
    # Shadow candidates: local names that also exist in other projects
    assert any(
        s["name"] == "Bob" and any(
            lbl in ("fake-b", "Fake-B") for lbl in s["also_at"]
        )
        for s in payload["shadow_candidates"]
    ), f"expected Bob flagged as shadow_candidate, got: {payload['shadow_candidates']}"
    # Alice is not a shadow candidate (unique to fake-a)
    assert all(s["name"] != "Alice" for s in payload["shadow_candidates"])


@pytest.mark.asyncio
async def test_list_agents_shadow_spanning_multiple_other_projects(isolated_env):
    """A local agent name that also exists in TWO+ other projects must be
    listed as a shadow_candidate once with both project slugs in `also_at`,
    deterministically sorted."""
    await ensure_schema()
    async with get_session() as s:
        p_home = Project(slug="p-home", human_key="P-Home")
        p_x = Project(slug="p-x", human_key="P-X")
        p_y = Project(slug="p-y", human_key="P-Y")
        p_z = Project(slug="p-z", human_key="P-Z")
        s.add_all([p_home, p_x, p_y, p_z])
        await s.commit()
        await s.refresh(p_home)
        await s.refresh(p_x)
        await s.refresh(p_y)
        await s.refresh(p_z)
        s.add_all([
            Agent(project_id=p_home.id, name="Echo",
                  program="claude-code", model="opus", task_description=""),
            Agent(project_id=p_x.id, name="Echo",
                  program="claude-code", model="opus", task_description=""),
            Agent(project_id=p_y.id, name="Echo",
                  program="claude-code", model="opus", task_description=""),
            Agent(project_id=p_z.id, name="Unrelated",
                  program="claude-code", model="opus", task_description=""),
        ])
        await s.commit()

    server = build_mcp_server()
    async with Client(server) as client:
        result = await client.call_tool("list_agents", {"project_key": "p-home"})

    payload = result.data if hasattr(result, "data") else result
    echo_shadows = [s for s in payload["shadow_candidates"] if s["name"] == "Echo"]
    assert len(echo_shadows) == 1, f"expected exactly one Echo shadow entry, got {echo_shadows}"
    also_at = echo_shadows[0]["also_at"]
    # Contains both p-x and p-y, excludes p-home (local) and p-z (different name).
    assert "p-x" in also_at, also_at
    assert "p-y" in also_at, also_at
    assert "p-home" not in also_at
    assert "p-z" not in also_at
    # Deterministic ordering.
    assert also_at == sorted(also_at), f"also_at not sorted: {also_at}"


@pytest.mark.asyncio
async def test_list_agents_empty_project_returns_empty_lists(isolated_env):
    """Project with zero local agents returns empty `local` and empty
    `shadow_candidates` without crashing (regression guard for bulk-query
    refactor that assumed non-empty agent list)."""
    await ensure_schema()
    async with get_session() as s:
        p = Project(slug="p-empty", human_key="P-Empty")
        s.add(p)
        await s.commit()

    server = build_mcp_server()
    async with Client(server) as client:
        result = await client.call_tool("list_agents", {"project_key": "p-empty"})

    payload = result.data if hasattr(result, "data") else result
    assert payload["local"] == []
    assert payload["shadow_candidates"] == []
