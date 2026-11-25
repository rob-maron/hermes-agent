"""Hosted artifacts routes: the desktop's Publish button, and who can see an artifact.

A user's own click uploads one local artifact to their Nous hosted artifacts (a new artifact, or a new
version of one they published before). The click is the consent, so the call runs as user-initiated
and the approval card the agent's uploads get is not shown again. Available only to a Nous Portal
account with artifacts access: the same decision that attaches the ``hermes-artifacts`` MCP server
(``tools/hosted_artifacts.py``) is re-read on every request, server-side, so a renderer can neither see
the button nor publish for an account the server would not attach for.

The sharing routes back the desktop's Share panel: one reads who can see an artifact (a read-only
``artifact_get``, no card); the other applies the panel's one switch, "everyone in your organisation can
view", as the user's own click. Inviting specific people is never done here: the panel opens the
Portal's share page with them filled in, and the user confirms there.
"""

import asyncio
import re
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from hermes_cli.web_deps import late

router = APIRouter()

_config_profile_scope = late("_config_profile_scope", "hermes_cli.web_server_profiles")
_require_token = late("_require_token")
load_config = late("load_config", "hermes_cli.config")

# A publish connects the server on first use and uploads up to the hosted server's per-file cap.
PUBLISH_TIMEOUT_S = 60.0
# A sharing read connects the server on first use too.
READ_TIMEOUT_S = 30.0
# The hosted server's per-file cap: refused here first, with a plain answer.
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_TITLE_CHARS = 200


