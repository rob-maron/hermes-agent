"""Black Forest Labs image generation backend.

Supports BFL's FLUX.1 and FLUX.2 model families. Two modes:

  - Direct: user supplies ``BFL_API_KEY`` and we call ``api.bfl.ai`` directly.
  - Managed: user is on a Nous Subscription; we route via the managed
    tool-gateway at ``bfl-gateway.<domain>``, no vendor key needed.

Mode selection mirrors the FAL pattern:

  - If ``BFL_API_KEY`` is set AND ``image_gen.use_gateway`` is not truthy
    -> direct (``api.bfl.ai``).
  - Otherwise -> managed (when subscription + token are available).

The gateway enforces a model allowlist, blocks webhook fields, rewrites
the polling URL, and bills via NAS. Hermes just needs to be a polite
HTTP client.

Selection precedence for the active model (first hit wins):

  1. ``BFL_IMAGE_MODEL`` env var (escape hatch for scripts / tests).
  2. ``image_gen.bfl.model`` in ``config.yaml``.
  3. ``image_gen.model`` in ``config.yaml`` (when it's a known BFL model).
  4. :data:`DEFAULT_MODEL` -- ``flux-2-pro``.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    success_response,
)
from tools.managed_tool_gateway import resolve_managed_tool_gateway
from tools.tool_backend_helpers import managed_nous_tools_enabled, prefers_gateway

logger = logging.getLogger(__name__)


DIRECT_API_ORIGIN = "https://api.bfl.ai"
DEFAULT_SUBMIT_TIMEOUT = 120.0
DEFAULT_POLL_TIMEOUT = 300.0
POLL_INTERVAL_SECONDS = 0.5

# BFL's documented terminal statuses (anything not "Pending" is terminal).
_TERMINAL_STATUSES = frozenset({
    "Ready",
    "Error",
    "Failed",
    "Task not found",
    "Request Moderated",
    "Content Moderated",
})

# The gateway rejects webhook fields with 400; we strip them defensively
# on the Hermes side so the user gets a cleaner error than HTTP 400.
_FORBIDDEN_FIELDS = frozenset({"webhook_url", "webhook_secret"})

# Hard allowlist for ``output_format``. The value is interpolated into
# the on-disk cache filename (as the file extension) so anything outside
# this set must NOT be trusted as a path component -- a value like
# ``"jpg/../../auth.json"`` would otherwise let a caller (e.g. an LLM
# acting on a prompt-injection) escape ``$HERMES_HOME/cache/images/``
# and clobber arbitrary files. Values outside the allowlist are
# silently coerced to "jpeg" before any file-path use.
_ALLOWED_OUTPUT_FORMATS = frozenset({"jpeg", "jpg", "png", "webp"})


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------
#
# Mirrors the gateway-side ``BFL_MODEL_ALLOWLIST`` (13 models -- 7 FLUX.2
# variable-priced + 6 FLUX.1 flat-priced). Each entry describes how to
# translate the unified Hermes inputs (prompt + aspect_ratio) into the
# model-specific BFL request body, plus per-model picker info.
#
# ``size_style`` controls aspect-ratio translation:
#   "wh"      -> send ``width``/``height`` integers (FLUX.2, FLUX 1.1 PRO)
#   "ar"      -> send ``aspect_ratio`` string ("16:9", "1:1", "9:16")
#                (FLUX 1.1 PRO Ultra, FLUX.1 Kontext family)
#   "edit"    -> editing model that requires an ``input_image``;
#                aspect ratio not driven by prompt-only inputs.
#
# ``supports`` is a whitelist of keys allowed in the outgoing payload --
# any caller-supplied key outside this set is dropped before submission
# so a request never trips the gateway's strict input validation.
#
# ``pricing_kind`` is informational only (drives UI labels). The gateway
# infers the kind dynamically from BFL's response.

_FLUX2_COMMON_SUPPORTS = frozenset({
    "prompt", "width", "height", "seed", "safety_tolerance",
    "output_format", "input_image", "input_image_2", "input_image_3",
    "input_image_4", "input_image_5", "input_image_6", "input_image_7",
    "input_image_8",
})

_MODELS: Dict[str, Dict[str, Any]] = {
    # ── FLUX.2 family — variable per-megapixel pricing ─────────────────
    "flux-2-pro": {
        "display": "FLUX.2 [PRO]",
        "speed": "~6s",
        "strengths": "Recommended for editing + generation",
        "price": "from $0.03/MP",
        "pricing_kind": "variable",
        "size_style": "wh",
        "sizes": {
            "landscape": (1536, 864),
            "square": (1024, 1024),
            "portrait": (864, 1536),
        },
        "defaults": {"output_format": "jpeg"},
        "supports": _FLUX2_COMMON_SUPPORTS,
    },
    "flux-2-pro-preview": {
        "display": "FLUX.2 [PRO] Preview",
        "speed": "~6s",
        "strengths": "Preview build of FLUX.2 [PRO]",
        "price": "from $0.03/MP",
        "pricing_kind": "variable",
        "size_style": "wh",
        "sizes": {
            "landscape": (1536, 864),
            "square": (1024, 1024),
            "portrait": (864, 1536),
        },
        "defaults": {"output_format": "jpeg"},
        "supports": _FLUX2_COMMON_SUPPORTS,
    },
    "flux-2-max": {
        "display": "FLUX.2 [MAX]",
        "speed": "~10s",
        "strengths": "Highest fidelity FLUX.2 tier",
        "price": "from $0.07/MP",
        "pricing_kind": "variable",
        "size_style": "wh",
        "sizes": {
            "landscape": (1536, 864),
            "square": (1024, 1024),
            "portrait": (864, 1536),
        },
        "defaults": {"output_format": "jpeg"},
        "supports": _FLUX2_COMMON_SUPPORTS,
    },
    "flux-2-flex": {
        "display": "FLUX.2 [FLEX]",
        "speed": "~5s",
        "strengths": "Balanced quality/speed",
        "price": "from $0.06/MP",
        "pricing_kind": "variable",
        "size_style": "wh",
        "sizes": {
            "landscape": (1536, 864),
            "square": (1024, 1024),
            "portrait": (864, 1536),
        },
        "defaults": {"output_format": "jpeg"},
        "supports": _FLUX2_COMMON_SUPPORTS,
    },
    "flux-2-klein-9b": {
        "display": "FLUX.2 [Klein 9B]",
        "speed": "~3s",
        "strengths": "Open-weights 9B, fast",
        "price": "from $0.015/MP",
        "pricing_kind": "variable",
        "size_style": "wh",
        "sizes": {
            "landscape": (1280, 720),
            "square": (1024, 1024),
            "portrait": (720, 1280),
        },
        "defaults": {"output_format": "jpeg"},
        "supports": _FLUX2_COMMON_SUPPORTS,
    },
    "flux-2-klein-9b-preview": {
        "display": "FLUX.2 [Klein 9B KV]",
        "speed": "~3s",
        "strengths": "Klein 9B preview build",
        "price": "from $0.015/MP",
        "pricing_kind": "variable",
        "size_style": "wh",
        "sizes": {
            "landscape": (1280, 720),
            "square": (1024, 1024),
            "portrait": (720, 1280),
        },
        "defaults": {"output_format": "jpeg"},
        "supports": _FLUX2_COMMON_SUPPORTS,
    },
    "flux-2-klein-4b": {
        "display": "FLUX.2 [Klein 4B]",
        "speed": "~2s",
        "strengths": "Smallest FLUX.2 tier, cheapest",
        "price": "from $0.014/MP",
        "pricing_kind": "variable",
        "size_style": "wh",
        "sizes": {
            "landscape": (1280, 720),
            "square": (1024, 1024),
            "portrait": (720, 1280),
        },
        "defaults": {"output_format": "jpeg"},
        "supports": _FLUX2_COMMON_SUPPORTS,
    },

    # ── FLUX.1 family — flat per-request pricing ───────────────────────
    "flux-pro-1.1": {
        "display": "FLUX 1.1 [PRO]",
        "speed": "~5s",
        "strengths": "Fast text-to-image",
        "price": "$0.04/image",
        "pricing_kind": "flat",
        "size_style": "wh",
        "sizes": {
            "landscape": (1440, 810),
            "square": (1024, 1024),
            "portrait": (810, 1440),
        },
        "defaults": {"output_format": "jpeg", "prompt_upsampling": False},
        "supports": frozenset({
            "prompt", "width", "height", "seed", "safety_tolerance",
            "output_format", "image_prompt", "image_prompt_strength",
            "prompt_upsampling",
        }),
    },
    "flux-pro-1.1-ultra": {
        "display": "FLUX 1.1 [PRO] Ultra",
        "speed": "~10s",
        "strengths": "Ultra-high resolution (4MP)",
        "price": "$0.06/image",
        "pricing_kind": "flat",
        "size_style": "ar",
        "sizes": {
            "landscape": "16:9",
            "square": "1:1",
            "portrait": "9:16",
        },
        "defaults": {"output_format": "jpeg", "raw": False},
        "supports": frozenset({
            "prompt", "aspect_ratio", "seed", "safety_tolerance",
            "output_format", "image_prompt", "raw",
        }),
    },
    "flux-kontext-pro": {
        "display": "FLUX.1 Kontext [PRO]",
        "speed": "~6s",
        "strengths": "Image editing with text + reference",
        "price": "$0.04/image",
        "pricing_kind": "flat",
        "size_style": "ar",
        "sizes": {
            "landscape": "16:9",
            "square": "1:1",
            "portrait": "9:16",
        },
        "defaults": {"output_format": "jpeg", "prompt_upsampling": False},
        "supports": frozenset({
            "prompt", "aspect_ratio", "input_image", "seed",
            "safety_tolerance", "output_format", "prompt_upsampling",
        }),
    },
    "flux-kontext-max": {
        "display": "FLUX.1 Kontext [MAX]",
        "speed": "~10s",
        "strengths": "Highest-fidelity Kontext editing",
        "price": "$0.08/image",
        "pricing_kind": "flat",
        "size_style": "ar",
        "sizes": {
            "landscape": "16:9",
            "square": "1:1",
            "portrait": "9:16",
        },
        "defaults": {"output_format": "jpeg", "prompt_upsampling": False},
        "supports": frozenset({
            "prompt", "aspect_ratio", "input_image", "seed",
            "safety_tolerance", "output_format", "prompt_upsampling",
        }),
    },
    "flux-pro-1.0-fill": {
        "display": "FLUX.1 Fill [PRO]",
        "speed": "~8s",
        "strengths": "Inpainting (requires input image + mask)",
        "price": "$0.05/image",
        "pricing_kind": "flat",
        "size_style": "edit",
        "sizes": {},
        "defaults": {"output_format": "jpeg", "steps": 50, "guidance": 60},
        "supports": frozenset({
            "prompt", "image", "mask", "steps", "guidance", "seed",
            "safety_tolerance", "output_format", "prompt_upsampling",
        }),
    },
    "flux-pro-1.0-expand": {
        "display": "FLUX.1 Expand [PRO]",
        "speed": "~8s",
        "strengths": "Outpainting / canvas expansion",
        "price": "$0.05/image",
        "pricing_kind": "flat",
        "size_style": "edit",
        "sizes": {},
        "defaults": {"output_format": "jpeg", "steps": 50, "guidance": 60},
        "supports": frozenset({
            "prompt", "image", "top", "bottom", "left", "right", "steps",
            "guidance", "seed", "safety_tolerance", "output_format",
            "prompt_upsampling",
        }),
    },
}

DEFAULT_MODEL = "flux-2-pro"


# ---------------------------------------------------------------------------
# Config + mode resolution
# ---------------------------------------------------------------------------


def _load_image_gen_config() -> Dict[str, Any]:
    """Read ``image_gen`` from config.yaml (returns {} on any failure)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not load image_gen config: %s", exc)
        return {}


