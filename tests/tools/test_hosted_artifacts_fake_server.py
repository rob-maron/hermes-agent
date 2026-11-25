"""The fake hosted artifacts server checks inputs the way the hosted server does."""

from __future__ import annotations

import asyncio
import base64

import pytest

from tests.fakes import hosted_artifacts_server as fake
from tests.fakes.hosted_artifacts_server import Store

HTML = [{"path": "index.html", "content": "<p>1</p>", "encoding": "utf8"}]


@pytest.fixture
def store():
    store = Store(lambda: 9000, "http://portal.localhost")
    created = store.create("t", HTML)
    store.update(created["id"], 1, files=[{"path": "index.html", "content": "<p>2</p>", "encoding": "utf8"}])
    return store, created["id"]


def _code(body):
    return body.get("error")


@pytest.mark.parametrize("read", ["read", "show"])
def test_existing_versions_and_the_head(store, read):
    store, artifact_id = store
    assert getattr(store, read)(artifact_id, None)["version"] == 2
    assert getattr(store, read)(artifact_id, 1)["version"] == 1
    if read == "read":
        assert store.read(artifact_id, 1)["files"][0]["content"] == "<p>1</p>"


@pytest.mark.parametrize("read", ["read", "show"])
@pytest.mark.parametrize("version", [3, 99])
def test_out_of_range_version_is_not_found(store, read, version):
    store, artifact_id = store
    assert _code(getattr(store, read)(artifact_id, version)) == "not_found"


@pytest.mark.parametrize("read", ["read", "show"])
@pytest.mark.parametrize("version", [0, -1, True])
def test_non_positive_version_is_invalid(store, read, version):
    store, artifact_id = store
    assert _code(getattr(store, read)(artifact_id, version)) == "invalid_request"


def test_unknown_artifact_is_not_found(store):
    store, _ = store
    assert _code(store.read("0" * 26, None)) == "not_found"


@pytest.mark.parametrize("title", ["", "   ", "x" * (fake.MAX_TITLE_CHARS + 1), 7])
def test_title_must_be_a_short_non_empty_string(store, title):
    store, artifact_id = store
    assert _code(store.create(title, HTML)) == "invalid_request"
    assert _code(store.update(artifact_id, 2, title=title, files=HTML)) == "invalid_request"


def test_update_needs_exactly_one_non_empty_change(store):
    store, artifact_id = store
    assert _code(store.update(artifact_id, 2, edits=[])) == "invalid_request"
    assert _code(store.update(artifact_id, 2)) == "invalid_request"
    assert _code(store.update(artifact_id, 2, edits=[{"path": "index.html", "old": "2", "new": "3"}],
                              files=HTML)) == "invalid_request"
    assert _code(store.update(artifact_id, 1, files=HTML)) == "stale_base"
    assert store.update(artifact_id, 2, edits=[{"path": "index.html", "old": "2", "new": "3"}])["version"] == 3


@pytest.mark.parametrize("files, code", [
    ([{"path": "a.png", "content": "not base64!", "encoding": "base64"}], "invalid_request"),
    ([{"path": "a.png", "content": "abc", "encoding": "base64"}], "invalid_request"),
    ([{"path": "a.txt", "content": "x", "encoding": "latin1"}], "invalid_request"),
    ([{"path": "../a.txt", "content": "x"}], "invalid_request"),
    ([], "invalid_request"),
    ([{"path": "big.txt", "content": "x" * (fake.MAX_FILE_BYTES + 1)}], "artifact_too_large"),
    ([{"path": f"f{i}.txt", "content": "x" * fake.MAX_FILE_BYTES} for i in range(6)], "artifact_too_large"),
])
def test_files_are_checked_like_the_hosted_server(store, files, code):
    store, _ = store
    assert _code(store.create("t", files)) == code


def test_valid_base64_is_accepted(store):
    store, _ = store
    content = base64.b64encode(b"\x89PNG").decode()
    assert "id" in store.create("t", [{"path": "a.png", "content": content, "encoding": "base64"}])


@pytest.mark.parametrize("changes", [
    {"org": "yes"},
    {"members": [""]},
    {"members": ["u" * (fake.MAX_SUBJECT_CHARS + 1)]},
    {"groups": ["g"] * (fake.MAX_SUBJECTS + 1)},
    {"emails": ["not-an-email"]},
    {"emails": ["a@example.net,c@example.org"]},
    {"emails": [f"p{i}@example.org" for i in range(fake.MAX_INVITES + 1)]},
    {"pin": 9},
    {"pin": "head"},
])
def test_share_lists_and_pin_are_checked(store, changes):
    store, artifact_id = store
    assert _code(store.share(artifact_id, **changes)) == "invalid_request"


