"""Usage-window plans: what a limit blocks, and what to offer instead.

One rule decides everywhere, so the picker's "used up" note, the limit card's suggestion and the switch
button can never disagree:

* a free model is never blocked;
* a full gauge with scope ``all`` (day / week / month / any new one) blocks every paid model;
* a full ``top_tier`` gauge blocks the models the gateway marks top tier.

Hermes never switches model on its own. When a request is refused, :func:`limit_hit` describes the
limit and names up to two models from the plan's recommended list (in order) that still
work. Each surface renders that as buttons where it already has them, otherwise as a sentence.

Everything here reads cached data only (credits headers, the portal account snapshot, the cached
catalog) and never raises: a failure means "no suggestion".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

PROVIDER = "nous"
# Leave room for the reply: a suggestion must fit the conversation with this many tokens to spare.
_CONTEXT_HEADROOM_TOKENS = 8_000
_MAX_SUGGESTIONS = 2

NOTE_FREE = "free"
NOTE_INCLUDED = "included"


@dataclass(frozen=True)
class ModelSuggestion:
    model: str      # routed id, e.g. "example/model-a"
    label: str      # display name ("Model A"), the id when the catalog has none
    note: str       # NOTE_FREE | NOTE_INCLUDED
    provider: str = PROVIDER

    def to_payload(self) -> dict[str, str]:
        return {"model": self.model, "label": self.label, "note": self.note, "provider": self.provider}


@dataclass(frozen=True)
class CreditsOption:
    """The second way on, past a limit: spend top-up credits.

    ``use_credits``: Keep going on credits is off and there is a balance, so turning it on lets the
    request through. ``add_credits``: no balance (or the setting is already on), so the way on is a
    top-up. Interactive surfaces render it as a button; messaging points at the Portal (a chat button
    could spend the owner's money for anyone in the room)."""
    action: str     # "use_credits" | "add_credits"

    def to_payload(self) -> dict[str, str]:
        return {"action": self.action}


@dataclass(frozen=True)
class LimitHit:
    gauge_id: str                   # day | week | month | top_tier | <server id>
    scope: str                      # all | top_tier
    limit_name: str                 # "daily" / "weekly" / "monthly large-model" / the server label
    resets_at_ms: Optional[int]
    message: str                    # the limit sentence (the server's display_message wins)
    suggestions: tuple[ModelSuggestion, ...] = ()
    credits: Optional[CreditsOption] = None
    current_model: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "gauge_id": self.gauge_id, "scope": self.scope, "limit_name": self.limit_name,
            "resets_at_ms": self.resets_at_ms, "message": self.message,
            "suggestions": [s.to_payload() for s in self.suggestions],
            "credits": self.credits.to_payload() if self.credits else None,
            "current_model": self.current_model,
        }


# ── catalog reads ────────────────────────────────────────────────────────────────────────────────

def cached_pricing(base_url: str) -> dict:
    try:
        from hermes_cli.models_pricing import peek_cached_pricing
        return peek_cached_pricing(base_url) or {}
    except Exception:
        return {}


def _billed_elsewhere(model: str, pricing: Optional[dict]) -> bool:
    try:
        from hermes_cli.models import _is_subscription_billed
        return _is_subscription_billed((pricing or {}).get(model))
    except Exception:
        return False


def is_free(model: str, pricing: Optional[dict]) -> bool:
    """$0 to the plan: a ``:free`` id or a row the catalog lists as free."""
    if not model:
        return False
    if model.endswith(":free"):
        return True
    try:
        from hermes_cli.models import _is_model_free
        return bool(pricing) and _is_model_free(model, pricing)
    except Exception:
        return False


def is_top_tier(model: str, pricing: Optional[dict]) -> bool:
    """The gateway's ``top_tier`` mark. Unmarked or no catalog: not top tier."""
    entry = (pricing or {}).get(model) if model else None
    if is_free(model, pricing):
        return False
    return isinstance(entry, dict) and entry.get("top_tier") is True


def model_label(model: str, pricing: Optional[dict]) -> str:
    entry = (pricing or {}).get(model)
    name = entry.get("name") if isinstance(entry, dict) else None
    return name.strip() if isinstance(name, str) and name.strip() else model


