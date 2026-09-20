#!/usr/bin/env python3
"""Offline review-quality evaluation harness (stdlib only; NOT part of
`unittest discover` — this directory holds no `test_*.py` module).

Runs the action's own review loop against a merged PR **without posting
anything to GitHub**: `fetch_pr_context` (needs `gh auth token`) + either the
in-process `drive_review` loop (`anthropic` / `openai`) or the agent-runner
`run_review` (`claude-code` / `codex` / `grok` / `cursor` — the CLI must be
installed) executed in a worktree checked out at the PR head. The result is
scored against the labelled corpus (`corpus.json`): must-find recall, false
positives against `must_not_flag`, unlabelled findings, severity match,
contract compliance (summary present), suggestion-block rate, coverage,
tokens and cost.

Usage:
  python3 tests/eval/run_eval.py run --repo owner/repo --pr 46 --worktree /path/at/pr/head \
      --provider openai --api-base https://api.x.ai/v1 --model grok-4.5 --api-key-env XAI_API_KEY \
      --prompt prompts/default.md [--extension .review/extension.md] --out results/xai-46.json
  python3 tests/eval/run_eval.py score results/*.json      # table across runs

Never pass a key on the command line; `--api-key-env` names the variable.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CORPUS_PATH = Path(__file__).resolve().parent / "corpus.json"
LINE_WINDOW = 25


def load_runtime() -> Any:
    spec = importlib.util.spec_from_file_location("reviewer", ROOT / "scripts/reviewer.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reviewer"] = mod
    spec.loader.exec_module(mod)
    mod.log = lambda msg: None  # quiet
    return mod


def gh_token() -> str:
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not tok:
        tok = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False).stdout.strip()
    if not tok:
        sys.exit("no GitHub token (set GH_TOKEN or log in with gh)")
    return tok


def compose_prompt(prompt_file: Path, extension: Path | None) -> str:
    text = prompt_file.read_text(encoding="utf-8")
    if extension and extension.exists():
        text = text.rstrip("\n") + "\n\n---\n\n" + extension.read_text(encoding="utf-8")
    return text


def run(args: argparse.Namespace) -> dict[str, Any]:
    t_start = time.time()
    r = load_runtime()
    token = gh_token()
    key = os.environ.get(args.api_key_env, "")
    if not key:
        sys.exit(f"{args.api_key_env} is not set")
    os.chdir(args.worktree)
    ctx = r.fetch_pr_context(repo=args.repo, pr_number=args.pr, base_ref=args.base_ref, token=token)
    # Timing separation (PLAN Task 4 / F8): context fetch + provider build are
    # "setup"; the provider loop below is timed separately as t0..end.
    setup_seconds = time.time() - t_start
    api_base = r.validate_api_base(args.api_base or "")
    provider = r.build_provider(args.provider, api_key=key, model=args.model or "", api_base=api_base)
    system_prompt = compose_prompt(Path(args.prompt), Path(args.extension) if args.extension else None)
    t0 = time.time()
    turns = 0
    if isinstance(provider, r.AgentRunnerProvider):
        with tempfile.TemporaryDirectory() as out_dir:
            result = provider.run_review(
                pr_context=ctx, review_instructions=system_prompt,
                workspace=Path(args.worktree), output_dir=Path(out_dir),
            )
        usage = result.usage
        turns = usage.turns if usage else 0
        cost = usage.cost_usd if usage and usage.cost_usd is not None else (r.estimate_cost_usd(args.model or "", usage) if usage else None)
        tool_calls = None
    else:
        state = r.ReviewState(max_inline_comments=10)
        messages = [{"role": "user", "content": r.render_user_prompt(ctx)}]
        tools = r.tools_schema(10)

        class Counting:
            def __init__(self, inner: Any) -> None:
                self.inner = inner

            def complete(self, **kw: Any) -> Any:
                nonlocal turns
                turns += 1
                return self.inner.complete(**kw)

        r.drive_review(provider=Counting(provider), system_prompt=system_prompt, messages=messages, tools=tools, state=state, max_turns=args.max_turns)
        result = r.state_to_review_result(state)
        usage = state.usage
        cost = r.estimate_cost_usd(args.model or "", usage) if usage else None
        tool_calls = sum(1 for m in messages if m["role"] == "assistant" for b in (m["content"] if isinstance(m["content"], list) else []) if isinstance(b, dict) and b.get("type") == "tool_use")
    payload = {
        "pr": args.pr, "repo": args.repo, "provider": args.provider, "api_base": api_base, "model": args.model or "",
        "prompt": os.path.basename(args.prompt), "extension": bool(args.extension), "runtime_head": subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip(),
        "turns": turns, "tool_calls": tool_calls,
        "seconds": round(time.time() - t0, 1),
        "setup_seconds": round(setup_seconds, 1),
        "total_seconds": round(time.time() - t_start, 1),
        "usage": {"in": usage.input_tokens, "cache_read": usage.cache_read_tokens, "cache_write": usage.cache_write_tokens, "out": usage.output_tokens, "source": usage.source} if usage else None,
        "cost_usd": cost,
        "changed_files": [f.get("path") for f in ctx.changed_files],
        # Full finding evidence (PLAN Task 4): truncate far beyond the old
        # 400 chars so scoring/adjudication sees the whole finding body.
        "findings": [{"path": f.path, "line": f.line, "severity": f.severity, "body": f.body[:8000]} for f in result.findings],
        "summary": (result.summary or "")[:2000],
    }
    payload["score"] = score_run(payload)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, indent=2))
    print(fmt_row(payload))
    return payload


def _matches(finding: dict[str, Any], label: dict[str, Any]) -> bool:
    if label.get("path") and finding.get("path") != label["path"]:
        return False
    if label.get("line") is not None and finding.get("line") is not None:
        if abs(int(finding["line"]) - int(label["line"])) > label.get("window", LINE_WINDOW):
            return False
    body = (finding.get("body") or "").lower()
    kws = [k.lower() for k in label.get("keywords", [])]
    return all(k in body for k in kws) if label.get("all_keywords") else (not kws or any(k in body for k in kws))


def score_run(payload: dict[str, Any]) -> dict[str, Any]:
    corpus = json.loads(CORPUS_PATH.read_text()) if CORPUS_PATH.exists() else {}
    entry = corpus.get(str(payload["pr"])) or {}
    findings = payload["findings"]
    must, acceptable, must_not = entry.get("must_find", []), entry.get("acceptable", []), entry.get("must_not_flag", [])
    hits = [l["id"] for l in must if any(_matches(f, l) for f in findings)]
    misses = [l["id"] for l in must if l["id"] not in hits]
    fps = [l["id"] for l in must_not if any(_matches(f, l) for f in findings)]
    labelled = [f for f in findings if any(_matches(f, l) for l in must + acceptable + must_not)]
    unlabelled = len(findings) - len(labelled)
    sev_match = sum(1 for l in must if l.get("severity") and any(_matches(f, l) and f.get("severity") == l["severity"] for f in findings))
    suggestions = sum(1 for f in findings if "```suggestion" in (f.get("body") or ""))
    return {
        "must_find_total": len(must), "must_find_hits": len(hits), "hits": hits, "misses": misses,
        "false_positives": fps, "unlabelled_findings": unlabelled,
        "severity_matches": sev_match, "summary_present": bool((payload.get("summary") or "").strip()),
        "suggestion_blocks": suggestions,
        "coverage_paths": len({f.get("path") for f in findings}), "changed_files": len(payload.get("changed_files") or []),
    }


def fmt_row(p: dict[str, Any]) -> str:
    s = p["score"]; u = p.get("usage") or {}
    cost = f"${p['cost_usd']:.3f}" if p.get("cost_usd") is not None else "n/a"
    return (f"| #{p['pr']} | {p['provider']}/{p['model'] or 'default'} | {len(p['findings'])} findings | "
            f"recall {s['must_find_hits']}/{s['must_find_total']} | FP {len(s['false_positives'])} | unlabelled {s['unlabelled_findings']} | "
            f"sev-match {s['severity_matches']}/{s['must_find_total']} | summary {'yes' if s['summary_present'] else 'NO'} | sugg {s['suggestion_blocks']} | "
            f"turns {p['turns']} | in {u.get('in', 0) + u.get('cache_read', 0)} out {u.get('out', 0)} | {cost} | {p['seconds']}s |")


def score_cmd(paths: list[str]) -> None:
    print("| PR | runner/model | findings | must-find recall | FP | unlabelled | severity match | summary | suggestions | turns | tokens | cost | wall |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for path in paths:
        p = json.loads(Path(path).read_text()); p["score"] = score_run(p); print(fmt_row(p))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("run")
    rp.add_argument("--repo", required=True); rp.add_argument("--pr", type=int, required=True); rp.add_argument("--worktree", required=True)
    rp.add_argument("--base-ref", default="main"); rp.add_argument("--provider", required=True); rp.add_argument("--api-base", default="")
    rp.add_argument("--model", default=""); rp.add_argument("--api-key-env", required=True); rp.add_argument("--prompt", default=str(ROOT / "prompts/default.md"))
    rp.add_argument("--extension", default=""); rp.add_argument("--max-turns", type=int, default=30); rp.add_argument("--out", required=True)
    sp = sub.add_parser("score"); sp.add_argument("paths", nargs="+")
    args = ap.parse_args()
    if args.cmd == "run":
        run(args)
    else:
        score_cmd(args.paths)


if __name__ == "__main__":
    main()
