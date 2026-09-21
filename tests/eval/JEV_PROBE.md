# JEV_PROBE — the isolated evaluation client (`tests/eval/jev_probe.py`)

Task 3 deliverable. `jev_probe.py` is the ONLY component in this plan that
speaks to TypeSafe during evaluation phases; it is deliberately outside
`scripts/reviewer.py` and ships nowhere. Its guarantees (experiment contract
F3/F4/F5):

- **Bounded batches** — one request carries the state plus all questions;
  request bytes capped (`MAX_REQUEST_BYTES`), checked before sending.
- **Strict validation** — exact answer-key match (missing/unexpected ids
  rejected), duplicate JSON keys rejected at parse time, NaN/Infinity
  rejected, booleans rejected as probabilities, ranges enforced,
  distributions sum-checked, confidence checked, `score` bounded by the
  rubric, echoed `model` must equal the pin.
- **Insufficient evidence** — per-question `confidence_floor` (via
  `build_question`) marks weak answers `insufficient_evidence` instead of
  forcing a decision.
- **Deadline** — monotonic overall deadline covers connect + read; a slow
  transport fails as `timeout` even when the socket stays quiet.
- **Failure taxonomy** — `auth` (401/403), `schema` (422/malformed),
  `rate_limited` (429), `overloaded` (529), `timeout`, `connection`,
  `redirect_refused`, `response_too_large`, `request_too_large`,
  `validation`, `config`.
- **Credential hygiene** — the key comes from one named env var
  (`TYPESAFE_API_KEY`), goes only into the Authorization header, and is
  scrubbed from every error. The client never reads files (it cannot be fed
  a `.env`), never builds URLs with the key, and never logs payloads.
- **Redirects refused** — a 30x never gets followed with credentials.

## Commands (the registered gate)

```bash
# EVAL gate — offline unit tests (no network; fixture-driven)
python3 -m unittest discover -s tests -p 'test_jev_eval*.py' -v

# transport compile check
python3 -m py_compile tests/eval/jev_probe.py

# corpus-side hygiene: fixtures are visibly synthetic and credential-free
# (asserted by FixtureHygieneTests inside the EVAL gate)
ls tests/eval/fixtures/
```

## Live use (Task 5+, only with authorized budget)

Run from a shell where `TYPESAFE_API_KEY` is **already exported** (the
devcontainer exports it; never inline `TYPESAFE_API_KEY=...` on the command
line — inline env assignments land in shell history and process argv):

```bash
python3 - <<'PY'
import sys; sys.path.insert(0, "tests/eval")
from jev_probe import JevClient, JevConfig, build_question
c = JevClient(JevConfig())   # model pinned: jev-1.13.0; key read from env
r = c.ask({"state": "..."}, {"is_bug": build_question("noul", "...", confidence_floor=0.7)})
print(r.answers["is_bug"].value, r.usage, r.elapsed_seconds)
PY
```

## Fixtures (`tests/eval/fixtures/`)

Hand-written, visibly marked `synthetic-fixture`: one OK response, four error
shapes (401/422/429/529), and the captured request shape (Authorization
header deliberately excluded). `FixtureHygieneTests` fails if any fixture
loses its synthetic marker or contains credential-like material.

## Explicit non-claims

Mocked tests establish transport/validation behavior, NOT model quality.
No finding, probability, or benchmark number may come from this module
before Task 5's authorized pilot; every such number must name its run
manifest and revision pair (BENCH gate, Task 4).