# ── the one rule ─────────────────────────────────────────────────────────────────────────────────

def blocking_gauge(model: str, gauges: Iterable[Any], *, free: bool, top_tier: bool) -> Optional[Any]:
    """The full gauge that stops *model*, or None while it still has included room. Free models are
    never blocked. Of several full gauges, the one that frees up last (that is when the model works)."""
    if free:
        return None
    from agent.usage_gauges import SCOPE_ALL, SCOPE_TOP_TIER
    full = [g for g in gauges
            if getattr(g, "full", False) and (g.scope == SCOPE_ALL or (g.scope == SCOPE_TOP_TIER and top_tier))]
    return max(full, key=lambda g: g.resets_at_ms or 0) if full else None


def is_blocked(model: str, gauges: Iterable[Any], pricing: Optional[dict]) -> bool:
    return blocking_gauge(model, gauges, free=is_free(model, pricing),
                          top_tier=is_top_tier(model, pricing)) is not None


# ── suggestions ──────────────────────────────────────────────────────────────────────────────────

def _plan_usage() -> Any:
    # Plan shape only (featured models), so the last account snapshot at any age answers: the 60s
    # cache has long lapsed by the time a window runs out mid-session.
    try:
        from hermes_cli.model_picker_usage import nous_picker_usage
        return nous_picker_usage(any_age=True)
    except Exception:
        return None


def _portal_free_models() -> list[str]:
    """The Portal's admin-edited free picks, from what Hermes already holds (in-process, else the
    last copy on disk). Never fetches: this runs mid-turn."""
    try:
        from hermes_cli import models as m
        payload = next((data for data, _ts in reversed(list(m._nous_recommended_cache.values())) if data), None)
        if payload is None:
            disk = m._read_nous_recommended_disk(m._resolve_nous_portal_url())
            payload = disk[0] if disk else None
        entries = payload.get("freeRecommendedModels") if isinstance(payload, dict) else None
        return [n for e in (entries or []) if (n := m._extract_model_name(e))]
    except Exception:
        return []


def _fits_context(model: str, needed_tokens: Optional[int], base_url: str) -> bool:
    if not needed_tokens or needed_tokens <= 0:
        return True
    try:
        from agent.model_metadata import get_model_context_length
        ctx = get_model_context_length(model, base_url=base_url, provider=PROVIDER)
        return not ctx or ctx >= needed_tokens + _CONTEXT_HEADROOM_TOKENS
    except Exception:
        return True


def suggest_models(
    current_model: str, gauges: Iterable[Any], *, pricing: Optional[dict] = None, featured: Iterable[str] = (),
    needed_tokens: Optional[int] = None, base_url: str = "", limit: int = _MAX_SUGGESTIONS,
) -> tuple[ModelSuggestion, ...]:
    """The plan's recommended models (in order) that the full gauges do not block, then the Portal's free picks.
    Never the current model, only a model whose context window holds the conversation."""
    gauges = tuple(gauges)
    pool = [m for m in featured if isinstance(m, str) and m]
    pool += [m for m in _portal_free_models() if m not in pool]
    out: list[ModelSuggestion] = []
    for model in pool:
        if model == current_model or _billed_elsewhere(model, pricing):
            continue
        if is_blocked(model, gauges, pricing) or not _fits_context(model, needed_tokens, base_url):
            continue
        out.append(ModelSuggestion(model=model, label=model_label(model, pricing),
                                   note=NOTE_FREE if is_free(model, pricing) else NOTE_INCLUDED))
        if len(out) >= limit:
            break
    return tuple(out)


def credits_option(state: Any) -> Optional[CreditsOption]:
    """How credits could carry on past the limit (see :class:`CreditsOption`). None while paused (a card
    update restores included usage; the paused copy says so)."""
    if state is None or getattr(state, "paused_reason", None):
        return None
    purchased = int(getattr(state, "purchased_micros", 0) or 0)
    if getattr(state, "keep_going_on_credits", None) is False and purchased > 0:
        return CreditsOption("use_credits")
    return CreditsOption("add_credits")


