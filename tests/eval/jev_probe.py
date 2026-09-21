#!/usr/bin/env python3
"""Isolated Jev evaluation client (PLAN_jev_review_acceleration Task 3).

Bounded, strictly-validating transport for TypeSafe SystemOne (`jev-1.13.0`)
used ONLY by the evaluation harness. This module never ships in the Action:
it is evaluation tooling, so it may import nothing beyond the standard
library and must stay runnable offline (tests inject a fake transport).

Design contract (experiment contract F3/F4/F5):

- Bounded batch requests: one call carries the state plus ALL questions
  (parallel-in-isolation semantics); request bytes are bounded and checked
  before sending.
- Immutable model selection: the pinned model id is required on every
  response and anything else is a schema error.
- Strict answer validation: exact key match (missing and unexpected answer
  ids rejected), duplicate JSON keys rejected at parse time, NaN/Infinity
  rejected, booleans rejected as probabilities, ranges enforced, probability
  distributions sum-checked, confidence checked when present.
- Explicit insufficient evidence: per-question confidence floors map weak
  answers to `insufficient_evidence` instead of forcing a decision.
- Monotonic overall deadline covering connect + read, enforced beyond the
  socket inactivity timeout.
- Response-size abuse rejected by construction (bounded read).
- Redirects refused.
- Credentials: the key is read once from a named environment variable, used
  only in the Authorization header, and scrubbed from every error message.
  The client never reads files (it will never "source" a .env) and never
  places the key in a URL, log, or payload.

No live quality claim may be derived from this module; fixture-driven tests
are visibly synthetic.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_KEY_ENV = "TYPESAFE_API_KEY"

MAX_REQUEST_BYTES = 200_000
MAX_RESPONSE_BYTES = 2_000_000
ANSWER_TYPES = {"noul", "choice", "score"}


class JevError(Exception):
    """Taxonomied failure; `kind` is machine-readable, message is sanitized."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirects are refused: credentials must never follow a 30x."""

    def redirect_request(self, *a: Any, **k: Any) -> None:
        raise JevError("redirect_refused", "redirect refused")


@dataclass(frozen=True)
class JevConfig:
    endpoint: str = DEFAULT_ENDPOINT
    model: str = DEFAULT_MODEL
    key_env: str = DEFAULT_KEY_ENV
    deadline_seconds: float = 5.0
    max_request_bytes: int = MAX_REQUEST_BYTES
    max_response_bytes: int = MAX_RESPONSE_BYTES


@dataclass
class JevAnswer:
    """One validated answer; `insufficient_evidence` is client-derived (F3)."""

    id: str
    type: str
    value: Any
    confidence: float | None
    probabilities: dict[str, float] | None
    insufficient_evidence: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class JevResult:
    model: str
    answers: dict[str, JevAnswer]
    usage: dict[str, int | None]
    elapsed_seconds: float


class _NonFiniteNumber(ValueError):
    """Raised by the JSON parser for NaN/Infinity literals (validation kind)."""