def _bfl_api_key() -> str:
    """Return the configured direct BFL key (whitespace-stripped)."""
    return (os.getenv("BFL_API_KEY") or "").strip()


def _resolve_model_id() -> Tuple[str, Dict[str, Any]]:
    """Decide which BFL model to use and return ``(model_id, meta)``.

    See module docstring for the precedence chain.
    """
    env_override = (os.environ.get("BFL_IMAGE_MODEL") or "").strip()
    if env_override and env_override in _MODELS:
        return env_override, _MODELS[env_override]

    cfg = _load_image_gen_config()
    bfl_section = cfg.get("bfl") if isinstance(cfg.get("bfl"), dict) else {}
    if isinstance(bfl_section, dict):
        candidate = bfl_section.get("model")
        if isinstance(candidate, str) and candidate.strip() in _MODELS:
            return candidate.strip(), _MODELS[candidate.strip()]

    top = cfg.get("model")
    if isinstance(top, str) and top.strip() in _MODELS:
        return top.strip(), _MODELS[top.strip()]

    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


def _resolve_client_config() -> Tuple[str, str, bool]:
    """Return ``(origin, key, is_managed)`` for whichever mode applies.

    Direct mode wins when ``BFL_API_KEY`` is set and the user has not
    explicitly opted into the gateway via ``image_gen.use_gateway: true``.
    Otherwise managed mode is attempted; if neither is available we
    return empty strings so the caller can surface a clear error.
    """
    direct_key = _bfl_api_key()
    use_gateway = prefers_gateway("image_gen")

    if direct_key and not use_gateway:
        return DIRECT_API_ORIGIN, direct_key, False

    managed = resolve_managed_tool_gateway("bfl")
    if managed is not None:
        return managed.gateway_origin.rstrip("/"), managed.nous_user_token, True

    if direct_key:
        return DIRECT_API_ORIGIN, direct_key, False

    return "", "", False


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------


