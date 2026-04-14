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
