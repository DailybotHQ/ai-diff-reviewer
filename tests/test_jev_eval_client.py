#!/usr/bin/env python3
"""Offline tests for the isolated Jev evaluation client (Task 3).

Every test injects a fake transport; nothing here touches the network.
Fixtures under tests/eval/fixtures/ are visibly synthetic (marker required)
and are themselves checked for credential leakage.

Run: python3 -m unittest discover -s tests -p 'test_jev_eval*.py' -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR / "eval"))

import jev_probe  # noqa: E402
from jev_probe import JevClient, JevConfig, JevError, build_question  # noqa: E402

KEY = "test-key-not-a-real-credential"
ENV = {jev_probe.DEFAULT_KEY_ENV: KEY}
QUESTIONS = {
    "urgent": build_question("noul", "Is this time-sensitive?", confidence_floor=0.7),
    "department": build_question(
        "choice", "Which team?", {"billing": "charges", "technical": "bugs"}
    ),
    "severity": build_question("score", "Rate it.", ["none", "warning", "critical"]),
}


def ok_response(answers: dict, model: str = jev_probe.DEFAULT_MODEL, usage: dict | None = None) -> bytes:
    payload = {"model": model, "answers": answers}
    if usage is not None:
        payload["usage"] = usage
    return json.dumps(payload).encode()


def client(post, config: JevConfig | None = None, env: dict | None = None) -> JevClient:
    # NOTE: `env if env is not None` — an explicitly empty env must NOT fall
    # back to ENV (that distinction is exactly what the missing-key test pins).
    return JevClient(config or JevConfig(), post=post, env=ENV if env is None else env, clock=lambda: 0.0)


class TransportTests(unittest.TestCase):
    def capture(self, status: int = 200, body: bytes = b"{}"):
        seen: dict[str, object] = {}

        def post(url: str, headers: dict[str, str], payload: bytes, timeout: float) -> tuple[int, bytes]:
            seen["url"], seen["headers"], seen["payload"], seen["timeout"] = url, headers, payload, timeout
            return status, body

        return client(post), seen

    def test_happy_path_all_three_types(self) -> None:
        c, seen = self.capture(body=ok_response({
            "urgent": {"type": "noul", "noul": 0.95},
            "department": {"type": "choice", "choice": "billing", "confidence": 1.0,
                           "probabilities": {"billing": 1.0, "technical": 0.0}},
            "severity": {"type": "score", "score": 1.9, "confidence": 0.9,
                         "probabilities": {"0": 0.02, "1": 0.02, "2": 0.96}},
        }, usage={"input_tokens": 415, "output_tokens": 57}))
        result = c.ask({"ticket": "synthetic"}, QUESTIONS)
        self.assertEqual(result.answers["urgent"].value, 0.95)
        self.assertEqual(result.answers["department"].value, "billing")
        self.assertAlmostEqual(result.answers["severity"].value, 1.9)
        self.assertEqual(result.usage, {"input_tokens": 415, "output_tokens": 57})
        self.assertFalse(result.answers["urgent"].insufficient_evidence)
        sent = json.loads(seen["payload"])
        self.assertEqual(sent["model"], jev_probe.DEFAULT_MODEL)  # immutable pin
        self.assertNotIn(KEY, seen["payload"].decode())  # key never in payload
        self.assertTrue(seen["headers"]["Authorization"].startswith("Bearer "))
        for name, q in sent["questions"].items():  # internal fields stripped
            self.assertFalse(any(k.startswith("_") for k in q), f"{name}: internal field leaked")

    def test_confidence_floor_flags_insufficient_evidence(self) -> None:
        c, _ = self.capture(body=ok_response({
            "urgent": {"type": "noul", "noul": 0.55, "confidence": 0.4},
            "department": {"type": "choice", "choice": "billing"},
            "severity": {"type": "score", "score": 0.0},
        }))
        result = c.ask({}, QUESTIONS)
        self.assertTrue(result.answers["urgent"].insufficient_evidence)
        self.assertFalse(result.answers["department"].insufficient_evidence)

    def test_usage_missing_is_unknown_not_zero(self) -> None:
        c, _ = self.capture(body=ok_response({
            "urgent": {"type": "noul", "noul": 0.5},
            "department": {"type": "choice", "choice": "billing"},
            "severity": {"type": "score", "score": 0.0},
        }))
        result = c.ask({}, QUESTIONS)
        self.assertIsNone(result.usage["input_tokens"])

    def test_missing_key_env_is_config_error(self) -> None:
        with self.assertRaises(JevError) as cm:
            client(lambda *a: (200, b"{}"), env={}).ask({}, QUESTIONS)
        self.assertEqual(cm.exception.kind, "config")


class SchemaRejectionTests(unittest.TestCase):
    def rejects(self, body: bytes, kind: str | None = None) -> JevError:
        c = client(lambda *a: (200, body))
        try:
            c.ask({}, QUESTIONS)
        except JevError as exc:
            if kind is not None:
                self.assertEqual(exc.kind, kind)
            return exc
        self.fail("client accepted an invalid response")

    def test_missing_answer_rejected(self) -> None:
        self.rejects(ok_response({"urgent": {"type": "noul", "noul": 0.5}}), "schema")

    def test_unexpected_answer_rejected(self) -> None:
        extra = {"urgent": {"type": "noul", "noul": 0.5},
                 "department": {"type": "choice", "choice": "billing"},
                 "severity": {"type": "score", "score": 0.0}, "extra": {"type": "noul", "noul": 0.1}}
        self.rejects(ok_response(extra), "schema")

    def test_model_mismatch_rejected(self) -> None:
        self.rejects(ok_response({}, model="jev-something-else"), "schema")

    def test_duplicate_json_keys_rejected(self) -> None:
        body = b'{"model": "jev-1.13.0", "answers": {}, "answers": {}}'
        self.rejects(body, "schema")

    def test_nan_rejected(self) -> None:
        body = json.dumps({"model": jev_probe.DEFAULT_MODEL,
                           "answers": {"urgent": {"type": "noul", "noul": float("nan")},
                                       "department": {"type": "choice", "choice": "billing"},
                                       "severity": {"type": "score", "score": 0.0}}}).encode()
        self.rejects(body, "validation")

    def test_boolean_probability_rejected(self) -> None:
        body = json.dumps({"model": jev_probe.DEFAULT_MODEL,
                           "answers": {"urgent": {"type": "noul", "noul": True},
                                       "department": {"type": "choice", "choice": "billing"},
                                       "severity": {"type": "score", "score": 0.0}}}).encode()
        self.rejects(body, "validation")

    def test_noul_out_of_range_rejected(self) -> None:
        self.rejects(ok_response({"urgent": {"type": "noul", "noul": 1.5},
                                  "department": {"type": "choice", "choice": "billing"},
                                  "severity": {"type": "score", "score": 0.0}}), "validation")

    def test_choice_outside_criteria_rejected(self) -> None:
        self.rejects(ok_response({"urgent": {"type": "noul", "noul": 0.5},
                                  "department": {"type": "choice", "choice": "sales"},
                                  "severity": {"type": "score", "score": 0.0}}), "validation")

    def test_probabilities_bad_sum_rejected(self) -> None:
        self.rejects(ok_response({"urgent": {"type": "noul", "noul": 0.5},
                                  "department": {"type": "choice", "choice": "billing",
                                                 "probabilities": {"billing": 0.5, "technical": 0.2}},
                                  "severity": {"type": "score", "score": 0.0}}), "validation")

    def test_score_out_of_rubric_rejected(self) -> None:
        self.rejects(ok_response({"urgent": {"type": "noul", "noul": 0.5},
                                  "department": {"type": "choice", "choice": "billing"},
                                  "severity": {"type": "score", "score": 9.0}}), "validation")

    def test_answer_type_mismatch_rejected(self) -> None:
        self.rejects(ok_response({"urgent": {"type": "score", "score": 0.5},
                                  "department": {"type": "choice", "choice": "billing"},
                                  "severity": {"type": "score", "score": 0.0}}), "schema")

    def test_malformed_json_rejected(self) -> None:
        self.rejects(b"<html>gateway error page</html>", "schema")

    def test_oversized_response_rejected(self) -> None:
        c = client(lambda *a: (200, b"x" * (jev_probe.MAX_RESPONSE_BYTES + 10)))
        with self.assertRaises(JevError) as cm:
            c.ask({}, QUESTIONS)
        self.assertEqual(cm.exception.kind, "response_too_large")

    def test_request_size_bounded_before_send(self) -> None:
        sent = {"called": False}

        def post(*a: object) -> tuple[int, bytes]:
            sent["called"] = True
            return 200, b"{}"

        c = client(post, config=JevConfig(max_request_bytes=1_000))
        with self.assertRaises(JevError) as cm:
            c.ask({"blob": "x" * 5_000}, QUESTIONS)
        self.assertEqual(cm.exception.kind, "request_too_large")
        self.assertFalse(sent["called"])


class TransportFailureTests(unittest.TestCase):
    def status_map(self, status: int, kind: str) -> None:
        def post(*a: object) -> tuple[int, bytes]:
            return status, b'{"error": "synthetic"}'

        with self.assertRaises(JevError) as cm:
            client(post).ask({}, QUESTIONS)
        self.assertEqual(cm.exception.kind, kind)

    def test_401_auth(self) -> None:
        self.status_map(401, "auth")

    def test_403_auth(self) -> None:
        self.status_map(403, "auth")

    def test_422_schema(self) -> None:
        self.status_map(422, "schema")

    def test_429_rate_limited(self) -> None:
        self.status_map(429, "rate_limited")

    def test_529_overloaded(self) -> None:
        self.status_map(529, "overloaded")

    def test_transport_timeout_kind(self) -> None:
        def post(*a: object) -> tuple[int, bytes]:
            raise JevError("timeout", "request timed out")

        with self.assertRaises(JevError) as cm:
            client(post).ask({}, QUESTIONS)
        self.assertEqual(cm.exception.kind, "timeout")

    def test_connection_error_kind(self) -> None:
        def post(*a: object) -> tuple[int, bytes]:
            raise JevError("connection", "connection failed")

        with self.assertRaises(JevError) as cm:
            client(post).ask({}, QUESTIONS)
        self.assertEqual(cm.exception.kind, "connection")

    def test_redirect_refusal_wired(self) -> None:
        # The module-level handler is what _http_post installs; a redirect
        # attempt must raise the dedicated refusal error, never follow.
        with self.assertRaises(JevError) as cm:
            jev_probe._NoRedirectHandler().redirect_request(
                None, None, None, None
            )
        self.assertEqual(cm.exception.kind, "redirect_refused")

    def test_deadline_enforced_over_transport(self) -> None:
        ticks = iter([0.0, 99.0])

        def post(*a: object) -> tuple[int, bytes]:
            return 200, ok_response({"urgent": {"type": "noul", "noul": 0.5},
                                     "department": {"type": "choice", "choice": "billing"},
                                     "severity": {"type": "score", "score": 0.0}})

        c = JevClient(JevConfig(), post=post, env=ENV, clock=lambda: next(ticks))
        with self.assertRaises(JevError) as cm:
            c.ask({}, QUESTIONS)
        self.assertEqual(cm.exception.kind, "timeout")

    def test_secret_scrubbed_from_error(self) -> None:
        def post(*a: object) -> tuple[int, bytes]:
            raise JevError("http", f"upstream echoed {KEY}")

        with self.assertRaises(JevError) as cm:
            client(post).ask({}, QUESTIONS)
        self.assertNotIn(KEY, str(cm.exception))


class FixtureHygieneTests(unittest.TestCase):
    """The acceptance criterion: inspect fixtures for credential/context leaks."""

    FIXTURES = TESTS_DIR / "eval" / "fixtures"

    def test_fixtures_exist_and_are_marked_synthetic(self) -> None:
        files = sorted(self.FIXTURES.glob("*.json"))
        self.assertGreaterEqual(len(files), 4)
        for path in files:
            text = path.read_text()
            self.assertIn("synthetic-fixture", text, f"{path.name}: missing synthetic marker")

    def test_fixtures_carry_no_credentials(self) -> None:
        markers = ("sk-ant-", "AKIA", "ghp_", "xoxb-", "Bearer ")
        for path in self.FIXTURES.glob("*.json"):
            text = path.read_text()
            for marker in markers:
                self.assertNotIn(marker, text, f"{path.name}: credential-like material")

    def test_captured_request_fixture_has_no_authorization_content(self) -> None:
        captured = json.loads((self.FIXTURES / "captured_request.json").read_text())
        self.assertNotIn("Authorization", captured["headers"])
        self.assertNotIn(KEY, captured["body"])


if __name__ == "__main__":
    unittest.main()
