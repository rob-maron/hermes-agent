"""The hosted artifacts prompt hint: present only when the session's tools include them, per surface."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.prompt_builder import (
    HOSTED_ARTIFACTS_DESKTOP_GUIDANCE,
    HOSTED_ARTIFACTS_GUIDANCE,
    HOSTED_ARTIFACTS_LINK_HINT,
    HOSTED_ARTIFACTS_PUBLISH_GUIDANCE,
    HOSTED_ARTIFACTS_SURFACE_HINTS,
)
from agent.system_prompt import _tool_guidance_block, build_system_prompt_parts
from tools.hosted_artifacts import contract_tool_names

ARTIFACT_TOOLS = set(contract_tool_names())


def _agent(names, platform="cli"):
    return SimpleNamespace(
        load_soul_identity=False, skip_context_files=True, valid_tool_names=set(names),
        _task_completion_guidance=False, _parallel_tool_call_guidance=False, _tool_use_enforcement=False,
        _execution_guidance=False, _environment_probe=False, _bot_mode_protocol=False,
        _kanban_worker_guidance="", _memory_store=None, _memory_manager=None, model="", provider="",
        platform=platform, pass_session_id=False, session_id="", _emit_status=lambda *_a, **_k: None,
    )


def _full_prompt(agent) -> str:
    with patch("agent.prompt_builder.load_soul_md", return_value=""), \
         patch("agent.prompt_builder.build_environment_hints", return_value=""):
        return "\n\n".join(build_system_prompt_parts(agent).values())


@pytest.mark.parametrize("platform, surface", [
    ("desktop", HOSTED_ARTIFACTS_SURFACE_HINTS["desktop"]),
    ("cli", HOSTED_ARTIFACTS_SURFACE_HINTS["cli"]),
    ("tui", HOSTED_ARTIFACTS_SURFACE_HINTS["tui"]),
    ("telegram", HOSTED_ARTIFACTS_LINK_HINT),
    ("slack", HOSTED_ARTIFACTS_LINK_HINT),
])
def test_hint_names_the_surface_when_attached(platform, surface):
    block = _tool_guidance_block(_agent(ARTIFACT_TOOLS | {"terminal"}, platform))
    assert block is not None and surface in block and HOSTED_ARTIFACTS_GUIDANCE in block
    others = (set(HOSTED_ARTIFACTS_SURFACE_HINTS.values()) | {HOSTED_ARTIFACTS_LINK_HINT}) - {surface}
    assert not any(other in block for other in others)


def test_hint_teaches_the_update_and_link_rules():
    text = HOSTED_ARTIFACTS_GUIDANCE
    for needle in ("artifact_update", "base_version", "stale_base", "viewer_url", "content_url"):
        assert needle in text
    assert "never its content_url" in text


@pytest.mark.parametrize("platform", ["cli", "tui", "telegram", "slack"])
def test_surfaces_without_local_pages_publish_what_is_worth_keeping(platform):
    # A surface that can't show a local page publishes keepable work (artifact vs a throwaway ::preview).
    block = _tool_guidance_block(_agent(ARTIFACT_TOOLS, platform)) or ""
    assert HOSTED_ARTIFACTS_PUBLISH_GUIDANCE in block and HOSTED_ARTIFACTS_DESKTOP_GUIDANCE not in block
    assert "artifact_create instead of ::preview" in HOSTED_ARTIFACTS_PUBLISH_GUIDANCE


def test_desktop_keeps_pages_local_and_says_once_they_can_be_shared():
    # The desktop shows a page first and uploads it only when the user presses Share on its card: the
    # agent makes it locally, mentions Share once, and uploads only when asked.
    block = _tool_guidance_block(_agent(ARTIFACT_TOOLS, "desktop")) or ""
    assert HOSTED_ARTIFACTS_DESKTOP_GUIDANCE in block and HOSTED_ARTIFACTS_PUBLISH_GUIDANCE not in block
    text = HOSTED_ARTIFACTS_DESKTOP_GUIDANCE
    assert "```html block in your reply instead of a file with ::preview" in text and "side panel" in text
    assert "widgets that live in the chat" in text
    assert "stays on their computer until they press Share" in text and "privately" in text
    assert "The first time you make such a page in a chat" in text and "Share on its card" in text
    assert "Don't call artifact_create or artifact_update unless the user asks" in text


def test_hint_guards_sharing():
    # Who changes sharing: only the user, on the share page Hermes's Share button opens.
    text = HOSTED_ARTIFACTS_GUIDANCE
    assert "private by default" in text and "artifact_share" in text
    assert "only the user changes who can see one" in text and "Share button" in text
    assert "remove personal data first" in text
    assert "invite_url" not in text  # Hermes draws the share link itself


def test_hint_covers_upload_cards_denials_and_scheduled_jobs():
    text = HOSTED_ARTIFACTS_GUIDANCE
    assert "approval card for every upload" in text and "don't ask for it in chat first" in text
    assert "don't retry or ask again" in text
    assert "scheduled job" in text and '"Always"' in text


@pytest.mark.parametrize("names", [
    set(), {"terminal", "memory"}, {"mcp__other__artifact_create", "mcp__hermes_artifact__artifact_show"},
])
def test_hint_absent_without_the_artifact_tools(names):
    assert HOSTED_ARTIFACTS_GUIDANCE not in (_tool_guidance_block(_agent(names)) or "")
    assert HOSTED_ARTIFACTS_GUIDANCE not in _full_prompt(_agent(names))


def test_hint_present_when_tool_search_defers_the_artifact_tools():
    # Tool Search swaps deferred tools for its bridge, so the session's tool list lacks the artifact tools
    # while the agent can still call them: the session-start record decides (latch_session_surface).
    from tools.tool_search_catalog import BRIDGE_TOOL_NAMES

    deferred = _agent(set(BRIDGE_TOOL_NAMES) | {"terminal"}, "desktop")
    deferred._hosted_artifacts_at_start = True
    block = _tool_guidance_block(deferred) or ""
    assert HOSTED_ARTIFACTS_DESKTOP_GUIDANCE in block and HOSTED_ARTIFACTS_GUIDANCE in block

    without = _agent(set(BRIDGE_TOOL_NAMES) | {"terminal"}, "desktop")
    without._hosted_artifacts_at_start = False
    assert HOSTED_ARTIFACTS_GUIDANCE not in (_tool_guidance_block(without) or "")


def test_hint_rides_the_built_prompt_and_is_byte_stable():
    agent = _agent(ARTIFACT_TOOLS, "telegram")
    first = _full_prompt(agent)
    assert HOSTED_ARTIFACTS_LINK_HINT in first and HOSTED_ARTIFACTS_GUIDANCE in first
    assert _full_prompt(agent) == first
