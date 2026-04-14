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


@pytest.mark.asyncio
async def test_auto_contact_if_blocked_emits_deprecation_warning(isolated_env, caplog):
    """auto_contact_if_blocked=True must log a deprecation warning so callers
    migrate to macro_contact_handshake up front."""
    import logging

    await ensure_schema()
    async with get_session() as s:
        p = Project(slug="fake-dep", human_key="Fake-Dep")
        s.add(p)
        await s.commit()
        await s.refresh(p)
        a = Agent(project_id=p.id, name="Alice",
                  program="claude-code", model="opus", task_description="")
        s.add(a)
        await s.commit()

    caplog.set_level(logging.WARNING, logger="mcp_agent_mail.app")

    server = build_mcp_server()
    async with Client(server) as client:
        # Send to a non-existent recipient with auto_contact_if_blocked=True.
        # Will likely fail for other reasons; we only care that the
        # deprecation warning fires.
        try:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "fake-dep",
                    "sender_name": "Alice",
                    "to": ["Ghost"],
                    "subject": "smoke",
                    "body_md": "test",
                    "auto_contact_if_blocked": True,
                },
            )
        except Exception:
            pass  # we don't care about the send result, only the warning

    matches = [
        r for r in caplog.records
        if "auto_contact_if_blocked" in r.getMessage()
        and "deprecated" in r.getMessage().lower()
    ]
    assert matches, (
        "no deprecation warning found. "
        f"records: {[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_recipient_not_found_lists_all_projects_where_name_exists(isolated_env):
    """When a recipient name exists in MULTIPLE other projects without any
    approved AgentLink, the RECIPIENT_NOT_FOUND payload must list ALL of
    those projects (sorted) in `found_at_projects`, not just one."""
    await ensure_schema()
    async with get_session() as s:
        p_sender = Project(slug="m-sender", human_key="M-Sender")
        p_1 = Project(slug="m-one", human_key="M-One")
        p_2 = Project(slug="m-two", human_key="M-Two")
        p_3 = Project(slug="m-three", human_key="M-Three")
        s.add_all([p_sender, p_1, p_2, p_3])
        await s.commit()
        await s.refresh(p_sender)
        await s.refresh(p_1)
        await s.refresh(p_2)
        await s.refresh(p_3)
        s.add_all([
            Agent(project_id=p_sender.id, name="Alice",
                  program="claude-code", model="opus", task_description=""),
            Agent(project_id=p_1.id, name="Dax",
                  program="claude-code", model="opus", task_description=""),
            Agent(project_id=p_2.id, name="Dax",
                  program="claude-code", model="opus", task_description=""),
            Agent(project_id=p_3.id, name="Dax",
                  program="claude-code", model="opus", task_description=""),
        ])
        await s.commit()

    server = build_mcp_server()
    with pytest.raises(Exception) as excinfo:
        async with Client(server) as client:
            await client.call_tool(
                "send_message",
                {
                    "project_key": "m-sender",
                    "sender_name": "Alice",
                    "to": ["Dax"],
                    "subject": "multi-project probe",
                    "body_md": "test",
                },
            )

    msg = str(excinfo.value)
    # The fail-loud message must enumerate ALL three projects where Dax
    # lives, not just one. Accept slug form.
    joined = msg.lower()
    assert "m-one" in joined, f"missing m-one in: {msg}"
    assert "m-two" in joined, f"missing m-two in: {msg}"
    assert "m-three" in joined, f"missing m-three in: {msg}"

    # Additionally, call the in-process tool function directly via its
    # registered FunctionTool.fn to inspect the structured
    # ToolExecutionError.data payload.
    from mcp_agent_mail.app import ToolExecutionError
    from fastmcp.tools.tool import FunctionTool  # type: ignore

    async def _invoke_direct() -> None:
        # Find the wrapped send_message function from the registered tool
        # manager so we bypass the MCP client serialization.
        server_inner = build_mcp_server()
        tools = server_inner._tool_manager._tools  # type: ignore[attr-defined]
        assert "send_message" in tools
        ftool = tools["send_message"]
        assert isinstance(ftool, FunctionTool)
        raw_fn = ftool.fn
        # Build a minimal stub Context that satisfies info/error/debug coroutines.
        class _Ctx:
            async def info(self, *a, **kw): pass
            async def error(self, *a, **kw): pass
            async def debug(self, *a, **kw): pass
            async def warning(self, *a, **kw): pass
            metadata: dict = {}

        await raw_fn(
            ctx=_Ctx(),
            project_key="m-sender",
            sender_name="Alice",
            to=["Dax"],
            subject="multi-project probe",
            body_md="test",
        )

    with pytest.raises(ToolExecutionError) as tee_info:
        await _invoke_direct()
    tee = tee_info.value
    assert tee.error_type == "RECIPIENT_NOT_FOUND"
    unknown = tee.data["unknown_recipients"]
    dax_entry = next((u for u in unknown if u["name"] == "Dax"), None)
    assert dax_entry is not None, f"no Dax entry: {unknown}"
    found = dax_entry["found_at_projects"]
    joined_list = " ".join(found).lower()
    assert "m-one" in joined_list, found
    assert "m-two" in joined_list, found
    assert "m-three" in joined_list, found
    assert len(found) == 3, f"expected 3 projects, got {len(found)}: {found}"
    assert found == sorted(found), f"found_at_projects not sorted: {found}"
    # And the suggested_tool_calls entry for Dax uses the first (sorted)
    # project as to_project — deterministic.
    suggested = tee.data["suggested_tool_calls"]
    dax_sugg = next(
        (s for s in suggested if s["arguments"].get("target") == "Dax"), None
    )
    assert dax_sugg is not None, suggested
    assert dax_sugg["arguments"]["to_project"] == found[0]


def test_messaging_auto_handshake_on_block_defaults_false(isolated_env, monkeypatch):
    """Least astonishment: the server-side silent-handshake-on-block knob
    defaults to False. Flipping an env var to True is the only way to
    re-enable the anti-pattern; doing so must log a loud startup WARNING
    naming the setting."""
    import logging
    from mcp_agent_mail.config import get_settings, clear_settings_cache

    # Default (nothing set)
    monkeypatch.delenv("MESSAGING_AUTO_HANDSHAKE_ON_BLOCK", raising=False)
    clear_settings_cache()
    settings = get_settings()
    assert settings.messaging_auto_handshake_on_block is False, (
        "default must be False for least astonishment — silent handshake is "
        "an anti-pattern that erodes contact discipline"
    )

    # Operator opts back in: must log a loud WARNING
    monkeypatch.setenv("MESSAGING_AUTO_HANDSHAKE_ON_BLOCK", "true")
    clear_settings_cache()
    caplog_logger = logging.getLogger("mcp_agent_mail.config")
    # Re-load settings and capture warnings
    with _capture_logs(caplog_logger) as records:
        settings2 = get_settings()
    assert settings2.messaging_auto_handshake_on_block is True
    warns = [
        r for r in records
        if r.levelno >= logging.WARNING
        and "messaging_auto_handshake_on_block" in r.getMessage().lower()
        and ("deprecated" in r.getMessage().lower()
             or "anti-pattern" in r.getMessage().lower()
             or "silent" in r.getMessage().lower())
    ]
    assert warns, (
        f"re-enabling must log a loud WARNING. "
        f"records: {[(r.levelname, r.getMessage()) for r in records]}"
    )


import contextlib
import logging as _logging


@contextlib.contextmanager
def _capture_logs(logger):
    records: list[_logging.LogRecord] = []

    class _Handler(_logging.Handler):
        def emit(self, record):
            records.append(record)

    h = _Handler(level=_logging.DEBUG)
    logger.addHandler(h)
    prior_level = logger.level
    logger.setLevel(_logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(h)
        logger.setLevel(prior_level)


# =============================================================================
# Gemini round 3 regressions — fixes #1, #2, #3, #5
# =============================================================================


@pytest.mark.asyncio
async def test_globally_unlinked_scan_excludes_current_project(isolated_env):
    """Gemini #1: the hoisted invariant scan must NOT include agents from
    the sender's own project. Otherwise a name that exists locally (but
    misses the local_lookup cache for any reason) gets flagged as
    `globally_unlinked` and triggers RECIPIENT_NOT_FOUND, breaking
    legitimate local delivery.

    Repro: seed Alice (sender) and an agent named "Lima" in the SAME
    project. Force a path where "Lima" reaches `unknown_local` despite
    being a local agent — easiest via a stale local_lookup cache after a
    direct DB seed that bypasses the canonical naming pipeline. The
    bulk SELECT must exclude project.id == sender.project_id.
    """
    from sqlalchemy import select as _select

    await ensure_schema()
    async with get_session() as s:
        p_local = Project(slug="r3-local", human_key="R3-Local")
        s.add(p_local)
        await s.commit()
        await s.refresh(p_local)
        s.add_all([
            Agent(project_id=p_local.id, name="Alice",
                  program="claude-code", model="opus", task_description=""),
            # Note: directly seeding "Lima" with a name that the
            # local_lookup pipeline canonicalizes the same way. We rely
            # on the auto-register flow to hit the bug instead.
        ])
        await s.commit()

    # The reliable trigger: send to a new name "Lima". After the hoisted
    # scan finds NO global match, auto-register creates Lima in the
    # current project. The recursive _route should not re-trigger
    # RECIPIENT_NOT_FOUND against the just-created local row. Net:
    # send must succeed, Lima must be delivered.
    server = build_mcp_server()
    async with Client(server) as client:
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "r3-local",
                "sender_name": "Alice",
                "to": ["Lima"],
                "subject": "auto-register smoke",
                "body_md": "should deliver locally",
            },
        )

    deliveries = result.data.get("deliveries") or []
    assert deliveries, f"expected at least one delivery, got: {result.data}"
    delivery_to = []
    for d in deliveries:
        delivery_to.extend(d.get("payload", {}).get("to", []))
    assert "Lima" in delivery_to, (
        f"Lima not delivered. recipients across deliveries: {delivery_to}"
    )

    # Lima should now be a local agent — and exist EXACTLY once locally
    # (no shadow).
    async with get_session() as s:
        rows = await s.execute(
            _select(Agent).join(Project, Project.id == Agent.project_id)
            .where(Project.slug == "r3-local", Agent.name == "Lima")
        )
        local_lima = rows.scalars().all()
        assert len(local_lima) == 1, f"expected exactly 1 local Lima, got {len(local_lima)}"


