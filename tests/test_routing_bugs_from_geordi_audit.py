"""Tests reproducing three routing bugs discovered 2026-04-12 during the
Geordi@geordi → Adama@servitor handshake audit.

Bug 1: reply_message defaults `to` to original sender — when replying to
       your OWN outbound message, this loops the reply back to yourself
       instead of to the original recipients.

Bug 2: macro_contact_handshake gates welcome_message send on
       `not target_project_key` — cross-project handshakes (exactly when
       you most want the welcome) get welcome_message=null silently.

Bug 3: send_message has no explicit to_project parameter. The
       `AgentName@project` explicit format already works in the router
       but there's no documented, first-class parameter, so callers end
       up using auto_contact_if_blocked which can create shadow
       mailboxes in the sender's project.
"""
from __future__ import annotations

import pytest
from fastmcp import Client

from mcp_agent_mail.app import build_mcp_server
from mcp_agent_mail.db import ensure_schema, get_session
from mcp_agent_mail.models import Agent, AgentLink, Project


async def _seed_cross_project_link(
    sender_project_slug: str,
    sender_project_key: str,
    sender_name: str,
    target_project_slug: str,
    target_project_key: str,
    target_name: str,
) -> None:
    """Seed two projects with an approved cross-project agent link."""
    await ensure_schema()
    async with get_session() as s:
        p1 = Project(slug=sender_project_slug, human_key=sender_project_key)
        p2 = Project(slug=target_project_slug, human_key=target_project_key)
        s.add(p1)
        s.add(p2)
        await s.commit()
        await s.refresh(p1)
        await s.refresh(p2)
        a = Agent(project_id=p1.id, name=sender_name, program="claude-code", model="opus", task_description="")
        b = Agent(project_id=p2.id, name=target_name, program="claude-code", model="opus", task_description="")
        s.add(a)
        s.add(b)
        await s.commit()
        await s.refresh(a)
        await s.refresh(b)
        s.add(AgentLink(
            a_project_id=p1.id, a_agent_id=a.id,
            b_project_id=p2.id, b_agent_id=b.id,
            status="approved",
        ))
        s.add(AgentLink(
            a_project_id=p2.id, a_agent_id=b.id,
            b_project_id=p1.id, b_agent_id=a.id,
            status="approved",
        ))
        await s.commit()


@pytest.mark.asyncio
async def test_reply_to_own_outbound_defaults_to_original_recipients(isolated_env):
    """Bug 1: reply_message to your OWN outbound should default `to` to the
    original recipients, not back to yourself."""
    await _seed_cross_project_link(
        "geordi", "Geordi-Home", "Geordi",
        "servitor", "Servitor", "Adama",
    )
    server = build_mcp_server()
    async with Client(server) as client:
        first = await client.call_tool(
            "send_message",
            {
                "project_key": "geordi",
                "sender_name": "Geordi",
                "to": ["Adama@servitor"],
                "subject": "Original",
                "body_md": "hello",
            },
        )
        deliveries = first.data.get("deliveries") or []
        assert deliveries, "send_message returned no deliveries"
        # Find the Servitor delivery to get the outbound message id at geordi's end
        # Geordi sees its own outbound in its outbox; the same message id is what
        # gets returned. We reply to that id.
        msg_id = deliveries[0]["payload"]["id"]

        rep = await client.call_tool(
            "reply_message",
            {
                "project_key": "geordi",
                "message_id": msg_id,
                "sender_name": "Geordi",
                "body_md": "follow-up",
            },
        )
        reply_deliveries = rep.data.get("deliveries") or []
        assert reply_deliveries, "reply_message returned no deliveries"
        # The reply MUST go to Adama (original recipient) AT servitor, not
        # back to Geordi. Check EVERY delivery, not just deliveries[0] —
        # a shadow-local delivery landing first would still fail the bug.
        delivery_projects = [d.get("project") for d in reply_deliveries]
        assert any(p in ("Servitor", "servitor") for p in delivery_projects), (
            f"Reply did not land at Servitor project. deliveries={reply_deliveries}"
        )
        assert not any(p in ("Geordi-Home", "geordi") for p in delivery_projects), (
            f"Reply leaked a Geordi-local delivery (self-loop). deliveries={reply_deliveries}"
        )
        for d in reply_deliveries:
            reply_to_names: list[str] = d["payload"].get("to", [])
            assert "Geordi" not in reply_to_names, (
                f"Self-reply loop in {d.get('project')}: reply_to={reply_to_names}."
            )
            assert "Adama" in reply_to_names, (
                f"Expected Adama in recipients of {d.get('project')} delivery, got {reply_to_names}"
            )


