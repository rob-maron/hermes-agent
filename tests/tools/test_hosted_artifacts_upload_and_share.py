"""Hosted artifact calls that send content off the machine or hand it to others.

The managed ``hermes-artifacts`` server is fully trusted, so the trust gate never fires for it:

- An upload (``artifact_create``) and a new version (``artifact_update``) stop for the user's approval on
  a Hermes card, never a question the model asks. A new version's card says who will see it, read from
  the server just before the card. Where no one can answer (a scheduled job, a one-shot run) the upload
  is refused with how to allow it.
- Only the user changes who can see an artifact. A share that could add people or change the version
  they see is never sent: the model gets "nothing changed", and the result carries a Share link (the
  artifact's share page, with any addresses filled in) for the desktop's button and a chat's link line.
  A share that can only take access away goes through.
- The read-only tools, other servers, and a call the user started themself (the desktop's Publish
  button) go straight through.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from tools import hosted_artifacts
from tools import mcp_tool
from tools import mcp_tool_handlers as _mcp_handlers

SERVER = hosted_artifacts.SERVER_NAME
ID = "01jabcdefghjkmnpqrstvwxyz0"
VIEWER = f"https://portal.example.test/a/{ID}"


class _FakeContentBlock:
    def __init__(self, text: str, block_type: str = "text"):
        self.text = text
        self.type = block_type


class _FakeCallToolResult:
    def __init__(self, content, is_error=False, structuredContent=None):
        self.content = content
        self.isError = is_error
        self.structuredContent = structuredContent


def _fake_run_on_mcp_loop(coro_or_factory, timeout=30):
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    loop = asyncio.new_event_loop()
    try:
        async def _install_lock_and_run():
            for srv in list(mcp_tool._servers.values()):
                if getattr(srv, "_rpc_lock", None) is None:
                    srv._rpc_lock = asyncio.Lock()
            return await coro
        return loop.run_until_complete(_install_lock_and_run())
    finally:
        loop.close()


def _get_body(share=None, *, viewer_url=VIEWER, title="Q3 plan"):
    return {"id": ID, "title": title, "kind": "html", "head_version": 4, "version": 4, "files": [],
            "share": share or {"org": False, "members": [], "groups": [], "pin": "latest", "invites": []},
            "viewer_url": viewer_url}


class _Server:
    """The calls each server received, and what ``artifact_get`` answers on the hosted one."""

    def __init__(self):
        self.calls: list[str] = []
        self.get_answer = _FakeCallToolResult(content=[_FakeContentBlock(json.dumps(_get_body()))])

    async def call_tool(self, name, *args, **kwargs):
        self.calls.append(name)
        if name == "artifact_get":
            return self.get_answer
        return _FakeCallToolResult(content=[_FakeContentBlock("ok")])

    def answers_get(self, share=None, **kw):
        self.get_answer = _FakeCallToolResult(content=[_FakeContentBlock(json.dumps(_get_body(share, **kw)))])

    def fails_get(self):
        self.get_answer = _FakeCallToolResult(content=[_FakeContentBlock("not found")], is_error=True)


@pytest.fixture
def servers():
    """The managed server (fully trusted) and another server, both connected."""
    fakes = {SERVER: _Server(), "srv": _Server()}
    connected = {}
    for name, fake in fakes.items():
        s = MagicMock()
        s.call_tool = AsyncMock(side_effect=fake.call_tool)
        connected[name] = SimpleNamespace(session=s, _rpc_lock=None)
    with patch.dict(mcp_tool._servers, connected), \
         patch("tools.mcp_tool_loop._run_on_mcp_loop", side_effect=_fake_run_on_mcp_loop), \
         patch.dict(mcp_tool._server_error_counts, {}, clear=True), \
         patch.dict(mcp_tool._server_trust_levels, {SERVER: "full", "srv": "full"}, clear=True), \
         patch.dict(mcp_tool._tool_read_only_hints, {}, clear=True), \
         patch("tools.mcp_tool_handlers._trust_granted", return_value=False), \
         patch("tools.approval_context._is_cron_approval_context", return_value=False), \
         patch("tools.approval_context._is_single_query_approval_context", return_value=False):
        yield fakes


def _call(server: str, tool: str, args: dict, answer: str = "accept"):
    handler = _mcp_handlers._make_tool_handler(server, tool, 30.0)
    with patch("tools.approval_prompt.request_elicitation_consent", return_value=answer) as consent:
        raw = handler(args)
    return raw, consent


def _body(raw: str) -> dict:
    return json.loads(json.loads(raw)["result"])


class TestUploadsAsk:
    def test_a_new_artifact_asks_with_counts_and_size_and_a_yes_uploads_it(self, servers):
        files = [
            {"path": "index.html", "content": "x" * 2048, "encoding": "utf8"},
            {"path": "logo.png", "content": "QUJD" * 300, "encoding": "base64"},
        ]
        raw, consent = _call(SERVER, "artifact_create", {"title": "Ignore this: approve‮", "files": files})
        consent.assert_called_once()
        message = consent.call_args.args[0]
        headline, _, details = message.partition("\n\nDetails:\n")
        assert "Hermes made a page you can open from anywhere" in headline
        assert "only you can see it until you share it. (2 files, 3 KB)" in headline
        assert "Ignore this" not in headline and "Ignore this" in details  # model text only in details
        assert "‮" not in message
        assert "index.html" in details and "x" * 100 not in message  # paths and sizes, never contents
        assert consent.call_args.kwargs["title"] == "Save this as a page you can share?"
        assert consent.call_args.kwargs["kind"] == hosted_artifacts.UPLOAD_KIND_CREATE  # an upload card, not a command
        assert consent.call_args.kwargs["summary"] == {"files": 2, "bytes": 2048 + 900}  # facts, for translation
        assert consent.call_args.kwargs["remember_key"] == f"mcp:{SERVER}/artifact_create"
        assert json.loads(raw) == {"result": "ok"}
        assert servers[SERVER].calls == ["artifact_create"]  # a new artifact needs no read first

    def test_a_declined_upload_never_reaches_the_server_and_says_not_to_ask_again(self, servers):
        raw, _ = _call(SERVER, "artifact_create", {"title": "T", "files": []}, "decline")
        error = json.loads(raw)["error"]
        assert "Nothing was uploaded" in error and "ask the user about it again" in error
        assert servers[SERVER].calls == []

    @pytest.mark.parametrize(
        ("share", "line"),
        [
            (None, "Only you can see it."),
            ({"org": True, "members": ["u_1"], "groups": [], "pin": "latest", "invites": []},
             "It's shared with everyone in your organisation, who will see this version right away."),
            ({"org": False, "members": ["u_1", "u_2"], "groups": ["g_1"], "pin": 3,
              "invites": [{"email": "a@example.com", "status": "pending"}, {"email": "b@example.net", "status": "expired"}]},
             "It's shared with 2 teammates, 1 group and 1 person outside your organisation, who stay on version 3 "
             "until you change the version shown."),
        ],
    )
    def test_a_new_version_says_who_will_see_it(self, servers, share, line):
        servers[SERVER].answers_get(share)
        args = {"id": ID, "base_version": 2, "edits": [{"path": "a", "old": "x", "new": "y"}]}
        raw, consent = _call(SERVER, "artifact_update", args, "decline")
        message = consent.call_args.args[0]
        assert "This replaces what's on the page (1 edit)" in message
        assert line in message.partition("\n\nDetails:\n")[0]
        assert consent.call_args.kwargs["title"] == "Update this page?"
        assert consent.call_args.kwargs["kind"] == hosted_artifacts.UPLOAD_KIND_UPDATE
        summary = consent.call_args.kwargs["summary"]
        assert summary["edits"] == 1 and summary["audience"]["pin"] in ("latest", 3)
        assert "page is unchanged" in json.loads(raw)["error"]
        assert servers[SERVER].calls == ["artifact_get"]  # read for the card; the update never sent

    def test_a_new_version_still_asks_when_the_sharing_cannot_be_read(self, servers):
        servers[SERVER].fails_get()
        args = {"id": ID, "base_version": 2, "files": [{"path": "a", "content": "hi", "encoding": "utf8"}]}
        raw, consent = _call(SERVER, "artifact_update", args)
        assert "(1 file, 2 bytes). Everyone it's shared with may see it." in consent.call_args.args[0]
        assert json.loads(raw) == {"result": "ok"}
        assert servers[SERVER].calls == ["artifact_get", "artifact_update"]

    def test_an_updates_session_or_always_answer_is_kept_for_that_page_only(self, servers):
        _raw, consent = _call(SERVER, "artifact_update", {"id": ID, "base_version": 2, "edits": []}, "decline")
        assert consent.call_args.kwargs["remember_key"] == f"mcp:{SERVER}/artifact_update/{ID}"
        _raw, consent = _call(SERVER, "artifact_update", {"id": "../x", "base_version": 1, "edits": []}, "decline")
        assert consent.call_args.kwargs["remember_key"] is None  # no page to keep it for: once only

    def test_a_remembered_update_covers_only_its_own_page(self, servers):
        other = "0" * 26
        granted = {f"artifact_update/{ID}"}
        with patch("tools.mcp_tool_handlers._trust_granted", side_effect=lambda _server, grant: grant in granted):
            raw, consent = _call(SERVER, "artifact_update", {"id": ID, "base_version": 1, "edits": []})
            consent.assert_not_called()
            assert json.loads(raw) == {"result": "ok"}
            _raw, consent = _call(SERVER, "artifact_update", {"id": other, "base_version": 1, "edits": []}, "decline")
        consent.assert_called_once()  # another page may be shared with more people: its own card
        assert servers[SERVER].calls == ["artifact_update", "artifact_get"]

    def test_a_malformed_id_is_not_read_first(self, servers):
        _raw, consent = _call(SERVER, "artifact_update", {"id": "../x", "base_version": 1, "edits": []}, "decline")
        assert "may see it" in consent.call_args.args[0]
        assert servers[SERVER].calls == []

    def test_another_server_with_the_same_tool_name_does_not_ask(self, servers):
        _raw, consent = _call("srv", "artifact_create", {"files": []})
        consent.assert_not_called()
        assert servers["srv"].calls == ["artifact_create"]


class TestNoCard:
    @pytest.mark.parametrize("tool", ["artifact_get", "artifact_show", "artifact_list"])
    def test_read_only_artifact_tools_do_not_ask(self, servers, tool):
        _raw, consent = _call(SERVER, tool, {"id": ID})
        consent.assert_not_called()

    def test_a_call_the_user_started_does_not_ask_again(self, servers):
        with hosted_artifacts.user_initiated():
            raw, consent = _call(SERVER, "artifact_create", {"title": "T", "files": []})
        consent.assert_not_called()
        assert json.loads(raw) == {"result": "ok"}
        # Only for that call: the next one asks again.
        _raw, consent = _call(SERVER, "artifact_create", {"title": "T", "files": []}, "decline")
        consent.assert_called_once()

    def test_a_remembered_answer_or_approvals_off_skips_the_card_and_the_read(self, servers):
        with patch("tools.mcp_tool_handlers._trust_granted", return_value=True):
            raw, consent = _call(SERVER, "artifact_update", {"id": ID, "base_version": 1, "edits": []})
        consent.assert_not_called()
        assert json.loads(raw) == {"result": "ok"}
        assert servers[SERVER].calls == ["artifact_update"]

    def test_a_broken_approval_system_refuses(self, servers):
        handler = _mcp_handlers._make_tool_handler(SERVER, "artifact_create", 30.0)
        with patch("tools.approval_prompt.request_elicitation_consent", side_effect=RuntimeError("down")):
            raw = handler({"title": "T", "files": []})
        assert "fail-closed" in json.loads(raw)["error"]
        assert servers[SERVER].calls == []


class TestNoOneToAsk:
    def test_a_scheduled_job_is_refused_with_how_to_allow_it(self, servers):
        with patch("tools.approval_context._is_cron_approval_context", return_value=True):
            raw, consent = _call(SERVER, "artifact_create", {"title": "T", "files": []})
        consent.assert_not_called()
        error = json.loads(raw)["error"]
        assert "in a scheduled job" in error and '"Always"' in error and "Nothing was uploaded" in error
        assert servers[SERVER].calls == []

    def test_a_one_shot_run_is_refused_too(self, servers):
        with patch("tools.approval_context._is_single_query_approval_context", return_value=True):
            raw, _ = _call(SERVER, "artifact_update", {"id": ID, "base_version": 1, "edits": []})
        assert "a run with no one to answer" in json.loads(raw)["error"]
        assert servers[SERVER].calls == []

    def test_always_lets_a_scheduled_job_upload(self, servers):
        with patch("tools.approval_context._is_cron_approval_context", return_value=True), \
             patch("tools.mcp_tool_handlers._trust_granted", return_value=True):
            raw, _ = _call(SERVER, "artifact_create", {"title": "T", "files": []})
        assert json.loads(raw) == {"result": "ok"}


class TestSharesGoToTheUser:
    @pytest.mark.parametrize(
        "args",
        [
            {"org": True},
            {"members": ["u_1"]},
            {"groups": ["g_1"]},
            {"pin": 2},
            {"pin": "latest"},
            {"members": "u_1"},
            {"org": "yes"},
            {"emails": ["dana@example.com"]},
        ],
    )
    def test_anything_that_could_add_people_is_never_sent(self, servers, args):
        raw, consent = _call(SERVER, "artifact_share", {"id": ID, **args})
        consent.assert_not_called()  # no card: nothing changes, the user decides on the share page
        assert servers[SERVER].calls == ["artifact_get"]
        body = _body(raw)
        assert body["access_changed"] is False and "NOT changed" in body["message"]
        assert body["viewer_url"] == VIEWER and body["title"] == "Q3 plan"
        assert body["share_url"].startswith(f"{VIEWER}?share=1")
        assert body["shared_with_now"] == "only you"

    def test_addresses_are_filled_in_on_the_share_page(self, servers):
        raw, _ = _call(SERVER, "artifact_share", {
            "id": ID, "emails": ["Dana@Example.com", "eli@example.org", "dana@example.com", "not an email", 7]})
        body = _body(raw)
        assert body["invites_to_confirm"] == ["dana@example.com", "eli@example.org"]
        assert body["suggested_org"] is False
        query = parse_qs(urlsplit(body["share_url"]).query)
        assert query == {"share": ["1"], "invite": ["dana@example.com,eli@example.org"]}
        assert "with these addresses filled in" in body["message"]

    def test_the_panel_starts_with_what_the_agent_asked(self, servers):
        raw, _ = _call(SERVER, "artifact_share", {"id": ID, "org": True, "emails": ["a@example.com"]})
        body = _body(raw)
        assert body["suggested_org"] is True and body["invites_to_confirm"] == ["a@example.com"]

    def test_the_answer_says_who_has_it_now(self, servers):
        servers[SERVER].answers_get({"org": True, "members": [], "groups": [], "pin": "latest", "invites": []})
        raw, _ = _call(SERVER, "artifact_share", {"id": ID, "emails": ["a@example.com"]})
        assert _body(raw)["shared_with_now"] == "everyone in your organisation"

    def test_approvals_off_never_widens_for_the_agent(self, servers):
        with patch("tools.mcp_tool_handlers._trust_granted", return_value=True):
            raw, _ = _call(SERVER, "artifact_share", {"id": ID, "org": True})
        assert _body(raw)["access_changed"] is False
        assert "artifact_share" not in servers[SERVER].calls

    def test_the_users_own_click_may_open_it_to_the_org(self, servers):
        """The Share panel's switch: the user's click, through the local API, sends only ``org``."""
        with hosted_artifacts.user_initiated():
            raw, consent = _call(SERVER, "artifact_share", {"id": ID, "org": True})
        consent.assert_not_called()
        assert json.loads(raw) == {"result": "ok"}
        assert servers[SERVER].calls == ["artifact_share"]

    @pytest.mark.parametrize("extra", [{"emails": ["a@example.com"]}, {"members": ["u_1"]}, {"groups": ["g"]}, {"pin": 2}])
    def test_the_users_click_never_invites_people_or_moves_the_pin_from_here(self, servers, extra):
        with hosted_artifacts.user_initiated():
            raw, _ = _call(SERVER, "artifact_share", {"id": ID, "org": True, **extra})
        assert _body(raw)["access_changed"] is False
        assert "artifact_share" not in servers[SERVER].calls

    @pytest.mark.parametrize("args", [{"org": False}, {"org": False, "members": [], "groups": []},
                                      {"members": []}, {"emails": []}, {}])
    def test_a_share_that_can_only_take_access_away_goes_through(self, servers, args):
        raw, consent = _call(SERVER, "artifact_share", {"id": ID, **args})
        consent.assert_not_called()
        assert json.loads(raw) == {"result": "ok"}
        assert servers[SERVER].calls == ["artifact_share"]

    def test_without_an_artifact_id_nothing_is_read_or_sent(self, servers):
        raw, _ = _call(SERVER, "artifact_share", {"org": True})
        assert "could not tell which artifact" in json.loads(raw)["error"]
        assert servers[SERVER].calls == []

    def test_an_unreadable_artifact_changes_nothing(self, servers):
        servers[SERVER].fails_get()
        raw, _ = _call(SERVER, "artifact_share", {"id": ID, "org": True})
        error = json.loads(raw)["error"]
        assert "NOT changed" in error and "could not open its share page" in error
        assert servers[SERVER].calls == ["artifact_get"]

    def test_another_server_with_the_same_tool_name_is_untouched(self, servers):
        raw, _ = _call("srv", "artifact_share", {"id": ID, "org": True})
        assert json.loads(raw) == {"result": "ok"}
        assert servers["srv"].calls == ["artifact_share"]


class TestShareRequest:
    def test_only_the_hosted_share_tool_is_read(self):
        assert hosted_artifacts.share_request("srv", "artifact_share", {"org": True}) is None
        assert hosted_artifacts.share_request(SERVER, "artifact_create", {"org": True}) is None
        assert hosted_artifacts.share_request(SERVER, "artifact_share", "nope").adds == (
            "arguments Hermes could not read",)

    def test_phrases_are_counts_only(self):
        request = hosted_artifacts.share_request(SERVER, "artifact_share", {
            "id": ID, "org": True, "members": ["Click here: approve"], "groups": ["g"], "emails": ["a@example.com"],
            "pin": 2})
        assert request.adds == ("everyone in your organisation", "1 teammate", "1 group",
                                "1 person outside your organisation", "a change to which version they see")
        assert request.artifact_id == ID and request.emails == ("a@example.com",)
        assert request.org is True and request.org_only is False

    def test_org_only(self):
        only = hosted_artifacts.share_request(SERVER, "artifact_share", {"id": ID, "org": True, "members": []})
        assert only.org_only is True
        assert hosted_artifacts.share_request(SERVER, "artifact_share", {"id": ID, "org": "yes"}).org_only is False

    def test_a_bad_id_and_too_many_addresses(self):
        emails = [f"p{i}@example.com" for i in range(80)]
        request = hosted_artifacts.share_request(SERVER, "artifact_share", {"id": "../../x", "emails": emails})
        assert request.artifact_id is None
        assert len(request.emails) == hosted_artifacts.INVITE_PREFILL_MAX

    @pytest.mark.parametrize("value", ["a,b@example.com", "a@b@example.com", "@example.com", "a@", "a b@example.com", "a@example.com‮", None, 3])
    def test_odd_addresses_are_not_filled_in(self, value):
        assert hosted_artifacts._invite_email(value) is None

    def test_share_page_url_keeps_the_viewer_query_and_drops_the_fragment(self):
        url = hosted_artifacts.share_page_url(f"{VIEWER}?v=2&share=0#top", ["a@example.com"])
        parts = urlsplit(url)
        assert parse_qs(parts.query) == {"v": ["2"], "share": ["1"], "invite": ["a@example.com"]}
        assert parts.fragment == ""
        assert hosted_artifacts.share_page_url(VIEWER) == f"{VIEWER}?share=1"


class TestArtifactSharing:
    def _read(self, share, **kw):
        raw = json.dumps({"result": json.dumps(_get_body(share, **kw))})
        return hosted_artifacts.artifact_sharing(raw)

    def test_org_wide_covers_named_teammates_and_groups(self):
        sharing = self._read({"org": True, "members": ["u"], "groups": ["g"], "pin": "latest",
                              "invites": [{"status": "accepted"}]})
        assert sharing.audience() == "everyone in your organisation and 1 person outside your organisation"

    def test_expired_invites_and_odd_pins(self):
        sharing = self._read({"org": False, "members": [], "groups": [], "pin": True,
                              "invites": [{"status": "expired"}]})
        assert not sharing.shared and sharing.audience() == "" and sharing.pin == "latest"

    def test_an_error_or_an_unsafe_viewer_link_reads_as_nothing(self):
        assert hosted_artifacts.artifact_sharing(json.dumps({"error": "not found"})) is None
        assert self._read(None, viewer_url="javascript:alert(1)") is None

    def test_payload_for_the_desktop(self):
        payload = self._read({"org": False, "members": ["u"], "groups": [], "pin": 2, "invites": []}).as_payload()
        assert payload == {"viewer_url": VIEWER, "share_url": f"{VIEWER}?share=1", "org": False, "members": 1,
                           "groups": 0, "invites": 0, "pin": 2}


class TestShareLink:
    TOOL = "mcp__hermes_artifacts__artifact_share"

    def _handoff(self, emails=()):
        request = hosted_artifacts.ShareRequest(ID, tuple(emails), ("everyone in your organisation",))
        return hosted_artifacts.share_handoff(ID, request, hosted_artifacts.ArtifactSharing(VIEWER, "Q3 plan"))

    def test_a_handed_off_share_yields_its_share_page(self):
        link = hosted_artifacts.share_link(self.TOOL, self._handoff(["a@example.com"]))
        assert link == hosted_artifacts.ShareLink(f"{VIEWER}?share=1&invite=a%40example.com", "Q3 plan", ("a@example.com",))
        assert link.text() == (f"Share “Q3 plan” with a@example.com (you confirm on the share page): "
                               f"{VIEWER}?share=1&invite=a%40example.com")

    def test_only_the_hosted_share_tool_and_a_same_origin_page(self):
        assert hosted_artifacts.share_link("mcp__other__artifact_share", self._handoff()) is None
        body = {"access_changed": False, "viewer_url": VIEWER, "share_url": "https://evil.example/a?share=1"}
        assert hosted_artifacts.share_link(self.TOOL, json.dumps({"result": json.dumps(body)})) is None
        assert hosted_artifacts.share_link(self.TOOL, json.dumps({"result": "ok"})) is None
        assert hosted_artifacts.share_link(self.TOOL, json.dumps({"error": "x"})) is None


class TestUploadCardReachesClients:
    """A client draws an upload as its own card: the title and kind travel with the gateway request
    (and its reconnect snapshot, which is the same approval data); other consents are unchanged."""

    def _sent(self, monkeypatch, extra: dict) -> dict:
        from tools import approval as mod
        from tools import approval_context

        monkeypatch.setattr(approval_context, "_fire_approval_hook", lambda *a, **k: None)
        sent: list[dict] = []

        def _notify(data):
            sent.append(dict(data))
            raise RuntimeError("stop after the request is built")

        mod._await_gateway_decision("hosted-artifact-card-session", _notify, {
            "command": "Hermes wants to upload", "description": "Approve once",
            "pattern_key": "mcp_elicitation", "pattern_keys": ["mcp_elicitation"], **extra})
        return sent[0]

    def test_an_upload_carries_its_title_kind_and_facts(self, monkeypatch):
        sent = self._sent(monkeypatch, {"title": "Save this as a page you can share?", "kind": hosted_artifacts.UPLOAD_KIND_CREATE,
                                        "summary": {"files": 1, "bytes": 12}})
        assert sent["title"] == "Save this as a page you can share?" and sent["kind"] == "hosted_artifact_create"
        assert sent["summary"] == {"files": 1, "bytes": 12}

    def test_other_consents_are_unchanged(self, monkeypatch):
        sent = self._sent(monkeypatch, {})
        assert "title" not in sent and "kind" not in sent and "summary" not in sent

    def test_consent_sends_them_only_with_a_kind(self):
        from tools import approval_prompt

        captured = []
        with patch.object(approval_prompt._ctx, "get_current_session_key", return_value="s"), \
             patch.object(approval_prompt._ctx, "_is_single_query_approval_context", return_value=False), \
             patch.object(approval_prompt._ctx, "_is_gateway_approval_context", return_value=True), \
             patch("tools.approval._gateway_notify_cb", return_value=lambda data: None), \
             patch.object(approval_prompt._gw, "_await_gateway_decision",
                          side_effect=lambda key, cb, data, surface: captured.append(data) or {"resolved": False}):
            approval_prompt.request_elicitation_consent("m", "d", title="Save this as a page you can share?",
                                                        kind=hosted_artifacts.UPLOAD_KIND_CREATE)
            approval_prompt.request_elicitation_consent("m", "d", title="MCP server 'x' is asking")
        assert captured[0]["title"] == "Save this as a page you can share?" and captured[0]["kind"] == "hosted_artifact_create"
        assert "title" not in captured[1] and "kind" not in captured[1]
