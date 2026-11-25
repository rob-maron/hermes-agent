"""The desktop's Publish button: /api/hosted-artifacts/status and /api/hosted-artifacts/publish.

Available only to a Nous Portal account with artifacts access (the decision that attaches the hosted
server is re-read per request, server-side). A publish is the user's own click, so it runs as
user-initiated (no approval card); it never returns the short-lived content_url.
"""

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from hermes_cli.web_routers import hosted_artifacts as routes
from tools import hosted_artifacts as ha

ID = "01jabcdefghjkmnpqrstvwxyz0"
VIEWER = f"https://portal.example.test/a/{ID}"
TICKET = f"https://{ID}.artifacts.example.test/__av?t=secret"
REQUEST = SimpleNamespace(state=SimpleNamespace())
ATTACHED = ha.AttachDecision(ha.Reason.attached, url="https://plane.example.test/v1/artifacts/mcp")


@pytest.fixture(autouse=True)
def _scoped():
    with patch.object(routes, "_require_token", lambda request: None), \
         patch.object(routes, "_config_profile_scope", lambda profile: contextlib.nullcontext()):
        yield


def _ok(version: int = 1) -> str:
    body = {"id": ID, "version": version, "viewer_url": VIEWER, "content_url": TICKET}
    return json.dumps({"result": json.dumps(body)})