@pytest.mark.asyncio
async def test_macro_contact_handshake_welcome_sends_cross_project(isolated_env):
    """Bug 2: welcome_message must fire even when target_project_key is set.
    Previously the welcome was gated behind `not target_project_key`."""
    await _seed_cross_project_link(
        "geordi", "Geordi-Home", "Geordi",
        "servitor", "Servitor", "Adama",
    )
    server = build_mcp_server()
    async with Client(server) as client:
        res = await client.call_tool(
            "macro_contact_handshake",
            {
                "project_key": "geordi",
                "requester": "Geordi",
                "to_agent": "Adama",
                "to_project": "servitor",
                "auto_accept": True,
                "welcome_subject": "Hello Adama",
                "welcome_body": "Pipe check — confirming routing from geordi → servitor.",
            },
        )
        assert res.data.get("welcome_message") is not None, (
            "welcome_message returned null for cross-project handshake. "
            "Expected the welcome send to execute and return a payload."
        )
        # And it should have been delivered to the Servitor project
        welcome = res.data["welcome_message"]
        deliveries = welcome.get("deliveries") or []
        delivery_projects = [d.get("project") for d in deliveries]
        assert any(p in ("Servitor", "servitor") for p in delivery_projects), (
            f"Welcome not delivered to Servitor project. deliveries={deliveries}"
        )
        assert not any(p in ("Geordi-Home", "geordi") for p in delivery_projects), (
            f"Welcome leaked a Geordi-local shadow delivery. deliveries={deliveries}"
        )


@pytest.mark.asyncio
async def test_send_to_globally_visible_without_link_fails_loud(isolated_env):
    """When a recipient name exists in another project but no approved AgentLink
    connects sender->recipient, send_message must raise RECIPIENT_NOT_FOUND with
    actionable data, NOT silently shadow-create a local copy."""
    from sqlalchemy import select as _select

    await ensure_schema()

    # Seed two projects + one agent in each. NO AgentLink between them.
    async with get_session() as s:
        p_sender = Project(slug="fake-sender", human_key="Fake-Sender")
        p_recipient = Project(slug="fake-recipient", human_key="Fake-Recipient")
        s.add_all([p_sender, p_recipient])
        await s.commit()
        await s.refresh(p_sender)
        await s.refresh(p_recipient)
        a_sender = Agent(project_id=p_sender.id, name="Alice",
                         program="claude-code", model="opus", task_description="")
        a_recipient = Agent(project_id=p_recipient.id, name="Bob",
                            program="claude-code", model="opus", task_description="")
        s.add_all([a_sender, a_recipient])
        await s.commit()

    server = build_mcp_server()
    with pytest.raises(Exception) as excinfo:
        async with Client(server) as client:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "fake-sender",
                    "sender_name": "Alice",
                    "to": ["Bob"],  # Bob exists globally at fake-recipient, no AgentLink
                    "subject": "smoke",
                    "body_md": "test",
                },
            )

    msg = str(excinfo.value)
    # The fastmcp wrapper strips the ToolExecutionError type prefix, so check
    # for the distinctive fail-loud message content that only the
    # RECIPIENT_NOT_FOUND path emits.
    assert "Bob" in msg, f"expected Bob in error message, got: {msg}"
    assert "no approved contact link" in msg, f"expected fail-loud message, got: {msg}"
    assert "macro_contact_handshake" in msg, f"expected handshake suggestion, got: {msg}"

    # Must NOT have silently created a shadow Bob in sender's project.
    async with get_session() as s:
        q = (
            _select(Agent)
            .join(Project, Project.id == Agent.project_id)
            .where(Project.slug == "fake-sender", Agent.name == "Bob")
        )
        shadow = (await s.execute(q)).scalar_one_or_none()
        assert shadow is None, "shadow agent was silently created - BUG STILL PRESENT"


@pytest.mark.asyncio
async def test_send_message_to_project_whitespace_only_rejected(isolated_env):
    """Bug 3 edge case (Codex review): whitespace-only `to_project` must
    fail loud, not silently no-op. Silent no-op would route to sender's
    local project — exactly the shadow-drop failure we're fixing."""
    await _seed_cross_project_link(
        "geordi", "Geordi-Home", "Geordi",
        "servitor", "Servitor", "Adama",
    )
    server = build_mcp_server()
    async with Client(server) as client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "geordi",
                    "sender_name": "Geordi",
                    "to": ["Adama"],
                    "to_project": "   ",
                    "subject": "x",
                    "body_md": "y",
                },
            )
        assert "INVALID_ARGUMENT" in str(excinfo.value) or "to_project" in str(excinfo.value), (
            f"Expected INVALID_ARGUMENT / to_project error, got: {excinfo.value}"
        )


@pytest.mark.asyncio
async def test_send_message_to_project_param_routes_cross_project(isolated_env):
    """Bug 3: send_message gains an explicit `to_project` parameter so
    callers don't need to compose `name@project` strings manually."""
    await _seed_cross_project_link(
        "geordi", "Geordi-Home", "Geordi",
        "servitor", "Servitor", "Adama",
    )
    server = build_mcp_server()
    async with Client(server) as client:
        res = await client.call_tool(
            "send_message",
            {
                "project_key": "geordi",
                "sender_name": "Geordi",
                "to": ["Adama"],
                "to_project": "servitor",
                "subject": "Explicit cross",
                "body_md": "hello via to_project param",
            },
        )
        deliveries = res.data.get("deliveries") or []
        assert deliveries, "send_message returned no deliveries"
        delivery_projects = [d.get("project") for d in deliveries]
        assert any(p in ("Servitor", "servitor") for p in delivery_projects), (
            f"to_project='servitor' did not route cross-project. "
            f"Delivered to: {delivery_projects}"
        )
        assert not any(p in ("Geordi-Home", "geordi") for p in delivery_projects), (
            f"to_project='servitor' leaked a geordi-local shadow delivery. "
            f"Delivered to: {delivery_projects}"
        )
