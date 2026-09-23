# `tests/eval/records/` — stored run records and verdicts

The eval gate (RFC-01) reasons over **run records** (`run-record/3.0`, the
file the runtime writes to `.aiprr/run-record.json` on every run) and emits
**verdicts** (`verdict/1.0`). This tree is where the repository keeps the
ones that matter for releases.

| Path | Contents | Retention (RFC-08 D-01) |
|---|---|---|
| `verdicts/` | one `verdict/1.0` JSON per evaluated candidate (`<campaign_id>.json`) — **committed**; `auto-release.yml` reads the newest one whose runtime/prompt hashes match the release candidate | permanent |
| `campaigns/<campaign_id>/` | the raw run records of a campaign plus its `campaign.json` manifest (`cells`, `repetitions`, lanes, budget) — produced as workflow artifacts by `eval-campaign.yml` (90 days) and squashed here monthly | artifacts 90 d; squash committed |
| `adjudications/` | per-finding blinded adjudication records used for precision (RFC-01 § Metrics) | permanent |

Rules:

- Records are **append-only**. A bad campaign is marked by a companion
  `<campaign_id>.failed.md` note, never deleted or edited.
- No record carries a hostname or a credential — the schema has no such
  field (`endpoint_kind` only) and the runtime scrubs registered secrets
  before writing.
- Validate the whole tree offline: `python3 tests/eval/records_validate.py --records tests/eval/records`
  (schema validity, unique `run_id`, `usage_known=false ⇒ null usage/cost`,
  manifest completeness, verdict validity). CI runs it in `eval-gate-offline`.
- Summaries and verdicts: `python3 tests/eval/determinism.py summarize --records <dir>` and
  `python3 tests/eval/determinism.py verdict --baseline <dir> --candidate <dir> --out verdicts/<id>.json`.