def test_a_share_merges_into_the_access_list(store):
    # Omitted fields keep their value, as on the hosted server.
    store, artifact_id = store
    assert store.share(artifact_id, members=["b", "a", "a"], pin=1) == {
        "org": False, "members": ["b", "a"], "groups": [], "pin": 1, "invites": []}
    assert store.share(artifact_id, org=True) == {
        "org": True, "members": ["b", "a"], "groups": [], "pin": 1, "invites": []}
    assert store.share(artifact_id, org=False, members=[]) == {
        "org": False, "members": [], "groups": [], "pin": 1, "invites": []}


def test_an_outside_email_is_handed_back_never_invited(store):
    # The agent cannot invite someone outside the org: the result carries the link that opens the
    # Portal's share sheet with the address filled in, for a person to confirm.
    store, artifact_id = store
    body = store.share(artifact_id, emails=[" Dana@Example.org "])
    assert body["invites"] == [] and body["invites_need_confirmation"] == ["dana@example.org"]
    assert body["invite_url"] == f"http://portal.localhost/a/{artifact_id}?share=1&invite=dana%40example.org"
    store.invite(artifact_id, "dana@example.org")  # the person confirmed in the Portal
    kept = store.share(artifact_id, emails=["dana@example.org"])
    assert [i["email"] for i in kept["invites"]] == ["dana@example.org"] and "invite_url" not in kept
    assert store.share(artifact_id, emails=[])["invites"] == []  # removing is always allowed


def test_listing_is_paginated_newest_first():
    store = Store(lambda: 9000, "http://portal.localhost")
    ids = [store.create(f"t{i}", HTML)["id"] for i in range(fake.LIST_PAGE_SIZE + 3)]
    first = store.listing()
    assert len(first["items"]) == fake.LIST_PAGE_SIZE and first["items"][0]["id"] == ids[-1]
    second = store.listing(first["next_cursor"])
    assert [item["id"] for item in second["items"]] == ids[2::-1] and second["next_cursor"] is None
    assert _code(store.listing("not-a-cursor")) == "invalid_request"
    assert _code(store.listing("x" * (fake.MAX_CURSOR_CHARS + 1))) == "invalid_request"


@pytest.mark.parametrize("tool, args", [
    ("artifact_get", {"version": "1"}),
    ("artifact_get", {"version": True}),
    ("artifact_show", {"version": "1"}),
    ("artifact_update", {"base_version": "2", "files": HTML}),
])
def test_numbers_are_not_coerced_from_strings_or_booleans(store, tool, args):
    """The hosted server's integer fields are strict: "1" or true is an error, not 1."""
    store, artifact_id = store
    server = fake.build_server(store)
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="validation error"):
        asyncio.run(server.call_tool(tool, {"id": artifact_id, **args}))


@pytest.mark.parametrize("tool, args", [
    ("artifact_get", {"version": None}),
    ("artifact_show", {"version": None}),
    ("artifact_list", {"cursor": None}),
    ("artifact_update", {"base_version": 2, "title": None, "files": HTML}),
    ("artifact_share", {"org": True, "members": None}),
])
def test_null_for_an_optional_field_is_refused(store, tool, args):
    """The hosted server's input schemas are non-nullable: an explicit null is an error, not "absent"."""
    store, artifact_id = store
    server = fake.build_server(store)
    arguments = args if tool == "artifact_list" else {"id": artifact_id, **args}
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="validation error"):  # answered to the client as a tool error
        asyncio.run(server.call_tool(tool, arguments))
    omitted = {key: value for key, value in arguments.items() if value is not None}
    assert not getattr(asyncio.run(server.call_tool(tool, omitted)), "is_error", True)


def test_a_version_keeps_the_provenance_its_request_stated():
    # Over the real wire: the client's tools/call _meta reaches the server's tool context.
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from tools.mcp_tool import sdk_httpx

    base, stop, store = fake.serve_in_thread(token="t")
    source = {"session_id": "s1", "cron_job_id": "j1", "cron_job_name": "Nightly"}

    async def run():
        httpx = sdk_httpx()
        async with httpx.AsyncClient(headers={"Authorization": "Bearer t"}) as client, \
                streamable_http_client(base + fake.MCP_PATH, http_client=client) as streams, \
                ClientSession(*streams[:2]) as session:
            await session.initialize()
            created = await session.call_tool("artifact_create", {"title": "t", "files": HTML})
            artifact_id = created.structured_content["id"]
            await session.call_tool("artifact_update", {"id": artifact_id, "base_version": 1, "files": HTML},
                                    meta={fake.HERMES_META_KEY: source})
            return artifact_id

    try:
        artifact_id = asyncio.run(run())
    finally:
        stop()
    assert store.sources(artifact_id) == [None, source]