@pytest.mark.asyncio
async def test_recipient_not_found_payload_does_not_leak_unlinked_projects(isolated_env):
    """Gemini #2 [security]: the RECIPIENT_NOT_FOUND payload must not
    enumerate every project where a name happens to exist. Sender should
    only see projects they already have approved AgentLinks to. If they
    have no link to any of them, `found_at_projects` is empty and the
    hint advises an explicit handshake or `to_project`."""
    from mcp_agent_mail.app import ToolExecutionError
    from fastmcp.tools.tool import FunctionTool  # type: ignore

    await ensure_schema()
    async with get_session() as s:
        p_sender = Project(slug="r3-leak-sender", human_key="R3-Leak-Sender")
        p_secret_a = Project(slug="r3-secret-a", human_key="R3-Secret-A")
        p_secret_b = Project(slug="r3-secret-b", human_key="R3-Secret-B")
        s.add_all([p_sender, p_secret_a, p_secret_b])
        await s.commit()
        await s.refresh(p_sender)
        await s.refresh(p_secret_a)
        await s.refresh(p_secret_b)
        s.add_all([
            Agent(project_id=p_sender.id, name="Alice",
                  program="claude-code", model="opus", task_description=""),
            Agent(project_id=p_secret_a.id, name="Mike",
                  program="claude-code", model="opus", task_description=""),
            Agent(project_id=p_secret_b.id, name="Mike",
                  program="claude-code", model="opus", task_description=""),
        ])
        await s.commit()

    server = build_mcp_server()
    tools = server._tool_manager._tools  # type: ignore[attr-defined]
    ftool = tools["send_message"]
    assert isinstance(ftool, FunctionTool)
    raw_fn = ftool.fn

    class _Ctx:
        async def info(self, *a, **kw): pass
        async def error(self, *a, **kw): pass
        async def debug(self, *a, **kw): pass
        async def warning(self, *a, **kw): pass
        metadata: dict = {}

    with pytest.raises(ToolExecutionError) as tee_info:
        await raw_fn(
            ctx=_Ctx(),
            project_key="r3-leak-sender",
            sender_name="Alice",
            to=["Mike"],
            subject="leak probe",
            body_md="",
        )
    tee = tee_info.value
    assert tee.error_type == "RECIPIENT_NOT_FOUND"
    unknown = tee.data["unknown_recipients"]
    mike_entry = next((u for u in unknown if u["name"] == "Mike"), None)
    assert mike_entry is not None, unknown
    # Sender has no AgentLink to either secret project — so it must not
    # learn of their existence via this payload.
    assert mike_entry["found_at_projects"] == [], (
        f"info leak: sender saw projects it has no AgentLink to: "
        f"{mike_entry['found_at_projects']}"
    )
    # The error message must not enumerate the secret project labels.
    msg = str(tee).lower()
    assert "r3-secret-a" not in msg, f"leaked project slug in message: {msg}"
    assert "r3-secret-b" not in msg, f"leaked project slug in message: {msg}"