class _Handler:
    """Stands in for ``_make_tool_handler``: records each call and whether it ran as user-initiated."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict, bool]] = []

    def __call__(self, server: str, tool: str, timeout: float):
        assert server == ha.SERVER_NAME

        def _call(args: dict) -> str:
            self.calls.append((tool, args, ha.is_user_initiated()))
            return self.answers.pop(0)

        return _call


def _publish(body: dict, handler: _Handler, *, decision=ATTACHED, connected=True):
    discover = MagicMock()
    with patch.object(routes, "_decision", return_value=decision), \
         patch("tools.mcp_tool_handlers._make_tool_handler", handler), \
         patch("tools.mcp_tool_discovery._get_connected_server_for_call",
               return_value=object() if connected else None), \
         patch("tools.mcp_tool_discovery.discover_mcp_tools", discover):
        result = asyncio.run(routes.publish_hosted_artifact(routes.HostedArtifactPublish(**body), REQUEST))
    return result, discover


class TestStatus:
    @pytest.mark.parametrize(("reason", "available"), [
        (ha.Reason.attached, True), (ha.Reason.not_logged_in, False), (ha.Reason.not_enabled, False),
        (ha.Reason.disabled, False),
    ])
    def test_reports_whether_the_account_may_publish(self, reason, available):
        with patch.object(routes, "_decision", return_value=ha.AttachDecision(reason)):
            result = asyncio.run(routes.hosted_artifacts_status(REQUEST))
        assert result == {"available": available, "reason": reason.value}


class TestPublish:
    def test_a_new_artifact_uploads_as_the_users_own_call_and_never_returns_the_ticket(self):
        handler = _Handler(_ok())
        result, _ = _publish({"title": "Dashboard", "html": "<!doctype html><h1>Hi</h1>"}, handler)
        assert result == {"ok": True, "id": ID, "version": 1, "viewer_url": VIEWER}
        assert TICKET not in json.dumps(result)
        tool, args, user_initiated = handler.calls[0]
        assert tool == "artifact_create" and user_initiated is True
        assert args == {"title": "Dashboard",
                        "files": [{"path": "index.html", "content": "<!doctype html><h1>Hi</h1>", "encoding": "utf8"}]}
        assert ha.is_user_initiated() is False  # only for that call

    @pytest.mark.parametrize("reason", [ha.Reason.not_logged_in, ha.Reason.not_enabled, ha.Reason.account_unreadable])
    def test_refused_unless_the_account_may_publish_and_nothing_is_called(self, reason):
        handler = _Handler()
        result, discover = _publish({"title": "T", "html": "<p>x</p>"}, handler, decision=ha.AttachDecision(reason))
        assert result["ok"] is False and result["reason"] == reason.value
        assert handler.calls == []
        discover.assert_not_called()

    def test_a_later_version_updates_the_same_hosted_artifact(self):
        handler = _Handler(_ok(version=2))
        result, _ = _publish({"title": "T", "html": "<p>2</p>", "artifact_id": ID, "base_version": 1}, handler)
        assert result["version"] == 2
        tool, args, _ = handler.calls[0]
        assert tool == "artifact_update" and args["id"] == ID and args["base_version"] == 1

    def test_changed_since_on_the_server_uploads_over_the_head_once(self):
        stale = json.dumps({"error": json.dumps({"error": "stale_base", "message": "not the head", "head_version": 3})})
        handler = _Handler(stale, _ok(version=4))
        result, _ = _publish({"title": "T", "html": "<p>2</p>", "artifact_id": ID, "base_version": 1}, handler)
        assert result["ok"] is True and result["version"] == 4
        assert [args["base_version"] for _, args, _ in handler.calls] == [1, 3]

    def test_connects_only_the_hosted_server_when_not_connected_yet(self):
        handler = _Handler(_ok())
        _result, discover = _publish({"title": "T", "html": "<p>x</p>"}, handler, connected=False)
        discover.assert_called_once_with(allowed_mcp_names=[ha.SERVER_NAME])

    @pytest.mark.parametrize("body", [
        {"title": "T", "html": "<p>x</p>", "artifact_id": ID},
        {"title": "T", "html": "<p>x</p>", "base_version": 1},
        {"title": "T", "html": "<p>x</p>", "artifact_id": "not-an-id", "base_version": 1},
    ])
    def test_refuses_a_malformed_request_before_anything_runs(self, body):
        with pytest.raises(HTTPException) as err:
            _publish(body, _Handler())
        assert err.value.status_code == 400

    def test_refuses_a_page_over_the_hosted_size_cap(self):
        with pytest.raises(HTTPException) as err:
            _publish({"title": "T", "html": "x" * (routes.MAX_HTML_BYTES + 1)}, _Handler())
        assert err.value.status_code == 413


class TestPublishOutcome:
    def test_an_answer_without_a_safe_viewer_link_is_not_a_success(self):
        body = {"id": ID, "version": 1, "viewer_url": "javascript:alert(1)", "content_url": TICKET}
        assert ha.publish_outcome("artifact_create", json.dumps({"result": json.dumps(body)}))["ok"] is False

    def test_an_error_is_reported_in_plain_printable_text(self):
        raw = json.dumps({"error": "Refused:‮ quota\nreached"})
        outcome = ha.publish_outcome("artifact_create", raw)
        assert outcome["ok"] is False and "‮" not in outcome["error"] and "\n" not in outcome["error"]


def _get_answer(share: dict) -> str:
    body = {"id": ID, "title": "T", "head_version": 2, "version": 2, "files": [], "share": share,
            "viewer_url": VIEWER}
    return json.dumps({"result": json.dumps(body)})


def _sharing(artifact_id: str, handler: _Handler, *, decision=ATTACHED):
    with patch.object(routes, "_decision", return_value=decision), \
         patch("tools.mcp_tool_handlers._make_tool_handler", handler), \
         patch("tools.mcp_tool_discovery._get_connected_server_for_call", return_value=object()):
        return asyncio.run(routes.hosted_artifact_sharing(REQUEST, artifact_id))


class TestSharing:
    def test_reports_counts_and_the_share_page_from_a_read_only_call(self):
        share = {"org": False, "members": ["u_1", "u_2"], "groups": [], "pin": 3,
                 "invites": [{"email": "a@example.com", "status": "pending"}]}
        handler = _Handler(_get_answer(share))
        result = _sharing(ID, handler)
        assert result == {"ok": True, "viewer_url": VIEWER, "share_url": f"{VIEWER}?share=1", "org": False,
                          "members": 2, "groups": 0, "invites": 1, "pin": 3}
        tool, args, user_initiated = handler.calls[0]
        assert (tool, args, user_initiated) == ("artifact_get", {"id": ID}, False)
        assert "a@example.com" not in json.dumps(result)  # counts only, never the addresses

    def test_refused_unless_the_account_may_use_artifacts(self):
        handler = _Handler()
        result = _sharing(ID, handler, decision=ha.AttachDecision(ha.Reason.not_logged_in))
        assert result == {"ok": False, "reason": "not_logged_in"} and handler.calls == []

    def test_an_unreadable_answer_is_not_ok(self):
        assert _sharing(ID, _Handler(json.dumps({"error": "not found"}))) == {"ok": False}

    def test_refuses_a_malformed_id(self):
        with pytest.raises(HTTPException) as err:
            _sharing("../x", _Handler())
        assert err.value.status_code == 400


def _share(body: dict, handler: _Handler, *, decision=ATTACHED):
    with patch.object(routes, "_decision", return_value=decision), \
         patch("tools.mcp_tool_handlers._make_tool_handler", handler), \
         patch("tools.mcp_tool_discovery._get_connected_server_for_call", return_value=object()):
        return asyncio.run(routes.share_hosted_artifact(routes.HostedArtifactOrgShare(**body), REQUEST))


class TestOrgShare:
    def test_the_switch_sends_only_org_as_the_users_click_and_answers_who_has_it(self):
        share = {"org": True, "members": [], "groups": [], "pin": "latest", "invites": []}
        handler = _Handler(json.dumps({"result": "ok"}), _get_answer(share))
        result = _share({"artifact_id": ID, "org": True}, handler)
        assert result["ok"] is True and result["org"] is True
        (tool, args, user_initiated), (read, _, read_user) = handler.calls
        assert (tool, args, user_initiated) == ("artifact_share", {"id": ID, "org": True}, True)
        assert read == "artifact_get" and read_user is False

    def test_a_refusal_is_reported_in_plain_text(self):
        handler = _Handler(json.dumps({"error": "not your\nartifact"}))
        result = _share({"artifact_id": ID, "org": True}, handler)
        assert result == {"ok": False, "error": "not yourartifact"}

    def test_refused_unless_the_account_may_use_artifacts(self):
        handler = _Handler()
        result = _share({"artifact_id": ID, "org": True}, handler, decision=ha.AttachDecision(ha.Reason.not_enabled))
        assert result["ok"] is False and handler.calls == []

    def test_refuses_a_malformed_id(self):
        with pytest.raises(HTTPException) as err:
            _share({"artifact_id": "../x", "org": True}, _Handler())
        assert err.value.status_code == 400