def _build_payload(
    model_id: str,
    prompt: str,
    aspect_ratio: str,
    *,
    seed: Optional[int],
    overrides: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Translate unified Hermes inputs into a BFL-native request body.

    Filters the result to the model's ``supports`` whitelist so unknown
    keys never leak through to BFL or the gateway. Webhook fields are
    always stripped (defense-in-depth -- the gateway rejects them with
    400 anyway).
    """
    meta = _MODELS[model_id]
    payload: Dict[str, Any] = dict(meta.get("defaults", {}))
    payload["prompt"] = prompt

    size_style = meta["size_style"]
    sizes = meta.get("sizes", {})
    if size_style == "wh":
        wh = sizes.get(aspect_ratio) or sizes.get(DEFAULT_ASPECT_RATIO)
        if wh:
            payload["width"], payload["height"] = wh
    elif size_style == "ar":
        ar = sizes.get(aspect_ratio) or sizes.get(DEFAULT_ASPECT_RATIO)
        if ar:
            payload["aspect_ratio"] = ar
    # size_style == "edit" -> caller must supply image/mask via overrides

    if isinstance(seed, int):
        payload["seed"] = seed

    if overrides:
        for key, value in overrides.items():
            if value is None:
                continue
            if key in _FORBIDDEN_FIELDS:
                continue
            payload[key] = value

    supports = meta["supports"]
    return {k: v for k, v in payload.items() if k in supports}


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


def _request_headers(key: str) -> Dict[str, str]:
    """Build the BFL-native request headers (``x-key`` auth scheme).

    The gateway accepts ``x-key`` as a Nous JWT and strips/replaces it
    before forwarding to BFL with the vendor key. ``x-idempotency-key``
    is honored by the gateway so a retry is a no-op.
    """
    return {
        "x-key": key,
        "content-type": "application/json",
        "accept": "application/json",
        "x-idempotency-key": str(uuid.uuid4()),
    }


def _submit(model_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """POST the request body to BFL/the gateway and return the JSON envelope.

    Raises :class:`ValueError` for managed-mode 4xx responses (with a
    user-facing remediation message) and re-raises everything else.
    """
    origin, key, is_managed = _resolve_client_config()
    if not origin or not key:
        raise RuntimeError(
            "BFL backend not configured (no BFL_API_KEY and no Nous Subscription)"
        )

    headers = _request_headers(key)
    url = f"{origin}/v1/{model_id}"

    with httpx.Client(timeout=DEFAULT_SUBMIT_TIMEOUT) as client:
        response = client.post(url, json=body, headers=headers)

    if response.status_code >= 400:
        if is_managed and 400 <= response.status_code < 500:
            try:
                detail = response.json().get("error", {})
                message = detail.get("message") or response.text
            except Exception:
                message = response.text or response.reason_phrase
            raise ValueError(
                f"Nous Subscription gateway rejected BFL model "
                f"'{model_id}' (HTTP {response.status_code}): {message}. "
                f"The model may not be on the gateway allowlist or the "
                f"Nous Portal may not have a price entry enabled for it."
            )
        response.raise_for_status()

    return response.json()


def _polling_url_is_trusted(polling_url: str, origin: str) -> bool:
    """Return True iff ``polling_url`` shares the submit ``origin``'s host.

    Defense against a tampered submit response that points polling at an
    attacker-controlled host (which would receive the auth header sent
    via ``x-key`` -- in direct mode the user's BFL_API_KEY, in managed
    mode the Nous user token).

    Accepts:

    * Exact host match (``api.bfl.ai`` -> ``api.bfl.ai``;
      ``bfl-gateway.example.com`` -> ``bfl-gateway.example.com``).
    * Same-registrable-domain subdomain match. BFL routes polling to
      regional endpoints (e.g. submitting to ``api.bfl.ai`` returns a
      polling URL on ``api.us1.bfl.ai``), so a host that ends in
      ``.<last-two-labels-of-origin>`` is also trusted.

    Anything else (foreign host, non-http(s) scheme, malformed URL)
    fails closed.
    """
    from urllib.parse import urlparse

    try:
        polled = urlparse(polling_url)
        submitted = urlparse(origin)
    except Exception:
        return False

    if polled.scheme not in ("http", "https"):
        return False
    if polled.scheme != submitted.scheme:
        return False

    polled_host = (polled.hostname or "").lower()
    submitted_host = (submitted.hostname or "").lower()
    if not polled_host or not submitted_host:
        return False
    if polled_host == submitted_host:
        return True

    labels = submitted_host.split(".")
    if len(labels) < 2:
        return False
    base = ".".join(labels[-2:])
    return polled_host.endswith(f".{base}")


def _poll_until_terminal(
    polling_url: str,
    key: str,
    *,
    timeout_seconds: float = DEFAULT_POLL_TIMEOUT,
) -> Dict[str, Any]:
    """Poll the gateway-rewritten ``polling_url`` until a terminal status.

    The polling URL returned by the gateway is already pointed at the
    gateway origin -- callers must use it verbatim and not rewrite the
    host. Tests patch ``time.sleep``/``time.monotonic`` on this module
    to short-circuit the loop.
    """
    deadline = time.monotonic() + timeout_seconds
    headers = {"x-key": key, "accept": "application/json"}

    with httpx.Client(timeout=30.0) as client:
        while time.monotonic() < deadline:
            response = client.get(polling_url, headers=headers)
            response.raise_for_status()
            data = response.json()
            status = data.get("status", "")
            if status in _TERMINAL_STATUSES:
                return data
            time.sleep(POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        f"BFL task did not reach a terminal status within {timeout_seconds}s"
    )


def _save_delivery_url(url: str, prefix: str, *, extension: str = "jpg") -> str:
    """Download a BFL delivery URL into ``$HERMES_HOME/cache/images``.

    BFL delivery URLs expire after ~10 minutes and have no CORS, so we
    download promptly and re-host as a local file. Returns the absolute
    file path on success; falls back to the original URL if anything goes
    wrong (the URL is still usable for the duration of its TTL).
    """
    try:
        from agent.image_gen_provider import _images_cache_dir

        with httpx.Client(timeout=60.0) as client:
            response = client.get(url)
            response.raise_for_status()
            content = response.content
    except Exception as exc:
        logger.debug("Could not download BFL delivery URL %s: %s", url, exc)
        return url

    import datetime as _dt

    cache_dir = _images_cache_dir()
    timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:8]
    # Strip any path separators that may have leaked into prefix/extension
    # so the resulting filename is guaranteed to live directly under
    # cache_dir (no traversal). The primary defense is the _ALLOWED_OUTPUT_FORMATS
    # allowlist at the call site; this is belt-and-suspenders for future callers.
    safe_prefix = str(prefix).replace("/", "_").replace("\\", "_").replace("..", "_")
    safe_extension = str(extension).replace("/", "_").replace("\\", "_").replace("..", "_")
    path = cache_dir / f"{safe_prefix}_{timestamp}_{short}.{safe_extension}"
    try:
        path.write_bytes(content)
    except Exception as exc:
        logger.debug("Could not save BFL image to cache: %s", exc)
        return url
    return str(path)


# ---------------------------------------------------------------------------
# Provider implementation
# ---------------------------------------------------------------------------


class BflImageGenProvider(ImageGenProvider):
    """Black Forest Labs (FLUX) image generation provider.

    Selects between direct (``api.bfl.ai``) and managed-gateway
    (``bfl-gateway.<domain>``) modes per request based on credential
    availability and the user's gateway opt-in.
    """

    @property
    def name(self) -> str:
        return "bfl"

    @property
    def display_name(self) -> str:
        return "Black Forest Labs"

    def is_available(self) -> bool:
        if _bfl_api_key():
            return True
        if managed_nous_tools_enabled():
            return resolve_managed_tool_gateway("bfl") is not None
        return False

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": meta["price"],
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Black Forest Labs",
            "badge": "paid",
            "tag": "FLUX.2 (variable per-MP) and FLUX.1 (flat-priced)",
            "env_vars": [
                {
                    "key": "BFL_API_KEY",
                    "prompt": "BFL API key (skip if using Nous Subscription)",
                    "url": "https://api.bfl.ai/auth/profile",
                    "optional": True,
                },
            ],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        model: Optional[str] = None,
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt_str = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)

        if not prompt_str:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider="bfl",
                aspect_ratio=aspect,
            )

        if isinstance(model, str) and model.strip() in _MODELS:
            model_id = model.strip()
            meta = _MODELS[model_id]
        else:
            model_id, meta = _resolve_model_id()

        # Caller-supplied overrides flow through kwargs; webhook fields
        # and unsupported keys are filtered in _build_payload().
        overrides = {k: v for k, v in kwargs.items() if v is not None}
        body = _build_payload(
            model_id, prompt_str, aspect, seed=seed, overrides=overrides,
        )

        try:
            envelope = _submit(model_id, body)
        except ValueError as exc:
            return error_response(
                error=str(exc),
                error_type="gateway_rejected",
                provider="bfl",
                model=model_id,
                prompt=prompt_str,
                aspect_ratio=aspect,
            )
        except httpx.TimeoutException:
            return error_response(
                error=f"BFL submit timed out after {DEFAULT_SUBMIT_TIMEOUT:.0f}s",
                error_type="timeout",
                provider="bfl",
                model=model_id,
                prompt=prompt_str,
                aspect_ratio=aspect,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            try:
                detail = exc.response.json().get("error", {}).get("message")
            except Exception:
                detail = None
            message = detail or (
                exc.response.text[:300] if exc.response is not None else str(exc)
            )
            return error_response(
                error=f"BFL submit failed ({status}): {message}",
                error_type="api_error",
                provider="bfl",
                model=model_id,
                prompt=prompt_str,
                aspect_ratio=aspect,
            )
        except Exception as exc:
            logger.debug("BFL submit failed", exc_info=True)
            return error_response(
                error=f"BFL submit failed: {exc}",
                error_type="api_error",
                provider="bfl",
                model=model_id,
                prompt=prompt_str,
                aspect_ratio=aspect,
            )

        polling_url = envelope.get("polling_url")
        task_id = envelope.get("id") or envelope.get("task_id")
        if not isinstance(polling_url, str) or not polling_url.strip():
            return error_response(
                error="BFL submit response did not include a polling_url",
                error_type="invalid_response",
                provider="bfl",
                model=model_id,
                prompt=prompt_str,
                aspect_ratio=aspect,
            )

        # The gateway/BFL rewrites the polling URL, so we use it verbatim
        # -- but only after confirming it points back at the same host
        # (or a same-domain regional subdomain). This blocks a tampered
        # submit response from exfiltrating the auth token to a foreign
        # host via the next GET.
        origin, key, _ = _resolve_client_config()
        if not _polling_url_is_trusted(polling_url, origin):
            return error_response(
                error=(
                    f"BFL submit returned a polling_url on an untrusted host "
                    f"(submit origin: {origin}). Refusing to follow it."
                ),
                error_type="invalid_response",
                provider="bfl",
                model=model_id,
                prompt=prompt_str,
                aspect_ratio=aspect,
            )
        try:
            terminal = _poll_until_terminal(polling_url, key)
        except TimeoutError as exc:
            return error_response(
                error=str(exc),
                error_type="timeout",
                provider="bfl",
                model=model_id,
                prompt=prompt_str,
                aspect_ratio=aspect,
            )
        except Exception as exc:
            logger.debug("BFL poll failed", exc_info=True)
            return error_response(
                error=f"BFL poll failed: {exc}",
                error_type="api_error",
                provider="bfl",
                model=model_id,
                prompt=prompt_str,
                aspect_ratio=aspect,
            )

        status = terminal.get("status", "")
        if status != "Ready":
            details = terminal.get("details") or {}
            detail_message = details.get("message") if isinstance(details, dict) else None
            return error_response(
                error=f"BFL task ended with status '{status}'"
                + (f": {detail_message}" if detail_message else ""),
                error_type="task_failed",
                provider="bfl",
                model=model_id,
                prompt=prompt_str,
                aspect_ratio=aspect,
            )

        result = terminal.get("result") or {}
        sample_url = result.get("sample") if isinstance(result, dict) else None
        if not isinstance(sample_url, str) or not sample_url.strip():
            return error_response(
                error="BFL Ready response did not include result.sample URL",
                error_type="invalid_response",
                provider="bfl",
                model=model_id,
                prompt=prompt_str,
                aspect_ratio=aspect,
            )

        # Sanitize output_format before using it as a filename extension.
        # See _ALLOWED_OUTPUT_FORMATS for the rationale (path traversal).
        raw_format = body.get("output_format") or "jpeg"
        fmt = raw_format.lower() if isinstance(raw_format, str) else "jpeg"
        if fmt not in _ALLOWED_OUTPUT_FORMATS:
            fmt = "jpeg"
        extension = "jpg" if fmt == "jpeg" else fmt
        image_ref = _save_delivery_url(
            sample_url, prefix=f"bfl_{model_id}", extension=extension,
        )

        extra: Dict[str, Any] = {
            "task_id": task_id,
            "delivery_url": sample_url,
        }
        cost = envelope.get("cost")
        if cost is not None:
            extra["cost"] = cost
        if envelope.get("input_mp") is not None:
            extra["input_mp"] = envelope.get("input_mp")
        if envelope.get("output_mp") is not None:
            extra["output_mp"] = envelope.get("output_mp")

        return success_response(
            image=image_ref,
            model=model_id,
            prompt=prompt_str,
            aspect_ratio=aspect,
            provider="bfl",
            extra=extra,
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:
    """Plugin entry point — wire :class:`BflImageGenProvider` into the registry."""
    ctx.register_image_gen_provider(BflImageGenProvider())
