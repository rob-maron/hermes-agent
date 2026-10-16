"""slash.exec /usage: only a session already known to be on a usage-window plan pays the portal fetch."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from tui_gateway import methods_slash


@pytest.fixture
def calls(monkeypatch):
    seen: list[object] = []

    def _view(*, credits_state=None, **_kw):
        seen.append(credits_state)
        return (["Today  ███░░  30% used"], True)

    monkeypatch.setattr("agent.account_usage.nous_credits_view", _view)
    monkeypatch.setattr(methods_slash, "_session_usage_snapshot", lambda session: {"model": "m"}, raising=False)
    monkeypatch.setattr(methods_slash, "_metadata_mirror", lambda session: {}, raising=False)
    return seen


def _session(state):
    agent = SimpleNamespace(model="m", _credits_state=state)
    return {"agent": agent, "history": [], "history_lock": threading.Lock()}


def test_no_credits_state_never_fetches_the_portal(calls):
    out = methods_slash._format_live_usage_output("sid", _session(None), "")
    assert calls == []
    assert "Nous Plan" not in out


def test_non_window_credits_state_never_fetches_the_portal(calls):
    out = methods_slash._format_live_usage_output("sid", _session(SimpleNamespace(is_usage_window=False)), "")
    assert calls == []
    assert "Nous Plan" not in out


def test_window_session_appends_the_plan_block(calls):
    state = SimpleNamespace(is_usage_window=True)
    out = methods_slash._format_live_usage_output("sid", _session(state), "")
    assert calls == [state]
    assert "Nous Plan" in out
