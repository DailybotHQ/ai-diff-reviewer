"""Bedrock wire adapter for `provider: anthropic` (InvokeModel via SigV4).

Request-shape tests: URL path carries the model id, the body carries the
in-body `anthropic_version`, auth is SigV4 (no `x-api-key` /
`anthropic-version`), and credentials resolve per the Task 2 contract.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

_ROOT: Path = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "reviewer", _ROOT / "scripts" / "reviewer.py"
)
assert _SPEC is not None and _SPEC.loader is not None
reviewer = importlib.util.module_from_spec(_SPEC)
sys.modules["reviewer"] = reviewer
_SPEC.loader.exec_module(reviewer)

_BEDROCK_BASE = "https://bedrock-runtime.us-east-1.amazonaws.com"
_CANNED_RESPONSE = {
    "stop_reason": "end_turn",
    "content": [{"type": "text", "text": "ok"}],
    "usage": {"input_tokens": 10, "output_tokens": 1},
}


def _bedrock_provider(api_key: str = "packed-ignored-when-env-set") -> Any:
    prof = reviewer.resolve_endpoint_profile(_BEDROCK_BASE, "anthropic")
    return reviewer.AnthropicProvider(
        api_key=api_key, model="anthropic.claude-sonnet-5", profile=prof
    )


def _capture(prov: Any, *, env: dict[str, str] | None = None) -> Any:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float = 0) -> Any:
        captured["request"] = request
        return mock.mock_open(
            read_data=json.dumps(_CANNED_RESPONSE).encode()
        ).return_value

    env_patch = mock.patch.dict(os.environ, env or {}, clear=True)
    with env_patch, mock.patch.object(
        reviewer.urllib.request.OpenerDirector, "open", side_effect=fake_urlopen
    ):
        prov.complete(system_prompt="S", messages=[{"role": "user", "content": "u"}], tools=[])
    return captured["request"]



class BedrockWireTests(unittest.TestCase):
    def test_request_shape(self) -> None:
        synthetic_secret = "aws-synthetic-secret-0123456789abcdef"
        req = _capture(
            _bedrock_provider(),
            env={"AWS_ACCESS_KEY_ID": "AKID", "AWS_SECRET_ACCESS_KEY": synthetic_secret},
        )
        self.assertEqual(
            req.full_url,
            _BEDROCK_BASE + "/model/anthropic.claude-sonnet-5/invoke",
        )
        body = json.loads(req.data)
        self.assertEqual(body["anthropic_version"], "bedrock-2023-05-31")
        # InvokeModel takes the model id from the URL path — the body must
        # NOT carry a top-level `model` field (AWS rejects it).
        self.assertNotIn("model", body)
        self.assertIn("max_tokens", body)
        # Deterministic sampling applies on Bedrock too.
        self.assertEqual(body["temperature"], 0.0)
        self.assertFalse(
            any("cache_control" in json.dumps(block) for block in body["system"])
        )
        headers = {k.lower(): v for k, v in req.header_items()}
        self.assertNotIn("x-api-key", headers)
        self.assertNotIn("anthropic-version", headers)
        self.assertTrue(headers["authorization"].startswith("AWS4-HMAC-SHA256 "))
        self.assertIn("x-amz-date", headers)
        self.assertNotIn("x-amz-security-token", headers)
        # The resolved AWS secret is registered for the outbound scrub gate.
        self.assertIn(synthetic_secret, reviewer._SECRET_VALUES)

    def test_session_token_env_is_signed_and_sent(self) -> None:
        req = _capture(
            _bedrock_provider(),
            env={
                "AWS_ACCESS_KEY_ID": "AKID",
                "AWS_SECRET_ACCESS_KEY": "sk",
                "AWS_SESSION_TOKEN": "tok-123",
            },
        )
        headers = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(headers["x-amz-security-token"], "tok-123")
        self.assertIn(
            "x-amz-security-token", headers["authorization"].lower()
        )

    def test_missing_credentials_error_names_sources_not_values(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                _capture(_bedrock_provider(api_key=""))
        msg = str(ctx.exception)
        self.assertIn("AWS_ACCESS_KEY_ID", msg)
        self.assertIn("KEY:SECRET[:SESSION_TOKEN]", msg)

    def test_non_bedrock_region_host_is_rejected(self) -> None:
        prof = reviewer.EndpointProfile(
            kind=reviewer.ENDPOINT_KIND_BEDROCK,
            base_url="https://example.com",
            host="example.com",
            is_default=False,
            supports_anthropic_cache_control=False,
            anthropic_auth_style=reviewer.ANTHROPIC_AUTH_STYLE_BOTH,
            openai_auth_style=reviewer.OPENAI_AUTH_STYLE_BEARER,
            codex_wire_api=reviewer.CODEX_WIRE_API_RESPONSES,
            codex_extra_toml="",
        )
        prov = reviewer.AnthropicProvider(
            api_key="k", model="m", profile=prof
        )
        with self.assertRaises(ValueError) as ctx:
            prov._bedrock_request_parts(b"{}")
        self.assertIn("bedrock-runtime.{region}.amazonaws.com", str(ctx.exception))


class BedrockRunnerGateTests(unittest.TestCase):
    """Only the `anthropic` runner implements the SigV4 InvokeModel wire —
    every other runner fails fast on a bedrock api-base."""

    def test_non_anthropic_runners_are_rejected(self) -> None:
        for pid in ("openai", "claude-code", "cursor", "codex", "grok"):
            with self.subTest(provider=pid):
                with self.assertRaises(ValueError) as ctx:
                    reviewer.build_provider(
                        pid,
                        api_key="k",
                        model="anthropic.claude-sonnet-5",
                        api_base=_BEDROCK_BASE,
                    )
                self.assertIn("provider: anthropic", str(ctx.exception))

    def test_anthropic_runner_constructs(self) -> None:
        prov = reviewer.build_provider(
            "anthropic",
            api_key="k",
            model="anthropic.claude-sonnet-5",
            api_base=_BEDROCK_BASE,
        )
        self.assertEqual(prov.profile.kind, reviewer.ENDPOINT_KIND_BEDROCK)


class BedrockOidcStartupTests(unittest.TestCase):
    """The OIDC lane: `api-key` empty + AWS env credentials + a bedrock
    api-base must pass the startup env checks (round-7 regression: the
    reorder crashed every leg with NameError before this test existed)."""

    def _env(self, *, with_aws: bool) -> dict[str, str]:
        env: dict[str, str] = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "AIPRR_PROVIDER": "anthropic",
            "AIPRR_API_BASE": _BEDROCK_BASE,
            "AIPRR_GH_TOKEN": "gh-token",
            "AIPRR_REPO": "fake/repo",
            "AIPRR_PR_NUMBER": "1",
            "AIPRR_HEAD_SHA": "a" * 40,
            "AIPRR_MODEL": "anthropic.claude-sonnet-5",
        }
        if with_aws:
            env["AWS_ACCESS_KEY_ID"] = "AKIDEXAMPLE"
            env["AWS_SECRET_ACCESS_KEY"] = "aws-synthetic-secret-0123456789abcdef"
        return env

    def test_bedrock_oidc_lane_gets_past_startup_checks(self) -> None:
        # The observable startup marker: `Backend: kind=bedrock` is logged by
        # log_backend_selection only AFTER the env checks, backend selection
        # and model resolution all pass (the round-7 regression was a
        # NameError in exactly that window, before any marker printed).
        env = self._env(with_aws=True)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
             contextlib.redirect_stdout(buf):
            reviewer.main()  # the fake-token 401 later in the loop is fine
        self.assertIn("Backend: kind=bedrock", buf.getvalue())

    def test_bedrock_without_any_credentials_aborts_at_startup(self) -> None:
        env = self._env(with_aws=False)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
             contextlib.redirect_stdout(buf), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reviewer.main(), 1)
        # the bedrock probe surfaces the actionable credential guidance,
        # not the generic missing-env abort
        self.assertIn("CONFIGURATION ERROR", buf.getvalue())
        self.assertIn("KEY:SECRET[:SESSION_TOKEN]", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
