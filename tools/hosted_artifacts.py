"""Hosted artifacts: the Nous-managed ``hermes-artifacts`` MCP server.

A profile signed in to Nous whose ``/api/oauth/account`` body says ``artifacts_access: true`` gets one
more MCP server, added to the configured
``mcp_servers`` when they are loaded (``tools.mcp_tool_config._load_mcp_config``). It connects with
``auth: nous``: every request carries a freshly resolved Nous access token, never a stored header.

The server joins a conversation only at its start. MCP discovery can finish after a session has
built its tool list, and the between-turns MCP refresh would then append the new tools mid-session;
:func:`latch_session_surface` records, per session, whether the server's tools were in that
session's first tool load (its definitions, or the Tool Search catalog of its own toolset selection),
and :func:`late_attach_drops` keeps them out of that session for good when they were not.

Every failure (config off, an insecure base URL, signed out, account unreadable, ``artifacts_access``
not true) leaves the server out; the gateway stays authoritative for entitlement on every call.

``HOSTED_ARTIFACTS_CLIENT_VERSION`` is the stable probe external end-to-end harnesses use to detect
that this client supports hosted artifacts.
"""

from __future__ import annotations

import contextvars
import enum
import logging
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Probe for cross-repository end-to-end tests: present (and >= 1) when this client can attach the
# hosted artifacts MCP server. Bumped only when the client's side of the wire contract changes.
HOSTED_ARTIFACTS_CLIENT_VERSION = 1

SERVER_NAME = "hermes-artifacts"
MCP_PATH = "/v1/artifacts/mcp"
VIEWER_RESOURCE_URI = "ui://hermes/artifact-viewer"
AUTH_TYPE = "nous"
TOOLSET_NAME = f"mcp-{SERVER_NAME}"
GATEWAY_VENDOR = "gateway"  # default origin: the managed gateway named like every vendor gateway
CONTRACT_TOOLS = ("artifact_create", "artifact_update", "artifact_get", "artifact_list",
                  "artifact_share", "artifact_show")

# One account read per profile per window: the MCP config is loaded by discovery, reconcile and
# status reads alike, and none of them may turn into a Portal round trip each.
DECISION_TTL_SECONDS = 300.0

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off"})


def contract_tool_names() -> frozenset[str]:
    """Registry names of the contract's tools (``mcp__hermes_artifacts__artifact_show`` ...)."""
    from tools.mcp_tool_schema import mcp_prefixed_tool_name

    return frozenset(mcp_prefixed_tool_name(SERVER_NAME, tool) for tool in CONTRACT_TOOLS)


class Reason(str, enum.Enum):
    attached = "attached"
    disabled = "disabled"
    insecure_base_url = "insecure_base_url"
    not_logged_in = "not_logged_in"
    account_unreadable = "account_unreadable"
    not_enabled = "not_enabled"


@dataclass(frozen=True)
class AttachDecision:
    reason: Reason
    url: Optional[str] = None

    @property
    def attach(self) -> bool:
        return self.reason is Reason.attached


@dataclass(frozen=True)
class ArtifactsConfig:
    """The ``artifacts`` config block: ``enabled`` (default on) and ``base_url`` (empty: the managed
    gateway, :func:`default_base_url`)."""

    enabled: bool = True
    base_url: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> ArtifactsConfig:
        if not isinstance(raw, dict):
            return cls()
        enabled = raw.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in _FALSE_STRINGS
        base_url = raw.get("base_url")
        return cls(enabled=enabled is not False and bool(enabled),
                   base_url=base_url.strip() if isinstance(base_url, str) else "")


def default_base_url() -> str:
    """The managed artifacts gateway: ``{scheme}://gateway-gateway.{domain}``, built by the same helper
    and environment (``TOOL_GATEWAY_SCHEME``, ``TOOL_GATEWAY_DOMAIN``) as the managed tool gateways, so
    a non-production domain follows them. ``artifacts.base_url`` overrides it."""
    from tools.managed_tool_gateway import build_vendor_gateway_url

    return build_vendor_gateway_url(GATEWAY_VENDOR)


