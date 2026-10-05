"""Every tool carries MCP annotations, and they agree with the read-only allowlist."""

import pytest

from signal_mcp import tool_annotations as ta
from signal_mcp.server import TOOLS, _READ_ONLY_TOOLS


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_every_tool_has_complete_annotations(tool):
    a = tool.annotations
    assert a is not None, f"{tool.name} has no annotations"
    assert a.title
    for hint in ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint"):
        assert getattr(a, hint) is not None, f"{tool.name}: {hint} unset"


def test_table_only_names_real_tools():
    names = {t.name for t in TOOLS}
    for group in (ta.READ_ONLY, ta.DESTRUCTIVE, ta.IDEMPOTENT, ta.LOCAL_ONLY):
        assert group <= names, sorted(group - names)


def test_read_only_hint_implies_allowed_in_read_only_mode():
    # A tool we call read-only must stay usable under SIGNAL_MCP_READONLY.
    assert ta.READ_ONLY <= _READ_ONLY_TOOLS


def test_destructive_tools_are_blocked_in_read_only_mode():
    assert not (ta.DESTRUCTIVE & _READ_ONLY_TOOLS)


def test_read_only_tools_are_never_destructive():
    for t in TOOLS:
        if t.annotations.read_only_hint:
            assert t.annotations.destructive_hint is False


def test_tools_that_contact_signal_are_open_world():
    by_name = {t.name: t.annotations for t in TOOLS}
    for name in ("send_message", "send_group_message", "receive_messages", "get_profile", "delete_message"):
        assert by_name[name].open_world_hint is True
    for name in ("store_stats", "clear_local_store", "search_messages", "set_webhook"):
        assert by_name[name].open_world_hint is False