class HostedArtifactPublish(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    # The full HTML document (the desktop wraps a fragment the way its preview does).
    html: str = Field(min_length=1)
    # An earlier publish of this artifact: upload a new version of it instead of a new artifact.
    artifact_id: Optional[str] = None
    base_version: Optional[int] = Field(default=None, ge=1)


def _decision():
    from tools.hosted_artifacts import ArtifactsConfig, cached_decision

    return cached_decision(ArtifactsConfig.from_raw((load_config() or {}).get("artifacts")))


@router.get("/api/hosted-artifacts/status")
async def hosted_artifacts_status(request: Request, profile: Optional[str] = None):
    """Whether this profile's account may publish hosted artifacts (signed in to the Nous Portal, with
    artifacts access), and why not when it may not."""
    _require_token(request)

    def _run() -> dict[str, Any]:
        with _config_profile_scope(profile):
            decision = _decision()
            return {"available": decision.attach, "reason": decision.reason.value}

    return await asyncio.to_thread(_run)


def _call(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    from tools import hosted_artifacts
    from tools.mcp_tool_handlers import _make_tool_handler

    with hosted_artifacts.user_initiated():
        raw = _make_tool_handler(hosted_artifacts.SERVER_NAME, tool, PUBLISH_TIMEOUT_S)(args)
    return hosted_artifacts.publish_outcome(tool, raw)


def _connect() -> None:
    """Not used in this profile since the app started: connect only the hosted server."""
    from tools import hosted_artifacts
    from tools.mcp_tool_discovery import _get_connected_server_for_call, discover_mcp_tools

    if _get_connected_server_for_call(hosted_artifacts.SERVER_NAME) is None:
        discover_mcp_tools(allowed_mcp_names=[hosted_artifacts.SERVER_NAME])


def _publish(body: HostedArtifactPublish) -> dict[str, Any]:
    _connect()
    files = [{"path": "index.html", "content": body.html, "encoding": "utf8"}]
    if body.artifact_id is None:
        return _call("artifact_create", {"title": body.title, "files": files})
    args = {"id": body.artifact_id, "base_version": body.base_version, "title": body.title, "files": files}
    outcome = _call("artifact_update", args)
    # It changed since this desktop last published it (the agent updated it, say): the user asked to
    # publish what they see, so upload it over the current head, once.
    if not outcome["ok"] and outcome.get("stale") and isinstance(outcome.get("head_version"), int):
        outcome = _call("artifact_update", {**args, "base_version": outcome["head_version"]})
    return outcome


@router.post("/api/hosted-artifacts/publish")
async def publish_hosted_artifact(body: HostedArtifactPublish, request: Request, profile: Optional[str] = None):
    """Upload one artifact the user chose to publish; ``{ok, id, version, viewer_url}`` or ``{ok: False,
    error, reason?}``. Refused unless this profile's account may publish hosted artifacts."""
    from tools.hosted_artifacts import ARTIFACT_ID_PATTERN

    _require_token(request)
    if (body.artifact_id is None) != (body.base_version is None):
        raise HTTPException(status_code=400, detail="artifact_id and base_version go together")
    if body.artifact_id is not None and not re.fullmatch(ARTIFACT_ID_PATTERN, body.artifact_id):
        raise HTTPException(status_code=400, detail="artifact_id is not a hosted artifact id")
    if len(body.html.encode("utf-8")) > MAX_HTML_BYTES:
        raise HTTPException(status_code=413, detail="The artifact is larger than the 2 MB a hosted page may be.")

    def _run() -> dict[str, Any]:
        with _config_profile_scope(profile):
            decision = _decision()
            if not decision.attach:
                return {"ok": False, "reason": decision.reason.value,
                        "error": "Hosted artifacts are not available for this account."}
            return _publish(body)

    return await asyncio.to_thread(_run)


@router.get("/api/hosted-artifacts/sharing")
async def hosted_artifact_sharing(request: Request, artifact_id: str, profile: Optional[str] = None):
    """Who can see one hosted artifact, for the desktop's card: ``{ok: True, viewer_url, share_url, org,
    members, groups, invites, pin}`` (counts, and the share page the Share button opens), or ``{ok: False,
    reason?}``. Refused unless this profile's account may use hosted artifacts."""
    from tools import hosted_artifacts
    from tools.mcp_tool_handlers import _make_tool_handler

    _require_token(request)
    if not re.fullmatch(hosted_artifacts.ARTIFACT_ID_PATTERN, artifact_id):
        raise HTTPException(status_code=400, detail="artifact_id is not a hosted artifact id")

    def _run() -> dict[str, Any]:
        with _config_profile_scope(profile):
            decision = _decision()
            if not decision.attach:
                return {"ok": False, "reason": decision.reason.value}
            _connect()
            raw = _make_tool_handler(hosted_artifacts.SERVER_NAME, "artifact_get", READ_TIMEOUT_S)({"id": artifact_id})
            sharing = hosted_artifacts.artifact_sharing(raw)
            return {"ok": True, **sharing.as_payload()} if sharing is not None else {"ok": False}

    return await asyncio.to_thread(_run)


class HostedArtifactOrgShare(BaseModel):
    artifact_id: str
    # True: everyone in the artifact's organisation can view it; False: only the people it names.
    org: bool


def _share_error(raw: Any) -> Optional[str]:
    """The refusal in a share answer, as plain printable text, or None when it went through."""
    from tools.hosted_artifacts import _result_bodies

    for body in _result_bodies(raw):
        if isinstance(body, dict) and "error" in body:
            text = body["error"]
            message = text.get("message") if isinstance(text, dict) else text
            return "".join(ch for ch in str(message) if ch.isprintable())[:300] or "The share was refused."
    return None


@router.post("/api/hosted-artifacts/share")
async def share_hosted_artifact(body: HostedArtifactOrgShare, request: Request, profile: Optional[str] = None):
    """The Share panel's switch: let everyone in the artifact's organisation view it, or stop that. The
    click is the consent, so it runs as user-initiated; it sends only ``org``, never people. Answers who
    can see it now (as ``/sharing`` does) or ``{ok: False, error}``."""
    from tools import hosted_artifacts
    from tools.mcp_tool_handlers import _make_tool_handler

    _require_token(request)
    if not re.fullmatch(hosted_artifacts.ARTIFACT_ID_PATTERN, body.artifact_id):
        raise HTTPException(status_code=400, detail="artifact_id is not a hosted artifact id")

    def _run() -> dict[str, Any]:
        with _config_profile_scope(profile):
            decision = _decision()
            if not decision.attach:
                return {"ok": False, "reason": decision.reason.value,
                        "error": "Hosted artifacts are not available for this account."}
            _connect()
            with hosted_artifacts.user_initiated():
                raw = _make_tool_handler(hosted_artifacts.SERVER_NAME, hosted_artifacts.SHARE_TOOL,
                                         READ_TIMEOUT_S)({"id": body.artifact_id, "org": body.org})
            error = _share_error(raw)
            if error is not None:
                return {"ok": False, "error": error}
            read = _make_tool_handler(hosted_artifacts.SERVER_NAME, "artifact_get",
                                      READ_TIMEOUT_S)({"id": body.artifact_id})
            sharing = hosted_artifacts.artifact_sharing(read)
            return {"ok": True, **sharing.as_payload()} if sharing is not None else {"ok": True}

    return await asyncio.to_thread(_run)