def _origin(url: str) -> Optional[str]:
    """``scheme://host[:port]`` of *url* when it may carry a bearer: https anywhere, http only to a
    loopback or ``*.localhost`` host. None otherwise."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host or parts.scheme not in ("http", "https"):
        return None
    if parts.scheme == "http" and host not in _LOCAL_HOSTS and not host.endswith(".localhost"):
        return None
    return f"{parts.scheme}://{parts.netloc}"


def resolve_mcp_url(config: ArtifactsConfig) -> tuple[Optional[str], Reason]:
    try:
        raw = config.base_url or default_base_url()
    except ValueError:  # TOOL_GATEWAY_SCHEME is neither http nor https
        return None, Reason.insecure_base_url
    origin = _origin(raw)
    if origin is None:
        return None, Reason.insecure_base_url
    return origin + MCP_PATH, Reason.attached


def _read_account() -> Any:
    """The fresh ``/api/oauth/account`` read (a JWT snapshot carries no ``artifacts_access``)."""
    from hermes_cli.nous_account import get_nous_portal_account_info

    return get_nous_portal_account_info(force_fresh=True)


def entitlement(account: Any) -> Reason:
    """``attached`` only for a fresh account body whose ``artifacts_access`` is literally true. That one
    flag decides: ``paid_access`` is not read here, so an account the Portal let in is never refused."""
    if account is None or not getattr(account, "logged_in", False):
        return Reason.not_logged_in
    if getattr(account, "source", None) != "account_api":
        return Reason.account_unreadable
    raw = getattr(account, "raw_account", None) or {}
    if raw.get("artifacts_access") is not True:
        return Reason.not_enabled
    return Reason.attached


def decide(config: ArtifactsConfig, *, account_reader: Optional[Callable[[], Any]] = None) -> AttachDecision:
    """Whether this profile attaches the server, and why not. Never raises: any failure is a refusal."""
    if not config.enabled:
        return AttachDecision(Reason.disabled)
    try:
        url, reason = resolve_mcp_url(config)
        if url is None:
            return AttachDecision(reason)
        reason = entitlement((account_reader or _read_account)())
    except Exception as exc:  # health: allow BLE001 -- the account read spans auth refresh, HTTP and JSON; any failure is a refusal
        logger.debug("Hosted artifacts attach check failed: %s", exc)
        return AttachDecision(Reason.account_unreadable)
    return AttachDecision(reason, url if reason is Reason.attached else None)


_decisions: dict[str, tuple[float, AttachDecision]] = {}
_decisions_lock = threading.Lock()

# (profile home, session id) -> whether the server's tools were in that session's first tool load.
# Survives an agent rebuild for the same conversation in this process. Bounded, oldest first.
_session_latch: OrderedDict[tuple[str, str], bool] = OrderedDict()
_SESSION_LATCH_MAX = 4096


def reset_decisions() -> None:
    """Forget every remembered attach decision and session latch."""
    with _decisions_lock:
        _decisions.clear()
        _session_latch.clear()


def _on_event_loop() -> bool:
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def cached_decision(config: ArtifactsConfig, *, now: Callable[[], float] = time.monotonic,
                    **kwargs: Any) -> AttachDecision:
    """:func:`decide`, remembered per profile home and config for :data:`DECISION_TTL_SECONDS`. On a running
    event loop (the MCP loop's parked self-probe, a transport rebuild) any remembered answer is reused: the
    blocking account read would stall every server on that loop. Signed out is never remembered: it is
    known from the local sign-in state with no Portal read, and the first session after signing in must
    read the account rather than inherit it."""
    from hermes_constants import get_hermes_home

    key = f"{get_hermes_home()}|{config.enabled}|{config.base_url}"
    with _decisions_lock:
        hit = _decisions.get(key)
    if hit is not None and (now() - hit[0] < DECISION_TTL_SECONDS or _on_event_loop()):
        return hit[1]
    decision = decide(config, **kwargs)
    if decision.reason is Reason.account_unreadable and hit is not None and hit[1].attach:
        decision = hit[1]  # a failed read is no revocation (the gateway checks every call): keep the server
    with _decisions_lock:
        if decision.reason is Reason.not_logged_in:
            _decisions.pop(key, None)
        else:
            _decisions[key] = (now(), decision)
    if not decision.attach:
        logger.debug("Hosted artifacts MCP server not attached: %s", decision.reason.value)
    return decision


def attached_server_names(root_config: dict) -> frozenset[str]:
    """``{SERVER_NAME}`` when this profile's remembered decision attached the server, else empty. Never
    reads the account: the decision is the one MCP config loading made (a toolset resolver must not
    turn into a Portal round trip), so before that load the server is simply not enabled yet."""
    from hermes_constants import get_hermes_home

    config = ArtifactsConfig.from_raw((root_config or {}).get("artifacts"))
    key = f"{get_hermes_home()}|{config.enabled}|{config.base_url}"
    with _decisions_lock:
        hit = _decisions.get(key)
    return frozenset({SERVER_NAME}) if hit is not None and hit[1].attach else frozenset()


def server_entry(url: str) -> dict[str, Any]:
    """The ``mcp_servers`` entry for the hosted server."""
    return {"url": url, "auth": AUTH_TYPE}


def _profile_config() -> ArtifactsConfig:
    from hermes_cli.config import load_config

    return ArtifactsConfig.from_raw(load_config().get("artifacts"))


def attach_to(servers: dict[str, dict], root_config: dict) -> None:
    """Add the hosted server to *servers* (the loaded ``mcp_servers``) when this profile is entitled.
    Its name is Hermes's whether or not it attaches: any configured or plugin entry whose tools would
    register under the same names (``hermes-artifacts``, ``hermes_artifacts``, ``hermes.artifacts``...)
    is dropped, so nothing else can stand in for it."""
    from tools.mcp_tool_schema import sanitize_mcp_name_component

    reserved = sanitize_mcp_name_component(SERVER_NAME)
    for name in [n for n in servers if sanitize_mcp_name_component(n) == reserved]:
        logger.warning("MCP server '%s' is managed by Hermes; the configured entry of that name is ignored", name)
        del servers[name]
    decision = cached_decision(ArtifactsConfig.from_raw(root_config.get("artifacts")))
    if not (decision.attach and decision.url):
        return
    servers[SERVER_NAME] = server_entry(decision.url)


def bearer_allowed(url: str) -> bool:
    """``auth: nous`` hands out the Nous access token, so only the attached hosted server URL gets it."""
    decision = cached_decision(_profile_config())
    return decision.attach and decision.url == url


def _endpoint(url: str) -> Optional[tuple[str, str, int, str]]:
    try:
        parts = urlsplit(url)
        port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host or port is None:
        return None
    return parts.scheme, host, port, parts.path or "/"


def same_endpoint(request_url: str, permitted_url: str) -> bool:
    """True when *request_url* names exactly *permitted_url*: scheme, host, port and path (the query
    is ignored). A redirect elsewhere on the same origin is not the endpoint."""
    request = _endpoint(request_url)
    return request is not None and request == _endpoint(permitted_url)


def nous_bearer_auth(url: str, server_name: str):
    """An ``httpx.Auth`` that sets ``Authorization: Bearer <Nous access token>`` on each request,
    resolving (and refreshing) the token off the event loop. Refuses any server but the managed one at
    its attached URL, so another MCP entry declaring ``auth: nous`` cannot receive the token, even one
    pointed at the same URL."""
    import asyncio

    from hermes_cli.auth import resolve_nous_access_token
    from tools.mcp_tool import sdk_httpx

    # The SDK's httpx flavour (httpx2 on mcp >= 2.0): its AsyncClient rejects any other Auth class.
    httpx = sdk_httpx()

    if server_name != SERVER_NAME or not bearer_allowed(url):
        raise ValueError(f"auth: {AUTH_TYPE} is reserved for the managed {SERVER_NAME} server")

    class NousBearerAuth(httpx.Auth):
        # The client follows redirects through this flow, so each request is checked again: only the
        # attached endpoint itself (same scheme, host, port and path) ever carries the token.
        def sync_auth_flow(self, request):
            if same_endpoint(str(request.url), url):
                request.headers["Authorization"] = f"Bearer {resolve_nous_access_token()}"
            else:
                request.headers.pop("Authorization", None)
            yield request

        async def async_auth_flow(self, request):
            if same_endpoint(str(request.url), url):
                token = await asyncio.to_thread(resolve_nous_access_token)
                request.headers["Authorization"] = f"Bearer {token}"
            else:
                request.headers.pop("Authorization", None)
            yield request

    return NousBearerAuth()



def _without_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_nulls(item) for item in value if item is not None]
    return value


def call_arguments(server_name: str, args: Any) -> Any:
    """*args* as sent to *server_name*. The hosted server's input schemas are non-nullable and refuse
    a null, so for it a null a model wrote for an optional field (``"version": null``) means "not
    given" and is left out, at any depth, as is a null list entry (``"subjects": [null]``). Every other server gets *args* unchanged."""
    if server_name != SERVER_NAME or not isinstance(args, dict):
        return args
    return _without_nulls(args)


# Request ``_meta`` key the hosted server reads a version's provenance under.
PROVENANCE_META_KEY = "io.nousresearch/hermes"
# Tools that write a version, so the only ones whose provenance the server keeps.
_VERSION_TOOLS = frozenset({"artifact_create", "artifact_update"})


def call_meta(server_name: str, tool_name: str) -> Optional[dict[str, Any]]:
    """The ``tools/call`` ``_meta`` for one call: for the hosted server's create/update, which session or
    cron job is writing the version (the Portal shows "Updated by the scheduled job <name>"), else None.
    Read from this turn's bound session and cron execution; model output cannot reach either."""
    if server_name != SERVER_NAME or tool_name not in _VERSION_TOOLS:
        return None
    from cron.execution_identity import current_cron_execution
    from gateway.session_context import get_session_env

    source: dict[str, str] = {}
    session_id = get_session_env("HERMES_SESSION_ID", "")
    if session_id:
        source["session_id"] = session_id
    cron = current_cron_execution()
    if cron is not None:
        source.update(cron_job_id=cron.job_id, cron_job_name=cron.job_name)
    return {PROVENANCE_META_KEY: source} if source else None


# The hosted server's share tool. Only the user gives anyone access, on the artifact's share page: a call
# that could add people (or change the version they see) is handed to them as a Share button instead.
SHARE_TOOL = "artifact_share"
# Tools that upload content to Nous (a new artifact, or a new version of one): each asks the user first.
UPLOAD_TOOLS = frozenset({"artifact_create", "artifact_update"})
# What a client draws an upload's approval as (a card of its own, not a command): a new artifact, or a
# new version of one.
UPLOAD_KIND_CREATE = "hosted_artifact_create"
UPLOAD_KIND_UPDATE = "hosted_artifact_update"
# The Portal fills in at most this many addresses from a share link's ``invite`` parameter.
INVITE_PREFILL_MAX = 50
_EMAIL_MAX_CHARS = 254

# Set only by a user's own click (the desktop's Publish button, through the local API): that click is
# the consent, so the approval card is not shown again for that one call. Model output cannot reach it.
_USER_INITIATED: contextvars.ContextVar[bool] = contextvars.ContextVar("hermes_artifacts_user_initiated",
                                                                       default=False)


@contextmanager
def user_initiated():
    """Run a hosted artifact call the user started themself (no approval card for it)."""
    token = _USER_INITIATED.set(True)
    try:
        yield
    finally:
        _USER_INITIATED.reset(token)


def is_user_initiated() -> bool:
    return _USER_INITIATED.get()


@dataclass(frozen=True)
class ArtifactApproval:
    """What the approval card shows for one hosted artifact call. ``headline`` is built only from
    counts, sizes and the server's own sharing fields, never from model-written text; ``details``
    (titles, paths, ids) is shown in the card's sanitized arguments block. ``kind`` tells a client to
    draw it as an upload card (``UPLOAD_KIND_*``)."""

    title: str
    headline: str
    details: dict[str, Any]
    refusal: str
    kind: str = UPLOAD_KIND_CREATE
    # The headline's facts as data (counts, bytes, who sees a new version), for a client that says them
    # in the user's language; the headline stays the text every other surface shows.
    summary: dict[str, Any] = field(default_factory=dict)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _join(parts: list[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


@dataclass(frozen=True)
class ArtifactSharing:
    """Who can see one artifact, read from the server's ``artifact_get`` answer (its own fields, never
    model text). ``invites`` counts the outside invites that still work."""

    viewer_url: str
    title: Optional[str] = None
    org: bool = False
    members: int = 0
    groups: int = 0
    invites: int = 0
    pin: Any = "latest"

    @property
    def shared(self) -> bool:
        return bool(self.org or self.members or self.groups or self.invites)

    def audience(self) -> str:
        """``everyone in your organisation and 1 person outside it``; empty when only the owner can see it.
        Org-wide covers named teammates and groups, so they are not counted again."""
        parts: list[str] = []
        if self.org:
            parts.append("everyone in your organisation")
        else:
            if self.members:
                parts.append(_plural(self.members, "teammate", "teammates"))
            if self.groups:
                parts.append(_plural(self.groups, "group", "groups"))
        if self.invites:
            parts.append(_plural(self.invites, "person outside your organisation",
                                 "people outside your organisation"))
        return _join(parts) if parts else ""

    def new_version_line(self) -> str:
        """What a new version means for the people who can see it."""
        if not self.shared:
            return "Only you can see it."
        if self.pin == "latest":
            return f"It's shared with {self.audience()}, who will see this version right away."
        return (f"It's shared with {self.audience()}, who stay on version {self.pin} until you change the "
                "version shown.")

    def counts(self) -> dict[str, Any]:
        return {"org": self.org, "members": self.members, "groups": self.groups, "invites": self.invites,
                "pin": self.pin}

    def as_payload(self) -> dict[str, Any]:
        return {"viewer_url": self.viewer_url, "share_url": share_page_url(self.viewer_url), **self.counts()}


def artifact_sharing(raw: Any) -> Optional[ArtifactSharing]:
    """The sharing an ``artifact_get`` answer reports, or None for an error or an answer Hermes cannot
    read. Only a viewer URL that passes the chat link checks is kept."""
    for body in _result_bodies(raw):
        if not isinstance(body, dict):
            continue
        if "error" in body:
            return None
        url, share = body.get("viewer_url"), body.get("share")
        if not (isinstance(url, str) and _printable(url) and _origin(url) is not None and isinstance(share, dict)):
            continue

        def _count(key: str) -> int:
            value = share.get(key)
            return len(value) if isinstance(value, list) else 0

        invites = share.get("invites")
        live = sum(1 for i in invites if isinstance(i, dict) and i.get("status") != "expired") \
            if isinstance(invites, list) else 0
        pin = share.get("pin")
        return ArtifactSharing(
            viewer_url=url, title=_clean_title(body.get("title")), org=share.get("org") is True,
            members=_count("members"), groups=_count("groups"), invites=live,
            pin=pin if pin == "latest" or (isinstance(pin, int) and not isinstance(pin, bool)) else "latest",
        )
    return None


def _size_label(total: int) -> str:
    return f"{total} bytes" if total < 1024 else f"{total / 1024:.0f} KB" if total < 1024 * 1024 \
        else f"{total / (1024 * 1024):.1f} MB"


def _file_bytes(entry: Any) -> Optional[int]:
    """Bytes a ``{path, content, encoding}`` entry carries, or None when it cannot be told."""
    if not isinstance(entry, dict) or not isinstance(entry.get("content"), str):
        return None
    content = entry["content"]
    return len(content) * 3 // 4 if entry.get("encoding") == "base64" else len(content.encode("utf-8"))


def _files_summary(files: Any) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """``"3 files, 12 KB"``, the per-file details (path and size) for the card, and the same facts as
    data (``{"files": 3, "bytes": 12288}``)."""
    if not isinstance(files, list):
        return "files Hermes could not read", [], {}
    sizes = [_file_bytes(f) for f in files]
    known = [n for n in sizes if n is not None]
    count = f"{len(files)} file{'' if len(files) == 1 else 's'}"
    summary = f"{count}, {_size_label(sum(known))}" if known else count
    details = [{"path": f.get("path") if isinstance(f, dict) else None,
                **({"size": _size_label(n)} if n is not None else {})} for f, n in zip(files, sizes)]
    return summary, details, {"files": len(files), **({"bytes": sum(known)} if known else {})}


_NO_RETRY = "Don't retry it or ask the user about it again unless they bring it up."


def approval_for(server_name: str, tool_name: str, args: Any, *,
                 sharing: Optional[ArtifactSharing] = None) -> Optional[ArtifactApproval]:
    """The approval card for a hosted artifact call that uploads content (a new artifact, a new version);
    None for every other call and every other server. *sharing* (read from the server just before the
    card) lets a new version's card say who will see it. The hosted server is attached only for a Nous
    Portal account with artifacts access, so these cards only ever appear for such accounts. Sharing
    never gets a card: Hermes does not change it (:func:`share_request`)."""
    if server_name != SERVER_NAME:
        return None
    if tool_name == "artifact_create":
        a = args if isinstance(args, dict) else {}
        summary, files, facts = _files_summary(a.get("files"))
        return ArtifactApproval(
            title="Save this as a page you can share?",
            headline=("Hermes made a page you can open from anywhere and share when you're ready. It's saved "
                      f"privately to your Nous account, so only you can see it until you share it. ({summary})"),
            details={"title": a.get("title"), "files": files},
            refusal=f"The user chose not to save this page. Nothing was uploaded. {_NO_RETRY}",
            kind=UPLOAD_KIND_CREATE,
            summary=facts,
        )
    if tool_name == "artifact_update":
        a = args if isinstance(args, dict) else {}
        if isinstance(a.get("edits"), list):
            edits = a["edits"]
            summary = f"{len(edits)} edit{'' if len(edits) == 1 else 's'}"
            change: dict[str, Any] = {"edits": [{"path": e.get("path") if isinstance(e, dict) else None}
                                                for e in edits]}
            facts: dict[str, Any] = {"edits": len(edits)}
        else:
            summary, files, facts = _files_summary(a.get("files"))
            change = {"files": files}
        audience = sharing.new_version_line() if sharing is not None else \
            "Everyone it's shared with may see it."
        return ArtifactApproval(
            title="Update this page?",
            headline=f"This replaces what's on the page ({summary}). {audience}",
            details={"id": a.get("id"), "base_version": a.get("base_version"),
                     **({"title": a["title"]} if a.get("title") is not None else {}), **change},
            refusal=("The user chose not to update this page. Nothing was uploaded and the page is "
                     f"unchanged. {_NO_RETRY}"),
            kind=UPLOAD_KIND_UPDATE,
            summary={**facts, **({"audience": sharing.counts()} if sharing is not None else {})},
        )
    return None


def unattended_upload_refusal(scheduled: bool) -> str:
    """Why an upload did not run where no one can approve it (a scheduled job, a one-shot run), and how
    the user lets it run next time: a remembered "Always" answer skips the card."""
    where = "a scheduled job" if scheduled else "a run with no one to answer"
    return (f"Nothing was uploaded: uploads need the user's approval, and Hermes can't ask for it in {where}. "
            "Tell the user that to let this upload artifacts, they choose \"Always\" the next time Hermes asks "
            "about an upload in chat. Don't retry it in this run.")


def _invite_email(value: Any) -> Optional[str]:
    """*value* as one plain address to fill in on the share page (lower-cased), else None. A loose
    check: the Portal validates again, and the user confirms every address there."""
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    local, at, domain = email.partition("@")
    if (not at or not local or not domain or "@" in domain or "," in email or len(email) > _EMAIL_MAX_CHARS
            or not _printable(email)):
        return None
    return email


@dataclass(frozen=True)
class ShareRequest:
    """An ``artifact_share`` call that could give someone access or change which version they see. Hermes
    never sends it: the user gets a Share button for the artifact instead (with *emails* filled in), and
    makes the change on its share page. *adds* holds plain phrases built only from flags and counts."""

    artifact_id: Optional[str]
    emails: tuple[str, ...]
    adds: tuple[str, ...]
    # It asked to let everyone in the organisation view it (the Share panel starts with that on).
    org: bool = False
    # Opening it to the organisation is ALL it asks: the one widening a user's own click may make here.
    org_only: bool = False


def share_request(server_name: str, tool_name: str, args: Any) -> Optional[ShareRequest]:
    """The :class:`ShareRequest` for an ``artifact_share`` call on the hosted server that could add
    people, or None when the call can only take access away (it goes to the server as it is) or is any
    other call.

    Read from the arguments alone: each list is the full new access list, so a non-empty one may add
    people (a non-empty ``emails`` list could also drop invites it leaves out); ``org: true`` opens the
    artifact to the whole organisation; a ``pin`` changes which version everyone it is shared with sees.
    Anything unexpected counts as adding people."""
    import re

    if server_name != SERVER_NAME or tool_name != SHARE_TOOL:
        return None
    if not isinstance(args, dict):
        return ShareRequest(None, (), ("arguments Hermes could not read",))
    adds: list[str] = []
    if args.get("org") not in (None, False):
        adds.append("everyone in your organisation")
    for key, one, many in (("members", "teammate", "teammates"), ("groups", "group", "groups")):
        value = args.get(key)
        if value is None or value == []:
            continue
        adds.append(_plural(len(value), one, many) if isinstance(value, list) else f"{key} Hermes could not read")
    emails: list[str] = []
    value = args.get("emails")
    if value is not None and value != []:
        if isinstance(value, list):
            emails = list(dict.fromkeys(e for e in map(_invite_email, value) if e))[:INVITE_PREFILL_MAX]
            adds.append(_plural(len(value), "person outside your organisation", "people outside your organisation"))
        else:
            adds.append("emails Hermes could not read")
    if args.get("pin") is not None:
        adds.append("a change to which version they see")
    if not adds:
        return None
    ident = args.get("id")
    artifact_id = ident if isinstance(ident, str) and re.fullmatch(ARTIFACT_ID_PATTERN, ident) else None
    org = args.get("org") is True
    return ShareRequest(artifact_id, tuple(emails), tuple(adds), org=org,
                        org_only=org and adds == ["everyone in your organisation"])


def share_page_url(viewer_url: str, emails: Any = ()) -> str:
    """The artifact's viewer page with its share sheet open (``?share=1``) and *emails* filled in for
    the user to confirm (``&invite=a@example.com,b@example.net``)."""
    from urllib.parse import parse_qsl, urlencode, urlunsplit

    parts = urlsplit(viewer_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in ("share", "invite")]
    query.append(("share", "1"))
    if emails:
        query.append(("invite", ",".join(emails)))
    return urlunsplit(parts._replace(query=urlencode(query), fragment=""))


def share_handoff(artifact_id: str, request: ShareRequest, sharing: ArtifactSharing) -> str:
    """The answer the model gets in place of a share Hermes did not make: nothing changed, and the user
    has a Share button that opens the share page (with any addresses filled in). The same body carries
    ``share_url`` for the surfaces that draw that button (the desktop's card, a chat's link line)."""
    import json

    with_emails = ", with these addresses filled in" if request.emails else ""
    body = {
        "id": artifact_id,
        "title": sharing.title,
        "viewer_url": sharing.viewer_url,
        "share_url": share_page_url(sharing.viewer_url, request.emails),
        "invites_to_confirm": list(request.emails),
        "suggested_org": request.org,
        "access_changed": False,
        "shared_with_now": sharing.audience() or "only you",
        "message": ("Who can see this artifact was NOT changed: only the user changes sharing, on its share "
                    f"page. Hermes has shown them a Share button for it{with_emails}. Tell them to click Share "
                    "and confirm there. Don't call artifact_share again for this request."),
    }
    return json.dumps({"result": json.dumps(body, ensure_ascii=False)}, ensure_ascii=False)


@dataclass(frozen=True)
class ShareLink:
    """A Share button for one artifact: its share page, and the addresses filled in there."""

    url: str
    title: Optional[str] = None
    emails: tuple[str, ...] = ()

    def text(self) -> str:
        """One plain line for a chat that cannot draw a button."""
        title = _chat_plain(self.title or "")
        name = f" “{title}”" if title else ""
        emails = [e for e in (_chat_plain(e, _CHAT_MARKUP) for e in self.emails) if e]
        who = f" with {', '.join(emails)}" if emails else ""
        return f"Share{name}{who} (you confirm on the share page): {self.url}"


def share_link(tool_name: Any, result: Any) -> Optional[ShareLink]:
    """The Share button of a share request Hermes handed to the user (:func:`share_handoff`), else None.
    The share page must be on the viewer page's own origin."""
    from tools.mcp_tool_schema import mcp_prefixed_tool_name

    if tool_name != mcp_prefixed_tool_name(SERVER_NAME, SHARE_TOOL):
        return None
    for body in _result_bodies(result):
        if not isinstance(body, dict):
            continue
        if "error" in body:
            return None
        url, viewer = body.get("share_url"), body.get("viewer_url")
        if (body.get("access_changed") is False and isinstance(url, str) and isinstance(viewer, str)
                and _printable(url) and _origin(url) is not None and _origin(url) == _origin(viewer)):
            emails = body.get("invites_to_confirm")
            return ShareLink(url, _clean_title(body.get("title")),
                             tuple(e for e in emails if _invite_email(e) == e) if isinstance(emails, list) else ())
    return None


def tool_call_arguments(tool_name: str, args: Any) -> Any:
    """:func:`call_arguments` by registry tool name: what the hosted server's handler will send, for checks
    that run before it (the Tool Search bridge validates a deferred call's arguments first)."""
    return call_arguments(SERVER_NAME, args) if tool_name in _artifact_names() else args


def _registered_tool_names() -> frozenset[str]:
    from tools.registry import registry

    return frozenset(registry.get_tool_names_for_toolset(TOOLSET_NAME) or ())


def _artifact_names() -> frozenset[str]:
    return contract_tool_names() | _registered_tool_names()


def _catalog_names(agent: Any) -> frozenset[str]:
    """Every tool *agent*'s toolset selection offers before Tool Search collapses it into the bridge.
    Empty when that cannot be read (the latch then fails closed)."""
    try:
        import model_tools

        return frozenset((tool.get("function") or {}).get("name") for tool in model_tools.get_tool_definitions(
            enabled_toolsets=getattr(agent, "enabled_toolsets", None),
            disabled_toolsets=getattr(agent, "disabled_toolsets", None),
            quiet_mode=True, skip_tool_search_assembly=True) or [])
    except Exception as exc:  # health: allow BLE001 -- a catalog failure must only ever narrow the session
        logger.debug("Hosted artifacts: tool catalog unreadable at session start: %s", exc)
        return frozenset()


def _surface_names(agent: Any, tools: list[dict[str, Any]]) -> frozenset[str]:
    """Every tool name *tools* (a session's first tool load) offers: its definitions and, when the
    Tool Search bridge stands in for deferred tools, the deferred catalog of the session's selection."""
    from tools.tool_search_catalog import BRIDGE_TOOL_NAMES

    names = frozenset((tool.get("function") or {}).get("name") for tool in tools or ())
    return names | _catalog_names(agent) if names & BRIDGE_TOOL_NAMES else names


def _session_key(agent: Any) -> Optional[tuple[str, str]]:
    from hermes_constants import get_hermes_home

    session_id = getattr(agent, "session_id", None)
    return (str(get_hermes_home()), session_id) if isinstance(session_id, str) and session_id else None


def _store_latch(agent: Any, latched: bool) -> None:
    try:
        agent._hosted_artifacts_at_start = latched
    except AttributeError:
        pass


def _recorded_latch(agent: Any) -> Optional[bool]:
    latched = getattr(agent, "_hosted_artifacts_at_start", None)
    if isinstance(latched, bool):
        return latched
    key = _session_key(agent)
    with _decisions_lock:
        latched = _session_latch.get(key) if key is not None else None
    if latched is not None:
        _store_latch(agent, latched)
    return latched


def latch_session_surface(agent: Any, tools: list[dict[str, Any]]) -> None:
    """Record, once per session, whether *tools* (the session's first tool load, before any drop)
    carried the hosted artifacts tools. Later loads (a rebuilt agent, a toggled toolset,
    ``/reload-mcp``, a model switch) find the record and never widen it."""
    if agent is None or _recorded_latch(agent) is not None:
        return
    latched = bool(_surface_names(agent, tools) & _artifact_names())
    key = _session_key(agent)
    with _decisions_lock:
        if key is not None:
            latched = _session_latch.setdefault(key, latched)
            while len(_session_latch) > _SESSION_LATCH_MAX:
                _session_latch.popitem(last=False)
    _store_latch(agent, latched)


def late_attach_drops(agent: Any) -> frozenset[str]:
    """The hosted server's tool names unless *agent*'s session started with them. A session whose
    first tool load was never recorded (:func:`latch_session_surface`) gets none of them."""
    if agent is None or _recorded_latch(agent) is True:
        return frozenset()
    return _artifact_names()


def session_has_artifacts(tool_names: Any) -> bool:
    """True when a session's tool names include the hosted artifacts tools."""
    names = set(tool_names or ())
    return bool(names & contract_tool_names())


def session_offers_artifacts(agent: Any, tool_names: Any) -> bool:
    """True when *agent*'s session can use the hosted artifacts tools: listed in *tool_names*, or deferred
    behind the Tool Search bridge, which :func:`latch_session_surface` recorded at its first tool load.
    The tool list alone misses the deferred case, and with it the guidance for the tools."""
    return session_has_artifacts(tool_names) or _recorded_latch(agent) is True



# ── Viewer links for surfaces that cannot render artifacts inline ─────────────────────────────

# Tools whose successful result names one artifact the user can open.
LINK_TOOLS = ("artifact_create", "artifact_update", "artifact_show", "artifact_get")
_TITLE_MAX_CHARS = 120


@dataclass(frozen=True)
class ViewerLink:
    """The shareable viewer page of one artifact, and its title when the result carried one."""

    url: str
    title: Optional[str] = None

    def as_payload(self) -> dict[str, str]:
        return {"url": self.url, **({"title": self.title} if self.title else {})}

    def text(self) -> str:
        """One plain line naming the artifact and its viewer page."""
        title = _chat_plain(self.title or "")
        return f"Artifact \u201c{title}\u201d: {self.url}" if title else f"Artifact: {self.url}"


# Characters that make links, styling or code in a chat's markdown (``[x](url)``, ``<url>``, ``**``).
# Text Hermes appends to a reply drops them, so an artifact's title or an address can never pose as a
# link or styling of Hermes's own. Addresses keep ``_``, which is common in them.
_CHAT_MARKUP = "\\`*~|<>[]"


def _chat_plain(text: str, markup: str = _CHAT_MARKUP + "_") -> str:
    """*text* with every character of *markup* turned into a space, whitespace collapsed."""
    return " ".join("".join(" " if ch in markup else ch for ch in text).split())


def _printable(value: str) -> bool:
    return all(ch.isprintable() and not ch.isspace() for ch in value)


def _clean_title(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    title = " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())
    if len(title) > _TITLE_MAX_CHARS:
        title = title[:_TITLE_MAX_CHARS - 1].rstrip() + "\u2026"
    return title or None


def _result_bodies(result: Any) -> list[Any]:
    """The JSON objects a tool result may carry the artifact in: the handler envelope itself, its
    ``result`` (JSON text or an object) and its ``structuredContent``."""
    import json

    def _load(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return None
        return value

    envelope = _load(result)
    if not isinstance(envelope, dict):
        return []
    return [envelope, _load(envelope.get("result")), envelope.get("structuredContent")]


def viewer_link(tool_name: Any, result: Any) -> Optional[ViewerLink]:
    """The viewer link of a successful create/update/show/get result, else None.

    Only ``viewer_url`` is ever returned: ``content_url`` is a short-lived ticket for an embedded
    frame and must not be printed or sent. A result that reports an error, a URL that is not a
    plain http(s) link (or that equals the ticket URL) and anything unparsable yield None."""
    from tools.mcp_tool_schema import mcp_prefixed_tool_name

    if not isinstance(tool_name, str) or tool_name not in {
            mcp_prefixed_tool_name(SERVER_NAME, tool) for tool in LINK_TOOLS}:
        return None
    for body in _result_bodies(result):
        if not isinstance(body, dict):
            continue
        if "error" in body:
            return None
        url = body.get("viewer_url")
        if (isinstance(url, str) and _printable(url) and _origin(url) is not None
                and url != body.get("content_url")):
            return ViewerLink(url, _clean_title(body.get("title")))
    return None


# ---------------------------------------------------------------------------------------------------
# The desktop's Publish button (a user's own click; hermes_cli/web_routers/hosted_artifacts.py).

# A hosted artifact id (a ULID).
ARTIFACT_ID_PATTERN = r"[0-9a-hjkmnp-tv-z]{26}"
_PUBLISH_ERROR_MAX_CHARS = 300


def publish_outcome(tool_name: str, raw: Any) -> dict[str, Any]:
    """A user-initiated create/update result as the desktop shows it: ``{ok: True, id, version,
    viewer_url}``, or ``{ok: False, error, stale, head_version}``. Never the short-lived
    ``content_url``; the viewer URL passes the same checks as the chat's artifact links."""
    import json
    import re

    from tools.mcp_tool_schema import mcp_prefixed_tool_name

    bodies = [b for b in _result_bodies(raw) if isinstance(b, dict)]
    for body in bodies:
        if "error" not in body:
            continue
        text = body["error"]
        nested: Any = text
        if isinstance(text, str):
            try:
                nested = json.loads(text)
            except ValueError:
                nested = None
        nested = nested if isinstance(nested, dict) else {}
        head = nested.get("head_version", body.get("head_version"))
        if not isinstance(head, int):
            match = re.search(r'head_version"?\s*[:=]\s*(\d+)', str(text))
            head = int(match.group(1)) if match else None
        stale = nested.get("error") == "stale_base" or "stale_base" in str(text)
        message = nested.get("message") if isinstance(nested.get("message"), str) else str(text)
        message = "".join(ch for ch in message if ch.isprintable())[:_PUBLISH_ERROR_MAX_CHARS]
        return {"ok": False, "error": message or "The artifacts service refused the upload.", "stale": stale,
                "head_version": head}
    link = viewer_link(mcp_prefixed_tool_name(SERVER_NAME, tool_name), raw)
    ident = next((b for b in bodies if isinstance(b.get("id"), str) and isinstance(b.get("version"), int)), None)
    if link is None or ident is None or not re.fullmatch(ARTIFACT_ID_PATTERN, ident["id"]):
        return {"ok": False, "error": "The artifacts service gave an answer Hermes could not read.", "stale": False,
                "head_version": None}
    return {"ok": True, "id": ident["id"], "version": ident["version"], "viewer_url": link.url}