@pytest.mark.asyncio
async def test_recipient_not_found_payload_lists_only_visible_projects(isolated_env):
    """Companion to the leak test: when the sender HAS approved AgentLinks
    to one of the projects (but not all), only the visible projects are
    listed."""
    from mcp_agent_mail.app import ToolExecutionError
    from fastmcp.tools.tool import FunctionTool  # type: ignore

    await ensure_schema()
    async with get_session() as s:
        p_sender = Project(slug="r3-vis-sender", human_key="R3-Vis-Sender")
        p_seen = Project(slug="r3-vis-seen", human_key="R3-Vis-Seen")
        p_hidden = Project(slug="r3-vis-hidden", human_key="R3-Vis-Hidden")
        s.add_all([p_sender, p_seen, p_hidden])
        await s.commit()
        await s.refresh(p_sender)
        await s.refresh(p_seen)
        await s.refresh(p_hidden)
        a_alice = Agent(project_id=p_sender.id, name="Alice",
                        program="claude-code", model="opus", task_description="")
        a_seen_anchor = Agent(project_id=p_seen.id, name="Anchor",
                              program="claude-code", model="opus", task_description="")
        a_seen_oscar = Agent(project_id=p_seen.id, name="Oscar",
                             program="claude-code", model="opus", task_description="")
        a_hidden_oscar = Agent(project_id=p_hidden.id, name="Oscar",
                               program="claude-code", model="opus", task_description="")
        s.add_all([a_alice, a_seen_anchor, a_seen_oscar, a_hidden_oscar])
        await s.commit()
        await s.refresh(a_alice)
        await s.refresh(a_seen_anchor)
        # Alice has an approved AgentLink to Anchor in p_seen — but NOT
        # an Oscar link. The visibility rule should still surface
        # p_seen because Alice is "linked into" it.
        s.add(AgentLink(
            a_project_id=p_sender.id, a_agent_id=a_alice.id,
            b_project_id=p_seen.id, b_agent_id=a_seen_anchor.id,
            status="approved",
        ))
        await s.commit()

    server = build_mcp_server()
    tools = server._tool_manager._tools  # type: ignore[attr-defined]
    ftool = tools["send_message"]
    assert isinstance(ftool, FunctionTool)
    raw_fn = ftool.fn

    class _Ctx:
        async def info(self, *a, **kw): pass
        async def error(self, *a, **kw): pass
        async def debug(self, *a, **kw): pass
        async def warning(self, *a, **kw): pass
        metadata: dict = {}

    with pytest.raises(ToolExecutionError) as tee_info:
        await raw_fn(
            ctx=_Ctx(),
            project_key="r3-vis-sender",
            sender_name="Alice",
            to=["Oscar"],
            subject="visibility probe",
            body_md="",
        )
    tee = tee_info.value
    unknown = tee.data["unknown_recipients"]
    oscar_entry = next((u for u in unknown if u["name"] == "Oscar"), None)
    assert oscar_entry is not None, unknown
    found = oscar_entry["found_at_projects"]
    # p_seen is sender-visible; p_hidden is not.
    joined = " ".join(found).lower()
    assert "r3-vis-seen" in joined, found
    assert "r3-vis-hidden" not in joined, (
        f"hidden project leaked into visible-projects list: {found}"
    )