def _reject_constant(token: str) -> float:
    raise _NonFiniteNumber(f"non-finite JSON number: {token}")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def _finite_prob(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JevError("validation", f"{where}: not a number")
    number = float(value)
    if not math.isfinite(number):
        raise JevError("validation", f"{where}: non-finite probability")
    return number


def build_question(
    kind: str,
    instructions: str,
    criteria: dict[str, str] | list[str] | None = None,
    confidence_floor: float | None = None,
) -> dict[str, Any]:
    """Typed question builder; score takes an ORDERED list of level strings."""
    q: dict[str, Any] = {"type": kind, "instructions": instructions}
    if criteria is not None:
        q["criteria"] = criteria
    if confidence_floor is not None:
        q["_confidence_floor"] = confidence_floor  # stripped before sending
    return q


def _check_question(name: str, q: dict[str, Any]) -> None:
    if q.get("type") not in {"noul", "choice", "score"}:
        raise JevError("config", f"question {name!r}: bad type")
    if not q.get("instructions"):
        raise JevError("config", f"question {name!r}: instructions required")
    if q["type"] in {"choice", "score"} and not q.get("criteria"):
        raise JevError("config", f"question {name!r}: criteria required")
    if isinstance(q.get("criteria"), list) and len(q["criteria"]) < 2:
        raise JevError("config", f"question {name!r}: score needs >=2 levels")


class JevClient:
    """SystemOne transport. `post` is injectable for offline tests."""

    def __init__(
        self,
        config: JevConfig | None = None,
        post: Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]] | None = None,
        env: dict[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or JevConfig()
        self._post = post
        self._env = env if env is not None else dict(os.environ)
        self._clock = clock

    # -- transport ---------------------------------------------------------

    def _http_post(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
        deadline = self._clock() + timeout
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        opener = urllib.request.build_opener(_NoRedirectHandler)
        try:
            with opener.open(request, timeout=timeout) as resp:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise JevError("timeout", "deadline exceeded before read")
                data = resp.read(self.config.max_response_bytes + 1)
                if len(data) > self.config.max_response_bytes:
                    raise JevError("response_too_large", "response exceeds cap")
                return int(resp.status or 200), data
        except JevError:
            raise
        except urllib.error.HTTPError as exc:
            detail = exc.read(2_000).decode("utf-8", "replace")
            kind = {401: "auth", 403: "auth", 422: "schema", 429: "rate_limited", 529: "overloaded"}.get(
                exc.code, "http"
            )
            raise JevError(kind, f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise JevError("timeout", "request timed out") from exc
            raise JevError("connection", f"connection failed: {type(reason).__name__}") from exc
        except TimeoutError as exc:
            raise JevError("timeout", "request timed out") from exc

    # -- request/response ----------------------------------------------------

    def _key(self) -> str:
        key = self._env.get(self.config.key_env, "")
        if not key:
            raise JevError("config", f"credential env {self.config.key_env!r} is not set")
        return key

    @staticmethod
    def _scrub(message: str, secret: str) -> str:
        return message.replace(secret, "<redacted>") if secret else message

    def ask(self, state: Any, questions: dict[str, dict[str, Any]]) -> JevResult:
        for name, q in questions.items():
            _check_question(name, q)
        floors = {
            name: q["_confidence_floor"]
            for name, q in questions.items()
            if q.get("_confidence_floor") is not None
        }
        wire_questions = {
            name: {k: v for k, v in q.items() if not k.startswith("_")}
            for name, q in questions.items()
        }
        payload = json.dumps({"state": state, "model": self.config.model, "questions": wire_questions}).encode("utf-8")
        if len(payload) > self.config.max_request_bytes:
            raise JevError("request_too_large", f"request body {len(payload)} exceeds cap")

        key = self._key()
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        started = self._clock()
        post = self._post or self._http_post
        try:
            status, raw = post(
                self.config.endpoint, headers, payload, self.config.deadline_seconds
            )
        except JevError as exc:
            # `from None` is deliberate: the original's message may embed the
            # credential, and __cause__ would leak it through any traceback.
            raise JevError(exc.kind, self._scrub(str(exc), key)) from None
        elapsed = self._clock() - started
        if elapsed > self.config.deadline_seconds + 0.05:
            raise JevError("timeout", f"deadline exceeded ({elapsed:.2f}s)")

        if status != 200:
            kind = {401: "auth", 403: "auth", 422: "schema", 429: "rate_limited", 529: "overloaded"}.get(
                status, "http"
            )
            raise JevError(kind, f"HTTP {status}")
        # Response-size abuse is rejected transport-independently: an injected
        # transport (tests) must not bypass the cap enforced in _http_post.
        if len(raw) > self.config.max_response_bytes:
            raise JevError("response_too_large", "response exceeds cap")
        try:
            body = json.loads(
                raw.decode("utf-8"),
                parse_constant=_reject_constant,
                object_pairs_hook=_no_duplicate_keys,
            )
        except _NonFiniteNumber as exc:
            raise JevError("validation", str(exc)) from exc
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise JevError("schema", f"invalid response JSON: {exc}") from exc

        if not isinstance(body, dict):
            raise JevError("schema", "response must be an object")
        if body.get("model") != self.config.model:
            raise JevError("schema", f"model mismatch: {body.get('model')!r}")
        answers_in = body.get("answers")
        if not isinstance(answers_in, dict):
            raise JevError("schema", "answers missing")
        unexpected = set(answers_in) - set(questions)
        if unexpected:
            raise JevError("schema", f"unexpected answer ids: {sorted(unexpected)}")
        missing = set(questions) - set(answers_in)
        if missing:
            raise JevError("schema", f"missing answer ids: {sorted(missing)}")

        answers: dict[str, JevAnswer] = {}
        for name, q in questions.items():
            raw_answer = answers_in[name]
            if not isinstance(raw_answer, dict) or raw_answer.get("type") != q["type"]:
                raise JevError("schema", f"answer {name!r}: type mismatch")
            kind = q["type"]
            confidence = raw_answer.get("confidence")
            confidence = None if confidence is None else _finite_prob(confidence, f"answer {name} confidence")
            if confidence is not None and not 0.0 <= confidence <= 1.0:
                raise JevError("validation", f"answer {name}: confidence out of range")
            probabilities = raw_answer.get("probabilities")
            if probabilities is not None:
                if not isinstance(probabilities, dict) or not probabilities:
                    raise JevError("schema", f"answer {name}: bad probabilities")
                probabilities = {
                    k: _finite_prob(v, f"answer {name} p[{k}]") for k, v in probabilities.items()
                }
                total = sum(probabilities.values())
                if abs(total - 1.0) > 0.01:
                    raise JevError("validation", f"answer {name}: probabilities sum {total:.4f}")
            if kind == "noul":
                value = _finite_prob(raw_answer.get("noul"), f"answer {name} noul")
                if not 0.0 <= value <= 1.0:
                    raise JevError("validation", f"answer {name}: noul out of range")
            elif kind == "choice":
                value = raw_answer.get("choice")
                allowed = set(q["criteria"]) if isinstance(q["criteria"], dict) else set(q["criteria"])
                if value not in allowed:
                    raise JevError("validation", f"answer {name}: choice not in criteria")
                if probabilities is not None and set(probabilities) - allowed:
                    raise JevError("validation", f"answer {name}: probability keys outside criteria")
            else:  # score
                value = raw_answer.get("score")
                levels = len(q["criteria"])
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise JevError("validation", f"answer {name}: score not a finite number")
                if not 0 <= float(value) <= levels - 1:
                    raise JevError("validation", f"answer {name}: score out of rubric range")
                value = float(value)
            floor = floors.get(name)
            weak = floor is not None and confidence is not None and confidence < floor
            answers[name] = JevAnswer(
                id=name, type=kind, value=value, confidence=confidence,
                probabilities=probabilities, insufficient_evidence=bool(weak),
                raw=dict(raw_answer),
            )

        usage_in = body.get("usage") or {}
        usage = {
            "input_tokens": usage_in.get("input_tokens") if isinstance(usage_in, dict) else None,
            "output_tokens": usage_in.get("output_tokens") if isinstance(usage_in, dict) else None,
        }
        return JevResult(model=self.config.model, answers=answers, usage=usage, elapsed_seconds=elapsed)


__all__ = [
    "DEFAULT_ENDPOINT", "DEFAULT_MODEL", "DEFAULT_KEY_ENV", "JevAnswer", "JevClient",
    "JevConfig", "JevError", "JevResult", "build_question",
]
