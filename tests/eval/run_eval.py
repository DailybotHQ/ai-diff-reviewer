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
  python3 tests/eval/run_eval.py run --tree tests/eval/cases/C001.json \
      --provider openai --api-base https://api.x.ai/v1 --model grok-4.5 --api-key-env XAI_API_KEY \
      --out results/xai-C001.json                          # fixture tree: no GitHub needed
  python3 tests/eval/run_eval.py score results/*.json      # table across runs

`--tree` (v3, RFC-01 / D-05) materialises a corpus case's `fixture.base` and
`fixture.head` trees as two commits in a temporary git repository, builds the
review context locally (`build_pr_context_from_local`), runs the same review
loop, scores against the case's own labels, and writes a `run-record/3.0`
next to the result (`<out>.run-record.json`) with `repo_kind = fixture_tree`.

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


CASES_DIR = Path(__file__).resolve().parent / "cases"


def canonical_hash(payload: Any) -> str:
    """Same canonical SHA-256 as `corpus_validate.canonical_hash` (kept local so
    `run_eval` stays importable without the validator)."""
    import hashlib
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


ROUND_MODES: tuple[str, ...] = ("", "2", "nochange")   # "" = single full round (base → head); "2" = incremental round 2; "nochange" = verifier-only round


def materialise_tree(case: dict[str, Any], root: Path, *, with_round1: bool = False) -> tuple[Path, str, str] | tuple[Path, str, str, str]:
    """Write `fixture.base` then `fixture.head` as two commits under `root`.
    With `with_round1` (multi-round fixtures, `fixture.iar.round1_head`), the
    round-1 tree is committed between them and its SHA is returned too.

    Returns `(repo_dir, base_sha, head_sha)`. Files present in base and
    absent from head are deleted in the head commit; paths are validated to
    stay inside the repo (fixture content is repository data, but a `..`
    path in a case file must never write outside the temp dir).
    """
    fixture = case.get("fixture") or {}
    if fixture.get("kind", "trees") != "trees":
        raise ValueError(f"{case.get('id')}: --tree needs a `trees` fixture, got {fixture.get('kind')!r}")
    repo = root / "repo"; repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "eval@example.invalid"); _git(repo, "config", "user.name", "eval")
    _git(repo, "config", "commit.gpgsign", "false")

    def write_tree(tree: dict[str, str]) -> None:
        for rel, content in tree.items():
            target = (repo / rel).resolve()
            if repo.resolve() not in target.parents:
                raise ValueError(f"fixture path escapes the tree: {rel!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    base: dict[str, str] = dict(fixture.get("base") or {})
    head: dict[str, str] = dict(fixture.get("head") or {})
    write_tree(base)
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "base", "--allow-empty")
    base_sha = _git(repo, "rev-parse", "HEAD")
    round1_sha = ""
    previous: dict[str, str] = base
    if with_round1:
        round1: dict[str, str] = dict(((fixture.get("iar") or {}).get("round1_head")) or {})
        if not round1:
            raise ValueError(f"{case.get('id')}: multi-round mode needs `fixture.iar.round1_head`")
        write_tree(round1)
        _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "round1", "--allow-empty")
        round1_sha = _git(repo, "rev-parse", "HEAD")
        previous = {**base, **round1}
    for rel in previous:
        if rel not in head:
            (repo / rel).unlink(missing_ok=True)
    write_tree(head)
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "head", "--allow-empty")
    head_sha = _git(repo, "rev-parse", "HEAD")
    if with_round1:
        return repo, base_sha, round1_sha, head_sha
    return repo, base_sha, head_sha


def build_round2_context(r: Any, *, repo: Path, base_sha: str, round1_sha: str, head_sha: str, round1_findings: list[dict[str, Any]], max_turns: int) -> tuple[Any, int]:
    """The round-2 PR context of a multi-round fixture: the PR still spans
    base → head, the incremental delta is round1 → head, and the prior
    findings are the fixture's round-1 findings fingerprinted at the round-1
    head — what `run_iar_pre_llm` would hand `main`. Returns
    `(pre_context, effective_max_turns)`; an empty delta is verifier-only."""
    cwd = os.getcwd(); os.chdir(repo)
    try:
        findings = [r.Finding(path=str(f["path"]), line=int(f["line"]), body=str(f["body"]), severity=str(f.get("severity") or "warning")) for f in round1_findings]
        contexts = r._load_code_contexts_for_findings(findings=findings, review_sha=round1_sha)
        priors = tuple(
            r.PriorFinding(thread_id=f"T{i}", comment_id=f"C{i}", comment_database_id=i + 1, path=f.path, line=f.line, severity=f.severity,
                           fingerprint=r.finding_fingerprint(finding=f, code_context=contexts.get(f.path)), body_excerpt=f.body[:160], is_outdated=False)
            for i, f in enumerate(findings)
        )
        delta = r.compute_incremental_delta(prior_head_sha=round1_sha, head_sha=head_sha, new_lines_pct=0.0, repo_root=str(repo))
    finally:
        os.chdir(cwd)
    if delta is None:
        raise RuntimeError("incremental delta not trusted on a materialised fixture")
    verifier_only = not delta.changed_files
    turns = 0 if verifier_only else r.incremental_budget(len(delta.changed_files), len(priors), max_turns)
    pre = r.IARPreLLMContext(
        prior_state=None, transition=r.GenerationTransition.NEW_COMMITS, base_sha=base_sha, head_sha=head_sha, range_hash="eval", new_lines_pct=0.0, pr_labels=[],
        pre_policy_result=r.PolicyResult(findings_to_surface=[], findings_silenced=[], effective_max_inline_comments=10, prompt_addendum=r.IAR_INCREMENTAL_PROMPT_ADDENDUM, policy_applied=r.IAR_POLICY_ITERATIVE),
        mode=r.IAR_MODE_INCREMENTAL, mode_reason=("no code changes — verifier-only round" if verifier_only else f"{len(delta.changed_files)} file(s) changed since round 1"),
        delta=delta, prior_findings=priors, effective_max_turns=turns, verifier_only=verifier_only,
    )
    return pre, turns


def case_labels_as_corpus_entry(case: dict[str, Any]) -> dict[str, Any]:
    """Map a v2 case's `labels` + `expected` to the corpus.json entry shape
    `score_run` understands (`must_find`, `must_not_flag`, `acceptable`)."""
    expected = case.get("expected") or {}
    must_ids = set(expected.get("must_flag") or [])
    must_not_ids = set(expected.get("must_not_flag") or [])
    must, must_not, acceptable = [], [], []
    for label in case.get("labels") or []:
        entry = {
            "id": label["id"], "path": label.get("path"), "line": label.get("line"),
            "keywords": label.get("keywords", []), "all_keywords": label.get("all_keywords", False),
            "window": label.get("window", LINE_WINDOW), "severity": label.get("severity"),
        }
        if label["id"] in must_not_ids:
            must_not.append(entry)
        elif label["id"] in must_ids:
            must.append(entry)
        else:
            acceptable.append(entry)
    return {"must_find": must, "acceptable": acceptable, "must_not_flag": must_not}


def apply_verifier(
    r: Any,
    result: Any,
    *,
    policy: Any,
    provider_id: str,
    api_key: str,
    api_base: str,
    review_model: str,
    inventory: Any,
    verifier_provider: Any = None,
) -> Any:
    """Verifier + severity policy exactly as `main` runs them (Task 14):
    build the in-process verifier provider for the lane (or use the one
    passed by tests), verify the selected findings, publish per the policy
    table. Returns the `VerifierReport`."""
    reason = ""
    model, alias, kind = "", "", ""
    prov = verifier_provider
    if prov is None and policy.enabled:
        prov, model, alias, kind, reason = r.build_verifier_provider(
            provider_id=provider_id, api_key=api_key, api_base=api_base, requested_model=policy.model, review_model=review_model,
        )
    elif prov is not None:
        model, alias, kind = getattr(prov, "model", "fake"), "fake", getattr(getattr(prov, "profile", None), "kind", "fake")
    report = r.run_verifier(result, policy=policy, provider=prov, model=model, alias=alias, endpoint_kind=kind,
                            unavailable_reason=reason or ("verifier off" if not policy.enabled else ""), inventory=inventory)
    r.apply_severity_policy(result, strict_unverified_criticals=policy.strict_unverified_criticals)
    return report


def verifier_payload(r: Any, result: Any, report: Any) -> dict[str, Any]:
    """The verification facets of a result payload (both eval paths)."""
    return {
        "verification": [{"path": f.path, "line": f.line, "severity_claimed": f.severity_claimed, "status": f.verification.status,
                          "reason": f.verification.reason[:300]} for f in result.findings],
        "refuted": [{"path": f.path, "line": f.line, "severity_claimed": f.severity_claimed, "title": f.effective_title(),
                     "reason": f.verification.reason[:300], "body": f.body[:8000]} for f in result.refuted],
        "verifier": {"runs": report.runs, "seconds": report.seconds, "verified": report.verified, "refuted": report.refuted,
                     "downgraded": report.downgraded, "unverified": report.unverified, "skipped": report.skipped,
                     "model": report.model, "alias": report.alias, "endpoint_kind": report.endpoint_kind, "reason": report.reason,
                     "usage": {"in": report.usage.input_tokens, "out": report.usage.output_tokens},
                     "cost_usd": (r.estimate_cost_usd(report.model, report.usage) if report.runs and report.model else None)},
    }


def stamp_verifier_record(record: Any, report: Any, counts: dict[str, int]) -> None:
    record.verifier_runs = report.runs
    record.verifier_seconds = report.seconds if report.runs else None
    record.findings_verified = counts.get("verified", 0)
    record.findings_downgraded = counts.get("downgraded", 0)
    record.findings_refuted = counts.get("refuted", 0)


def run_case(
    *,
    case_path: Path,
    provider: Any,
    runtime: Any,
    system_prompt: str,
    max_turns: int,
    out: Path,
    provider_id: str,
    model: str,
    api_base: str = "",
    campaign: dict[str, Any] | None = None,
    verifier_policy: Any = None,
    verifier_provider: Any = None,
    api_key: str = "",
    round_mode: str = "",
) -> dict[str, Any]:
    """Review one fixture-tree case with an already-built provider.

    `round_mode` (Task 27): `""` reviews base → head in one full round; `"2"`
    replays a multi-round fixture's round 2 in incremental mode (delta
    round1 → head, the round-1 findings as priors, the RFC-06 turn budget);
    `"nochange"` reviews round1 → round1 — the verifier-only round.

    `verifier_policy` (a `VerifierPolicy`, default off) runs the Task 14
    verifier + severity policy after the review — the precision arm of the
    Phase 1 campaigns; `verifier_provider` lets tests inject a fake.

    Testable without GitHub, a token or a vendor key: callers pass the
    provider (a fake in tests). Writes the result payload to `out` and a
    `run-record/3.0` to `<out>.run-record.json`.
    """
    r = runtime
    case = json.loads(case_path.read_text(encoding="utf-8"))
    t_start = time.time()
    record = r.RunRecord()
    record.provider = provider_id if provider_id in r.PROVIDER_IDS_FOR_RECORD else record.provider
    record.model = model
    record.repo_kind = "fixture_tree"
    record.corpus_case_id = str(case.get("id"))
    record.corpus_sha256 = canonical_hash(case.get("fixture"))
    record.campaign = campaign
    record.prompt_sha256 = r._sha256_text(system_prompt)
    record.runtime_sha = r._runtime_sha(str(ROOT))
    profile = getattr(provider, "profile", None)
    record.endpoint_kind = getattr(profile, "kind", "unknown") or "unknown"
    if round_mode not in ROUND_MODES:
        raise ValueError(f"unknown round mode {round_mode!r}")
    with tempfile.TemporaryDirectory() as tmp:
        pre_context: Any = None
        effective_turns: int = max_turns
        if round_mode:
            repo, base_sha, round1_sha, head_sha = materialise_tree(case, Path(tmp), with_round1=True)
            if round_mode == "nochange":
                head_sha = round1_sha  # the round-2 push carried no code change
                _git(repo, "checkout", "-q", round1_sha)
            round1_findings = list(((case.get("fixture") or {}).get("iar") or {}).get("round1_findings") or [])
            pre_context, effective_turns = build_round2_context(r, repo=repo, base_sha=base_sha, round1_sha=round1_sha, head_sha=head_sha, round1_findings=round1_findings, max_turns=max_turns)
        else:
            repo, base_sha, head_sha = materialise_tree(case, Path(tmp))
        meta = (case.get("fixture") or {}).get("pr_metadata") or {}
        ctx = r.build_pr_context_from_local(
            base_sha=base_sha, head_sha=head_sha, repo_root=str(repo),
            title=str(meta.get("title", "")), body=str(meta.get("body", "")),
        )
        if pre_context is not None:
            ctx.incremental = pre_context
        record.head_sha = head_sha
        record.populate_context(ctx, base_sha=base_sha, iar_mode=("none" if not round_mode else ("verifier-only" if pre_context.verifier_only else "incremental")))
        setup_seconds = time.time() - t_start
        record.setup_seconds = round(setup_seconds, 3)
        record.run_started = True
        t0 = time.time()
        cwd = os.getcwd()
        os.chdir(repo)  # the review tools (read_file / grep / glob) resolve against cwd
        try:
            turns = 0
            outstanding_verdicts: list[Any] = []
            if pre_context is not None and pre_context.verifier_only:
                # RFC-06 verifier-only round: no model review at all.
                result = r.ReviewResult(findings=[], summary="", overall_severity=r.SEVERITY_NONE)
                usage = r.UsageTelemetry(); state = r.ReviewState(max_inline_comments=10, inventory=ctx.inventory); tool_calls = 0
            elif isinstance(provider, r.AgentRunnerProvider):
                with tempfile.TemporaryDirectory() as out_dir:
                    result = provider.run_review(
                        pr_context=ctx, review_instructions=system_prompt,
                        workspace=repo, output_dir=Path(out_dir),
                    )
                usage = result.usage or r.UsageTelemetry()
                turns = usage.turns
                state = None
                tool_calls = None
            else:
                state = r.ReviewState(max_inline_comments=10, inventory=ctx.inventory)
                messages = [{"role": "user", "content": r.render_user_prompt(ctx)}]
                stop_reason = r.drive_review(provider=provider, system_prompt=system_prompt, messages=messages,
                                             tools=r.tools_schema(10, allow_update_prior_finding=pre_context is not None), state=state, max_turns=effective_turns)
                result = r.state_to_review_result(state, stop_reason=stop_reason, max_turns=effective_turns)
                usage = state.usage
                turns = usage.turns
                tool_calls = state.tool_call_count
            # Verifier + severity policy (Task 14) inside the tree checkout so
            # the read-only tools see the fixture at head.
            vpolicy = verifier_policy if verifier_policy is not None else r.VerifierPolicy(enabled=False)
            r.complete_finding_evidence(result, state=state, head_sha=head_sha, run_id=record.ensure_run_id(),
                                        provider_id=provider_id, endpoint_kind=record.endpoint_kind, model=model)
            if pre_context is not None and pre_context.verifier_only:
                prov = verifier_provider; v_model, v_alias, v_kind, v_reason = "", "", "", ""
                if prov is None and vpolicy.enabled:
                    prov, v_model, v_alias, v_kind, v_reason = r.build_verifier_provider(provider_id=provider_id, api_key=api_key, api_base=api_base, requested_model=vpolicy.model, review_model=model)
                report, outstanding_verdicts = r.verify_outstanding_findings(pre_context.prior_findings, policy=vpolicy, provider=prov, model=v_model, alias=v_alias, endpoint_kind=v_kind,
                                                                            unavailable_reason=v_reason or ("verifier off" if not vpolicy.enabled else ""), inventory=ctx.inventory)
                result.summary = r.render_verifier_only_narrative(outstanding_verdicts, prior_head=pre_context.delta.prior_head_sha)
                # the round's only spend is the verifier's: charge it to the run so cost is known
                usage = r.UsageTelemetry(input_tokens=report.usage.input_tokens, output_tokens=report.usage.output_tokens, source=r.USAGE_SOURCE_API)
                if report.runs and report.model:
                    usage.cost_usd = r.estimate_cost_usd(report.model, report.usage)
            else:
                report = apply_verifier(r, result, policy=vpolicy, provider_id=provider_id, api_key=api_key, api_base=api_base,
                                        review_model=model, inventory=ctx.inventory, verifier_provider=verifier_provider)
        finally:
            os.chdir(cwd)
        record.provider_seconds = round(time.time() - t0, 3)
        if usage.source != r.USAGE_SOURCE_UNAVAILABLE and usage.cost_usd is None:
            usage.cost_usd = r.estimate_cost_usd(model, usage)
        if isinstance(provider, r.AgentRunnerProvider):
            record.instruction_files_read = list(getattr(provider, "last_instruction_files_read", ()))
        record.populate_from_run(provider=provider, state=state, result=result, usage=usage, max_turns=max_turns)
        record.status = r.RUN_STATUS_INCOMPLETE if result.incomplete else r.RUN_STATUS_COMPLETED
        stamp_verifier_record(record, report, {"verified": report.verified, "downgraded": report.downgraded, "refuted": report.refuted})
    payload = {
        "case": case.get("id"), "pr": case.get("id"), "repo": "fixture", "provider": provider_id, "endpoint_kind": record.endpoint_kind,
        "model": model, "prompt": "composed", "extension": False,
        "runtime_head": record.runtime_sha[:12],
        "turns": turns, "tool_calls": tool_calls,
        "seconds": round(time.time() - t0, 1), "setup_seconds": round(setup_seconds, 1),
        "total_seconds": round(time.time() - t_start, 1),
        "usage": {"in": usage.input_tokens, "cache_read": usage.cache_read_tokens, "cache_write": usage.cache_write_tokens,
                  "out": usage.output_tokens, "source": usage.source} if usage.source != r.USAGE_SOURCE_UNAVAILABLE else None,
        "cost_usd": usage.cost_usd,
        "changed_files": [f.get("path") for f in ctx.changed_files],
        "findings": [{"path": f.path, "line": f.line, "severity": f.severity, "body": f.body[:8000]} for f in result.findings],
        "summary": (result.summary or "")[:2000],
        "round": round_mode or "full",
        "effective_max_turns": effective_turns,
        "prior_finding_updates": {fp: list(v) for fp, v in (result.prior_finding_updates or {}).items()},
        "outstanding_verdicts": [{"path": pf.path, "line": pf.line, "status": v.status, "reason": v.reason[:200]} for pf, v in outstanding_verdicts],
        **verifier_payload(r, result, report),
    }
    payload["score"] = score_run(payload, corpus_entry=case_labels_as_corpus_entry(case))
    sc = payload["score"]
    doc = record.to_dict(status=record.status or r.RUN_STATUS_COMPLETED, failure_class=None)
    doc["outcome"]["score"] = {
        "must_find_total": sc["must_find_total"], "must_find_hits": sc["must_find_hits"],
        "false_positives": len(sc["false_positives"]), "unlabelled": sc["unlabelled_findings"],
        "adjudicated_true": None, "adjudicated_false": None,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))
    Path(str(out) + ".run-record.json").write_text(r.scrub_secrets(json.dumps(doc, indent=2)) + "\n", encoding="utf-8")
    return payload


def compose_prompt(prompt_file: Path, extension: Path | None) -> str:
    text = prompt_file.read_text(encoding="utf-8")
    if extension and extension.exists():
        text = text.rstrip("\n") + "\n\n---\n\n" + extension.read_text(encoding="utf-8")
    return text


def _write_failed_record(r: Any, record: Any, args: argparse.Namespace, exc: BaseException, *, out_path: Path) -> None:
    """A PR-path run that crashed before its record was written still leaves a `failed` record.

    Setup crashes (GitHub fetch, provider build, prompt compose) are classed
    `github_api` unless the provider loop had started, which makes them
    `provider_error`. Written beside the result path the campaign driver
    expects, so the campaign counts the run instead of losing it.
    """
    record.provider = args.provider if args.provider in r.PROVIDER_IDS_FOR_RECORD else record.provider
    record.model = record.model or (args.model or "")
    record.runtime_sha = record.runtime_sha or r._runtime_sha(str(ROOT))
    failure_class: str = r.RUN_FAILURE_PROVIDER if record.run_started else r.RUN_FAILURE_GITHUB
    doc: dict[str, Any] = record.to_dict(status=r.RUN_STATUS_FAILED, failure_class=failure_class)
    print(f"run_eval: run failed before its record was written — {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    Path(str(out_path) + ".run-record.json").write_text(r.scrub_secrets(json.dumps(doc, indent=2)) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    t_start = time.time()
    r = load_runtime()
    record = r.RunRecord()  # created first so total_seconds spans the whole run
    key = os.environ.get(args.api_key_env, "")
    if not key:
        sys.exit(f"{args.api_key_env} is not set")
    if args.tree:
        api_base = r.validate_api_base(args.api_base or "")
        # Tier aliases (`balanced` / `economy` / `deep`) resolve per runner × kind
        # exactly as `main()` does — the CLI would reject the alias as a model id.
        profile = r.resolve_endpoint_profile(api_base, args.provider)
        model = r.resolve_model(args.provider, profile, args.model or "")
        provider = r.build_provider(args.provider, api_key=key, model=model, api_base=api_base)
        system_prompt = compose_prompt(Path(args.prompt), Path(args.extension) if args.extension else None)
        payload = run_case(
            case_path=Path(args.tree), provider=provider, runtime=r, system_prompt=system_prompt,
            max_turns=args.max_turns, out=Path(args.out), provider_id=args.provider, model=model,
            api_base=api_base, verifier_policy=r.VerifierPolicy(enabled=(args.verifier or "off").lower() == "on"), api_key=key,
            round_mode=(args.round or ""),
        )
        print(fmt_row(payload))
        return payload
    if not (args.repo and args.pr and args.worktree):
        sys.exit("give --repo, --pr and --worktree (or --tree CASE)")
    token = gh_token()
    worktree = str(Path(args.worktree).resolve())  # absolute: everything after chdir uses it
    args.worktree = worktree
    args.prompt = str(Path(args.prompt).resolve())  # relative prompt/extension paths break after chdir
    if args.extension:
        args.extension = str(Path(args.extension).resolve())
    out_path = Path(args.out).resolve()
    args.out = str(out_path)
    os.chdir(worktree)
    try:
        ctx = r.fetch_pr_context(repo=args.repo, pr_number=args.pr, base_ref=args.base_ref, token=token)
        api_base = r.validate_api_base(args.api_base or "")
        profile = r.resolve_endpoint_profile(api_base, args.provider)
        model = r.resolve_model(args.provider, profile, args.model or "")
        args.model = model
        provider = r.build_provider(args.provider, api_key=key, model=model, api_base=api_base)
        system_prompt = compose_prompt(Path(args.prompt), Path(args.extension) if args.extension else None)
        # Timing separation (PLAN Task 4 / F8): fetch + provider build + prompt
        # compose are "setup"; the provider loop below is timed as t0..end.
        setup_seconds = time.time() - t_start
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
            state = r.ReviewState(max_inline_comments=10, inventory=ctx.inventory)
            messages = [{"role": "user", "content": r.render_user_prompt(ctx)}]
            tools = r.tools_schema(10)

            class Counting:
                def __init__(self, inner: Any) -> None:
                    self.inner = inner

                def complete(self, **kw: Any) -> Any:
                    nonlocal turns
                    turns += 1
                    return self.inner.complete(**kw)

            stop_reason = r.drive_review(provider=Counting(provider), system_prompt=system_prompt, messages=messages, tools=tools, state=state, max_turns=args.max_turns)
            result = r.state_to_review_result(state, stop_reason=stop_reason, max_turns=args.max_turns)
            usage = state.usage
            cost = r.estimate_cost_usd(args.model or "", usage) if usage else None
            tool_calls = sum(1 for m in messages if m["role"] == "assistant" for b in (m["content"] if isinstance(m["content"], list) else []) if isinstance(b, dict) and b.get("type") == "tool_use")
        # Run record for the PR path (v3): same shape the runtime writes; the
        # campaign driver stamps `campaign` and stores it beside the result.
        record.provider = args.provider if args.provider in r.PROVIDER_IDS_FOR_RECORD else record.provider
        record.model = args.model or ""
        record.endpoint_kind = getattr(getattr(provider, "profile", None), "kind", "unknown") or "unknown"
        record.runtime_sha = r._runtime_sha(str(ROOT))
        record.prompt_sha256 = r._sha256_text(system_prompt)
        record.head_sha = ctx.head_ref
        record.populate_context(ctx, base_sha="", iar_mode="none")
        record.setup_seconds = round(setup_seconds, 3)
        record.run_started = True
        record.provider_seconds = round(time.time() - t0, 3)
        _usage_obj = usage or r.UsageTelemetry()
        if _usage_obj.source != r.USAGE_SOURCE_UNAVAILABLE and _usage_obj.cost_usd is None and cost is not None:
            _usage_obj.cost_usd = cost
        if isinstance(provider, r.AgentRunnerProvider):
            record.instruction_files_read = list(getattr(provider, "last_instruction_files_read", ()))
        record.populate_from_run(provider=provider, state=None if isinstance(provider, r.AgentRunnerProvider) else state,
                                 result=result, usage=_usage_obj, max_turns=args.max_turns)
        record.status = r.RUN_STATUS_INCOMPLETE if result.incomplete else r.RUN_STATUS_COMPLETED
        vpolicy = r.VerifierPolicy(enabled=(args.verifier or "off").lower() == "on")
        r.complete_finding_evidence(result, state=None if isinstance(provider, r.AgentRunnerProvider) else state, head_sha=r._git_sha("HEAD"),
                                    run_id=record.ensure_run_id(), provider_id=args.provider, endpoint_kind=record.endpoint_kind, model=args.model or "")
        report = apply_verifier(r, result, policy=vpolicy, provider_id=args.provider, api_key=key, api_base=api_base,
                                review_model=args.model or "", inventory=ctx.inventory)
        stamp_verifier_record(record, report, {"verified": report.verified, "downgraded": report.downgraded, "refuted": report.refuted})
        payload = {
            "pr": args.pr, "repo": args.repo, "provider": args.provider, "endpoint_kind": record.endpoint_kind, "model": args.model or "",
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
            **verifier_payload(r, result, report),
        }
        payload["score"] = score_run(payload)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, indent=2))
        doc = record.to_dict(status=record.status or r.RUN_STATUS_COMPLETED, failure_class=None)
        sc = payload["score"]
        doc["outcome"]["score"] = {"must_find_total": sc["must_find_total"], "must_find_hits": sc["must_find_hits"],
                                   "false_positives": len(sc["false_positives"]), "unlabelled": sc["unlabelled_findings"],
                                   "adjudicated_true": None, "adjudicated_false": None}
        Path(str(args.out) + ".run-record.json").write_text(r.scrub_secrets(json.dumps(doc, indent=2)) + "\n", encoding="utf-8")
        print(fmt_row(payload))
        return payload
    except Exception as exc:  # noqa: BLE001 — instrument rule (RFC-01 P-7): a crashed run still leaves a record
        _write_failed_record(r, record, args, exc, out_path=out_path)
        raise


def _matches(finding: dict[str, Any], label: dict[str, Any]) -> bool:
    if label.get("path") and finding.get("path") != label["path"]:
        return False
    if label.get("line") is not None and finding.get("line") is not None:
        if abs(int(finding["line"]) - int(label["line"])) > label.get("window", LINE_WINDOW):
            return False
    body = (finding.get("body") or "").lower()
    kws = [k.lower() for k in label.get("keywords", [])]
    return all(k in body for k in kws) if label.get("all_keywords") else (not kws or any(k in body for k in kws))


def score_run(payload: dict[str, Any], corpus_entry: dict[str, Any] | None = None) -> dict[str, Any]:
    if corpus_entry is None:
        corpus = json.loads(CORPUS_PATH.read_text()) if CORPUS_PATH.exists() else {}
        entry = corpus.get(str(payload["pr"])) or {}
    else:
        entry = corpus_entry
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
    v = p.get("verifier") or {}
    vtag = (f" verifier {v.get('verified', 0)}v/{v.get('downgraded', 0)}d/{v.get('refuted', 0)}r {v.get('runs')} runs "
            f"${(v.get('cost_usd') or 0):.3f} |") if v.get("runs") else ""
    cost = f"${p['cost_usd']:.3f}" if p.get("cost_usd") is not None else "n/a"
    return (f"| #{p['pr']} | {p['provider']}/{p['model'] or 'default'} | {len(p['findings'])} findings | "
            f"recall {s['must_find_hits']}/{s['must_find_total']} | FP {len(s['false_positives'])} | unlabelled {s['unlabelled_findings']} | "
            f"sev-match {s['severity_matches']}/{s['must_find_total']} | summary {'yes' if s['summary_present'] else 'NO'} | sugg {s['suggestion_blocks']} | "
            f"turns {p['turns']} | in {u.get('in', 0) + u.get('cache_read', 0)} out {u.get('out', 0)} | {cost} | {p['seconds']}s |{vtag}")


def score_cmd(paths: list[str]) -> None:
    print("| PR | runner/model | findings | must-find recall | FP | unlabelled | severity match | summary | suggestions | turns | tokens | cost | wall |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for path in paths:
        p = json.loads(Path(path).read_text()); p["score"] = score_run(p); print(fmt_row(p))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("run")
    rp.add_argument("--repo"); rp.add_argument("--pr", type=int); rp.add_argument("--worktree")
    rp.add_argument("--tree", help="corpus case JSON with a `trees` fixture (no GitHub access needed)")
    rp.add_argument("--base-ref", default="main"); rp.add_argument("--provider", required=True); rp.add_argument("--api-base", default="")
    rp.add_argument("--model", default=""); rp.add_argument("--api-key-env", default=""); rp.add_argument("--prompt", default=str(ROOT / "prompts/default.md"))
    rp.add_argument("--extension", default=""); rp.add_argument("--max-turns", type=int, default=30); rp.add_argument("--out", required=True)
    rp.add_argument("--verifier", default="off", choices=["on", "off"], help="run the v3 verifier + severity policy after the review (Task 14)")
    rp.add_argument("--round", default="", choices=["", "2", "nochange"], help="multi-round fixtures (Task 27): `2` = incremental round 2, `nochange` = verifier-only round")
    sp = sub.add_parser("score"); sp.add_argument("paths", nargs="+")
    return ap


def main() -> None:
    args = build_parser().parse_args()
    if args.cmd == "run":
        run(args)
    else:
        score_cmd(args.paths)


if __name__ == "__main__":
    main()
