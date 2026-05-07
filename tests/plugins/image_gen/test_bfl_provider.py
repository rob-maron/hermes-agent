"""Tests for the bundled BFL (Black Forest Labs / FLUX) image_gen plugin."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import plugins.image_gen.bfl as bfl_plugin


@pytest.fixture(autouse=True)
def _tmp_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


@pytest.fixture(autouse=True)
def _no_managed_nous_tools(monkeypatch):
    """Force ``managed_nous_tools_enabled()`` to False unless a test
    explicitly opts in. The plugin's resolve flow checks this when no
    direct key is present."""
    monkeypatch.setattr(
        "hermes_cli.auth.get_nous_auth_status",
        lambda: {"logged_in": False},
    )
    monkeypatch.setattr(
        "hermes_cli.models.check_nous_free_tier",
        lambda: True,
    )


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("BFL_API_KEY", "test-bfl-key")
    return bfl_plugin.BflImageGenProvider()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeHttpxClient:
    """Captures POST/GET requests and returns scripted responses."""

    def __init__(self, *, post_responses: List[SimpleNamespace], get_responses: List[SimpleNamespace]):
        self.post_responses = list(post_responses)
        self.get_responses = list(get_responses)
        self.posts: List[Dict[str, Any]] = []
        self.gets: List[Dict[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def post(self, url, *, json=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        if not self.post_responses:
            raise AssertionError("Unexpected POST call (no scripted response)")
        return self.post_responses.pop(0)

    def get(self, url, *, headers=None):
        self.gets.append({"url": url, "headers": headers})
        if not self.get_responses:
            raise AssertionError("Unexpected GET call (no scripted response)")
        return self.get_responses.pop(0)


def _http_response(
    *,
    status: int = 200,
    json_body: Optional[Dict[str, Any]] = None,
    content: bytes = b"",
    text: str = "",
):
    response = SimpleNamespace()
    response.status_code = status
    response.reason_phrase = "OK" if status < 400 else "Error"
    response.text = text or ""
    response.content = content

    def _json():
        if json_body is None:
            raise ValueError("no json body")
        return json_body

    response.json = _json

    def _raise_for_status():
        if status >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"{status} {response.reason_phrase}",
                request=SimpleNamespace(method="POST", url="https://example/"),
                response=response,
            )

    response.raise_for_status = _raise_for_status
    return response


@pytest.fixture
def patch_httpx(monkeypatch):
    """Install a fake httpx.Client that returns scripted responses.

    Yields a lambda `(post_responses, get_responses) -> _FakeHttpxClient`
    so each test can set up its own scripted scenario.
    """
    state: Dict[str, Any] = {"client": None}

    def factory(post_responses, get_responses):
        client = _FakeHttpxClient(
            post_responses=post_responses,
            get_responses=get_responses,
        )
        state["client"] = client
        monkeypatch.setattr(
            bfl_plugin.httpx,
            "Client",
            lambda *args, **kwargs: client,
        )
        return client

    yield factory


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


class TestMetadata:
    def test_name(self, provider):
        assert provider.name == "bfl"

    def test_default_model(self, provider):
        assert provider.default_model() == "flux-2-pro"

    def test_catalog_has_thirteen_models(self, provider):
        ids = [m["id"] for m in provider.list_models()]
        assert len(ids) == 13
        assert "flux-2-pro" in ids
        assert "flux-pro-1.1" in ids
        assert "flux-kontext-max" in ids

    def test_excluded_models_not_in_catalog(self, provider):
        ids = {m["id"] for m in provider.list_models()}
        assert "flux-dev" not in ids
        assert "flux-pro-1.1-finetuned" not in ids


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_no_key_no_managed_unavailable(self, monkeypatch):
        monkeypatch.delenv("BFL_API_KEY", raising=False)
        assert bfl_plugin.BflImageGenProvider().is_available() is False

    def test_direct_key_available(self, monkeypatch):
        monkeypatch.setenv("BFL_API_KEY", "test")
        assert bfl_plugin.BflImageGenProvider().is_available() is True

    def test_managed_subscription_available(self, monkeypatch):
        monkeypatch.delenv("BFL_API_KEY", raising=False)
        monkeypatch.setattr(
            "hermes_cli.auth.get_nous_auth_status",
            lambda: {"logged_in": True},
        )
        monkeypatch.setattr(
            "hermes_cli.models.check_nous_free_tier",
            lambda: False,
        )
        monkeypatch.setenv("TOOL_GATEWAY_USER_TOKEN", "nous-token")
        monkeypatch.setenv("TOOL_GATEWAY_DOMAIN", "example.com")
        assert bfl_plugin.BflImageGenProvider().is_available() is True


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


class TestClientConfig:
    def test_direct_mode_when_key_set_and_no_gateway(self, monkeypatch):
        monkeypatch.setenv("BFL_API_KEY", "test-key")
        origin, key, is_managed = bfl_plugin._resolve_client_config()
        assert origin == "https://api.bfl.ai"
        assert key == "test-key"
        assert is_managed is False

    def test_managed_mode_when_no_direct_key(self, monkeypatch, tmp_path):
        monkeypatch.delenv("BFL_API_KEY", raising=False)
        monkeypatch.setattr(
            "hermes_cli.auth.get_nous_auth_status",
            lambda: {"logged_in": True},
        )
        monkeypatch.setattr(
            "hermes_cli.models.check_nous_free_tier",
            lambda: False,
        )
        monkeypatch.setenv("TOOL_GATEWAY_USER_TOKEN", "nous-token")
        monkeypatch.setenv("TOOL_GATEWAY_DOMAIN", "example.com")
        origin, key, is_managed = bfl_plugin._resolve_client_config()
        assert origin == "https://bfl-gateway.example.com"
        assert key == "nous-token"
        assert is_managed is True

    def test_managed_mode_wins_when_use_gateway_opted_in(self, monkeypatch, tmp_path):
        """Direct key set, but config opts into the gateway."""
        import yaml
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"image_gen": {"use_gateway": True, "provider": "bfl"}})
        )
        monkeypatch.setenv("BFL_API_KEY", "test-key")
        monkeypatch.setattr(
            "hermes_cli.auth.get_nous_auth_status",
            lambda: {"logged_in": True},
        )
        monkeypatch.setattr(
            "hermes_cli.models.check_nous_free_tier",
            lambda: False,
        )
        monkeypatch.setenv("TOOL_GATEWAY_USER_TOKEN", "nous-token")
        monkeypatch.setenv("TOOL_GATEWAY_DOMAIN", "example.com")

        origin, key, is_managed = bfl_plugin._resolve_client_config()
        assert origin == "https://bfl-gateway.example.com"
        assert key == "nous-token"
        assert is_managed is True


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------


class TestBuildPayload:
    def test_flux2_uses_width_height(self):
        body = bfl_plugin._build_payload(
            "flux-2-pro", "a cat", "landscape", seed=None, overrides=None,
        )
        assert body["prompt"] == "a cat"
        assert body["width"] == 1536
        assert body["height"] == 864
        assert "aspect_ratio" not in body

    def test_flux_pro_1_1_ultra_uses_aspect_ratio(self):
        body = bfl_plugin._build_payload(
            "flux-pro-1.1-ultra", "a cat", "portrait", seed=None, overrides=None,
        )
        assert body["aspect_ratio"] == "9:16"
        assert "width" not in body
        assert "height" not in body

    def test_unsupported_keys_filtered(self):
        body = bfl_plugin._build_payload(
            "flux-2-pro", "a cat", "square", seed=None,
            overrides={"unknown_field": "value", "seed": 42},
        )
        assert "unknown_field" not in body
        assert body["seed"] == 42

    def test_webhook_fields_stripped(self):
        body = bfl_plugin._build_payload(
            "flux-2-pro", "a cat", "square", seed=None,
            overrides={
                "webhook_url": "https://attacker/",
                "webhook_secret": "shh",
            },
        )
        assert "webhook_url" not in body
        assert "webhook_secret" not in body

    def test_seed_passed_through_when_int(self):
        body = bfl_plugin._build_payload(
            "flux-2-pro", "a cat", "square", seed=99, overrides=None,
        )
        assert body["seed"] == 99


# ---------------------------------------------------------------------------
# Submit + poll flow
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_empty_prompt_rejected(self, provider):
        result = provider.generate("", aspect_ratio="square")
        assert result["success"] is False
        assert result["error_type"] == "invalid_argument"

    def test_no_credentials_returns_error(self, monkeypatch):
        monkeypatch.delenv("BFL_API_KEY", raising=False)
        result = bfl_plugin.BflImageGenProvider().generate("a cat")
        assert result["success"] is False

    def test_direct_submit_uses_bfl_origin_and_x_key(self, provider, patch_httpx, monkeypatch):
        client = patch_httpx(
            post_responses=[
                _http_response(json_body={
                    "id": "task-123",
                    "polling_url": "https://api.bfl.ai/v1/get_result?id=task-123",
                }),
            ],
            get_responses=[
                _http_response(json_body={
                    "id": "task-123",
                    "status": "Ready",
                    "result": {"sample": "https://delivery.us-east.bfl.ai/abc"},
                }),
                _http_response(content=b"fake-bytes-not-a-real-image"),
            ],
        )
        # Don't sleep between polls — first poll is Ready anyway.
        monkeypatch.setattr(bfl_plugin.time, "sleep", lambda _: None)

        result = provider.generate("a watercolor fox", aspect_ratio="landscape")

        assert result["success"] is True
        assert result["model"] == "flux-2-pro"
        assert result["aspect_ratio"] == "landscape"
        assert result["provider"] == "bfl"
        assert result["task_id"] == "task-123"
        assert result["delivery_url"] == "https://delivery.us-east.bfl.ai/abc"

        post = client.posts[0]
        assert post["url"] == "https://api.bfl.ai/v1/flux-2-pro"
        assert post["headers"]["x-key"] == "test-bfl-key"
        assert "x-idempotency-key" in post["headers"]
        assert "Authorization" not in post["headers"]
        body = post["json"]
        assert body["prompt"] == "a watercolor fox"
        assert body["width"] == 1536
        assert body["height"] == 864

    def test_managed_submit_uses_gateway_origin(self, patch_httpx, monkeypatch, tmp_path):
        monkeypatch.delenv("BFL_API_KEY", raising=False)
        monkeypatch.setattr(
            "hermes_cli.auth.get_nous_auth_status",
            lambda: {"logged_in": True},
        )
        monkeypatch.setattr(
            "hermes_cli.models.check_nous_free_tier",
            lambda: False,
        )
        monkeypatch.setenv("TOOL_GATEWAY_USER_TOKEN", "nous-token")
        monkeypatch.setenv("BFL_GATEWAY_URL", "https://bfl-gateway.example.com")

        polling_url = "https://bfl-gateway.example.com/v1/get_result?id=task-abc"

        client = patch_httpx(
            post_responses=[
                _http_response(json_body={
                    "id": "task-abc",
                    "polling_url": polling_url,
                    "cost": 12,
                    "input_mp": 0,
                    "output_mp": 4,
                }),
            ],
            get_responses=[
                _http_response(json_body={
                    "id": "task-abc",
                    "status": "Ready",
                    "result": {"sample": "https://delivery.us-east.bfl.ai/xyz"},
                }),
                _http_response(content=b"managed-image-bytes"),
            ],
        )
        monkeypatch.setattr(bfl_plugin.time, "sleep", lambda _: None)

        provider = bfl_plugin.BflImageGenProvider()
        result = provider.generate("a city skyline", aspect_ratio="square")

        assert result["success"] is True
        assert result["task_id"] == "task-abc"
        assert result["cost"] == 12
        assert result["input_mp"] == 0
        assert result["output_mp"] == 4

        post = client.posts[0]
        assert post["url"] == "https://bfl-gateway.example.com/v1/flux-2-pro"
        assert post["headers"]["x-key"] == "nous-token"

        # The polling URL is gateway-rewritten — Hermes must use it verbatim.
        get = client.gets[0]
        assert get["url"] == polling_url
        assert get["headers"]["x-key"] == "nous-token"

    def test_polling_handles_pending_then_ready(self, provider, patch_httpx, monkeypatch):
        polling_url = "https://api.bfl.ai/v1/get_result?id=task-99"
        client = patch_httpx(
            post_responses=[
                _http_response(json_body={
                    "id": "task-99",
                    "polling_url": polling_url,
                }),
            ],
            get_responses=[
                _http_response(json_body={"id": "task-99", "status": "Pending"}),
                _http_response(json_body={"id": "task-99", "status": "Pending"}),
                _http_response(json_body={
                    "id": "task-99",
                    "status": "Ready",
                    "result": {"sample": "https://delivery.example/img.jpg"},
                }),
                _http_response(content=b"png-bytes"),
            ],
        )
        sleeps: list = []
        monkeypatch.setattr(bfl_plugin.time, "sleep", sleeps.append)

        result = provider.generate("a cat", aspect_ratio="square")
        assert result["success"] is True
        assert len(client.gets) == 3 + 1  # 3 polls + 1 download
        assert sleeps  # we did sleep between polls

    def test_moderated_status_returns_error(self, provider, patch_httpx, monkeypatch):
        polling_url = "https://api.bfl.ai/v1/get_result?id=task-mod"
        patch_httpx(
            post_responses=[
                _http_response(json_body={"id": "task-mod", "polling_url": polling_url}),
            ],
            get_responses=[
                _http_response(json_body={
                    "id": "task-mod",
                    "status": "Content Moderated",
                    "details": {"message": "blocked by safety filter"},
                }),
            ],
        )
        monkeypatch.setattr(bfl_plugin.time, "sleep", lambda _: None)

        result = provider.generate("dangerous prompt", aspect_ratio="square")
        assert result["success"] is False
        assert result["error_type"] == "task_failed"
        assert "Content Moderated" in result["error"]

    def test_managed_4xx_maps_to_actionable_error(self, patch_httpx, monkeypatch):
        monkeypatch.delenv("BFL_API_KEY", raising=False)
        monkeypatch.setattr(
            "hermes_cli.auth.get_nous_auth_status",
            lambda: {"logged_in": True},
        )
        monkeypatch.setattr(
            "hermes_cli.models.check_nous_free_tier",
            lambda: False,
        )
        monkeypatch.setenv("TOOL_GATEWAY_USER_TOKEN", "nous-token")
        monkeypatch.setenv("BFL_GATEWAY_URL", "https://bfl-gateway.example.com")

        patch_httpx(
            post_responses=[
                _http_response(
                    status=400,
                    json_body={
                        "error": {
                            "code": "VALIDATION_ERROR",
                            "message": "Model 'flux-2-pro' is not allowlisted",
                        }
                    },
                    text='{"error":{"code":"VALIDATION_ERROR","message":"Model \'flux-2-pro\' is not allowlisted"}}',
                ),
            ],
            get_responses=[],
        )

        provider = bfl_plugin.BflImageGenProvider()
        result = provider.generate("hi", aspect_ratio="square")
        assert result["success"] is False
        assert result["error_type"] == "gateway_rejected"
        assert "Nous Subscription gateway" in result["error"]

    def test_missing_polling_url_returns_invalid_response(self, provider, patch_httpx):
        patch_httpx(
            post_responses=[_http_response(json_body={"id": "abc"})],
            get_responses=[],
        )
        result = provider.generate("a cat", aspect_ratio="square")
        assert result["success"] is False
        assert result["error_type"] == "invalid_response"

    def test_explicit_model_kwarg_overrides_default(self, provider, patch_httpx, monkeypatch):
        client = patch_httpx(
            post_responses=[
                _http_response(json_body={
                    "id": "t1",
                    "polling_url": "https://api.bfl.ai/v1/get_result?id=t1",
                }),
            ],
            get_responses=[
                _http_response(json_body={
                    "id": "t1",
                    "status": "Ready",
                    "result": {"sample": "https://delivery/x"},
                }),
                _http_response(content=b"x"),
            ],
        )
        monkeypatch.setattr(bfl_plugin.time, "sleep", lambda _: None)

        result = provider.generate("a cat", aspect_ratio="portrait", model="flux-pro-1.1-ultra")
        assert result["success"] is True
        assert result["model"] == "flux-pro-1.1-ultra"
        assert client.posts[0]["url"].endswith("/v1/flux-pro-1.1-ultra")
        # Ultra uses aspect_ratio (not width/height).
        assert "aspect_ratio" in client.posts[0]["json"]
        assert "width" not in client.posts[0]["json"]


# ---------------------------------------------------------------------------
# Image caching
# ---------------------------------------------------------------------------


class TestImageCache:
    def test_delivery_download_writes_to_cache(self, provider, patch_httpx, monkeypatch, tmp_path):
        client = patch_httpx(
            post_responses=[
                _http_response(json_body={
                    "id": "t1",
                    "polling_url": "https://api.bfl.ai/v1/get_result?id=t1",
                }),
            ],
            get_responses=[
                _http_response(json_body={
                    "id": "t1",
                    "status": "Ready",
                    "result": {"sample": "https://delivery.example/img.jpg"},
                }),
                _http_response(content=b"actual-jpeg-bytes"),
            ],
        )
        monkeypatch.setattr(bfl_plugin.time, "sleep", lambda _: None)

        result = provider.generate("a sunset", aspect_ratio="landscape")
        assert result["success"] is True

        saved = Path(result["image"])
        assert saved.exists()
        assert saved.parent == tmp_path / "cache" / "images"
        assert saved.read_bytes() == b"actual-jpeg-bytes"
        assert saved.suffix == ".jpg"

    def test_download_failure_falls_back_to_delivery_url(self, provider, patch_httpx, monkeypatch):
        polling_url = "https://api.bfl.ai/v1/get_result?id=t9"

        class _FlakyClient(_FakeHttpxClient):
            def get(self, url, *, headers=None):
                if url == polling_url:
                    return super().get(url, headers=headers)
                # Download attempt -- raise so the plugin falls back.
                raise RuntimeError("network down")

        flaky = _FlakyClient(
            post_responses=[
                _http_response(json_body={
                    "id": "t9",
                    "polling_url": polling_url,
                }),
            ],
            get_responses=[
                _http_response(json_body={
                    "id": "t9",
                    "status": "Ready",
                    "result": {"sample": "https://delivery.example/img.jpg"},
                }),
            ],
        )
        monkeypatch.setattr(bfl_plugin.httpx, "Client", lambda *a, **k: flaky)
        monkeypatch.setattr(bfl_plugin.time, "sleep", lambda _: None)

        result = provider.generate("a cat", aspect_ratio="square")
        assert result["success"] is True
        # The plugin returned the original delivery URL because the
        # cache download failed.
        assert result["image"] == "https://delivery.example/img.jpg"


# ---------------------------------------------------------------------------
# Setup schema
# ---------------------------------------------------------------------------


class TestSetupSchema:
    def test_schema_advertises_optional_key(self, provider):
        schema = provider.get_setup_schema()
        assert schema["name"] == "Black Forest Labs"
        keys = [v["key"] for v in schema["env_vars"]]
        assert keys == ["BFL_API_KEY"]
        assert schema["env_vars"][0]["optional"] is True


# ---------------------------------------------------------------------------
# Security: output_format path traversal
# ---------------------------------------------------------------------------


class TestOutputFormatSanitization:
    """``output_format`` is interpolated into the cache filename. A caller
    (e.g. an LLM acting on a prompt-injection) must NOT be able to escape
    ``$HERMES_HOME/cache/images/`` via path-separator characters."""

    def _scripted_run(self, patch_httpx, monkeypatch, *, output_format):
        polling_url = "https://api.bfl.ai/v1/get_result?id=trav"
        patch_httpx(
            post_responses=[
                _http_response(json_body={
                    "id": "trav",
                    "polling_url": polling_url,
                }),
            ],
            get_responses=[
                _http_response(json_body={
                    "id": "trav",
                    "status": "Ready",
                    "result": {"sample": "https://delivery.example/x"},
                }),
                _http_response(content=b"img-bytes"),
            ],
        )
        monkeypatch.setattr(bfl_plugin.time, "sleep", lambda _: None)

    def test_traversal_extension_does_not_escape_cache_dir(
        self, provider, patch_httpx, monkeypatch, tmp_path,
    ):
        self._scripted_run(patch_httpx, monkeypatch, output_format="../../etc/passwd")
        result = provider.generate(
            "a cat", aspect_ratio="square",
            output_format="../../etc/passwd",
        )
        assert result["success"] is True
        saved = Path(result["image"])
        # File must live directly under cache/images, not anywhere else.
        cache_root = tmp_path / "cache" / "images"
        assert saved.parent == cache_root, (
            f"saved path {saved} escaped cache dir {cache_root}"
        )
        # Extension must have been coerced to a safe value.
        assert saved.suffix == ".jpg"

    def test_unknown_format_falls_back_to_jpeg(
        self, provider, patch_httpx, monkeypatch,
    ):
        self._scripted_run(patch_httpx, monkeypatch, output_format="exe")
        result = provider.generate(
            "a cat", aspect_ratio="square", output_format="exe",
        )
        assert result["success"] is True
        assert Path(result["image"]).suffix == ".jpg"

    def test_known_png_format_preserved(
        self, provider, patch_httpx, monkeypatch,
    ):
        self._scripted_run(patch_httpx, monkeypatch, output_format="png")
        result = provider.generate(
            "a cat", aspect_ratio="square", output_format="png",
        )
        assert result["success"] is True
        assert Path(result["image"]).suffix == ".png"

    def test_save_delivery_url_strips_separators_in_extension(
        self, monkeypatch, tmp_path,
    ):
        """Belt-and-suspenders: even if a future caller bypasses the
        allowlist, ``_save_delivery_url`` must keep the file inside
        cache_dir."""
        captured: Dict[str, Any] = {}

        class _DLClient:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc_info):
                return None

            def get(self_inner, url):
                captured["dl_url"] = url
                return _http_response(content=b"bytes")

        monkeypatch.setattr(
            bfl_plugin.httpx, "Client", lambda *a, **k: _DLClient(),
        )
        cache_root = tmp_path / "cache" / "images"
        result = bfl_plugin._save_delivery_url(
            "https://delivery.example/x",
            prefix="bfl_../../bad",
            extension="jpg/../../escape",
        )
        saved = Path(result)
        assert saved.exists()
        assert saved.parent == cache_root


# ---------------------------------------------------------------------------
# Security: polling URL same-host enforcement
# ---------------------------------------------------------------------------


class TestPollingUrlTrust:
    """A tampered submit response must not be able to redirect polling
    -- and the auth header that goes with it -- to a foreign host."""

    def test_trusted_helper_accepts_exact_host(self):
        assert bfl_plugin._polling_url_is_trusted(
            "https://api.bfl.ai/v1/get_result?id=1",
            "https://api.bfl.ai",
        )

    def test_trusted_helper_accepts_regional_subdomain(self):
        # BFL routes polling to regional endpoints in direct mode.
        assert bfl_plugin._polling_url_is_trusted(
            "https://api.us1.bfl.ai/v1/get_result?id=1",
            "https://api.bfl.ai",
        )

    def test_trusted_helper_rejects_foreign_host(self):
        assert not bfl_plugin._polling_url_is_trusted(
            "https://attacker.example/v1/get_result?id=1",
            "https://api.bfl.ai",
        )

    def test_trusted_helper_rejects_lookalike_suffix(self):
        # "evilbfl.ai" must NOT be treated as a subdomain of "bfl.ai".
        assert not bfl_plugin._polling_url_is_trusted(
            "https://api.evilbfl.ai/v1/get_result?id=1",
            "https://api.bfl.ai",
        )

    def test_trusted_helper_rejects_scheme_downgrade(self):
        assert not bfl_plugin._polling_url_is_trusted(
            "http://api.bfl.ai/v1/get_result?id=1",
            "https://api.bfl.ai",
        )

    def test_trusted_helper_rejects_non_http_scheme(self):
        assert not bfl_plugin._polling_url_is_trusted(
            "file:///etc/passwd",
            "https://api.bfl.ai",
        )

    def test_trusted_helper_accepts_managed_gateway_exact(self):
        assert bfl_plugin._polling_url_is_trusted(
            "https://bfl-gateway.example.com/v1/get_result?id=abc",
            "https://bfl-gateway.example.com",
        )

    def test_generate_rejects_foreign_polling_url(
        self, provider, patch_httpx, monkeypatch,
    ):
        # Submit response claims polling lives at attacker.example.
        # Plugin must refuse to follow it (and never issue a GET).
        client = patch_httpx(
            post_responses=[
                _http_response(json_body={
                    "id": "evil",
                    "polling_url": "https://attacker.example/steal?token=1",
                }),
            ],
            get_responses=[],
        )
        monkeypatch.setattr(bfl_plugin.time, "sleep", lambda _: None)

        result = provider.generate("a cat", aspect_ratio="square")
        assert result["success"] is False
        assert result["error_type"] == "invalid_response"
        assert "untrusted host" in result["error"]
        # No GET was made -- the auth header never went out.
        assert client.gets == []