def limit_hit(agent: Any, *, message: str, needed_tokens: Optional[int] = None) -> Optional[LimitHit]:
    """After a usage-window 402: which limit stopped *agent*'s current model, when it resets, and what to
    offer. None when the state names no full gauge for this model (a paused plan, a stale snapshot)."""
    try:
        state = getattr(agent, "_credits_state", None)
        if state is None or not getattr(state, "is_usage_window", False):
            return None
        model = str(getattr(agent, "model", "") or "")
        base_url = str(getattr(agent, "base_url", "") or "")
        pricing = cached_pricing(base_url)
        gauges = tuple(getattr(state, "gauges", ()) or ())
        g = blocking_gauge(model, gauges, free=is_free(model, pricing), top_tier=is_top_tier(model, pricing))
        if g is None:
            # The 402 says this model was refused; without a top-tier mark in the cache a full top-tier
            # gauge is still the only explanation.
            from agent.usage_gauges import top_tier_gauge
            top = top_tier_gauge(gauges)
            g = top if top is not None and top.full else None
        if g is None:
            return None
        from agent.usage_gauges import gauge_limit_name
        usage = _plan_usage()
        featured = tuple(getattr(usage, "featured_model_ids", ()) or ())
        return LimitHit(
            gauge_id=g.id, scope=g.scope, limit_name=gauge_limit_name(g), resets_at_ms=g.resets_at_ms,
            message=message, current_model=model, credits=credits_option(state),
            suggestions=suggest_models(model, gauges, pricing=pricing, featured=featured,
                                       needed_tokens=needed_tokens, base_url=base_url),
        )
    except Exception:
        logger.debug("plan limits: limit_hit failed", exc_info=True)
        return None


# ── copy ─────────────────────────────────────────────────────────────────────────────────────────

def suggestion_phrase(s: ModelSuggestion) -> str:
    """``Model A (free)``."""
    return f"{s.label} ({s.note})"


def next_steps_text(hit: LimitHit, *, model_command: str = "/model", topup_command: Optional[str] = "/topup",
                    portal: bool = False) -> str:
    """One line on how to keep going, for a surface without buttons. ``model_command`` is the platform's
    own spelling (``!model`` on Slack / Matrix). ``portal``: point at the Portal for credits (messaging),
    instead of a command that would spend from the chat."""
    parts: list[str] = []
    if hit.suggestions:
        parts.append(f"Switch to {suggestion_phrase(hit.suggestions[0])} with {model_command}")
    else:
        parts.append(f"Pick another model with {model_command}")
    if hit.credits is not None:
        if portal or not topup_command:
            parts.append("turn on Keep going in the Portal to use credits" if hit.credits.action == "use_credits"
                         else "add credits in the Portal")
        else:
            parts.append(f"turn on Keep going in {topup_command} to use credits" if hit.credits.action == "use_credits"
                         else f"add credits with {topup_command}")
    return ", or ".join(parts) + "."


def limit_next_steps_from_payload(limit: dict, *, model_command: str = "/model", topup_command: Optional[str] = "/topup",
                                  portal: bool = False, markup: bool = False) -> str:
    """:func:`next_steps_text` for a :meth:`LimitHit.to_payload` dict (what the result carries). ``markup``
    bolds the commands for a Rich panel."""
    sug = [ModelSuggestion(model=str(s.get("model") or ""), label=str(s.get("label") or s.get("model") or ""),
                           note=str(s.get("note") or NOTE_INCLUDED))
           for s in (limit.get("suggestions") or []) if isinstance(s, dict) and s.get("model")]
    credits = limit.get("credits") if isinstance(limit.get("credits"), dict) else None
    hit = LimitHit(gauge_id=str(limit.get("gauge_id") or ""), scope=str(limit.get("scope") or ""),
                   limit_name=str(limit.get("limit_name") or ""), resets_at_ms=limit.get("resets_at_ms"),
                   message=str(limit.get("message") or ""), suggestions=tuple(sug),
                   credits=CreditsOption(str(credits.get("action"))) if credits and credits.get("action") else None)
    if markup:
        model_command, topup_command = f"[bold]{model_command}[/]", (f"[bold]{topup_command}[/]" if topup_command else None)
    return next_steps_text(hit, model_command=model_command, topup_command=topup_command, portal=portal)