@pytest.mark.asyncio
async def test_cc_and_bcc_kinds_preserved_through_resolution(isolated_env):
    """Gemini #5: globally-found (AgentLink-approved) recipients must
    land in the kind they were originally specified in (`to`/`cc`/`bcc`),
    not all promoted to primary `to`. Auto-registered new local
    recipients also must respect their original kind."""
    await _seed_cross_project_link(
        "geordi", "Geordi-Home", "Geordi",
        "servitor", "Servitor", "Adama",
    )
    server = build_mcp_server()
    async with Client(server) as client:
        # CC: Adama is bare-name CC'd. Should resolve via approved
        # AgentLink to Adama@servitor and land in CC, not TO.
        result = await client.call_tool(
            "send_message",
            {
                "project_key": "geordi",
                "sender_name": "Geordi",
                "to": ["Geordi"],  # self primary
                "cc": ["Adama@servitor"],  # explicit external CC
                "subject": "kind preservation probe",
                "body_md": "",
            },
        )
        deliveries = result.data.get("deliveries") or []
        assert deliveries, "no deliveries"
        # Find the Servitor-side delivery
        servitor_delivery = next(
            (d for d in deliveries if d.get("project") in ("Servitor", "servitor")),
            None,
        )
        assert servitor_delivery is not None, (
            f"no Servitor delivery: {deliveries}"
        )
        payload = servitor_delivery["payload"]
        # Adama must be in cc, not to
        assert "Adama" in payload.get("cc", []), (
            f"Adama lost CC kind. payload cc={payload.get('cc')}, "
            f"to={payload.get('to')}, bcc={payload.get('bcc')}"
        )
        assert "Adama" not in payload.get("to", []), (
            f"Adama promoted to TO from CC. payload to={payload.get('to')}"
        )
