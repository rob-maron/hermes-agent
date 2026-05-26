"""Shared FAL.ai SDK plumbing.

Holds the atoms that every FAL-backed tool needs:

* :func:`import_fal_client` — lazy import + ``lazy_deps`` integration so
  ``fal_client`` isn't pulled at cold start (it added ~64 ms per CLI
  invocation when imported eagerly).
* :class:`_ManagedFalSyncClient` — wrapper that drives a Nous-managed
  fal-queue gateway through the standard ``fal_client.SyncClient``
  primitives.
* :func:`_normalize_fal_queue_url_format`, :func:`_extract_http_status`
  — small helpers used by both the managed client wrapper and
  ``_submit_fal_request``.
* :func:`resolve_managed_fal_gateway_for_toolset` — shared
  managed-or-direct selector keyed by the toolset config section
  (``image_gen`` or ``video_gen``).
* :func:`get_or_create_shared_managed_fal_client` — process-wide cache
  of :class:`_ManagedFalSyncClient` keyed by gateway origin + token,
  shared across all FAL-backed toolsets because they all hit the same
  ``fal-queue-gateway`` host.

Per-toolset wrappers in :mod:`tools.image_generation_tool` (``_resolve_managed_fal_gateway``,
``_get_managed_fal_client``, ``_submit_fal_request``) delegate to these
helpers while preserving the legacy patch targets that existing test
suites (``tests/tools/test_image_generation.py``,
``tests/tools/test_managed_media_gateways.py``) and the
``plugins/image_gen/fal/`` plugin's ``_it`` indirection rely on. New
toolsets should call the shared helpers here directly instead of growing
parallel state.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional, Union
from urllib.parse import urlencode


def import_fal_client() -> Any:
    """Import ``fal_client`` (via ``lazy_deps`` when available) and return
    the module reference.

    Callers are responsible for caching the result on their own module
    global — keeping per-module globals lets tests monkey-patch the
    target module's ``fal_client`` attribute and have the patched value
    stick for that module's call sites.

    Raises :class:`ImportError` if the package is genuinely unavailable.
    """
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("image.fal", prompt=False)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 — lazy_deps surfaces install hints
        raise ImportError(str(exc))
    import fal_client  # type: ignore  # noqa: WPS433 — intentionally lazy
    return fal_client


def _normalize_fal_queue_url_format(queue_run_origin: str) -> str:
    normalized_origin = str(queue_run_origin or "").strip().rstrip("/")
    if not normalized_origin:
        raise ValueError("Managed FAL queue origin is required")
    return f"{normalized_origin}/"


def _extract_http_status(exc: BaseException) -> Optional[int]:
    """Return an HTTP status code from httpx/fal exceptions, else None.

    Defensive across exception shapes — httpx.HTTPStatusError exposes
    ``.response.status_code`` while fal_client wrappers may expose
    ``.status_code`` directly.
    """
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    return None


def _extract_http_error_message(exc: BaseException) -> Optional[str]:
    """Return a concise message from an HTTP error response when available."""
    response = getattr(exc, "response", None)
    if response is None:
        return None

    try:
        payload = response.json()
    except Exception:
        payload = None

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            code = error.get("code")
            if isinstance(message, str) and message.strip():
                if isinstance(code, str) and code.strip():
                    return f"{code}: {message.strip()}"
                return message.strip()
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()

    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()[:500]
    return None


def _managed_fal_gateway_error_message(
    *,
    model: str,
    status: int,
    response_message: Optional[str],
    hint: str,
) -> str:
    base = f"Nous Subscription gateway rejected model '{model}' (HTTP {status})"
    if response_message:
        base = f"{base}: {response_message}"

    if status == 409:
        return (
            f"{base}. This is a request conflict, usually an idempotency/replay "
            f"conflict or an upstream FAL conflict, not proof that the model is "
            f"missing from the proxy. {hint}"
        )

    if status == 400 or status == 404:
        return (
            f"{base}. The model or payload may not be enabled/supported by "
            f"the Nous Portal FAL proxy. {hint}"
        )

    return f"{base}. {hint}"


class _ManagedFalSyncClient:
    """Small per-instance wrapper around ``fal_client.SyncClient`` for
    managed queue hosts.

    The wrapper carries its own ``fal_client`` module reference instead
    of reaching into a module global, so callers stay in control of
    which module's ``fal_client`` is in scope (matters for the test
    patches that swap the legacy module's ``fal_client`` attribute).
    """

    def __init__(self, fal_client: Any, *, key: str, queue_run_origin: str):
        sync_client_class = getattr(fal_client, "SyncClient", None)
        if sync_client_class is None:
            raise RuntimeError("fal_client.SyncClient is required for managed FAL gateway mode")

        client_module = getattr(fal_client, "client", None)
        if client_module is None:
            raise RuntimeError("fal_client.client is required for managed FAL gateway mode")

        self._queue_url_format = _normalize_fal_queue_url_format(queue_run_origin)
        self._sync_client = sync_client_class(key=key)
        self._http_client = getattr(self._sync_client, "_client", None)
        self._maybe_retry_request = getattr(client_module, "_maybe_retry_request", None)
        self._raise_for_status = getattr(client_module, "_raise_for_status", None)
        self._request_handle_class = getattr(client_module, "SyncRequestHandle", None)
        self._add_hint_header = getattr(client_module, "add_hint_header", None)
        self._add_priority_header = getattr(client_module, "add_priority_header", None)
        self._add_timeout_header = getattr(client_module, "add_timeout_header", None)

        if self._http_client is None:
            raise RuntimeError("fal_client.SyncClient._client is required for managed FAL gateway mode")
        if self._maybe_retry_request is None or self._raise_for_status is None:
            raise RuntimeError("fal_client.client request helpers are required for managed FAL gateway mode")
        if self._request_handle_class is None:
            raise RuntimeError("fal_client.client.SyncRequestHandle is required for managed FAL gateway mode")

    def submit(
        self,
        application: str,
        arguments: Dict[str, Any],
        *,
        path: str = "",
        hint: Optional[str] = None,
        webhook_url: Optional[str] = None,
        priority: Any = None,
        headers: Optional[Dict[str, str]] = None,
        start_timeout: Optional[Union[int, float]] = None,
    ):
        url = self._queue_url_format + application
        if path:
            url += "/" + path.lstrip("/")
        if webhook_url is not None:
            url += "?" + urlencode({"fal_webhook": webhook_url})

        request_headers = dict(headers or {})
        if hint is not None and self._add_hint_header is not None:
            self._add_hint_header(hint, request_headers)
        if priority is not None:
            if self._add_priority_header is None:
                raise RuntimeError("fal_client.client.add_priority_header is required for priority requests")
            self._add_priority_header(priority, request_headers)
        if start_timeout is not None:
            if self._add_timeout_header is None:
                raise RuntimeError("fal_client.client.add_timeout_header is required for timeout requests")
            self._add_timeout_header(start_timeout, request_headers)

        response = self._maybe_retry_request(
            self._http_client,
            "POST",
            url,
            json=arguments,
            timeout=getattr(self._sync_client, "default_timeout", 120.0),
            headers=request_headers,
        )
        self._raise_for_status(response)

        data = response.json()
        return self._request_handle_class(
            request_id=data["request_id"],
            response_url=data["response_url"],
            status_url=data["status_url"],
            cancel_url=data["cancel_url"],
            client=self._http_client,
        )


# ---------------------------------------------------------------------------
# Shared managed-FAL helpers (used by both image and video toolsets)
# ---------------------------------------------------------------------------

_shared_managed_fal_client: Optional[_ManagedFalSyncClient] = None
_shared_managed_fal_client_config: Optional[tuple] = None
_shared_managed_fal_client_lock = threading.Lock()


def resolve_managed_fal_gateway_for_toolset(toolset_key: str):
    """Return managed fal-queue gateway config for a toolset config section.

    Used by both ``image_gen`` and ``video_gen``. Returns ``None`` when
    the user has a direct ``FAL_KEY`` and has not explicitly opted into
    the gateway for ``toolset_key``; otherwise returns the resolved
    :class:`~tools.managed_tool_gateway.ManagedToolGatewayConfig` (or
    ``None`` if no Nous subscription is available).
    """
    from tools.managed_tool_gateway import resolve_managed_tool_gateway
    from tools.tool_backend_helpers import fal_key_is_configured, prefers_gateway

    if fal_key_is_configured() and not prefers_gateway(toolset_key):
        return None
    return resolve_managed_tool_gateway("fal-queue")


def get_or_create_shared_managed_fal_client(
    managed_gateway,
    fal_client_module: Any,
) -> _ManagedFalSyncClient:
    """Return a process-wide cached :class:`_ManagedFalSyncClient`.

    The cache key is ``(gateway_origin, nous_user_token)`` — both image
    and video generation hit the same fal-queue gateway with the same
    Nous access token, so they share the underlying httpx.Client and
    avoid creating a fresh per-call connection pool.
    """
    global _shared_managed_fal_client, _shared_managed_fal_client_config

    client_config = (
        managed_gateway.gateway_origin.rstrip("/"),
        managed_gateway.nous_user_token,
    )
    with _shared_managed_fal_client_lock:
        if (
            _shared_managed_fal_client is not None
            and _shared_managed_fal_client_config == client_config
        ):
            return _shared_managed_fal_client

        _shared_managed_fal_client = _ManagedFalSyncClient(
            fal_client_module,
            key=managed_gateway.nous_user_token,
            queue_run_origin=managed_gateway.gateway_origin,
        )
        _shared_managed_fal_client_config = client_config
        return _shared_managed_fal_client


def reset_shared_managed_fal_client() -> None:
    """Drop the cached managed FAL client (used by tests for isolation)."""
    global _shared_managed_fal_client, _shared_managed_fal_client_config
    with _shared_managed_fal_client_lock:
        _shared_managed_fal_client = None
        _shared_managed_fal_client_config = None


def submit_via_managed_fal_gateway(
    model: str,
    arguments: Dict[str, Any],
    *,
    fal_client_module: Any,
    managed_gateway,
    idempotency_key: str,
    not_allowlisted_hint: Optional[str] = None,
) -> Any:
    """Submit a FAL request through the managed gateway, with a helpful
    ValueError on 4xx responses (allowlist miss, billing gate, etc.).

    Returns the same ``SyncRequestHandle`` shape as ``fal_client.submit``,
    so callers can `.get()` it for the result.
    """
    managed_client = get_or_create_shared_managed_fal_client(
        managed_gateway, fal_client_module
    )
    request_headers = {"x-idempotency-key": idempotency_key}
    try:
        return managed_client.submit(
            model,
            arguments=arguments,
            headers=request_headers,
        )
    except Exception as exc:  # noqa: BLE001 — surface a clearer error
        status = _extract_http_status(exc)
        if status is not None and 400 <= status < 500:
            hint = not_allowlisted_hint or (
                "Either:\n"
                "  • Set FAL_KEY in your environment to use FAL.ai directly, or\n"
                "  • Pick a different model via `hermes tools`."
            )
            raise ValueError(
                _managed_fal_gateway_error_message(
                    model=model,
                    status=status,
                    response_message=_extract_http_error_message(exc),
                    hint=hint,
                )
            ) from exc
        raise
