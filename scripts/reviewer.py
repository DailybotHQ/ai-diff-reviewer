#!/usr/bin/env python3
"""AI Diff Reviewer — composite-action entry point.

Runs the full review lifecycle from a single Python process:

    1. Label gate     — exit early if the configured label is missing.
    2. Collapse prev  — mark previous bot reviews/comments as OUTDATED.
    3. Tracking comm. — post a spinner comment with the review marker.
    4. PR fetch       — pull metadata + diff once for the agentic loop seed.
    5. Agentic loop   — Anthropic Messages API + tool use (read/grep/glob/
                        post_inline_comment/submit_review).
    6. Submit review  — single POST with summary + queued inline comments,
                        with a 422 fallback that drops inline comments and
                        re-posts summary-only.
    7. Apply label    — apply `applied-label` if set and the run was not
                        blocked by strictness.
    8. Strictness     — exit code 2 if the configured strictness level is
                        violated, turning the GitHub check red.

Stdlib only — no extra dependencies, runs on any GitHub-hosted or
self-hosted runner that has Python 3.10+.

Environment (set by the composite action's `env:` block; see action.yml):

    AIPRR_PROVIDER           Provider id (`anthropic`, `openai`, `claude-code`,
                            `cursor`, `codex`, or `grok`).
    AIPRR_API_KEY            Provider API key.
    AIPRR_GH_TOKEN           GitHub token for PR/review operations.
    AIPRR_MODEL              Model id (empty = provider default).
    AIPRR_API_BASE           Optional backend base URL (`api-base` input).
                            Empty = the provider's default endpoint. Lets a
                            runner talk to an Anthropic- or OpenAI-compatible
                            backend (Z.ai, xAI, Azure Foundry, self-hosted).
                            Resolved into an `EndpointProfile`; ignored by
                            `cursor`.
    AIPRR_PROMPT_FILE        Path to a markdown system prompt (empty =
                            bundled `prompts/default.md`). Fully replaces
                            the base prompt.
    AIPRR_PROMPT_EXTENSION_FILE  Path to a markdown file APPENDED to the
                            base prompt. Layer overrides without copying
                            the whole default.
    AIPRR_IGNORE_PATHS       Extra globs (comma/newline separated) whose diff
                            sections are omitted from the prompt, on top of
                            the built-in lock/minified/generated list.
    AIPRR_AUTHOR_ASSOCIATION Comma-separated whitelist of accepted
                             GitHub `pull_request.author_association`
                             values. Default `OWNER,MEMBER,COLLABORATOR`
                             (write-tier only). Empty disables the gate.
                             See docs/SECURITY.md § "Author-association
                             gate" for rationale.
    AIPRR_LABEL_GATE         Required label, or empty for no gate.
    AIPRR_TRIGGER_MODE       `always` | `label-required` | `label-once` |
                            `label-added-only`. Empty = auto (label-required
                            when `label-gate` is set, else `always`).
    AIPRR_APPLIED_LABEL      Label to apply on success, or empty.
    AIPRR_COLLAPSE_PREVIOUS  `true`/`false`.
    AIPRR_TRACKING_COMMENT   `true`/`false`.
    AIPRR_STRICTNESS         `lenient` | `block-on-critical` |
                            `block-on-warning` | `block-on-any`.
    AIPRR_MAX_INLINE_COMMENTS  Integer cap.
    AIPRR_MAX_TURNS          Integer cap.
    AIPRR_PR_DESCRIPTION_MODE  `off` | `warn` | `block` | `autocomplete`.
    AIPRR_PR_DESCRIPTION_MIN_LENGTH  Integer threshold for adequacy.
    AIPRR_COMPLEXITY_LABELS_ENABLED  `true`/`false`.
    AIPRR_COMPLEXITY_LABEL_PREFIX  Label prefix, e.g. `complexity:`.
    AIPRR_REPO               `owner/name`.
    AIPRR_PR_NUMBER          PR number.
    AIPRR_HEAD_SHA           Commit SHA the review anchors to.
    AIPRR_BASE_REF           Base branch name.
    AIPRR_ACTION_PATH        Filesystem path to this action's checkout
                            (used to locate the bundled prompt).
    GITHUB_OUTPUT           Path to the workflow outputs file (set by
                            the runner, written here for action outputs).
"""

from __future__ import annotations

import hashlib
import functools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ANTHROPIC_API_URL: str = "https://api.anthropic.com/v1/messages"
# Messages path appended to an Anthropic-compatible `api-base`
# (`resolve_endpoint_profile`); the default profile composes to
# `ANTHROPIC_API_URL` exactly (locked by tests/test_backends.py).
ANTHROPIC_MESSAGES_PATH: str = "/v1/messages"
ANTHROPIC_VERSION: str = "2023-06-01"
# Prompt-cache breakpoint marker. Sent only to profiles that support it
# (api.anthropic.com); compatible gateways cache server-side and may
# reject unknown block fields.
ANTHROPIC_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}

# OpenAI-compatible chat-completions runner (`provider: openai`, v2.1.0+).
# One in-process runner covers OpenAI, Azure Foundry (v1 endpoint), xAI, Z.ai
# and self-hosted gateways through `api-base`; translation happens at the
# provider boundary so the in-memory conversation stays Anthropic-shaped.
OPENAI_CHAT_COMPLETIONS_PATH: str = "/chat/completions"
OPENAI_TOOL_CHOICE_AUTO: str = "auto"
OPENAI_AZURE_API_KEY_HEADER: str = "api-key"
# Current-generation OpenAI / Azure models reject `max_tokens` in favour of
# `max_completion_tokens`; xAI, Z.ai and generic gateways document
# `max_tokens`. Keyed by endpoint kind; anything else falls back to
# `max_tokens`.
OPENAI_MAX_TOKENS_PARAM_BY_KIND: dict[str, str] = {
    "openai": "max_completion_tokens",
    "azure": "max_completion_tokens",
}
OPENAI_MAX_TOKENS_PARAM_DEFAULT: str = "max_tokens"
# `finish_reason` → Anthropic `stop_reason`. Unknown values pass through.
OPENAI_FINISH_REASON_TO_STOP_REASON: dict[str, str] = {
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "end_turn",
}

# Claude Code (subscription auth). A long-lived OAuth token generated by
# `claude setup-token` (requires a Claude Pro/Max subscription) is billed
# against that subscription instead of metered API usage. Such tokens start
# with this prefix; a normal Anthropic API key starts with `sk-ant-api`. The
# Claude Code CLI reads the token from the `CLAUDE_CODE_OAUTH_TOKEN` env var.
CLAUDE_OAUTH_TOKEN_PREFIX: str = "sk-ant-oat"
CLAUDE_CODE_OAUTH_TOKEN_ENV: str = "CLAUDE_CODE_OAUTH_TOKEN"
# Claude Code on a custom Anthropic-compatible backend (`api-base`, v2.1.0+).
# The env contract Z.ai documents for its Claude Code integration (and that
# xAI's Anthropic-compatible surface accepts): bearer-style auth token, base
# URL, a generous API timeout, and the three model-alias env vars pinned to
# the chosen model so Claude Code's internal opus/sonnet/haiku aliases all
# resolve to it.
CLAUDE_CODE_BASE_URL_ENV: str = "ANTHROPIC_BASE_URL"
CLAUDE_CODE_AUTH_TOKEN_ENV: str = "ANTHROPIC_AUTH_TOKEN"
CLAUDE_CODE_API_TIMEOUT_ENV: str = "API_TIMEOUT_MS"
CLAUDE_CODE_CUSTOM_BACKEND_TIMEOUT_MS: str = "3000000"
CLAUDE_CODE_DEFAULT_MODEL_ENVS: tuple[str, ...] = (
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)

GITHUB_REST_BASE: str = "https://api.github.com"
GITHUB_GRAPHQL_URL: str = "https://api.github.com/graphql"

# Provider defaults — keyed by `AIPRR_PROVIDER`. Adding a new provider means:
#   1. New entry here for the default model id (or a sentinel like "auto" for
#      agent-runner CLIs that pick their own default at invocation time).
#   2. New `Provider` or `AgentRunnerProvider` implementation below.
#   3. New branch in `build_provider()`.
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    # Agent-runner (CLI) providers. A `model:` input from the consumer
    # overrides these. For the METERED providers (Claude Code, Codex) we
    # always pin an explicit model — never `auto`, which defers to the account
    # default and can silently be Opus (≈$5/$25). Cursor deliberately keeps
    # `auto` because it is unlimited/flat-rate on Pro.
    # Claude Code: `claude-sonnet-4-6` is the quality/price sweet spot for
    # code review — it reliably finds the subtle bugs (logic, concurrency,
    # security) that make a review worth running, at ~1/5th of Opus cost.
    # Haiku 4.5 (≈$1/$5) is cheaper but a real step down at bug-finding; use
    # it only for smoke/dogfood reviews (see .github/workflows/self-review.yml).
    "claude-code": "claude-sonnet-4-6",
    # `auto` routes through Cursor's model dispatch and is unlimited on Pro
    # plans (metered premium models like `composer-2.5` burn monthly credits).
    "cursor": "auto",
    # `gpt-5-codex` is deprecated on the Codex CLI. `gpt-5.6-luna` is the
    # current-gen budget model — the OpenAI parallel of the Sonnet-class
    # choice above: strong enough to find subtle bugs (unlike the mini tier,
    # which we reserve for smoke/dogfood passes) while still ≈$1/$6 per 1M
    # tokens, far below the ≈$1.75/$14 codex-tier models. Pin `gpt-5.4-mini`
    # (≈$0.75/$4.50) for a cheaper/shallower smoke review.
    "codex": "gpt-5.6-luna",
    # In-process OpenAI-compatible runner. Same quality/cost reasoning as
    # Codex: `gpt-5.6-luna` is the current-gen budget model that still finds
    # subtle bugs; `gpt-5.4-mini` for smoke passes. On non-OpenAI backends
    # (`api-base`) consumers pin the backend's own id (e.g. `grok-4.3`,
    # `glm-5.3`, or an Azure deployment name).
    "openai": "gpt-5.6-luna",
    # xAI Grok CLI. `grok-4.3` is the "daily" tier the maintainer runs
    # locally: strong enough for real review at the lower price point;
    # `grok-4.6` is the reasoning tier for deeper passes. Never `auto` for a
    # metered CLI. (Ids/prices re-verified in the cost-controls task.)
    "grok": "grok-4.3",
}

# ---------------------------------------------------------------------------
# Cost controls (v2.1.0+): model tier aliases + indicative prices
# ---------------------------------------------------------------------------
# `model` accepts three tier words resolved per (runner, backend kind) —
# the one-word cost profile Cursor's `auto` proved people actually use.
# Empty `model` keeps resolving to DEFAULT_MODELS (no behaviour change for
# existing consumers); explicit ids pass through untouched. Azure and custom
# hosts have no rows (deployment names are consumer-defined) and fail fast.
MODEL_TIER_BALANCED: str = "balanced"
MODEL_TIER_ECONOMY: str = "economy"
MODEL_TIER_DEEP: str = "deep"
# Runners that ignore `api-base` (they only talk to their own vendor): an
# empty `model` keeps resolving to the built-in default for them.
PROVIDERS_WITHOUT_API_BASE_LANE: tuple[str, ...] = ("cursor", "grok")
# What `model` must name when `api-base` points at each kind of backend.
MODEL_REQUIRED_HINTS: dict[str, str] = {
    "azure": "your Azure deployment name (e.g. `gpt-5.4-mini-azure`)",
    "zai": "a GLM id (e.g. `glm-5.3`)",
    "xai": "a Grok id (e.g. `grok-4.6`)",
    "anthropic": "an Anthropic model id",
    "openai": "an OpenAI model id",
    "custom": "the gateway's model id",
}
MODEL_TIERS: tuple[str, ...] = (
    MODEL_TIER_BALANCED,
    MODEL_TIER_ECONOMY,
    MODEL_TIER_DEEP,
)
# Verified against the vendors' model/pricing pages on this date. Ids and
# prices move — re-verify when bumping. Rationale per row lives in
# docs/PROVIDERS.md § "Cost-efficient defaults matrix".
MODEL_TIERS_VERIFIED_ON: str = "2026-09-16"
_ANTHROPIC_TIERS: dict[str, str] = {
    # Sonnet 5 ($2/$10) is current and cheaper than the legacy
    # claude-sonnet-4-6 ($3/$15) that DEFAULT_MODELS still names for
    # back-compat; Haiku 4.5 ($1/$5) for smoke; Opus 5 ($5/$25) for deep.
    MODEL_TIER_BALANCED: "claude-sonnet-5",
    MODEL_TIER_ECONOMY: "claude-haiku-4-5",
    MODEL_TIER_DEEP: "claude-opus-5",
}
_OPENAI_TIERS: dict[str, str] = {
    # gpt-5.6-luna ($0.20/$1.20) is both the balanced AND the economy pick:
    # gpt-5.4-mini ($0.75/$4.50) is no longer cheaper. Terra ($2/$12) deep.
    MODEL_TIER_BALANCED: "gpt-5.6-luna",
    MODEL_TIER_ECONOMY: "gpt-5.6-luna",
    MODEL_TIER_DEEP: "gpt-5.6-terra",
}
_XAI_TIERS: dict[str, str] = {
    # Measured 2026-09-16 on the labelled corpus (tests/eval): through the
    # Grok CLI, grok-4.3 ($1.25/$2.50 under 200k) reported 0 of 4 known
    # defects (and once wrote no findings file) at ~$0.07/review, while
    # grok-4.6 ($2/$6) found 3 of 4 with zero false positives at
    # $0.47–0.85 and 4–10 min. A "balanced" review that finds nothing is not
    # balanced, so 4.6 is both the balanced and the deep pick; 4.3 remains
    # the economy/smoke tier.
    MODEL_TIER_BALANCED: "grok-4.6",
    MODEL_TIER_ECONOMY: "grok-4.3",
    MODEL_TIER_DEEP: "grok-4.6",
}
_ZAI_TIERS: dict[str, str] = {
    # glm-5.3 ($1.40/$4.40) flagship; glm-5.3-flash ($0.15/$0.50) smoke.
    # Flat-rate Coding Plan makes the marginal cost ≈ 0 either way.
    MODEL_TIER_BALANCED: "glm-5.3",
    MODEL_TIER_ECONOMY: "glm-5.3-flash",
    MODEL_TIER_DEEP: "glm-5.3",
}
_CURSOR_TIERS: dict[str, str] = {
    # `auto` is flat-rate on Pro and routes well; `composer-2.5` is the
    # premium in-house coding model (burns credits) for deep passes.
    MODEL_TIER_BALANCED: "auto",
    MODEL_TIER_ECONOMY: "auto",
    MODEL_TIER_DEEP: "composer-2.5",
}
# Keyed by (provider id, endpoint kind). Kind literals match ENDPOINT_KIND_*
# (defined below with the backend constants; a test asserts the agreement).
MODEL_TIER_TABLE: dict[tuple[str, str], dict[str, str]] = {
    ("anthropic", "anthropic"): _ANTHROPIC_TIERS,
    ("claude-code", "anthropic"): _ANTHROPIC_TIERS,
    ("anthropic", "zai"): _ZAI_TIERS,
    ("claude-code", "zai"): _ZAI_TIERS,
    ("anthropic", "xai"): _XAI_TIERS,
    ("claude-code", "xai"): _XAI_TIERS,
    ("openai", "openai"): _OPENAI_TIERS,
    ("codex", "openai"): _OPENAI_TIERS,
    ("openai", "xai"): _XAI_TIERS,
    ("codex", "xai"): _XAI_TIERS,
    ("openai", "zai"): _ZAI_TIERS,
    ("codex", "zai"): _ZAI_TIERS,
    ("grok", "xai"): _XAI_TIERS,
    ("cursor", "custom"): _CURSOR_TIERS,
}
# Indicative list prices, USD per 1M tokens (input, output), matched by the
# longest model-id prefix. Shared by the tier docs and the usage telemetry;
# estimates only — consumers must never gate CI on them.
INDICATIVE_PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.0, 12.0),
    "gpt-5.6-sol": (4.0, 20.0),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.3-codex": (1.75, 14.0),
    "grok-4.6": (2.0, 6.0),
    "grok-4.5": (2.0, 6.0),
    "grok-4.3": (1.25, 2.50),
    "glm-5.3-flash": (0.15, 0.50),
    "glm-5.3": (1.40, 4.40),
}
# Legacy defaults that still ship for back-compat but have a cheaper,
# current successor in the tier table — the run logs a one-line hint.
LEGACY_DEFAULT_MODEL_HINTS: dict[str, str] = {
    "claude-sonnet-4-6": "claude-sonnet-5",
}
# Usage telemetry (v2.1.0+). Every provider reports what it can; the source
# tag says how trustworthy the numbers are. Cost is an INDICATIVE estimate
# from `INDICATIVE_PRICES_USD_PER_MTOK` unless the CLI reported its own.
USAGE_SOURCE_API: str = "api"            # summed from API `usage` objects
USAGE_SOURCE_CLI: str = "cli"            # reported by the vendor CLI
USAGE_SOURCE_ESTIMATED: str = "estimated"
USAGE_SOURCE_UNAVAILABLE: str = "unavailable"
# Cache economics used by the estimate: reads ≈ 10 % of the input price
# (Anthropic's published ratio; OpenAI/xAI are in the same range), writes
# ≈ 125 % (Anthropic 5-minute cache). Indicative only.
CACHE_READ_PRICE_FACTOR: float = 0.10
CACHE_WRITE_PRICE_FACTOR: float = 1.25
# Bound on how much vendor-CLI stdout the usage parsers scan (tail).
CLI_STDOUT_SCAN_MAX_BYTES: int = 2_000_000
# Upper bound on the agent-runner findings file. The file is written by a
# vendor CLI running attacker-influenced input; a larger file is refused
# (summary-only failure) instead of being parsed into memory.
MAX_FINDINGS_FILE_BYTES: int = 5_000_000

# Agent-runner CLIs whose turn cap is enforced natively from `agent-max-turns`.
AGENT_MAX_TURNS_NATIVE_PROVIDERS: tuple[str, ...] = ("grok",)
GROK_MAX_TURNS_FLAG: str = "--max-turns"

DEFAULT_MAX_TURNS: int = 30
DEFAULT_MAX_INLINE_COMMENTS: int = 10
DEFAULT_BASE_REF: str = "main"

# ---------------------------------------------------------------------------
# Backends / endpoint profiles (v2.1.0+)
# ---------------------------------------------------------------------------
# `provider` names the RUNNER (who owns the tool-use loop); the optional
# `api-base` input names the BACKEND (where the model lives). The host of the
# base URL is classified into an endpoint *kind*, and an `EndpointProfile`
# carries the per-kind quirks every runner needs (auth header style, whether
# Anthropic `cache_control` may be sent, Codex wire API, Azure workarounds).
# An empty `api-base` resolves to the runner's default profile, which keeps
# every existing consumer byte-identical. See docs/PROVIDERS.md.
API_BASE_ENV: str = "AIPRR_API_BASE"

ENDPOINT_KIND_ANTHROPIC: str = "anthropic"
ENDPOINT_KIND_OPENAI: str = "openai"
ENDPOINT_KIND_AZURE: str = "azure"
ENDPOINT_KIND_XAI: str = "xai"
ENDPOINT_KIND_ZAI: str = "zai"
ENDPOINT_KIND_CUSTOM: str = "custom"
ENDPOINT_KINDS: tuple[str, ...] = (
    ENDPOINT_KIND_ANTHROPIC,
    ENDPOINT_KIND_OPENAI,
    ENDPOINT_KIND_AZURE,
    ENDPOINT_KIND_XAI,
    ENDPOINT_KIND_ZAI,
    ENDPOINT_KIND_CUSTOM,
)

# Host → kind classification. A suffix starting with `.` matches any
# subdomain; a bare host matches exactly. Order is irrelevant (no overlaps).
ENDPOINT_HOST_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("api.anthropic.com", ENDPOINT_KIND_ANTHROPIC),
    ("api.openai.com", ENDPOINT_KIND_OPENAI),
    (".openai.azure.com", ENDPOINT_KIND_AZURE),
    (".services.ai.azure.com", ENDPOINT_KIND_AZURE),
    (".cognitiveservices.azure.com", ENDPOINT_KIND_AZURE),
    ("api.x.ai", ENDPOINT_KIND_XAI),
    ("api.z.ai", ENDPOINT_KIND_ZAI),
)

# Well-known base URLs (documentation + runner defaults). The Anthropic base
# deliberately has no `/v1` — the Messages path is appended by the provider.
ANTHROPIC_DEFAULT_API_BASE: str = "https://api.anthropic.com"
OPENAI_DEFAULT_API_BASE: str = "https://api.openai.com/v1"
XAI_OPENAI_COMPAT_API_BASE: str = "https://api.x.ai/v1"
XAI_ANTHROPIC_COMPAT_API_BASE: str = "https://api.x.ai"
ZAI_ANTHROPIC_COMPAT_API_BASE: str = "https://api.z.ai/api/anthropic"
ZAI_OPENAI_COMPAT_API_BASE: str = "https://api.z.ai/api/coding/paas/v4"
ZAI_RESPONSES_API_BASE: str = "https://api.z.ai/api/v1"

# Azure Foundry + Codex quirk: plain text turns fail unless an image-generation
# deployment header is present and the feature is disabled (see
# docs/PROVIDERS.md § Codex on Azure Foundry).
AZURE_IMAGE_GEN_HEADER: str = "x-ms-oai-image-generation-deployment"
AZURE_IMAGE_GEN_DUMMY_DEPLOYMENT: str = "gpt-image-1"

# Auth header styles per protocol family.
ANTHROPIC_AUTH_STYLE_X_API_KEY: str = "x-api-key"
ANTHROPIC_AUTH_STYLE_BOTH: str = "both"          # x-api-key + Authorization
OPENAI_AUTH_STYLE_BEARER: str = "bearer"
OPENAI_AUTH_STYLE_AZURE: str = "azure"           # Bearer + `api-key` header
CODEX_WIRE_API_RESPONSES: str = "responses"

# `api-base` validation: https only, except loopback for local dev gateways.
API_BASE_ALLOWED_SCHEMES: tuple[str, ...] = ("https",)
API_BASE_LOCAL_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")

# Runner → default endpoint kind / base when `api-base` is empty. `cursor`
# has no bring-your-own endpoint (subscription-only), hence the custom kind
# with an empty base.
PROVIDER_DEFAULT_ENDPOINT_KIND: dict[str, str] = {
    "anthropic": ENDPOINT_KIND_ANTHROPIC,
    "claude-code": ENDPOINT_KIND_ANTHROPIC,
    "codex": ENDPOINT_KIND_OPENAI,
    "openai": ENDPOINT_KIND_OPENAI,
    "grok": ENDPOINT_KIND_XAI,
    "cursor": ENDPOINT_KIND_CUSTOM,
}
PROVIDER_DEFAULT_API_BASE: dict[str, str] = {
    "anthropic": ANTHROPIC_DEFAULT_API_BASE,
    "claude-code": ANTHROPIC_DEFAULT_API_BASE,
    "codex": OPENAI_DEFAULT_API_BASE,
    "openai": OPENAI_DEFAULT_API_BASE,
    "grok": XAI_OPENAI_COMPAT_API_BASE,
    "cursor": "",
}
# `grok` (xAI Grok CLI) talks to xAI only — no BYO endpoint either.
PROVIDERS_WITHOUT_API_BASE: tuple[str, ...] = ("cursor", "grok")

# xAI Grok CLI agent-runner (`provider: grok`, v2.1.0+). Headless surface
# verified on grok 1.0.30: `--prompt-file <path>` (the diff-carrying prompt;
# `-p` requires an inline value and does not read stdin), `--rules <text>`
# (appended to the system prompt — the analogue of Claude Code's
# `--append-system-prompt`), `--always-approve`, `--output-format json`
# (one JSON document with `usage`, `num_turns`, `total_cost_usd`),
# `--disable-web-search`, `--no-subagents`, `--no-plan`, `-m`, `--max-turns`.
GROK_CLI_BIN: str = "grok"
GROK_CLI_NAME: str = "xAI Grok"
GROK_API_KEY_ENV: str = "XAI_API_KEY"
GROK_PROMPT_FILE_FLAG: str = "--prompt-file"
GROK_RULES_FLAG: str = "--rules"
GROK_OUTPUT_FORMAT: str = "json"
GROK_PROMPT_FILENAME: str = "prompt.md"
# Hardening + cost defaults for a CI reviewer: a reviewer has no business
# fetching the web from an attacker-influenced diff, subagents multiply
# cost, and plan mode adds turns. Consumers can re-enable any of these via
# `agent-extra-args` (last flag wins in the CLI's own parsing).
GROK_HEADLESS_DEFAULT_FLAGS: tuple[str, ...] = (
    "--always-approve",
    "--output-format",
    GROK_OUTPUT_FORMAT,
    "--disable-web-search",
    "--no-subagents",
    "--no-plan",
)

# Codex on a custom backend (`api-base`, v2.1.0+): a `config.toml` written
# into the isolated per-run CODEX_HOME routes Codex to an OpenAI-compatible
# Responses API (Azure Foundry v1, xAI, Z.ai). `env_key` names the env var
# holding the credential — we already forward the key as OPENAI_API_KEY.
CODEX_CONFIG_TOML_FILENAME: str = "config.toml"
CODEX_CUSTOM_PROVIDER_ID: str = "aiprr"
CODEX_CUSTOM_PROVIDER_ENV_KEY: str = "OPENAI_API_KEY"
# Model catalog for custom backends. Codex resolves per-model capabilities
# (tool set, responses-lite presets, app/plugin tool namespaces) from its
# bundled catalog; a third-party Responses endpoint rejects several of those
# (xAI: `tools[].type: unknown variant "namespace"`). We clone a bundled
# entry under the consumer's model id with conservative capabilities and
# point `model_catalog_json` at it. Best-effort: if the bundled catalog
# cannot be read, the run proceeds without a catalog (Azure works either way).
CODEX_MODEL_CATALOG_FILENAME: str = "models.json"
# Backends observed (2026-09-16, Codex 0.154.0) to reject Codex's freeform
# `apply_patch` custom tool with HTTP 422. Warned, not blocked — a future
# CLI or gateway release may lift it.
CODEX_CUSTOM_TOOL_SENSITIVE_KINDS: tuple[str, ...] = ("xai", "zai", "custom")
# Preferred templates: current-gen, API-supported entries WITHOUT an
# `upgrade` redirect (an upgrade block would make Codex swap the model).
CODEX_CATALOG_TEMPLATE_SLUGS: tuple[str, ...] = (
    "gpt-5.6-luna",
    "gpt-5.4-mini",
    "gpt-5.4",
)
CODEX_CATALOG_CMD: tuple[str, ...] = ("codex", "debug", "models", "--bundled")
# Overrides applied to the cloned entry — only keys already present in the
# template are touched, so the shape stays valid across Codex versions.
CODEX_CATALOG_SAFE_OVERRIDES: dict[str, Any] = {
    "visibility": "list",
    "supported_in_api": True,
    "priority": 1,
    "use_responses_lite": False,
    "supports_search_tool": False,
    "additional_speed_tiers": [],
    "service_tiers": [],
    "experimental_supported_tools": [],
    "include_apps_usage_instructions": False,
    "include_plugin_usage_instructions": False,
    "include_skills_usage_instructions": False,
    # Legacy keys (older Codex catalogs) — applied only when present AND
    # already nullable in the template (see build_model_catalog_entry).
    "multi_agent_version": None,
    "tool_mode": None,
    "upgrade": None,
    "availability_nux": None,
}

# Tool-use loop guardrails.
MAX_TOOL_OUTPUT_BYTES: int = 32_000
MAX_FILE_READ_LINES: int = 2_000
# Max matches/paths a single grep/glob call returns before truncation.
MAX_SEARCH_RESULTS: int = 200
# Cap on the seed diff embedded in the first user message (characters). Larger
# diffs are truncated with a pointer to the read_file tool.
MAX_DIFF_CHARS: int = 200_000

# Diff shaping (v2.1.0+): lock / minified / generated / vendored files carry
# near-zero review value but dominate PR diffs and are re-sent on every
# turn. Sections matching these globs are removed from the diff body before
# truncation and listed back to the model as "omitted" with their line
# counts, so it knows what it did not see. Consumers extend the list with
# the `ignore-paths` input (additive; comma- or newline-separated globs).
# Globs are matched against repo-relative POSIX paths: `**` spans
# directories, `*` / `?` do not cross `/`, and a pattern without `/` matches
# the basename anywhere. IAR's own git inputs (range hash, new-lines %) are
# computed from unshaped git output and are unaffected.
IGNORE_PATHS_ENV: str = "AIPRR_IGNORE_PATHS"
# Caps on consumer-supplied globs: bounded regex count/length keeps the
# per-file match loop cheap even for pathological patterns.
MAX_IGNORE_GLOBS: int = 200
MAX_IGNORE_GLOB_LEN: int = 256
GLOB_ANY_DIRS: str = "**"
DEFAULT_IGNORE_PATH_GLOBS: tuple[str, ...] = (
    # JavaScript / TypeScript lockfiles
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    # Python
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
    "pdm.lock",
    # Other ecosystems
    "Cargo.lock",
    "go.sum",
    "composer.lock",
    "Gemfile.lock",
    "mix.lock",
    "pubspec.lock",
    "packages.lock.json",
    "Podfile.lock",
    "gradle.lockfile",
    "flake.lock",
    # Minified / bundled / maps
    "*.min.js",
    "*.min.css",
    "*.map",
    # Vendored trees and build output that landed in a diff
    "**/node_modules/**",
    "**/vendor/**",
    "**/dist/**",
    # Test snapshots
    "**/__snapshots__/**",
    "*.snap",
)
DIFF_SECTION_HEADER_PREFIX: str = "diff --git "
OMITTED_FILES_HEADING: str = "## Omitted from the diff (generated / lock files)"
# Substrings (case-insensitive) that mark a tool-arg key as sensitive in
# logs. The model isn't expected to ever pass these — but if a prompt
# injection tricked it into echoing env vars, we don't want them in the
# public workflow log.
LOG_REDACT_SUBSTRINGS: tuple[str, ...] = (
    "token",
    "key",
    "secret",
    "password",
    "auth",
)
# Soft cap on conversation history. Each turn appends an assistant message +
# a user (tool_results) message; with `MAX_TOOL_OUTPUT_BYTES = 32_000` and a
# 30-turn ceiling the worst case is ~2 MB serialised, growing O(turns²) in
# token billing on every API call. When we exceed this many turn-pairs we
# drop the oldest tool-result pairs (keeping the original user message and
# the most recent K turns), since older tool results have already informed
# the model.
MAX_CONVERSATION_TURNS_RETAINED: int = 12

# Anthropic API parameters.
ANTHROPIC_MAX_TOKENS: int = 8192
# Same output ceiling for the OpenAI-compatible runner (cost parity).
OPENAI_MAX_TOKENS: int = 8192
# Anthropic API timeouts (seconds).
API_REQUEST_TIMEOUT: int = 600
API_RETRY_DELAYS_S: tuple[int, ...] = (2, 5, 15)

# GitHub API timeouts.
GH_REQUEST_TIMEOUT: int = 60
# Page size for GitHub connection queries (REST `per_page` and the GraphQL
# `first:` argument). 100 is GitHub's hard ceiling for both.
GH_CONNECTION_PAGE_SIZE: int = 100
GH_MAX_REVIEW_THREAD_PAGES: int = 100

# Truncation caps (characters) for text we echo into logs or comments, so a
# single large error body or payload can't flood the workflow log / a comment.
MAX_ERROR_BODY_CHARS: int = 500
MAX_422_BODY_CHARS: int = 1000
MAX_TOOL_LOG_PREVIEW_CHARS: int = 120
MAX_TRACKING_ERROR_CHARS: int = 1500

# Strictness modes.
STRICTNESS_LENIENT: str = "lenient"
STRICTNESS_BLOCK_CRITICAL: str = "block-on-critical"
STRICTNESS_BLOCK_WARNING: str = "block-on-warning"
STRICTNESS_BLOCK_ANY: str = "block-on-any"
VALID_STRICTNESS: tuple[str, ...] = (
    STRICTNESS_LENIENT,
    STRICTNESS_BLOCK_CRITICAL,
    STRICTNESS_BLOCK_WARNING,
    STRICTNESS_BLOCK_ANY,
)

# PR description review modes (v1.2.0+).
PR_DESC_MODE_OFF: str = "off"
PR_DESC_MODE_WARN: str = "warn"
PR_DESC_MODE_BLOCK: str = "block"
PR_DESC_MODE_AUTOCOMPLETE: str = "autocomplete"
PR_DESC_MODES: tuple[str, ...] = (
    PR_DESC_MODE_OFF,
    PR_DESC_MODE_WARN,
    PR_DESC_MODE_BLOCK,
    PR_DESC_MODE_AUTOCOMPLETE,
)
PR_DESC_MIN_LENGTH_DEFAULT: int = 50
PR_DESC_AUTOCOMPLETE_MARKER: str = (
    "<!-- ai-pr-reviewer-description-autocompleted -->"
)

# PR complexity labeling (v1.2.0+).
PR_COMPLEXITY_LOW: str = "low"
PR_COMPLEXITY_MEDIUM: str = "medium"
PR_COMPLEXITY_HIGH: str = "high"
PR_COMPLEXITY_LEVELS: tuple[str, ...] = (
    PR_COMPLEXITY_LOW,
    PR_COMPLEXITY_MEDIUM,
    PR_COMPLEXITY_HIGH,
)
PR_COMPLEXITY_LABEL_PREFIX_DEFAULT: str = "complexity:"

# Trigger modes (v1.2.0+).
TRIGGER_ALWAYS: str = "always"
TRIGGER_LABEL_REQUIRED: str = "label-required"
TRIGGER_LABEL_ONCE: str = "label-once"
TRIGGER_LABEL_ADDED_ONLY: str = "label-added-only"
TRIGGER_MODES: tuple[str, ...] = (
    TRIGGER_ALWAYS,
    TRIGGER_LABEL_REQUIRED,
    TRIGGER_LABEL_ONCE,
    TRIGGER_LABEL_ADDED_ONLY,
)
TRIGGER_STATE_MARKER_OPEN: str = "<!-- ai-pr-reviewer-state: "
TRIGGER_STATE_MARKER_CLOSE: str = " -->"

# Author-association gate (v1.3.0+). GitHub attaches `author_association`
# to every `pull_request` / `pull_request_target` payload; the field is
# server-computed and cannot be spoofed by the PR author, which makes it
# the primary line of defense against LLM-budget abuse on public repos
# (an attacker opens N PRs → each burns ~50–200K tokens).
#
# The canonical values are the full enum accepted by GitHub. See
# https://docs.github.com/en/graphql/reference/enums#commentauthorassociation.
VALID_AUTHOR_ASSOCIATIONS: tuple[str, ...] = (
    "OWNER",
    "MEMBER",
    "COLLABORATOR",
    "CONTRIBUTOR",
    "FIRST_TIME_CONTRIBUTOR",
    "FIRST_TIMER",
    "MANNEQUIN",
    "NONE",
)

# The default write-tier — what `action.yml`'s `author-association` input
# defaults to and what the runtime falls back to when the env var is
# unset. Any consumer who wants to allow external contributors sets the
# input explicitly (see docs/SECURITY.md § "Author-association gate").
AUTHOR_ASSOCIATION_WRITE_TIER: tuple[str, ...] = (
    "OWNER",
    "MEMBER",
    "COLLABORATOR",
)

# GitHub collaborator permission levels that imply write-tier repo access.
# Used by the author-association gate when the webhook under-reports
# membership (common on private org repos with team-granted access).
COLLABORATOR_PERMISSION_WRITE_TIER: tuple[str, ...] = (
    "admin",
    "maintain",
    "write",
)

# Severity levels — ordered low→high so `max(SEVERITY_RANK)` yields the most
# severe finding in a review.
SEVERITY_NONE: str = "none"
SEVERITY_INFO: str = "info"
SEVERITY_WARNING: str = "warning"
SEVERITY_CRITICAL: str = "critical"
SEVERITY_RANK: dict[str, int] = {
    SEVERITY_NONE: 0,
    SEVERITY_INFO: 1,
    SEVERITY_WARNING: 2,
    SEVERITY_CRITICAL: 3,
}


def _sort_findings_criticals_first(findings: list["Finding"]) -> list["Finding"]:
    """Return a copy of `findings` sorted so critical severity findings
    come first, warnings second, infos third — preserving within-tier
    order via a stable sort. Used everywhere the runtime truncates a
    findings list against a cap, so the critical-always-surfaces
    safety rail (docs/ITERATION_AWARENESS.md § 7.1) is preserved
    regardless of the order the LLM (or an agent-runner CLI) emitted
    findings in. Findings with an unknown severity string are treated
    as SEVERITY_INFO (rank 1) so they sort behind warnings/criticals
    but ahead of unranked entries — the safe fallback.
    """
    return sorted(
        findings,
        key=lambda f: SEVERITY_RANK.get(f.severity, SEVERITY_RANK[SEVERITY_INFO]),
        reverse=True,
    )


# Marker embedded in the tracking comment so downstream automation can find
# the most recent review unambiguously, even if other bots also comment.
REVIEW_MARKER: str = "<!-- ai-pr-reviewer-marker -->"

# Per-provider marker embedded in both the tracking comment AND the review
# body. It lets `collapse-previous` scope to "this provider's own prior
# artefacts" so several providers can review the same PR concurrently (with
# one shared GITHUB_TOKEN / bot author) without collapsing each other. See
# docs/PROVIDERS.md § "Running more than one provider on the same PR".
PROVIDER_MARKER_PREFIX: str = "<!-- ai-pr-reviewer-provider:"


def provider_marker(provider_id: str) -> str:
    """The HTML-comment marker identifying which provider produced a comment."""
    return f"{PROVIDER_MARKER_PREFIX} {provider_id} -->"

# Agent-runner findings contract (see AgentRunnerProvider docstring).
# Each CLI provider writes its findings to `<output_dir>/<FINDINGS_JSON_REL>`
# before exiting; `parse_findings_file` reads + validates that file.
FINDINGS_JSON_REL: str = ".aiprr/findings.json"
ALLOWED_SEVERITIES: tuple[str, ...] = (
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    SEVERITY_INFO,
)
ALLOWED_SIDES: tuple[str, ...] = ("LEFT", "RIGHT")

# Timeout for a single agent-runner CLI invocation (seconds). Aligns with the
# recommended workflow `timeout-minutes: 15` in examples/*.yml.
CLI_INVOCATION_TIMEOUT: int = 900

# ---------------------------------------------------------------------------
# Iteration-Aware Review (IAR) — subsystem constants
# ---------------------------------------------------------------------------
# IAR runs on every review. Full spec in docs/ITERATION_AWARENESS.md.

# Convergence policy enum values (docs/ITERATION_AWARENESS.md § 6).
IAR_POLICY_ITERATIVE: str = "iterative"
IAR_POLICY_FIRST_PASS_EXHAUSTIVE: str = "first-pass-exhaustive"
IAR_POLICY_ROUND_CAPPED: str = "round-capped"
IAR_POLICY_CRITICAL_GATE: str = "critical-gate"
IAR_VALID_POLICIES: tuple[str, ...] = (
    IAR_POLICY_ITERATIVE,
    IAR_POLICY_FIRST_PASS_EXHAUSTIVE,
    IAR_POLICY_ROUND_CAPPED,
    IAR_POLICY_CRITICAL_GATE,
)

# Default multiplier applied to max-inline-comments on round 1 of each
# generation when convergence-policy is first-pass-exhaustive
# (docs/ITERATION_AWARENESS.md § 6.2).
IAR_DEFAULT_CAP_MULTIPLIER: int = 3

# Lines above + below a finding anchor included in the context hash for
# fingerprinting (docs/ITERATION_AWARENESS.md § 5.2). 10 above + 10 below
# = 21-line window.
IAR_CONTEXT_HASH_RADIUS: int = 10

# Prefix of the finding body included in the fingerprint payload before
# hashing (docs/ITERATION_AWARENESS.md § 5.2). Trades fingerprint
# stability (short prefix = more collisions across cosmetically
# different findings) against LLM-wording-drift robustness (long prefix
# = re-phrased-same-issue evades dedup). 200 chars covers the typical
# "≤ 3-sentence single-issue" body without pulling in trailing
# quote-block noise; the code-context hash carries the disambiguation
# load for near-collisions on the prefix.
IAR_FINGERPRINT_BODY_PREFIX_CHARS: int = 200

# When a generation change (NEW_COMMITS / REBASED) brings more than this
# percentage of new lines relative to the total diff, the safety net
# forces first-pass-exhaustive for that round regardless of the configured
# policy (docs/ITERATION_AWARENESS.md § 7.2).
IAR_SAFETY_NET_NEW_LINES_PCT: int = 30

# IterationState JSON schema version embedded in the marker state block
# (docs/ITERATION_AWARENESS.md § 12). Increment when the schema breaks
# backward-read compatibility; also extend _parse_state_from_marker_body
# with backward-read logic before incrementing.
IAR_STATE_SCHEMA_VERSION: int = 1

# Default escape label a human can apply to force a full review
# (docs/ITERATION_AWARENESS.md § 8). Consumers can rename via the
# iteration-escape-label input.
IAR_DEFAULT_ESCAPE_LABEL: str = "full-review-please"

# HTML-comment tags that delimit the embedded IterationState JSON block
# inside the tracking marker body. Nested inside REVIEW_MARKER so any
# consumer parser looking for the tracking marker still finds it.
IAR_STATE_TAG_OPEN: str = "<!-- ai-pr-reviewer-iteration-state"
IAR_STATE_TAG_CLOSE: str = "-->"

# Hardcoded prompt addendum spliced into the system prompt on round 1 of
# each generation when convergence-policy is first-pass-exhaustive. NEVER
# sourced from user input — this constant is the security surface
# (docs/ITERATION_AWARENESS.md § 6.2). Kept short; ~150 tokens
# (matches the budget quoted in docs/PROMPTS.md + docs/PERFORMANCE.md).
# ---- Incremental review mode (v2.1.0+) ----
# Rounds 2+ send the model only what changed since its last review plus its
# own prior open findings (read back from the PR's review threads), ask it to
# verify each, and scale the budget to the delta. Every failure path falls
# back to a full review — never to silence.
IAR_MODE_FULL: str = "full"
IAR_MODE_INCREMENTAL: str = "incremental"
# Hidden, STABLE marker appended to every inline comment body so prior
# findings can be matched back from the PR itself (survives collapse; no
# marker-state growth). Never rename (docs/STANDARDS.md § Marker constants).
INLINE_FINDING_MARKER_PREFIX: str = "<!-- ai-pr-reviewer-finding:"
INLINE_FINDING_MARKER_CLOSE: str = " -->"
IAR_INCREMENTAL_MIN_CAP: int = 3
IAR_INCREMENTAL_MIN_TURNS: int = 6
IAR_INCREMENTAL_MIN_DELTA_RATIO: float = 0.1
PRIOR_FINDINGS_MAX_LISTED: int = 40
PRIOR_FINDING_STATUS_RESOLVED: str = "resolved"
PRIOR_FINDING_STATUS_OPEN: str = "open"
PRIOR_FINDING_STATUS_REGRESSED: str = "regressed"
# Prior-finding resolution policy (v2.2.0+). `advisory` (default, byte-identical
# to v2.1.0): the model's `resolved` verdicts are reported but never retire a
# finding — a maintainer resolves the thread. `verified`: a `resolved` verdict
# is honoured only when the runtime can corroborate it (fingerprint absent
# from this round AND the file changed since the last reviewed head or no
# longer exists); the thread is then replied to and resolved.
PRIOR_FINDINGS_RESOLUTION_ENV: str = "AIPRR_PRIOR_FINDINGS_RESOLUTION"
RESOLUTION_POLICY_ADVISORY: str = "advisory"
RESOLUTION_POLICY_VERIFIED: str = "verified"
RESOLUTION_POLICIES: tuple[str, ...] = (RESOLUTION_POLICY_ADVISORY, RESOLUTION_POLICY_VERIFIED)
PRIOR_FINDING_STATUSES: tuple[str, ...] = (
    PRIOR_FINDING_STATUS_RESOLVED,
    PRIOR_FINDING_STATUS_OPEN,
    PRIOR_FINDING_STATUS_REGRESSED,
)
IAR_INCREMENTAL_DIFF_HEADING: str = "## Changes since your last review"
IAR_UNCHANGED_FILES_HEADING: str = (
    "## Other files changed in this PR (unchanged since your last review)"
)
PRIOR_FINDINGS_HEADING: str = "## Your prior findings still open"
IAR_INCREMENTAL_PROMPT_ADDENDUM: str = (
    "\n\n[Iteration-Aware Review — incremental follow-up mode active]\n"
    "You reviewed an earlier revision of this pull request. The user\n"
    "message shows only the hunks that changed since then plus your own\n"
    "prior findings that are still open. For each prior finding decide\n"
    "whether the new commits resolved it, left it open, or made it worse,\n"
    "and report that decision through the prior-findings channel described\n"
    "in the output contract — do NOT re-post an open prior finding as a\n"
    "new one. Review the new hunks with the normal rubric and severity\n"
    "model. Prior critical findings must be addressed first.\n"
)

IAR_EXHAUSTIVE_PROMPT_ADDENDUM: str = (
    "\n\n[Iteration-Aware Review — exhaustive first-pass mode active]\n"
    "This is round 1 of a fresh review generation. Prioritize exhaustive\n"
    "coverage over conciseness: surface every relevant finding you can\n"
    "identify in this diff, up to the increased inline-comments ceiling.\n"
    "Subsequent rounds will dedupe against these findings, so it is\n"
    "preferable to report a superset now than to trickle findings across\n"
    "future rounds. Focus areas, severity model, and output shape are\n"
    "unchanged.\n"
)


# ---------------------------------------------------------------------------
# Logging / utilities
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    """Print a tagged log line to stdout (the workflow log)."""
    sys.stdout.write(f"[ai-diff-reviewer] {msg}\n")
    sys.stdout.flush()


def parse_bool(raw: str, *, default: bool = False) -> bool:
    """Parse a workflow-input string as a bool. Empty = default."""
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def redact_for_log(args: dict[str, Any]) -> dict[str, Any]:
    """Mask tool-arg values whose key looks sensitive before logging."""
    return {
        k: ("***" if any(s in k.lower() for s in LOG_REDACT_SUBSTRINGS) else v)
        for k, v in args.items()
    }


# Registry of literal secret VALUES that must never reach a public surface
# (a PR comment or review body). `redact_for_log` scrubs by key *name*; this
# scrubs by exact value. Populated once in `main()` with the provider API key
# and the GitHub token. Defense-in-depth for the agent-runner path, where a
# prompt-injected vendor CLI could echo its API key into a finding body (see
# docs/SECURITY.md § "Agent-runner providers: residual exfiltration surface").
_SECRET_VALUES: set[str] = set()
# Below this length a "secret" is too short to scrub without risking mangling
# ordinary review prose. Real API keys / tokens are far longer.
MIN_SCRUBBABLE_SECRET_LEN: int = 8


def register_secret(value: str) -> None:
    """Register a secret value for scrubbing from public-facing text."""
    if value and len(value) >= MIN_SCRUBBABLE_SECRET_LEN:
        _SECRET_VALUES.add(value)


def scrub_secrets(text: str) -> str:
    """Replace every registered secret value in `text` with `***`.

    Applied to review summaries, inline-comment bodies, and failure messages
    before they are posted to the PR, so a leaked/echoed key can't surface in
    a public comment even if the model (or a vendor CLI) was tricked into
    embedding it.
    """
    if not text:
        return text
    for secret in _SECRET_VALUES:
        if secret in text:
            text = text.replace(secret, "***")
    return text


def truncate_for_tool(text: str, *, label: str) -> str:
    """Cap tool output so a single bad command can't blow up the prompt.

    Guarantees `len(output.encode("utf-8")) <= MAX_TOOL_OUTPUT_BYTES` by
    reserving space for the truncation notice inside the byte budget.
    """
    if len(text.encode("utf-8")) <= MAX_TOOL_OUTPUT_BYTES:
        return text
    notice: str = (
        f"\n\n[output truncated at {MAX_TOOL_OUTPUT_BYTES} bytes — "
        f"narrow your {label} call (e.g. add path/glob/limit) for full content]"
    )
    body_budget: int = max(0, MAX_TOOL_OUTPUT_BYTES - len(notice.encode("utf-8")))
    truncated: str = text.encode("utf-8")[:body_budget].decode(
        "utf-8", errors="ignore"
    )
    return truncated + notice


def run_cmd(
    args: list[str], *, cwd: str | None = None, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and capture its output as text."""
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def write_action_output(name: str, value: str) -> None:
    """Append a key=value pair to `$GITHUB_OUTPUT` so it surfaces as an
    action output. No-op when run outside Actions (the file env var is
    unset), so the script remains directly invocable for local debugging.

    Multi-line values use the heredoc-style delimiter form documented in
    https://docs.github.com/en/actions/using-workflows/workflow-commands-for-github-actions#multiline-strings —
    we don't need it for the small scalars we emit here, but the path is
    handled defensively in case a future output carries newlines.
    """
    out_path: str | None = os.environ.get("GITHUB_OUTPUT")
    if not out_path:
        return
    with open(out_path, "a", encoding="utf-8") as fh:
        if "\n" in value:
            delim: str = "AIPRR_OUTPUT_EOF"
            fh.write(f"{name}<<{delim}\n{value}\n{delim}\n")
        else:
            fh.write(f"{name}={value}\n")


def write_all_outputs(
    *,
    skipped: bool,
    severity: str = SEVERITY_NONE,
    inline_attached: int = 0,
    inline_dropped: int = 0,
    blocked: bool = False,
    review_url: str = "",
) -> None:
    """Write the complete set of six core action outputs + the five IAR
    outputs (as empty strings) in one call.

    Every exit path — success, skip, and hard failure — routes through here so
    downstream steps never read an empty string for an output they key on
    (e.g. `steps.review.outputs.blocked == 'false'`). Defaults describe the
    "no review produced" state used by the skip and failure paths.

    The five IAR outputs (`iteration-round`, `iteration-generation`,
    `iteration-policy-applied`, `iteration-tokens-used`,
    `iteration-cost-vs-baseline-estimate`) are always written as empty strings
    here as the safety-net default. When the reviewer reaches its IAR-
    populating code path after the LLM call, that path overwrites the five
    values with real data (`$GITHUB_OUTPUT` is append-only; last write wins).
    See docs/ITERATION_AWARENESS.md § 3.2.
    """
    write_action_output("skipped", "true" if skipped else "false")
    write_action_output("severity", severity)
    write_action_output("inline-attached", str(inline_attached))
    write_action_output("inline-dropped", str(inline_dropped))
    write_action_output("blocked", "true" if blocked else "false")
    write_action_output("review-url", review_url)
    write_iar_outputs_empty()


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def gh_request(
    method: str,
    path: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
) -> Any:
    """Call the GitHub REST API and return the parsed JSON response.

    Return type is `Any` rather than `dict[str, Any]` because GitHub's REST
    API legitimately returns both objects (e.g. `/pulls/{n}`) and arrays
    (e.g. `/pulls/{n}/files`) depending on the endpoint. Callers narrow the
    type at the call site.
    """
    url: str = f"{GITHUB_REST_BASE}{path}"
    data: bytes | None = (
        json.dumps(body).encode("utf-8") if body is not None else None
    )
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ai-diff-reviewer",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(request, timeout=GH_REQUEST_TIMEOUT) as response:
        raw: bytes = response.read()
        if not raw:
            return {}
        return json.loads(raw)


def gh_get_collaborator_permission(
    *, token: str, owner: str, repo: str, username: str
) -> tuple[str, bool]:
    """Return effective repo permission and whether the lookup failed.

    Returns ``(permission, lookup_failed)``. ``permission`` is one of
    ``admin|maintain|write|triage|read|none|unknown``. HTTP 404 means the
    user is not a collaborator → ``none`` with ``lookup_failed=False``.
    Other HTTP/network errors → ``unknown`` with ``lookup_failed=True``.
    """
    if not username or not owner or not repo:
        return ("unknown", True)
    try:
        payload: Any = gh_request(
            "GET",
            (
                f"/repos/{owner}/{repo}/collaborators/"
                f"{urllib.parse.quote(username)}/permission"
            ),
            token=token,
        )
        if not isinstance(payload, dict):
            return ("unknown", True)
        permission: str = str(payload.get("permission", "") or "none").lower()
        if permission in (
            "admin",
            "maintain",
            "write",
            "triage",
            "read",
            "none",
        ):
            return (permission, False)
        return ("unknown", False)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ("none", False)
        log(
            f"WARNING: collaborator permission lookup failed for "
            f"{username!r}: HTTP {e.code}"
        )
        return ("unknown", True)
    except Exception as e:  # noqa: BLE001 — best-effort gate lookup
        log(
            f"WARNING: collaborator permission lookup failed for "
            f"{username!r}: {e}"
        )
        return ("unknown", True)


def gh_graphql(query: str, variables: dict[str, Any], *, token: str) -> Any:
    """POST a GraphQL query to GitHub and return the parsed `data` payload."""
    body: bytes = json.dumps({"query": query, "variables": variables}).encode(
        "utf-8"
    )
    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "ai-diff-reviewer",
    }
    request = urllib.request.Request(
        GITHUB_GRAPHQL_URL, data=body, headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=GH_REQUEST_TIMEOUT) as response:
        raw: bytes = response.read()
    payload: dict[str, Any] = json.loads(raw)
    if payload.get("errors"):
        raise RuntimeError(f"GitHub GraphQL errors: {payload['errors']}")
    return payload.get("data", {})


DEFAULT_WORKFLOW_BOT_LOGIN: str = "github-actions[bot]"


def gh_get_authenticated_login(
    token: str, *, repo: str = "", pr_number: int = 0
) -> str:
    """Return the login the token authenticates as, with a 4-tier fallback.

    The naive `GET /user` call fails with `HTTP 403 Forbidden` when the
    caller is the built-in workflow `GITHUB_TOKEN` (an installation
    token, not a user token) — the well-known limitation that silently
    broke `collapse-previous` for every consumer using the recommended
    `github-token: ${{ secrets.GITHUB_TOKEN }}` pattern.

    The fallback chain, tried in order:

    1. `GET /user` — works for PATs and user OAuth tokens.
    2. `GET /app` — works for GitHub App installation tokens; returns
       `<slug>[bot]` (the shape GitHub uses in comment `.user.login`).
    3. Marker-scan the PR's issue comments for our
       `<!-- ai-pr-reviewer-marker -->` tracking comment; take that
       comment's `.user.login`. Works for any bot that previously
       posted here. Requires `repo` + `pr_number`.
    4. Hardcoded `"github-actions[bot]"` — the login of the built-in
       workflow `GITHUB_TOKEN`, which is the overwhelmingly common
       case in the wild.

    Failing all four tiers still returns tier 4's default, so callers
    downstream (`gh_collapse_previous_reviews`) get a login they can
    filter on. Their own error handling covers the case where the
    token is genuinely invalid.
    """
    try:
        me: dict[str, Any] = gh_request("GET", "/user", token=token)
        login: str = str(me.get("login", "") or "")
        if login:
            return login
    except Exception as e:  # noqa: BLE001 — best-effort; try next tier
        log(f"gh_get_authenticated_login: /user tier failed: {e}")

    try:
        app: dict[str, Any] = gh_request("GET", "/app", token=token)
        slug: str = str(app.get("slug", "") or "")
        if slug:
            return f"{slug}[bot]"
    except Exception as e:  # noqa: BLE001 — best-effort; try next tier
        log(f"gh_get_authenticated_login: /app tier failed: {e}")

    if repo and pr_number:
        try:
            owner, name = repo.split("/", 1)
            comments: list[dict[str, Any]] = gh_request(
                "GET",
                (
                    f"/repos/{owner}/{name}/issues/{pr_number}"
                    "/comments?per_page=100"
                ),
                token=token,
            )
            if isinstance(comments, list):
                for comment in reversed(comments):
                    if not isinstance(comment, dict):
                        continue
                    body: str = str(comment.get("body") or "")
                    if REVIEW_MARKER not in body:
                        continue
                    author: str = str(
                        (comment.get("user") or {}).get("login") or ""
                    )
                    if author:
                        return author
        except Exception as e:  # noqa: BLE001 — best-effort; fall through
            log(
                f"gh_get_authenticated_login: marker-scan tier failed: {e}"
            )

    return DEFAULT_WORKFLOW_BOT_LOGIN


def gh_post_issue_comment(
    *, token: str, repo: str, pr_number: int, body: str
) -> int:
    """Post a regular issue comment on the PR; return the new comment id."""
    owner, name = repo.split("/", 1)
    resp: Any = gh_request(
        "POST",
        f"/repos/{owner}/{name}/issues/{pr_number}/comments",
        token=token,
        body={"body": body},
    )
    return int(resp.get("id", 0)) if isinstance(resp, dict) else 0


def gh_update_issue_comment(
    *, token: str, repo: str, comment_id: int, body: str
) -> None:
    """Replace the body of an existing issue comment."""
    if comment_id <= 0:
        return
    owner, name = repo.split("/", 1)
    try:
        gh_request(
            "PATCH",
            f"/repos/{owner}/{name}/issues/comments/{comment_id}",
            token=token,
            body={"body": body},
        )
    except Exception as e:  # noqa: BLE001 — best-effort; do not crash the run
        log(f"Failed to update issue comment {comment_id}: {e}")


def gh_apply_label(
    *, token: str, repo: str, pr_number: int, label: str
) -> None:
    """Apply a single label to a PR. Creates the label on the fly if needed."""
    if not label:
        return
    owner, name = repo.split("/", 1)
    try:
        gh_request(
            "POST",
            f"/repos/{owner}/{name}/issues/{pr_number}/labels",
            token=token,
            body={"labels": [label]},
        )
    except urllib.error.HTTPError as e:
        # 422 here usually means the label doesn't exist yet — try to create
        # it then re-apply. Any other error is logged but non-fatal.
        if e.code == 422:
            try:
                gh_request(
                    "POST",
                    f"/repos/{owner}/{name}/labels",
                    token=token,
                    body={"name": label, "color": "0e8a16"},
                )
                gh_request(
                    "POST",
                    f"/repos/{owner}/{name}/issues/{pr_number}/labels",
                    token=token,
                    body={"labels": [label]},
                )
            except Exception as e2:  # noqa: BLE001
                log(f"Failed to create+apply label {label!r}: {e2}")
        else:
            log(f"Failed to apply label {label!r}: {e}")
    except Exception as e:  # noqa: BLE001
        log(f"Failed to apply label {label!r}: {e}")


def gh_pr_has_label(
    *, token: str, repo: str, pr_number: int, label: str
) -> bool:
    """Return True if the PR currently has the given label.

    Matching is case-insensitive (`ready` == `Ready` == `READY`).
    """
    owner, name = repo.split("/", 1)
    pr: dict[str, Any] = gh_request(
        "GET", f"/repos/{owner}/{name}/pulls/{pr_number}", token=token
    )
    labels: list[dict[str, Any]] = pr.get("labels", []) or []
    target: str = label.strip().lower()
    return any((lbl.get("name") or "").strip().lower() == target for lbl in labels)


def gh_remove_labels_by_prefix(
    *,
    token: str,
    repo: str,
    pr_number: int,
    prefix: str,
    except_label: str = "",
) -> int:
    """Remove all PR labels starting with `prefix`, except `except_label`.

    Returns the number of labels removed. Best-effort — callers wrap in
    try/except so label bookkeeping failures do not crash the review.
    """
    if not prefix:
        return 0
    owner, name = repo.split("/", 1)
    pr: dict[str, Any] = gh_request(
        "GET", f"/repos/{owner}/{name}/pulls/{pr_number}", token=token
    )
    labels: list[dict[str, Any]] = pr.get("labels", []) or []
    removed: int = 0
    for lbl in labels:
        lbl_name: str = (lbl.get("name") or "").strip()
        if not lbl_name.startswith(prefix):
            continue
        if except_label and lbl_name == except_label:
            continue
        try:
            gh_request(
                "DELETE",
                (
                    f"/repos/{owner}/{name}/issues/{pr_number}/labels/"
                    f"{urllib.parse.quote(lbl_name)}"
                ),
                token=token,
            )
            removed += 1
        except Exception as e:  # noqa: BLE001 — best-effort GH API call
            log(f"Could not remove label {lbl_name!r}: {e}")
    return removed


def gh_collapse_previous_reviews(
    *,
    token: str,
    repo: str,
    pr_number: int,
    bot_login: str,
    provider_marker_text: str = "",
) -> int:
    """Mark prior bot reviews/comments as `OUTDATED` via GraphQL.

    Returns the number of nodes minimized. Best-effort: failures are logged
    but the review still proceeds.

    When `provider_marker_text` is non-empty, collapsing is **scoped to this
    provider**: only bot-authored comments/reviews whose body contains that
    marker are minimized. This lets several providers review the same PR
    concurrently (sharing one bot author) without collapsing each other. When
    it is empty, the legacy behaviour applies — every non-minimized comment/
    review by `bot_login` is collapsed.

    `GH_CONNECTION_PAGE_SIZE` (100) is GitHub's hard limit on the `comments`
    and `reviews` connections of a PullRequest. If a PR ever exceeds that many
    non-minimized bot artefacts, switch to cursor pagination rather than
    raising the cap.
    """
    owner, name = repo.split("/", 1)
    page: int = GH_CONNECTION_PAGE_SIZE
    query: str = (
        "query($owner:String!, $repo:String!, $number:Int!, $page:Int!) {"
        "  repository(owner:$owner, name:$repo) {"
        "    pullRequest(number:$number) {"
        "      comments(first:$page) {"
        "        nodes { id isMinimized body author { login } }"
        "      }"
        "      reviews(first:$page) {"
        "        nodes {"
        "          id"
        "          isMinimized"
        "          body"
        "          author { login }"
        "          comments(first:$page) { nodes { id isMinimized } }"
        "        }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    try:
        data: Any = gh_graphql(
            query,
            {"owner": owner, "repo": name, "number": pr_number, "page": page},
            token=token,
        )
    except Exception as e:  # noqa: BLE001
        log(f"Could not list PR comments/reviews for collapsing: {e}")
        return 0

    pr: dict[str, Any] = (
        (data or {}).get("repository", {}) or {}
    ).get("pullRequest", {}) or {}
    issue_comments: list[dict[str, Any]] = (
        pr.get("comments", {}) or {}
    ).get("nodes", []) or []
    reviews: list[dict[str, Any]] = (
        pr.get("reviews", {}) or {}
    ).get("nodes", []) or []

    # GraphQL and REST disagree on the shape of a Bot's login. REST
    # `.user.login` returns `"github-actions[bot]"` (the shape the
    # /user endpoint, comment payloads, and our marker-scan tier all
    # use), but GraphQL `.author.login` on a Bot node returns
    # `"github-actions"` — no `[bot]` suffix. Comparing directly missed
    # every bot node and silently reported "Collapsed 0/N". Accept
    # both shapes so the filter matches regardless of where
    # `bot_login` came from.
    accepted_logins: set[str] = {bot_login}
    if bot_login.endswith("[bot]"):
        accepted_logins.add(bot_login[: -len("[bot]")])

    def _matches(author_login: str) -> bool:
        return author_login in accepted_logins

    def _in_scope(body: str) -> bool:
        """Provider scoping: in scoped mode (`provider_marker_text` set),
        only artefacts carrying this provider's marker are in scope. In
        legacy mode (empty), every bot-authored artefact is in scope."""
        if not provider_marker_text:
            return True
        return provider_marker_text in (body or "")

    targets: list[str] = []
    for c in issue_comments:
        author_login: str = str((c.get("author") or {}).get("login") or "")
        if (
            _matches(author_login)
            and not c.get("isMinimized", False)
            and _in_scope(str(c.get("body") or ""))
        ):
            targets.append(c["id"])
    for r in reviews:
        author_login = str((r.get("author") or {}).get("login") or "")
        if _matches(author_login) and _in_scope(str(r.get("body") or "")):
            if not r.get("isMinimized", False):
                targets.append(r["id"])
            inline: list[dict[str, Any]] = (
                r.get("comments", {}) or {}
            ).get("nodes", []) or []
            for ic in inline:
                if not ic.get("isMinimized", False):
                    targets.append(ic["id"])

    minimize_mutation: str = (
        "mutation($id:ID!) {"
        "  minimizeComment(input:{subjectId:$id, classifier:OUTDATED}) {"
        "    minimizedComment { isMinimized }"
        "  }"
        "}"
    )
    minimized: int = 0
    for node_id in targets:
        try:
            gh_graphql(minimize_mutation, {"id": node_id}, token=token)
            minimized += 1
        except Exception as e:  # noqa: BLE001
            log(f"  could not minimize {node_id}: {e}")
    log(f"Collapsed {minimized}/{len(targets)} previous bot artefact(s)")
    return minimized


def gh_submit_review(
    *,
    token: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    body: str,
    inline_comments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Submit a single PR review with the summary body + batched inline comments."""
    owner, name = repo.split("/", 1)
    payload: dict[str, Any] = {
        "commit_id": head_sha,
        "body": body,
        "event": "COMMENT",
        # The Reviews API accepts inline comments inline. The schema differs
        # from `pulls/{n}/comments`: here you pass `path`, `body`, `line`,
        # `side`, optionally `start_line`/`start_side` for multi-line.
        "comments": inline_comments,
    }
    return gh_request(
        "POST",
        f"/repos/{owner}/{name}/pulls/{pr_number}/reviews",
        token=token,
        body=payload,
    )


def gh_submit_review_with_fallback(
    *,
    token: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    result: "ReviewResult",
) -> tuple[dict[str, Any], int]:
    """Submit the review; on a 422, retry summary-only and report the drop.

    Consumes a provider-independent `ReviewResult`. Encodes findings into the
    GitHub Reviews API inline shape at the boundary so agent-runner providers
    can hand back a `ReviewResult` without knowing the GitHub API schema.

    Returns `(review, dropped_count)`. A 422 from `POST /pulls/{n}/reviews`
    rejects the entire request when any single inline comment points at a
    line outside the PR's diff hunks (off-by-one from the model, file moved,
    multi-line range crossing a hunk boundary, etc.). Without this fallback,
    a single bad line loses the summary and every other queued comment.
    With it we drop the inline comments and post summary-only — the original
    422 body is logged so an operator can see which comment was rejected.
    """
    inline_comments: list[dict[str, Any]] = findings_to_gh_inline_comments(
        result.findings
    )
    try:
        review: dict[str, Any] = gh_submit_review(
            token=token,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            body=result.summary,
            inline_comments=inline_comments,
        )
        return review, 0
    except urllib.error.HTTPError as e:
        if e.code != 422 or not inline_comments:
            raise
        err_body: str = e.read().decode("utf-8", errors="replace")
        log(
            "GitHub rejected the review with HTTP 422 — most likely an inline "
            f"comment referenced a line outside the diff. Retrying with "
            f"summary-only ({len(inline_comments)} inline comment(s) will be "
            f"dropped). Error body: {err_body[:MAX_422_BODY_CHARS]}"
        )
        review = gh_submit_review(
            token=token,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            body=result.summary,
            inline_comments=[],
        )
        return review, len(inline_comments)


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Endpoint profiles — the backend contract (v2.1.0+)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EndpointProfile:
    """Where the model lives and how a runner must talk to it.

    - `kind`: one of `ENDPOINT_KINDS`.
    - `base_url`: normalised base (scheme + host + path, no trailing slash,
      no query/fragment). Empty only for runners without a BYO endpoint.
    - `host`: the hostname (logged; the credential never is).
    - `is_default`: True when `api-base` was empty (byte-identical legacy
      behaviour for the four v1/v2 runners).
    - `supports_anthropic_cache_control`: send `cache_control` blocks only
      to api.anthropic.com; compatible gateways cache server-side.
    - `anthropic_auth_style`: `x-api-key` or `both` (adds a Bearer header for
      gateways that document bearer auth).
    - `openai_auth_style`: `bearer` or `azure` (adds the `api-key` header).
    - `codex_wire_api`: Codex `model_providers.*.wire_api` value.
    - `codex_extra_toml`: extra TOML appended to the Codex provider block
      (Azure image-generation workaround); empty otherwise.
    """

    kind: str
    base_url: str
    host: str
    is_default: bool
    supports_anthropic_cache_control: bool
    anthropic_auth_style: str
    openai_auth_style: str
    codex_wire_api: str
    codex_extra_toml: str


def validate_api_base(raw: str) -> str:
    """Validate and normalise the `api-base` input.

    Empty → `""` (provider default). Otherwise the value must be an absolute
    `https://` URL (plain `http://` is accepted only for loopback hosts, so a
    local dev gateway still works) with a host, no userinfo, no query and no
    fragment. A trailing slash is stripped. Raises `ValueError` with an
    actionable message — the credential in `api-key` is sent to this host,
    so a malformed or ambiguous value must never be guessed at.
    """
    if any(ord(char) < 32 or ord(char) == 127 for char in (raw or "")):
        raise ValueError("api-base must not contain control characters.")
    value: str = (raw or "").strip()
    if not value:
        return ""
    parts = urllib.parse.urlsplit(value)
    host: str = parts.hostname or ""
    if not parts.scheme or not host:
        raise ValueError(
            f"api-base {value!r} is not an absolute URL — expected e.g. "
            f"{ZAI_ANTHROPIC_COMPAT_API_BASE!r} or {XAI_OPENAI_COMPAT_API_BASE!r}."
        )
    scheme: str = parts.scheme.lower()
    if scheme not in API_BASE_ALLOWED_SCHEMES and not (
        scheme == "http" and host in API_BASE_LOCAL_HOSTS
    ):
        raise ValueError(
            f"api-base {value!r} must use https:// (plain http is allowed "
            f"only for {', '.join(API_BASE_LOCAL_HOSTS)})."
        )
    if parts.username is not None or parts.password is not None:
        raise ValueError(
            "api-base must not embed credentials (user:pass@host); pass the "
            "key via the `api-key` input."
        )
    if not host.isascii():
        # Internationalised hostnames are classified and sent as typed;
        # a homoglyph host would look like a vendor domain in logs while
        # resolving elsewhere. Require the explicit punycode (`xn--`) form.
        raise ValueError(
            f"api-base host {host!r} must be ASCII — use the punycode "
            "(`xn--…`) form of an internationalised domain."
        )
    if parts.query or parts.fragment:
        raise ValueError(
            f"api-base {value!r} must not carry a query string or fragment."
        )
    path: str = parts.path.rstrip("/")
    netloc: str = parts.netloc
    return f"{scheme}://{netloc}{path}"


def review_scope_id(provider_id: str, api_base: str) -> str:
    """Separate custom backend state while retaining historical default markers."""
    if not api_base:
        return provider_id
    endpoint: str = validate_api_base(api_base)
    digest: str = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
    return f"{provider_id}:{digest}"


def classify_endpoint_host(host: str) -> str:
    """Map a hostname to an endpoint kind via `ENDPOINT_HOST_SUFFIXES`."""
    h: str = (host or "").lower()
    for suffix, kind in ENDPOINT_HOST_SUFFIXES:
        if suffix.startswith("."):
            if h.endswith(suffix):
                return kind
        elif h == suffix:
            return kind
    return ENDPOINT_KIND_CUSTOM


def _profile_for_kind(
    kind: str, *, base_url: str, host: str, is_default: bool
) -> EndpointProfile:
    """Build the profile carrying the per-kind quirks."""
    extra_toml: str = ""
    if kind == ENDPOINT_KIND_AZURE:
        extra_toml = (
            "http_headers = { "
            f'"{AZURE_IMAGE_GEN_HEADER}" = "{AZURE_IMAGE_GEN_DUMMY_DEPLOYMENT}"'
            " }\n"
            "\n"
            "[features]\n"
            "image_generation = false\n"
        )
    return EndpointProfile(
        kind=kind,
        base_url=base_url,
        host=host,
        is_default=is_default,
        supports_anthropic_cache_control=(kind == ENDPOINT_KIND_ANTHROPIC),
        anthropic_auth_style=(
            ANTHROPIC_AUTH_STYLE_X_API_KEY
            if kind == ENDPOINT_KIND_ANTHROPIC
            else ANTHROPIC_AUTH_STYLE_BOTH
        ),
        openai_auth_style=(
            OPENAI_AUTH_STYLE_AZURE
            if kind == ENDPOINT_KIND_AZURE
            else OPENAI_AUTH_STYLE_BEARER
        ),
        codex_wire_api=CODEX_WIRE_API_RESPONSES,
        codex_extra_toml=extra_toml,
    )


def resolve_endpoint_profile(api_base: str, provider_id: str) -> EndpointProfile:
    """Resolve the backend profile for a runner.

    Empty `api_base` → the runner's default profile (`is_default=True`).
    Otherwise the host is classified; unknown hosts become `custom` (plain
    protocol behaviour for the runner's family). Never raises on
    classification — `validate_api_base` is the place that rejects input.
    """
    if not api_base:
        kind: str = PROVIDER_DEFAULT_ENDPOINT_KIND.get(
            provider_id, ENDPOINT_KIND_CUSTOM
        )
        base: str = PROVIDER_DEFAULT_API_BASE.get(provider_id, "")
        host: str = urllib.parse.urlsplit(base).hostname or "" if base else ""
        return _profile_for_kind(
            kind, base_url=base, host=host, is_default=True
        )
    parts = urllib.parse.urlsplit(api_base)
    host = parts.hostname or ""
    return _profile_for_kind(
        classify_endpoint_host(host),
        base_url=api_base,
        host=host,
        is_default=False,
    )


def log_backend_selection(profile: EndpointProfile) -> None:
    """One log line naming the backend; a WARNING when the host is not a
    recognised vendor, because the `api-key` credential is sent to it."""
    log(
        f"Backend: kind={profile.kind} host={profile.host or 'default'}"
        + ("" if profile.is_default else " (custom api-base)")
    )
    if not profile.is_default and profile.kind == ENDPOINT_KIND_CUSTOM:
        log(
            f"WARNING: api-base host {profile.host!r} is not a recognised "
            "vendor endpoint. The `api-key` credential will be sent to this "
            "host on every request — make sure you control it or trust it "
            "(gateway / proxy). See docs/SECURITY.md § \"Custom endpoints\"."
        )


def join_endpoint_path(base_url: str, path: str) -> str:
    """Join a backend base URL and a protocol path without doubling the
    version segment: `https://api.anthropic.com/v1` + `/v1/messages` →
    `…/v1/messages` (many vendor docs show the base *with* `/v1`; the
    canonical values in docs/PROVIDERS.md are without). A base that does
    not end in the path's leading segment is joined verbatim."""
    base: str = base_url.rstrip("/")
    first_segment: str = "/" + path.lstrip("/").split("/", 1)[0]
    if path.startswith(first_segment + "/") and base.endswith(first_segment):
        return base + path[len(first_segment):]
    return base + path


def resolve_model(
    provider_id: str, profile: EndpointProfile, raw_model: str
) -> str:
    """Resolve the `model` input to a concrete model id.

    - empty → `DEFAULT_MODELS[provider_id]` (unchanged legacy behaviour);
    - a tier word (`balanced` / `economy` / `deep`, case-insensitive) → the
      `MODEL_TIER_TABLE` row for `(provider_id, profile.kind)`; Azure and
      custom hosts have no rows and raise with guidance;
    - anything else → passed through as an explicit model id.
    Logs the resolution so the effective model is always visible.
    """
    value: str = (raw_model or "").strip()
    if (
        not value
        and not profile.is_default
        and provider_id not in PROVIDERS_WITHOUT_API_BASE_LANE
    ):
        # A runner's built-in default names the runner's own vendor model;
        # sending it to another backend is silently wrong (Z.ai would get
        # `claude-sonnet-4-6`, Azure `gpt-5.6-luna` as a deployment name).
        expected: str = MODEL_REQUIRED_HINTS.get(
            profile.kind, "the gateway's model id"
        )
        raise ValueError(
            f"model is required when provider {provider_id!r} runs on "
            f"api-base {profile.base_url!r} ({profile.kind}); the built-in "
            f"default is a {PROVIDER_DEFAULT_ENDPOINT_KIND.get(provider_id, 'vendor')} "
            f"model. Set `model` to {expected}, or to a tier alias "
            f"(`{MODEL_TIER_BALANCED}` / `{MODEL_TIER_ECONOMY}` / "
            f"`{MODEL_TIER_DEEP}`) where the backend has tier rows."
        )
    if not value:
        default: str = DEFAULT_MODELS.get(provider_id, "")
        hint: str = LEGACY_DEFAULT_MODEL_HINTS.get(default, "")
        if default and hint and profile.is_default:
            log(
                f"Model: {default} (built-in default, kept for compatibility). "
                f"Tip: `model: {MODEL_TIER_BALANCED}` selects {hint}, the "
                "current and cheaper balanced tier — see docs/PROVIDERS.md."
            )
        elif default:
            log(f"Model: {default} (built-in default)")
        return default
    tier: str = value.lower()
    if tier in MODEL_TIERS:
        row: dict[str, str] | None = MODEL_TIER_TABLE.get(
            (provider_id, profile.kind)
        )
        if row is None:
            raise ValueError(
                f"model tier {value!r} has no entry for provider "
                f"{provider_id!r} on backend kind {profile.kind!r} "
                f"(host {profile.host or 'default'}). Azure deployments and "
                "custom gateways name their own models — set `model` to the "
                "explicit id or deployment name."
            )
        resolved: str = row[tier]
        log(f"Model: {resolved} (tier={tier}, backend={profile.kind})")
        return resolved
    log(f"Model: {value} (explicit)")
    return value


def parse_agent_max_turns(raw: str) -> int:
    """`agent-max-turns` → non-negative int (0 = unset). Junk is an error."""
    value: str = (raw or "").strip()
    if not value:
        return 0
    try:
        turns: int = int(value)
    except ValueError as e:
        raise ValueError(
            f"agent-max-turns must be a whole number, got {value!r}."
        ) from e
    if turns < 0:
        raise ValueError(f"agent-max-turns must not be negative, got {turns}.")
    return turns


@dataclass
class UsageTelemetry:
    """Token/cost usage for one review, accumulated across turns.

    `source` ∈ {`api`, `cli`, `estimated`, `unavailable`}; `cost_usd` is the
    vendor-reported cost when the CLI gives one, else an indicative estimate
    (`estimate_cost_usd`) or None. Never gate CI on these numbers.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    turns: int = 0
    cost_usd: float | None = None
    source: str = USAGE_SOURCE_UNAVAILABLE

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.output_tokens

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def cached_ratio(self) -> float:
        denominator: int = self.total_input_tokens
        return (self.cache_read_tokens / denominator) if denominator else 0.0

    def add(self, other: "UsageTelemetry") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.turns += other.turns
        if other.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + other.cost_usd
        if other.source != USAGE_SOURCE_UNAVAILABLE:
            self.source = other.source


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalise_usage(raw: Any) -> UsageTelemetry | None:
    """Map any vendor `usage` object to `UsageTelemetry` (one call).

    Accepts the Anthropic / Claude Code / Grok key set (`input_tokens`,
    `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`),
    the OpenAI key set (`prompt_tokens`, `completion_tokens`,
    `prompt_tokens_details.cached_tokens`) and the Codex `--json` key set
    (`input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`,
    `output_tokens`). Returns None when nothing usable is present.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    if "prompt_tokens" in raw or "completion_tokens" in raw:
        details: Any = raw.get("prompt_tokens_details") or {}
        cached: int = (
            _as_int(details.get("cached_tokens")) if isinstance(details, dict) else 0
        )
        return UsageTelemetry(
            input_tokens=max(_as_int(raw.get("prompt_tokens")) - cached, 0),
            output_tokens=_as_int(raw.get("completion_tokens")),
            cache_read_tokens=cached,
            turns=1,
            source=USAGE_SOURCE_API,
        )
    if "input_tokens" in raw or "output_tokens" in raw:
        cache_read: int = _as_int(
            raw.get("cache_read_input_tokens", raw.get("cached_input_tokens"))
        )
        cache_write: int = _as_int(
            raw.get(
                "cache_creation_input_tokens", raw.get("cache_write_input_tokens")
            )
        )
        input_tokens: int = _as_int(raw.get("input_tokens"))
        if "cached_input_tokens" in raw:
            # Codex includes cached input in input_tokens, unlike Anthropic's
            # disjoint input/cache-read/cache-creation partitions.
            input_tokens = max(input_tokens - cache_read - cache_write, 0)
        return UsageTelemetry(
            input_tokens=input_tokens,
            output_tokens=_as_int(raw.get("output_tokens")),
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            turns=1,
            source=USAGE_SOURCE_API,
        )
    return None


def lookup_indicative_price(model: str) -> tuple[float, float] | None:
    """Longest-prefix match into `INDICATIVE_PRICES_USD_PER_MTOK`."""
    best: str = ""
    for prefix in INDICATIVE_PRICES_USD_PER_MTOK:
        if model.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return INDICATIVE_PRICES_USD_PER_MTOK.get(best) if best else None


def estimate_cost_usd(model: str, usage: UsageTelemetry) -> float | None:
    """Indicative cost from list prices; None when the model is unknown."""
    price: tuple[float, float] | None = lookup_indicative_price(model or "")
    if price is None:
        return None
    in_price, out_price = price
    cost: float = (
        usage.input_tokens * in_price
        + usage.cache_read_tokens * in_price * CACHE_READ_PRICE_FACTOR
        + usage.cache_write_tokens * in_price * CACHE_WRITE_PRICE_FACTOR
        + usage.output_tokens * out_price
    ) / 1_000_000
    return round(cost, 6)


def _scan_json_lines(stdout: str) -> list[dict[str, Any]]:
    """Parse JSON objects line by line from a (bounded) stdout tail."""
    text: str = stdout[-CLI_STDOUT_SCAN_MAX_BYTES:] if stdout else ""
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def parse_claude_code_usage(stdout: str) -> UsageTelemetry | None:
    """Claude Code `--output-format stream-json`: the final `result` event
    carries `usage` (Anthropic key set) and `total_cost_usd`."""
    result: dict[str, Any] | None = None
    for obj in _scan_json_lines(stdout):
        if obj.get("type") == "result":
            result = obj
    if result is None:
        return None
    usage: UsageTelemetry | None = normalise_usage(result.get("usage"))
    if usage is None:
        return None
    usage.source = USAGE_SOURCE_CLI
    usage.turns = _as_int(result.get("num_turns")) or usage.turns
    cost: Any = result.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        usage.cost_usd = float(cost)
    return usage


def parse_codex_usage(stdout: str) -> UsageTelemetry | None:
    """Codex `exec --json`: one `turn.completed` event per turn with `usage`
    (`input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`,
    `output_tokens`). Summed across turns; Codex reports no cost."""
    total: UsageTelemetry | None = None
    for obj in _scan_json_lines(stdout):
        if obj.get("type") != "turn.completed":
            continue
        one: UsageTelemetry | None = normalise_usage(obj.get("usage"))
        if one is None:
            continue
        if total is None:
            total = UsageTelemetry(source=USAGE_SOURCE_CLI)
        total.add(one)
        total.source = USAGE_SOURCE_CLI
    return total


def parse_grok_usage(stdout: str) -> UsageTelemetry | None:
    """Grok `--output-format json`: a single JSON document (possibly
    pretty-printed) with `usage`, `num_turns` and `total_cost_usd`."""
    text: str = (stdout or "")[-CLI_STDOUT_SCAN_MAX_BYTES:].strip()
    doc: Any = None
    if text.startswith("{"):
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            doc = None
    if not isinstance(doc, dict):
        # Fall back to a JSON-lines scan (streaming formats).
        for obj in _scan_json_lines(stdout):
            if isinstance(obj.get("usage"), dict):
                doc = obj
    if not isinstance(doc, dict):
        return None
    usage: UsageTelemetry | None = normalise_usage(doc.get("usage"))
    if usage is None:
        return None
    usage.source = USAGE_SOURCE_CLI
    usage.turns = _as_int(doc.get("num_turns")) or usage.turns
    cost: Any = doc.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        usage.cost_usd = float(cost)
    return usage


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def format_usage_line(
    usage: UsageTelemetry | None, *, model: str, wall_clock_ms: int
) -> str:
    """Human line for the tracking comment / log. Honest about the source:
    no `$` when nothing is known, `(indicative)` when estimated."""
    if usage is None or usage.source == USAGE_SOURCE_UNAVAILABLE:
        secs: str = f" · {wall_clock_ms // 1000}s" if wall_clock_ms else ""
        return f"**Usage:** not reported by this provider{secs}"
    parts: list[str] = []
    cached: str = (
        f" ({usage.cached_ratio:.0%} cached)" if usage.cache_read_tokens else ""
    )
    parts.append(f"{_fmt_tokens(usage.total_input_tokens)} in{cached}")
    parts.append(f"{_fmt_tokens(usage.output_tokens)} out")
    if usage.cost_usd is not None:
        label: str = "" if usage.source == USAGE_SOURCE_CLI else " (indicative)"
        parts.append(f"est. ${usage.cost_usd:.2f}{label}" if usage.cost_usd >= 0.005 else f"est. <$0.01{label}")
    if usage.turns:
        parts.append(f"{usage.turns} turn{'s' if usage.turns != 1 else ''}")
    if wall_clock_ms:
        parts.append(f"{wall_clock_ms // 1000}s")
    return "**Usage:** " + " · ".join(parts)


class ProviderRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward provider credentials or private review context to a redirect."""

    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str,
        headers: Any, newurl: str,
    ) -> urllib.request.Request | None:
        raise urllib.error.HTTPError(
            req.full_url, code, "Provider redirects are disabled; configure api-base directly",
            headers, fp,
        )


def _post_json_with_retries(
    *, url: str, body: bytes, headers: dict[str, str], api_label: str
) -> dict[str, Any]:
    """POST a JSON body and return the decoded JSON response.

    Shared by the chat-completions providers: bounded retries on 429 and
    5xx (`API_RETRY_DELAYS_S`), immediate failure on other HTTP errors,
    `API_REQUEST_TIMEOUT` per attempt. `api_label` names the endpoint kind
    and host in logs/errors — never the credential.
    """
    last_error: Exception | None = None
    opener: urllib.request.OpenerDirector = urllib.request.build_opener(ProviderRedirectHandler())
    for attempt, delay in enumerate((0,) + API_RETRY_DELAYS_S):
        if delay:
            log(f"{api_label} retry attempt {attempt} after {delay}s")
            time.sleep(delay)
        request = urllib.request.Request(
            url, data=body, headers=headers, method="POST"
        )
        try:
            with opener.open(
                request, timeout=API_REQUEST_TIMEOUT
            ) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            err_body: str = e.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(
                f"{api_label} HTTP {e.code}: {err_body[:MAX_ERROR_BODY_CHARS]}"
            )
            if e.code != 429 and not (500 <= e.code < 600):
                raise last_error
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = RuntimeError(f"{api_label} network error: {e}")
    assert last_error is not None
    raise last_error


class Provider:
    """Minimal interface every LLM provider must implement.

    The action treats the provider as a black box that takes the same
    Anthropic-shaped payload (system prompt, message history, tools) and
    returns the same Anthropic-shaped response (`stop_reason`, `content`
    blocks of `text` / `tool_use`). When we add OpenAI/Gemini we'll
    translate at the provider boundary so the rest of the code is
    unchanged.
    """

    def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        raise NotImplementedError


def _with_first_user_cache_breakpoint(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a COPY of `messages` whose first user message carries a
    `cache_control` breakpoint on its last text block.

    Anthropic caches prefixes in order tools → system → messages; the system
    breakpoint alone leaves the diff-bearing first user message (up to
    `MAX_DIFF_CHARS`) re-billed on every turn. `drive_review` never prunes
    message 0, so the prefix stays stable across the loop and turns 2..N
    read it from cache. The caller's list is never mutated — the in-memory
    conversation stays plain.
    """
    if not messages or messages[0].get("role") != "user":
        return messages
    first: dict[str, Any] = messages[0]
    content: Any = first.get("content")
    if isinstance(content, str):
        new_content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": content,
                "cache_control": dict(ANTHROPIC_CACHE_CONTROL),
            }
        ]
    elif isinstance(content, list) and content:
        new_content = [dict(b) if isinstance(b, dict) else b for b in content]
        for block in reversed(new_content):
            if isinstance(block, dict) and block.get("type") == "text":
                block["cache_control"] = dict(ANTHROPIC_CACHE_CONTROL)
                break
    else:
        return messages
    return [{**first, "content": new_content}] + list(messages[1:])


def _log_usage(api_label: str, resp: dict[str, Any]) -> None:
    """One compact usage line per call (Anthropic or OpenAI key sets)."""
    usage: Any = resp.get("usage")
    if not isinstance(usage, dict) or not usage:
        return
    if "input_tokens" in usage or "output_tokens" in usage:
        log(
            f"{api_label} usage: in={usage.get('input_tokens', 0)} "
            f"cache_read={usage.get('cache_read_input_tokens', 0)} "
            f"cache_write={usage.get('cache_creation_input_tokens', 0)} "
            f"out={usage.get('output_tokens', 0)}"
        )
        return
    details: Any = usage.get("prompt_tokens_details") or {}
    cached: Any = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
    log(
        f"{api_label} usage: in={usage.get('prompt_tokens', 0)} "
        f"cache_read={cached} out={usage.get('completion_tokens', 0)}"
    )


class AnthropicProvider(Provider):
    """Anthropic Messages API client with prompt caching + bounded retries.

    Honours `api-base`: any Anthropic-compatible backend (Z.ai GLM, xAI) via
    the resolved `EndpointProfile` — URL composition, auth header style and
    whether the `cache_control` breakpoint is sent. The default profile is
    byte-identical to the pre-`api-base` request.
    """

    PROVIDER_ID: str = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        profile: EndpointProfile | None = None,
    ) -> None:
        self.api_key: str = api_key
        self.model: str = model
        # Backend profile (Task 1 of the multi-backend plan stores it; the
        # request path honours it from Task 2 on). `None` = default endpoint.
        self.profile: EndpointProfile = (
            profile
            if profile is not None
            else resolve_endpoint_profile("", self.PROVIDER_ID)
        )

    def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # Cache the system prompt — it's stable across the loop's many
        # iterations and is by far the largest static input. The breakpoint
        # is only sent to profiles that support it (api.anthropic.com).
        system_block: dict[str, Any] = {"type": "text", "text": system_prompt}
        if self.profile.supports_anthropic_cache_control:
            system_block["cache_control"] = dict(ANTHROPIC_CACHE_CONTROL)
        # Second breakpoint: the diff-bearing first user message (see
        # `_with_first_user_cache_breakpoint`). Only where the profile
        # supports cache_control; the caller's list is never mutated.
        wire_messages: list[dict[str, Any]] = (
            _with_first_user_cache_breakpoint(messages)
            if self.profile.supports_anthropic_cache_control
            else messages
        )
        body: bytes = json.dumps(
            {
                "model": self.model,
                "max_tokens": ANTHROPIC_MAX_TOKENS,
                "system": [system_block],
                "messages": wire_messages,
                "tools": tools,
            }
        ).encode("utf-8")
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        if self.profile.anthropic_auth_style == ANTHROPIC_AUTH_STYLE_BOTH:
            # Anthropic-compatible gateways (Z.ai documents bearer auth, xAI
            # documents x-api-key); sending both is harmless and avoids a
            # per-gateway matrix.
            headers["Authorization"] = f"Bearer {self.api_key}"
        url: str = join_endpoint_path(self.profile.base_url, ANTHROPIC_MESSAGES_PATH)
        api_label: str = (
            "Anthropic API"
            if self.profile.is_default
            else f"{self.profile.kind} messages API ({self.profile.host})"
        )
        resp: dict[str, Any] = _post_json_with_retries(
            url=url, body=body, headers=headers, api_label=api_label
        )
        _log_usage(api_label, resp)
        return resp


# ---------------------------------------------------------------------------
# OpenAI-compatible chat-completions runner (`provider: openai`, v2.1.0+)
# ---------------------------------------------------------------------------
# Translation at the boundary: the loop in `drive_review()` keeps the
# Anthropic shape (content blocks `text` / `tool_use`, user `tool_result`
# blocks); these helpers convert to/from the chat-completions wire format.


def anthropic_tools_to_openai(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Anthropic `{name, description, input_schema}` → OpenAI function tools."""
    out: list[dict[str, Any]] = []
    for tool in tools:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get(
                        "input_schema", {"type": "object", "properties": {}}
                    ),
                },
            }
        )
    return out


def _blocks_text(blocks: list[dict[str, Any]]) -> str:
    """Concatenate the `text` blocks of an Anthropic content list."""
    return "\n".join(
        str(b.get("text", ""))
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    )


def anthropic_messages_to_openai(
    system_prompt: str, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Anthropic-shaped conversation → OpenAI chat-completions messages.

    - The system prompt becomes a leading `system` message.
    - A string user message passes through.
    - An assistant turn with content blocks becomes ONE assistant message
      whose `content` is the concatenated text (or `None`) and whose
      `tool_calls` carry every `tool_use` block (arguments JSON-encoded).
    - A user turn made of `tool_result` blocks becomes one `tool` message
      PER result, in order, each keyed by `tool_call_id` — chat-completions
      requires each result as its own message right after the assistant
      turn that requested it.
    """
    out: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for message in messages:
        role: str = str(message.get("role", "user"))
        content: Any = message.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        blocks: list[dict[str, Any]] = [
            b for b in (content or []) if isinstance(b, dict)
        ]
        if role == "assistant":
            tool_calls: list[dict[str, Any]] = []
            for b in blocks:
                if b.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "id": b.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": b.get("name", ""),
                                "arguments": json.dumps(b.get("input", {})),
                            },
                        }
                    )
            text: str = _blocks_text(blocks)
            assistant: dict[str, Any] = {
                "role": "assistant",
                "content": text if text else None,
            }
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            out.append(assistant)
            continue
        # user turn: tool results first (each its own message), then text
        texts: list[str] = []
        for b in blocks:
            if b.get("type") == "tool_result":
                result_content: Any = b.get("content", "")
                if isinstance(result_content, list):
                    result_content = _blocks_text(result_content)
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": b.get("tool_use_id", ""),
                        "content": str(
                            result_content if result_content is not None else ""
                        ),
                    }
                )
            elif b.get("type") == "text":
                texts.append(str(b.get("text", "")))
        if texts:
            out.append({"role": "user", "content": "\n".join(texts)})
    return out


def openai_response_to_anthropic(resp: dict[str, Any]) -> dict[str, Any]:
    """OpenAI chat-completions response → Anthropic-shaped response.

    `choices[0].message.content` → one `text` block (when non-empty);
    every `tool_calls[]` entry → a `tool_use` block with `input` parsed
    from the JSON `arguments` (malformed arguments become
    `{"_raw_arguments": …, "_error": …}` so `execute_tool` surfaces the
    problem to the model instead of crashing the loop). `finish_reason`
    maps through `OPENAI_FINISH_REASON_TO_STOP_REASON`; any tool call
    forces `tool_use`. The raw `usage` object is preserved for telemetry.
    """
    choices: list[dict[str, Any]] = resp.get("choices") or []
    if not choices:
        raise RuntimeError(
            "chat-completions response carried no choices: "
            f"{json.dumps(resp)[:MAX_ERROR_BODY_CHARS]}"
        )
    choice: dict[str, Any] = choices[0] or {}
    message: dict[str, Any] = choice.get("message") or {}
    blocks: list[dict[str, Any]] = []
    content: Any = message.get("content")
    if isinstance(content, list):
        content = _blocks_text(
            [
                {"type": "text", "text": part.get("text", "")}
                for part in content
                if isinstance(part, dict)
            ]
        )
    if isinstance(content, str) and content:
        blocks.append({"type": "text", "text": content})
    for index, call in enumerate(message.get("tool_calls") or []):
        function: dict[str, Any] = call.get("function") or {}
        raw_args: Any = function.get("arguments", "{}")
        parsed: Any
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args or "{}")
            except json.JSONDecodeError as e:
                parsed = {
                    "_raw_arguments": raw_args,
                    "_error": f"malformed JSON tool arguments: {e}",
                }
        else:
            parsed = raw_args
        if not isinstance(parsed, dict):
            parsed = {"_raw_arguments": raw_args}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or f"call_{index}",
                "name": function.get("name", ""),
                "input": parsed,
            }
        )
    finish_reason: str = str(choice.get("finish_reason") or "")
    stop_reason: str = OPENAI_FINISH_REASON_TO_STOP_REASON.get(
        finish_reason, finish_reason or "end_turn"
    )
    if any(b.get("type") == "tool_use" for b in blocks):
        stop_reason = "tool_use"
    return {
        "stop_reason": stop_reason,
        "content": blocks,
        "usage": resp.get("usage") or {},
        "model": resp.get("model", ""),
    }


class OpenAIProvider(Provider):
    """OpenAI-compatible chat-completions client (`provider: openai`).

    Zero install, bounded turns. Through `api-base` it covers OpenAI, Azure
    Foundry (v1 endpoint), xAI, Z.ai and self-hosted gateways. Auth is
    `Authorization: Bearer`; the Azure profile additionally sends the
    `api-key` header. The output ceiling parameter name follows the
    endpoint kind (`max_completion_tokens` on OpenAI/Azure, `max_tokens`
    elsewhere). Retries mirror `AnthropicProvider`.
    """

    PROVIDER_ID: str = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        profile: EndpointProfile | None = None,
    ) -> None:
        self.api_key: str = api_key
        self.model: str = model
        self.profile: EndpointProfile = (
            profile
            if profile is not None
            else resolve_endpoint_profile("", self.PROVIDER_ID)
        )

    def build_request_body(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """The chat-completions payload (pure — unit-tested directly)."""
        max_param: str = OPENAI_MAX_TOKENS_PARAM_BY_KIND.get(
            self.profile.kind, OPENAI_MAX_TOKENS_PARAM_DEFAULT
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages_to_openai(system_prompt, messages),
            max_param: OPENAI_MAX_TOKENS,
        }
        if tools:
            payload["tools"] = anthropic_tools_to_openai(tools)
            payload["tool_choice"] = OPENAI_TOOL_CHOICE_AUTO
        return payload

    def build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if self.profile.openai_auth_style == OPENAI_AUTH_STYLE_AZURE:
            headers[OPENAI_AZURE_API_KEY_HEADER] = self.api_key
        return headers

    def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        body: bytes = json.dumps(
            self.build_request_body(
                system_prompt=system_prompt, messages=messages, tools=tools
            )
        ).encode("utf-8")
        url: str = join_endpoint_path(self.profile.base_url, OPENAI_CHAT_COMPLETIONS_PATH)
        api_label: str = (
            f"{self.profile.kind} chat completions API ({self.profile.host})"
        )
        raw: dict[str, Any] = _post_json_with_retries(
            url=url, body=body, headers=self.build_headers(), api_label=api_label
        )
        _log_usage(api_label, raw)
        return openai_response_to_anthropic(raw)


class AgentRunnerProvider:
    """Provider that delegates the full review to a vendor's coding-agent CLI.

    Unlike `Provider` (chat-completions family — this action owns the tool-use
    loop), an `AgentRunnerProvider` hands off the entire agentic loop to the
    vendor CLI running in headless mode and receives structured findings via a
    file-based contract (`.aiprr/findings.json` — see `parse_findings_file`).

    Concrete implementations (`ClaudeCodeProvider`, `CursorProvider`,
    `CodexProvider`) live below this class.
    """

    def install(self) -> None:
        """Sanity-check that the CLI is on PATH.

        The composite action installs the CLI in a preceding step; this
        method is a defensive verification, not the install itself.
        """
        raise NotImplementedError

    def run_review(
        self,
        *,
        pr_context: PRContext,
        review_instructions: str,
        workspace: Path,
        output_dir: Path,
        require_complexity_in_findings: bool = False,
        max_inline_comments: int = 0,
    ) -> ReviewResult:
        """Invoke the vendor CLI headless; return a ReviewResult."""
        raise NotImplementedError


def _swap_mcp_config(
    src_file: str, dest_path: Path
) -> tuple[Path | None, str | None]:
    """Copy an MCP config to a CLI's expected location, backing up the previous.

    Returns `(dest_path_or_None, backup_content_or_None)` so the caller can
    restore/delete on exit. If `src_file` is empty, both return values are
    `None` — a no-op that the finally block can safely handle.
    """
    if not src_file:
        return None, None
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    backup: str | None = None
    if dest_path.exists():
        backup = dest_path.read_text(encoding="utf-8")
    shutil.copyfile(src_file, dest_path)
    return dest_path, backup


def _restore_mcp_config(dest_path: Path | None, backup: str | None) -> None:
    """Restore or delete the MCP config after a CLI invocation."""
    if dest_path is None:
        return
    if backup is not None:
        dest_path.write_text(backup, encoding="utf-8")
    else:
        dest_path.unlink(missing_ok=True)


# Environment variables the vendor CLIs need to function on ubuntu-latest.
# Everything else (notably AIPRR_GH_TOKEN and every other AIPRR_* secret)
# stays in the parent process. See docs/SECURITY.md and Security Review §2.
_CLI_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TERM",
    "SHELL",
    # Node.js CLIs (@anthropic-ai/claude-code, @openai/codex).
    "NODE_PATH",
    "NPM_CONFIG_PREFIX",
    "NODE_OPTIONS",
    # GitHub Actions runner metadata (harmless; useful for debug output).
    "RUNNER_OS",
    "RUNNER_ARCH",
    "GITHUB_ACTIONS",
    "CI",
    # Outbound-proxy configuration — a CLI on a corporate / self-hosted
    # runner behind a proxy can't reach its vendor API without these. They
    # are non-secret network config, not credentials.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    # Custom / self-hosted API endpoints for the vendor CLIs (e.g. an
    # Anthropic- or OpenAI-compatible gateway). Non-secret base URLs.
    "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL",
)


_INHERITED_BASE_URL_VARS: tuple[str, ...] = ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL")


def _build_cli_env(
    *, extra_vars: dict[str, str], allow_inherited_base_urls: bool = True
) -> dict[str, str]:
    """Build a scrubbed environment for a vendor-CLI subprocess.

    Forwards only variables the CLI likely needs to function (PATH,
    HOME, locale, Node.js paths). Adds `extra_vars` on top (typically
    the vendor-specific API key). Everything else — notably the
    consumer's GitHub token and any other secrets in the workflow's
    env: block — stays in the parent process.

    `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` inherited from the workflow env
    are the pre-2.1.0 bring-your-own-endpoint hook. They are forwarded only
    on a runner's default profile (`allow_inherited_base_urls=True`), after
    `validate_api_base` (an invalid value aborts) and with a WARNING naming
    the host, because the credential follows them. On a custom `api-base`
    the profile's own value wins and the inherited ones are dropped.
    """
    scrubbed: dict[str, str] = {}
    for name in _CLI_ENV_ALLOWLIST:
        val: str | None = os.environ.get(name)
        if val is None:
            continue
        if name in _INHERITED_BASE_URL_VARS and name not in extra_vars:
            if not allow_inherited_base_urls:
                log(
                    f"Ignoring inherited {name} from the workflow env: "
                    "api-base is set and takes precedence."
                )
                continue
            validated: str = validate_api_base(val)  # raises on a malformed value
            host: str = urllib.parse.urlsplit(validated).hostname or validated
            log(
                f"WARNING: {name}={host!r} inherited from the workflow env "
                "redirects the CLI (and its credential) to that host. Prefer "
                "the `api-base` input, which is validated and logged per run."
            )
            val = validated
        scrubbed[name] = val
    scrubbed.update(extra_vars)
    return scrubbed


def _invoke_cli_agent(
    *,
    argv: list[str],
    workspace: Path,
    findings_path: Path,
    env: dict[str, str],
    cli_name: str,
    stdin_input: str | None = None,
    usage_parser: "Callable[[str], UsageTelemetry | None] | None" = None,
) -> ReviewResult:
    """Run a CLI agent subprocess and parse its findings.json output.

    Common to all AgentRunnerProvider implementations. Enforces:
      - Argv-list form (no `shell=True`) — see docs/SECURITY.md.
      - Hard timeout via CLI_INVOCATION_TIMEOUT.
      - Structured error on non-zero exit with truncated stderr.
      - Delegation to parse_findings_file() for output validation.

    `stdin_input`, when provided, is piped to the subprocess' stdin. Providers
    that hit the OS ARG_MAX limit (Linux E2BIG on argv > ~128 KB) pass their
    large prompt this way instead of via a positional CLI argument.
    """
    log(f"Invoking {cli_name}: {' '.join(shlex.quote(a) for a in argv[:2])} …")
    # A findings file that exists AFTER the subprocess must have been written
    # by THIS run — a leftover from a previous step or a persistent
    # self-hosted workspace would otherwise be posted as a review.
    findings_path.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            argv,
            cwd=str(workspace),
            env=env,
            timeout=CLI_INVOCATION_TIMEOUT,
            check=False,
            capture_output=True,
            text=True,
            input=stdin_input,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"{cli_name} CLI exceeded the timeout of "
            f"{CLI_INVOCATION_TIMEOUT}s. Consider lowering `agent-max-turns` "
            f"or narrowing the PR scope."
        ) from e

    partial_note: str = ""
    if result.returncode != 0:
        stderr_tail: str = (result.stderr or "")[-MAX_ERROR_BODY_CHARS:]
        stdout_tail: str = (result.stdout or "")[-MAX_ERROR_BODY_CHARS:]
        if not findings_path.exists():
            raise RuntimeError(
                f"{cli_name} CLI exited with code {result.returncode}. "
                f"stderr tail: {stderr_tail!r}. stdout tail: {stdout_tail!r}."
            )
        # The agent wrote its findings before exiting non-zero (e.g. a
        # native turn cap or a late vendor error): keep the review and say
        # so, instead of failing the whole run (v2.2.0+).
        log(
            f"WARNING: {cli_name} CLI exited with code {result.returncode} "
            f"but wrote the findings file — posting a partial review. "
            f"stderr tail: {stderr_tail!r}."
        )
        partial_note = (
            f"\n\n---\n\n_Partial review: {cli_name} exited with code "
            f"{result.returncode}; findings recovered from the findings file._"
        )
    elif not findings_path.exists():
        # Exit 0 without a findings file: the agent ended its session
        # without producing the contract output (observed live with the
        # Grok CLI). Post an explicit summary-only review rather than a
        # failed run; the tracking comment and log both name the cause.
        stdout_tail_ok: str = (result.stdout or "")[-MAX_ERROR_BODY_CHARS:]
        log(
            f"WARNING: {cli_name} CLI exited 0 but did not write "
            f"{findings_path}; posting a summary-only review. "
            f"stdout tail: {stdout_tail_ok!r}."
        )
        incomplete_result: ReviewResult = ReviewResult(
            summary=(
                "## Code Review Summary\n\n"
                f"_The {cli_name} agent finished without writing its findings "
                "file, so this round carries no findings. This is an incomplete "
                "review — re-run (toggle the label) or check the workflow log "
                "for the agent's own output._"
            ),
            findings=[],
            incomplete=True,
        )
        if usage_parser is not None:
            try:
                incomplete_result.usage = usage_parser(result.stdout or "")
            except Exception as exc:  # noqa: BLE001 — telemetry never fails a run
                log(f"Usage parse skipped ({cli_name}): {type(exc).__name__}: {exc}")
        return incomplete_result

    parsed: ReviewResult = parse_findings_file(
        findings_path, allow_malformed_summary_fallback=True
    )
    if partial_note:
        parsed.summary = (parsed.summary or "").rstrip() + partial_note
    if usage_parser is not None:
        try:
            parsed.usage = usage_parser(result.stdout or "")
        except Exception as e:  # noqa: BLE001 — telemetry must never fail a review
            log(f"{cli_name}: usage parse failed (non-fatal): {e}")
            parsed.usage = None
        if parsed.usage is None:
            log(f"{cli_name}: no usage reported in CLI output.")
    return parsed


class ClaudeCodeProvider(AgentRunnerProvider):
    """Claude Code CLI (headless) as an agent-runner provider.

    Auth: the consumer's `api-key` input, mapped by `auth_env_vars()`. A
    metered API key (`sk-ant-api...`) is passed as `ANTHROPIC_API_KEY`; a
    subscription OAuth token from `claude setup-token` (`sk-ant-oat...`) is
    passed as `CLAUDE_CODE_OAUTH_TOKEN` so the review bills against a Claude
    Pro/Max subscription instead of API usage.
    CLI: `@anthropic-ai/claude-code` on npm. Installed by the composite step
    in `action.yml` when `provider: claude-code`.
    """

    PROVIDER_ID: str = "claude-code"
    CLI_NAME: str = "Claude Code"
    CLI_BIN: str = "claude"
    MCP_DEST: Path = Path.home() / ".claude" / "mcp.json"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        extra_args: str = "",
        mcp_config_file: str = "",
        profile: EndpointProfile | None = None,
    ) -> None:
        self.api_key: str = api_key
        self.model: str = model
        self.extra_args: str = extra_args
        self.mcp_config_file: str = mcp_config_file
        # Backend profile; `None` = the runner's default endpoint.
        self.profile: EndpointProfile = (
            profile
            if profile is not None
            else resolve_endpoint_profile("", self.PROVIDER_ID)
        )

    def auth_env_vars(self) -> dict[str, str]:
        """Map the consumer's `api-key` input to the right Claude Code auth
        env var.

        The `api-key` input accepts EITHER a metered Anthropic API key
        (`sk-ant-api...` → `ANTHROPIC_API_KEY`) OR a subscription OAuth token
        from `claude setup-token` (`sk-ant-oat...` → `CLAUDE_CODE_OAUTH_TOKEN`),
        so a consumer on a Claude Pro/Max plan can bill the review against
        their subscription instead of API usage — the same "use my
        subscription" model Cursor uses. Detection is by token prefix, so no
        new input and no change to the public contract.

        On a custom `api-base` (non-default profile) the mapping switches to
        the Anthropic-compatible-backend contract: `ANTHROPIC_AUTH_TOKEN` +
        `ANTHROPIC_BASE_URL` + `API_TIMEOUT_MS` + the three default-model
        alias env vars pinned to `self.model`. `ANTHROPIC_API_KEY` is
        deliberately NOT set there (no dual-auth ambiguity). A subscription
        token or `model: auto` on a custom backend fails fast.
        """
        if self.profile.is_default:
            if self.api_key.startswith(CLAUDE_OAUTH_TOKEN_PREFIX):
                return {CLAUDE_CODE_OAUTH_TOKEN_ENV: self.api_key}
            return {"ANTHROPIC_API_KEY": self.api_key}
        # Custom Anthropic-compatible backend (Z.ai GLM, xAI, gateway).
        if self.api_key.startswith(CLAUDE_OAUTH_TOKEN_PREFIX):
            raise ValueError(
                "api-key looks like a Claude subscription token "
                f"({CLAUDE_OAUTH_TOKEN_PREFIX}…) but api-base points at "
                f"{self.profile.host}. A subscription token can only "
                "authenticate against Anthropic; pass the backend's own API "
                "key instead."
            )
        if not self.model or self.model == "auto":
            raise ValueError(
                "model is required when claude-code runs on a custom "
                f"api-base ({self.profile.host}): `auto` has no meaning "
                "there. Examples: `glm-5.3` (Z.ai), `grok-4.3` (xAI)."
            )
        env: dict[str, str] = {
            CLAUDE_CODE_AUTH_TOKEN_ENV: self.api_key,
            CLAUDE_CODE_BASE_URL_ENV: self.profile.base_url,
            CLAUDE_CODE_API_TIMEOUT_ENV: CLAUDE_CODE_CUSTOM_BACKEND_TIMEOUT_MS,
        }
        for name in CLAUDE_CODE_DEFAULT_MODEL_ENVS:
            env[name] = self.model
        return env

    def install(self) -> None:
        result = run_cmd([self.CLI_BIN, "--version"])
        if result.returncode != 0:
            raise RuntimeError(
                f"{self.CLI_NAME} CLI not found on PATH. The composite step "
                "should install `@anthropic-ai/claude-code` before invoking "
                "reviewer.py."
            )

    def run_review(
        self,
        *,
        pr_context: PRContext,
        review_instructions: str,
        workspace: Path,
        output_dir: Path,
        require_complexity_in_findings: bool = False,
        max_inline_comments: int = 0,
    ) -> ReviewResult:
        findings_path: Path = output_dir / FINDINGS_JSON_REL
        findings_path.parent.mkdir(parents=True, exist_ok=True)

        # The review instructions + findings-contract directive go into the
        # system prompt as LITERAL TEXT via `--append-system-prompt <text>`.
        # (The flag takes a prompt string, not a path — passing a path would
        # deliver the literal filename to the model and the rubric/contract
        # would never arrive.) The instructions are a few KB, well under the
        # per-argv byte limit; only the diff-carrying user prompt is large,
        # and that goes via stdin below.
        enriched_instructions: str = write_findings_prompt_directive(
            review_instructions,
            findings_path,
            require_complexity=require_complexity_in_findings,
            prior_findings_expected=pr_context_is_incremental(pr_context),
            max_inline_comments=max_inline_comments,
        )

        mcp_dest, mcp_backup = _swap_mcp_config(
            self.mcp_config_file, self.MCP_DEST
        )
        try:
            # User prompt (PR metadata + full diff) is piped via stdin, not
            # argv: the diff can exceed the OS single-argument limit (~128 KB
            # E2BIG on Linux). `claude -p` reads the prompt from stdin when no
            # positional prompt is given.
            user_prompt: str = render_user_prompt(
                pr_context, for_agent_runner=True
            )
            argv: list[str] = [
                self.CLI_BIN,
                "-p",
                "--append-system-prompt",
                enriched_instructions,
                "--output-format",
                "stream-json",
                "--verbose",
                # Headless CI: the runner is already an isolated, ephemeral
                # sandbox, so bypass the interactive permission gate that
                # would otherwise block the Write tool (used to emit
                # findings.json) in non-interactive mode. Mirrors Cursor's
                # `--force --trust`. Consumers can override via
                # `agent-extra-args`.
                "--permission-mode",
                "bypassPermissions",
            ]
            # Claude Code only loads MCP servers from an explicit
            # `--mcp-config <file>` (or project `.mcp.json`) — a bare copy to
            # ~/.claude/mcp.json is NOT read. Point the flag at the consumer's
            # file directly so the passthrough actually takes effect.
            if self.mcp_config_file:
                argv += ["--mcp-config", self.mcp_config_file]
            # Default backend: `auto` defers to the CLI's own default. Custom
            # backend: the model is always explicit (auth_env_vars() already
            # rejected `auto`), so it is always forwarded.
            if self.model and (
                self.model != "auto" or not self.profile.is_default
            ):
                argv += ["--model", self.model]
            if self.extra_args:
                argv += shlex.split(self.extra_args)

            env: dict[str, str] = _build_cli_env(
                extra_vars=self.auth_env_vars(),
                allow_inherited_base_urls=self.profile.is_default,
            )
            if not self.profile.is_default:
                log(
                    f"Claude Code backend: {self.profile.kind} "
                    f"({self.profile.host}), model={self.model}"
                )
            return _invoke_cli_agent(
                argv=argv,
                workspace=workspace,
                findings_path=findings_path,
                env=env,
                cli_name=self.CLI_NAME,
                stdin_input=user_prompt,
                usage_parser=parse_claude_code_usage,
            )
        finally:
            _restore_mcp_config(mcp_dest, mcp_backup)


class CursorProvider(AgentRunnerProvider):
    """Cursor Agent CLI (headless, local runtime) as an agent-runner provider.

    Auth: `CURSOR_API_KEY` env var (from the consumer's `api-key` input). The
    key must belong to a Cursor Pro/Pro+/Ultra subscription — usage credits
    are debited from that subscription (there is no BYOK). Consumers on the
    Pro plan can select `model: auto` to route through Cursor's dispatch
    layer and avoid burning monthly credits on premium models.

    CLI: `cursor-agent` — installed via `curl -fsSL https://cursor.com/install
    | bash` by the composite step.

    Headless defaults (v1.2.0+): the invocation always passes `--force` and
    `--trust`, which are what Cursor's own headless CLI docs recommend for
    CI (they prevent interactive approval prompts that would otherwise stall
    the run). When `mcp_config_file` is set, `--approve-mcps` is added so
    the MCP approval prompt is also non-interactive. Consumers can still
    override any of this via `agent-extra-args`.

    Local runtime only for v1.1.0+ (no `/v1/agents` cloud REST path). The
    CLI operates against `workspace` directly.
    """

    PROVIDER_ID: str = "cursor"
    CLI_NAME: str = "Cursor Agent"
    CLI_BIN: str = "cursor-agent"
    MCP_DEST: Path = Path.home() / ".cursor" / "mcp.json"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        extra_args: str = "",
        mcp_config_file: str = "",
        profile: EndpointProfile | None = None,
    ) -> None:
        self.api_key: str = api_key
        self.model: str = model
        self.extra_args: str = extra_args
        self.mcp_config_file: str = mcp_config_file
        # Backend profile; `None` = the runner's default endpoint.
        self.profile: EndpointProfile = (
            profile
            if profile is not None
            else resolve_endpoint_profile("", self.PROVIDER_ID)
        )
        if not self.profile.is_default:
            log(
                "WARNING: api-base is set but provider=cursor has no "
                "bring-your-own-endpoint lane (Cursor CLI talks to Cursor's "
                f"own service). Ignoring api-base={self.profile.base_url!r}."
            )

    def install(self) -> None:
        result = run_cmd([self.CLI_BIN, "--version"])
        if result.returncode != 0:
            raise RuntimeError(
                f"{self.CLI_NAME} CLI not found on PATH. The composite step "
                "should install cursor-agent before invoking reviewer.py."
            )

    def run_review(
        self,
        *,
        pr_context: PRContext,
        review_instructions: str,
        workspace: Path,
        output_dir: Path,
        require_complexity_in_findings: bool = False,
        max_inline_comments: int = 0,
    ) -> ReviewResult:
        findings_path: Path = output_dir / FINDINGS_JSON_REL
        findings_path.parent.mkdir(parents=True, exist_ok=True)

        # Cursor Agent CLI does not expose a separate --append-system-prompt;
        # we inline our review instructions as the front of the user prompt.
        # The vendor's own code-tuned baseline system prompt still applies.
        enriched_instructions: str = write_findings_prompt_directive(
            review_instructions,
            findings_path,
            require_complexity=require_complexity_in_findings,
            prior_findings_expected=pr_context_is_incremental(pr_context),
            max_inline_comments=max_inline_comments,
        )
        user_prompt: str = (
            enriched_instructions
            + "\n\n---\n\n"
            + render_user_prompt(pr_context, for_agent_runner=True)
        )

        mcp_dest, mcp_backup = _swap_mcp_config(
            self.mcp_config_file, self.MCP_DEST
        )
        try:
            # Cursor CLI reads the prompt from stdin when `-p` is passed
            # without a positional argument. This avoids the E2BIG kernel
            # limit (~128 KB on Linux) which the argv path hits on large
            # PRs where the diff alone can exceed 200 KB.
            argv: list[str] = [
                self.CLI_BIN,
                "-p",
                "--output-format",
                "text",
                # Headless-CI defaults per Cursor's own documentation:
                # `--force` skips interactive tool approvals, `--trust` marks
                # the workspace as trusted for the run. Without these the
                # CLI can stall on approval prompts.
                "--force",
                "--trust",
            ]
            if self.model:
                argv += ["--model", self.model]
            if self.mcp_config_file:
                # Only relevant when an MCP config was injected; suppresses
                # the interactive "approve this MCP server" prompt.
                argv.append("--approve-mcps")
            if self.extra_args:
                argv += shlex.split(self.extra_args)

            env: dict[str, str] = _build_cli_env(
                extra_vars={"CURSOR_API_KEY": self.api_key},
                allow_inherited_base_urls=self.profile.is_default,
            )
            return _invoke_cli_agent(
                argv=argv,
                workspace=workspace,
                findings_path=findings_path,
                env=env,
                cli_name=self.CLI_NAME,
                stdin_input=user_prompt,
            )
        finally:
            _restore_mcp_config(mcp_dest, mcp_backup)


class CodexProvider(AgentRunnerProvider):
    """OpenAI Codex CLI (headless) as an agent-runner provider.

    Auth (Codex CLI 0.122+): Codex **no longer reads** `OPENAI_API_KEY`
    from the environment. It now reads credentials only from
    `$CODEX_HOME/auth.json`. Without that file (or with a ChatGPT-mode
    file present from a prior `codex login`), `codex exec` fails with:

        401 Unauthorized: Missing bearer or basic authentication in header,
        url: https://api.openai.com/v1/responses

    We materialize an apikey-mode `auth.json` in an isolated per-run
    `CODEX_HOME` (a `tempfile.mkdtemp()`-managed directory) before each
    invocation and remove it in a `finally` block. Doing this in an
    isolated home rather than `~/.codex/` means:
      - Self-hosted runners with a persistent `~/.codex/` (e.g. from a
        prior `codex login` in ChatGPT mode) don't override our apikey
        auth for this run.
      - We never clobber a user's real credentials on any runner.
      - Cleanup is fire-and-forget — `shutil.rmtree()` removes the whole
        temp directory, no per-file backup/restore dance.

    We also still forward `OPENAI_API_KEY` for back-compat with older
    Codex versions that read it from env (cost: zero).

    CLI: `@openai/codex` on npm. Installed by the composite step when
    `provider: codex`.

    Custom backends (`api-base`, v2.1.0+): when the resolved profile is not
    the default, a `config.toml` is written next to `auth.json` in the same
    isolated CODEX_HOME, declaring an OpenAI-compatible Responses-API
    provider (`wire_api = "responses"`) — Azure Foundry v1, xAI, Z.ai — plus
    the Azure image-generation workaround where the host is Azure. `--model`
    is required there (deployment name or the backend's model id).
    """

    PROVIDER_ID: str = "codex"
    CLI_NAME: str = "OpenAI Codex"
    CLI_BIN: str = "codex"
    MCP_DEST: Path = Path.home() / ".codex" / "mcp.json"
    # apikey-mode auth.json shape — validated against Codex CLI 0.122+
    # via the paperclipai/paperclip#5276 fix and the shell one-liner
    # `echo '{"OPENAI_API_KEY": "..."}' > $CODEX_HOME/auth.json`
    # that is documented in the wjduenow/clauditor#177 workaround.
    AUTH_JSON_FILENAME: str = "auth.json"

    @staticmethod
    def _materialize_apikey_auth_json(
        *, codex_home: Path, api_key: str
    ) -> None:
        """Write an apikey-mode auth.json under `codex_home`.

        Sets mode `0o600` on the file so a shared runner cannot read it
        from another process. Fails loudly on OSError — a missing
        auth.json is exactly the bug we are here to prevent.
        """
        codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        auth_path: Path = codex_home / CodexProvider.AUTH_JSON_FILENAME
        auth_path.write_text(
            json.dumps({"OPENAI_API_KEY": api_key}),
            encoding="utf-8",
        )
        try:
            os.chmod(auth_path, 0o600)
        except OSError as e:
            log(
                f"WARNING: could not chmod 0600 on Codex auth.json at "
                f"{auth_path}: {e}. Continuing — the temp CODEX_HOME "
                f"parent directory is already 0700."
            )

    @staticmethod
    def _toml_escape(value: str) -> str:
        """Escape a string for a double-quoted TOML basic string."""
        out: list[str] = []
        for ch in value:
            if ch == "\\":
                out.append("\\\\")
            elif ch == '"':
                out.append('\\"')
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            elif ord(ch) < 0x20:
                out.append(f"\\u{ord(ch):04X}")
            else:
                out.append(ch)
        return "".join(out)

    @classmethod
    def render_custom_provider_config(
        cls,
        *,
        profile: EndpointProfile,
        model: str,
        catalog_path: Path | None = None,
    ) -> str:
        """Render the `config.toml` that routes Codex to a custom backend.

        Pure (unit-tested directly). The provider block mirrors the
        maintainer-proven overlay for Azure Foundry / xAI / Z.ai:
        `wire_api = "responses"`, `env_key` pointing at the env var we
        already forward, plus the profile's extra TOML (Azure needs the
        image-generation header workaround and the feature disabled).
        """
        esc = cls._toml_escape
        pid: str = CODEX_CUSTOM_PROVIDER_ID
        lines: list[str] = [
            "# Generated per-run by AI Diff Reviewer — routes Codex to the",
            "# backend selected by `api-base`. Lives only in the isolated",
            "# CODEX_HOME for this invocation.",
            f'model = "{esc(model)}"',
            f'model_provider = "{pid}"',
        ]
        if catalog_path is not None:
            lines.append(f'model_catalog_json = "{esc(str(catalog_path))}"')
        lines += [
            "",
            f"[model_providers.{pid}]",
            f'name = "AI Diff Reviewer backend ({esc(profile.kind)})"',
            f'base_url = "{esc(profile.base_url)}"',
            f'env_key = "{CODEX_CUSTOM_PROVIDER_ENV_KEY}"',
            f'wire_api = "{esc(profile.codex_wire_api)}"',
        ]
        text: str = "\n".join(lines) + "\n"
        if profile.codex_extra_toml:
            text += profile.codex_extra_toml
        return text

    @staticmethod
    def build_model_catalog_entry(
        bundled: dict[str, Any], *, model: str, kind: str
    ) -> dict[str, Any] | None:
        """Clone a bundled ModelInfo under `model` with conservative
        capabilities (pure — unit-tested). Returns None when the bundled
        catalog has no usable template."""
        models: list[dict[str, Any]] = [
            m for m in (bundled.get("models") or []) if isinstance(m, dict)
        ]
        if not models:
            return None
        by_slug: dict[str, dict[str, Any]] = {
            str(m.get("slug", "")): m for m in models
        }
        def _no_upgrade(m: dict[str, Any]) -> bool:
            return m.get("upgrade") is None

        template: dict[str, Any] | None = None
        for slug in CODEX_CATALOG_TEMPLATE_SLUGS:
            candidate: dict[str, Any] | None = by_slug.get(slug)
            if candidate is not None and _no_upgrade(candidate):
                template = candidate
                break
        if template is None:
            template = next((m for m in models if _no_upgrade(m)), models[0])
        entry: dict[str, Any] = json.loads(json.dumps(template))  # deep copy
        entry["slug"] = model
        entry["display_name"] = model
        entry["description"] = f"AI Diff Reviewer backend model ({kind})."
        for key, value in CODEX_CATALOG_SAFE_OVERRIDES.items():
            if key not in entry:
                continue
            # Never write `null` into a field the template has non-null:
            # Codex's catalog parser is strict about types.
            if value is None and entry[key] is not None:
                continue
            entry[key] = json.loads(json.dumps(value))
        return entry

    @classmethod
    def _materialize_model_catalog(
        cls, *, codex_home: Path, profile: EndpointProfile, model: str
    ) -> Path | None:
        """Best-effort: write `models.json` cloned from the bundled catalog.

        Returns the catalog path, or None (with a log line) when the CLI
        cannot list its bundled catalog — the run then proceeds without a
        catalog, which is enough for Azure Foundry and any backend that
        tolerates Codex's default tool set.
        """
        result = run_cmd(list(CODEX_CATALOG_CMD))
        if result.returncode != 0 or not (result.stdout or "").strip():
            log(
                "Codex model catalog unavailable "
                f"(`{' '.join(CODEX_CATALOG_CMD)}` exit {result.returncode}); "
                "continuing without model_catalog_json."
            )
            return None
        try:
            bundled: dict[str, Any] = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            log(f"Codex model catalog is not JSON ({e}); continuing without it.")
            return None
        entry: dict[str, Any] | None = cls.build_model_catalog_entry(
            bundled, model=model, kind=profile.kind
        )
        if entry is None:
            log("Codex bundled catalog had no models; continuing without it.")
            return None
        catalog_path: Path = codex_home / CODEX_MODEL_CATALOG_FILENAME
        catalog_path.write_text(
            json.dumps({"models": [entry]}, indent=2) + "\n", encoding="utf-8"
        )
        try:
            os.chmod(catalog_path, 0o600)
        except OSError as e:  # noqa: BLE001 — perms are defense in depth
            log(f"WARNING: could not chmod 0600 on {catalog_path}: {e}")
        return catalog_path

    @classmethod
    def _materialize_custom_provider_config(
        cls, *, codex_home: Path, profile: EndpointProfile, model: str
    ) -> Path:
        """Write `config.toml` (0600) into `codex_home` for a custom backend,
        referencing a cloned model catalog when one could be produced."""
        catalog_path: Path | None = cls._materialize_model_catalog(
            codex_home=codex_home, profile=profile, model=model
        )
        config_path: Path = codex_home / CODEX_CONFIG_TOML_FILENAME
        config_path.write_text(
            cls.render_custom_provider_config(
                profile=profile, model=model, catalog_path=catalog_path
            ),
            encoding="utf-8",
        )
        try:
            os.chmod(config_path, 0o600)
        except OSError as e:
            log(
                f"WARNING: could not chmod 0600 on Codex config.toml at "
                f"{config_path}: {e}. Continuing — the temp CODEX_HOME "
                f"parent directory is already 0700."
            )
        return config_path

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        extra_args: str = "",
        mcp_config_file: str = "",
        profile: EndpointProfile | None = None,
    ) -> None:
        self.api_key: str = api_key
        self.model: str = model
        self.extra_args: str = extra_args
        self.mcp_config_file: str = mcp_config_file
        # Backend profile; `None` = the runner's default endpoint.
        self.profile: EndpointProfile = (
            profile
            if profile is not None
            else resolve_endpoint_profile("", self.PROVIDER_ID)
        )

    def install(self) -> None:
        result = run_cmd([self.CLI_BIN, "--version"])
        if result.returncode != 0:
            raise RuntimeError(
                f"{self.CLI_NAME} CLI not found on PATH. The composite step "
                "should install `@openai/codex` before invoking reviewer.py."
            )

    def run_review(
        self,
        *,
        pr_context: PRContext,
        review_instructions: str,
        workspace: Path,
        output_dir: Path,
        require_complexity_in_findings: bool = False,
        max_inline_comments: int = 0,
    ) -> ReviewResult:
        findings_path: Path = output_dir / FINDINGS_JSON_REL
        findings_path.parent.mkdir(parents=True, exist_ok=True)

        enriched_instructions: str = write_findings_prompt_directive(
            review_instructions,
            findings_path,
            require_complexity=require_complexity_in_findings,
            prior_findings_expected=pr_context_is_incremental(pr_context),
            max_inline_comments=max_inline_comments,
        )
        user_prompt: str = (
            enriched_instructions
            + "\n\n---\n\n"
            + render_user_prompt(pr_context, for_agent_runner=True)
        )

        if self.mcp_config_file:
            # Codex configures MCP servers via `~/.codex/config.toml`
            # ([mcp_servers] TOML), NOT a JSON file — the copied mcp.json is
            # ignored. Warn loudly rather than silently no-op so the consumer
            # knows the passthrough didn't take effect. See docs/PROVIDERS.md.
            log(
                "WARNING: mcp-config-file is set but Codex does not read a JSON "
                "MCP config (it uses ~/.codex/config.toml). The MCP passthrough "
                "will NOT take effect for provider=codex. Configure MCP via "
                "agent-extra-args (`-c mcp_servers...`) or a preconfigured "
                "config.toml instead."
            )

        # Isolated per-run CODEX_HOME with an apikey-mode auth.json —
        # see the class docstring for the 0.122+ auth breakage rationale.
        # `mkdtemp` creates a private dir with mode 0700 by default.
        codex_home: Path = Path(tempfile.mkdtemp(prefix="aiprr-codex-"))
        try:
            self._materialize_apikey_auth_json(
                codex_home=codex_home, api_key=self.api_key
            )
            if not self.profile.is_default:
                # Custom backend (Azure Foundry / xAI / Z.ai / gateway): the
                # model is the backend's own id or deployment name and must
                # be explicit — Codex's built-in default only exists on
                # OpenAI.
                if not self.model or self.model == "auto":
                    raise ValueError(
                        "model is required when codex runs on a custom "
                        f"api-base ({self.profile.host}) — e.g. an Azure "
                        "deployment name, `grok-4.3` (xAI) or `glm-5.3` "
                        "(Z.ai)."
                    )
                self._materialize_custom_provider_config(
                    codex_home=codex_home, profile=self.profile, model=self.model
                )
                log(
                    f"Codex backend: {self.profile.kind} ({self.profile.host}) "
                    f"wire_api={self.profile.codex_wire_api}, model={self.model}"
                )
                if self.profile.kind in CODEX_CUSTOM_TOOL_SENSITIVE_KINDS:
                    log(
                        "WARNING: Codex CLI 0.154+ always sends its freeform "
                        "apply_patch tool (`tools[].type: custom`), which "
                        f"{self.profile.kind} Responses endpoints have been "
                        "observed to reject with HTTP 422. If this run fails "
                        "that way, use `provider: openai` (in-process) or "
                        "`provider: grok` for xAI instead, or pin an older "
                        "`codex-version`. See docs/PROVIDERS.md."
                    )

            # Do not copy `mcp-config-file` to `~/.codex/mcp.json`: Codex
            # ignores that JSON file, and this run uses an isolated CODEX_HOME
            # anyway. The warning above points users at the supported
            # `config.toml` / `agent-extra-args` path.
            # Codex CLI headless is `codex exec`. Two CI-critical flags:
            #   --dangerously-bypass-approvals-and-sandbox: `codex exec`
            #     defaults to a READ-ONLY sandbox, so without this the agent
            #     physically cannot write findings.json and every review
            #     fails. This flag is documented as "intended solely for
            #     running in environments that are externally sandboxed" —
            #     exactly a GitHub-hosted runner. Mirrors Cursor's
            #     `--force --trust`.
            #   `-` positional: read the (diff-carrying, potentially >128 KB)
            #     prompt from stdin instead of argv, avoiding the OS E2BIG
            #     single-argument limit.
            argv: list[str] = [
                self.CLI_BIN,
                "exec",
                # JSONL event stream on stdout (`turn.completed` carries usage);
                # findings still arrive via the file contract.
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
            ]
            if self.model:
                argv += ["--model", self.model]
            if self.extra_args:
                argv += shlex.split(self.extra_args)
            # The stdin sentinel must be the final positional argument.
            argv.append("-")

            # CODEX_HOME redirects the CLI to read our apikey auth.json
            # (0.122+ requirement). OPENAI_API_KEY stays in the env for
            # back-compat with < 0.122 which read it directly.
            env: dict[str, str] = _build_cli_env(
                extra_vars={
                    "OPENAI_API_KEY": self.api_key,
                    "CODEX_HOME": str(codex_home),
                },
                allow_inherited_base_urls=self.profile.is_default,
            )
            return _invoke_cli_agent(
                argv=argv,
                workspace=workspace,
                findings_path=findings_path,
                env=env,
                cli_name=self.CLI_NAME,
                stdin_input=user_prompt,
                usage_parser=parse_codex_usage,
            )
        finally:
            # Best-effort cleanup of the isolated CODEX_HOME. The temp dir
            # is 0700 so cross-process leakage during the run is bounded;
            # unlink failures here are logged, not fatal.
            try:
                shutil.rmtree(codex_home)
            except OSError as e:  # noqa: BLE001 — cleanup is best-effort
                log(
                    f"Could not remove Codex temp home {codex_home}: {e}. "
                    "The runner is ephemeral; leftover files will be "
                    "destroyed with the VM."
                )


class GrokProvider(AgentRunnerProvider):
    """xAI Grok CLI (headless) as an agent-runner provider.

    Deliberately NOT xAI's suggested `grok -p "Review this PR" --always-approve`
    workflow: the agent never receives a GitHub token, it writes the shared
    `.aiprr/findings.json` contract (so severity gating, IAR dedup, collapse
    and the cap all apply), web search and subagents are disabled by default
    (exfiltration + cost hardening), and turns are capped natively when
    `agent-max-turns` is set.

    Auth: `XAI_API_KEY` (from the consumer's `api-key` input). CLI: installed
    by the composite step (`curl -fsSL https://x.ai/cli/install.sh | bash`)
    into `~/.grok/bin` when `provider: grok`. `api-base` is ignored — the
    CLI talks to xAI only. `mcp-config-file` is not wired (warned).
    """

    PROVIDER_ID: str = "grok"
    CLI_NAME: str = GROK_CLI_NAME
    CLI_BIN: str = GROK_CLI_BIN
    MCP_DEST: Path = Path.home() / ".grok" / "mcp.json"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        extra_args: str = "",
        mcp_config_file: str = "",
        profile: EndpointProfile | None = None,
        max_turns: int = 0,
    ) -> None:
        self.api_key: str = api_key
        self.model: str = model
        self.extra_args: str = extra_args
        self.mcp_config_file: str = mcp_config_file
        self.profile: EndpointProfile = (
            profile
            if profile is not None
            else resolve_endpoint_profile("", self.PROVIDER_ID)
        )
        # `agent-max-turns` → native `--max-turns` (0 = unset).
        self.max_turns: int = max_turns

    def install(self) -> None:
        result = run_cmd([self.CLI_BIN, "--version"])
        if result.returncode != 0:
            raise RuntimeError(
                f"{self.CLI_NAME} CLI not found on PATH. The composite step "
                "should install it (https://x.ai/cli/install.sh → "
                "~/.grok/bin) before invoking reviewer.py."
            )

    def build_argv(self, *, prompt_path: Path, instructions: str) -> list[str]:
        """The headless invocation (pure — unit-tested directly)."""
        argv: list[str] = [
            self.CLI_BIN,
            GROK_PROMPT_FILE_FLAG,
            str(prompt_path),
            GROK_RULES_FLAG,
            instructions,
            *GROK_HEADLESS_DEFAULT_FLAGS,
        ]
        if self.model and self.model != "auto":
            argv += ["-m", self.model]
        if self.max_turns > 0:
            argv += [GROK_MAX_TURNS_FLAG, str(self.max_turns)]
        if self.extra_args:
            argv += shlex.split(self.extra_args)
        return argv

    def run_review(
        self,
        *,
        pr_context: PRContext,
        review_instructions: str,
        workspace: Path,
        output_dir: Path,
        require_complexity_in_findings: bool = False,
        max_inline_comments: int = 0,
    ) -> ReviewResult:
        findings_path: Path = output_dir / FINDINGS_JSON_REL
        findings_path.parent.mkdir(parents=True, exist_ok=True)

        # Rubric + findings contract go into the system prompt via `--rules`
        # (a few KB of text — well under argv limits). The PR metadata + diff
        # can exceed ARG_MAX and Grok's `-p` does not read stdin, so it goes
        # through `--prompt-file` from a private temp dir (0700/0600).
        enriched_instructions: str = write_findings_prompt_directive(
            review_instructions,
            findings_path,
            require_complexity=require_complexity_in_findings,
            prior_findings_expected=pr_context_is_incremental(pr_context),
            max_inline_comments=max_inline_comments,
        )
        if self.mcp_config_file:
            log(
                "WARNING: mcp-config-file is set but the Grok CLI passthrough "
                "is not wired (configure MCP via `grok mcp` / agent-extra-args). "
                "The MCP passthrough will NOT take effect for provider=grok."
            )
        prompt_dir: Path = Path(tempfile.mkdtemp(prefix="aiprr-grok-"))
        try:
            prompt_path: Path = prompt_dir / GROK_PROMPT_FILENAME
            prompt_path.write_text(
                render_user_prompt(pr_context, for_agent_runner=True),
                encoding="utf-8",
            )
            try:
                os.chmod(prompt_path, 0o600)
            except OSError as e:  # noqa: BLE001 — perms are defense in depth
                log(f"WARNING: could not chmod 0600 on {prompt_path}: {e}")
            argv: list[str] = self.build_argv(
                prompt_path=prompt_path, instructions=enriched_instructions
            )
            env: dict[str, str] = _build_cli_env(
                extra_vars={GROK_API_KEY_ENV: self.api_key},
                allow_inherited_base_urls=self.profile.is_default,
            )
            return _invoke_cli_agent(
                argv=argv,
                workspace=workspace,
                findings_path=findings_path,
                env=env,
                cli_name=self.CLI_NAME,
                usage_parser=parse_grok_usage,
            )
        finally:
            try:
                shutil.rmtree(prompt_dir)
            except OSError as e:  # noqa: BLE001 — cleanup is best-effort
                log(f"Could not remove Grok temp prompt dir {prompt_dir}: {e}")


def build_provider(
    provider_id: str, *, api_key: str, model: str, api_base: str = ""
) -> Provider | AgentRunnerProvider:
    """Construct the provider implementation for `provider_id`.

    Returns either a `Provider` (chat-completions family, action owns the
    tool-use loop) or an `AgentRunnerProvider` (vendor CLI owns the loop).
    `main()` dispatches on the returned instance type. `api_base` (already
    validated by `validate_api_base`) selects the backend profile; empty
    keeps the runner's default endpoint.
    """
    profile: EndpointProfile = resolve_endpoint_profile(api_base, provider_id)
    if api_base and provider_id in PROVIDERS_WITHOUT_API_BASE:
        log(
            f"WARNING: api-base is set but provider {provider_id!r} has no "
            "bring-your-own endpoint (subscription-only CLI) — ignoring it."
        )
    if provider_id == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model, profile=profile)
    if provider_id == "openai":
        return OpenAIProvider(api_key=api_key, model=model, profile=profile)

    # Agent-runner providers share a common constructor shape — extra_args
    # and mcp_config_file come from the AIPRR_* env vars set by action.yml.
    extra_args: str = os.environ.get("AIPRR_AGENT_EXTRA_ARGS", "").strip()
    mcp_config: str = os.environ.get("AIPRR_MCP_CONFIG_FILE", "").strip()
    # `agent-max-turns` is enforced natively where the CLI exposes a turn cap
    # (Grok: `--max-turns`). Elsewhere warn — accurately, per provider — so the
    # consumer knows the effective bound is CLI_INVOCATION_TIMEOUT and which
    # vendor-native lever exists. See docs/PROVIDERS.md.
    agent_max_turns: int = parse_agent_max_turns(
        os.environ.get("AIPRR_AGENT_MAX_TURNS", "")
    )
    if agent_max_turns and provider_id not in AGENT_MAX_TURNS_NATIVE_PROVIDERS:
        alternative: str = {
            "claude-code": "Claude Code's `--max-budget-usd <amount>` via agent-extra-args",
            "codex": "no vendor cap flag on `codex exec`",
            "cursor": "no vendor cap flag on `cursor-agent`",
        }.get(provider_id, "no vendor cap flag")
        log(
            f"WARNING: agent-max-turns={agent_max_turns} is set but the "
            f"{provider_id} CLI has no turn-count flag to forward it to "
            f"({alternative}). The effective bound is the "
            f"{CLI_INVOCATION_TIMEOUT}s invocation timeout. Natively enforced "
            f"on: {', '.join(AGENT_MAX_TURNS_NATIVE_PROVIDERS)}."
        )
    if provider_id == "claude-code":
        return ClaudeCodeProvider(
            api_key=api_key,
            model=model,
            extra_args=extra_args,
            mcp_config_file=mcp_config,
            profile=profile,
        )
    if provider_id == "cursor":
        return CursorProvider(
            api_key=api_key,
            model=model,
            extra_args=extra_args,
            mcp_config_file=mcp_config,
            profile=profile,
        )
    if provider_id == "codex":
        return CodexProvider(
            api_key=api_key,
            model=model,
            extra_args=extra_args,
            mcp_config_file=mcp_config,
            profile=profile,
        )
    if provider_id == "grok":
        return GrokProvider(
            api_key=api_key,
            model=model,
            extra_args=extra_args,
            mcp_config_file=mcp_config,
            profile=profile,
            max_turns=agent_max_turns,
        )
    raise ValueError(
        f"Unsupported provider: {provider_id!r}. Currently supported: "
        f"{sorted(DEFAULT_MODELS)}."
    )


# ---------------------------------------------------------------------------
# Iteration-Aware Review (IAR) config
# ---------------------------------------------------------------------------
# The runtime reads the 4 IAR env vars once at the top of main() and packages
# them into an IARConfig dataclass consumed by every IAR touchpoint. See
# docs/ITERATION_AWARENESS.md.


@dataclass(frozen=True)
class IARConfig:
    """Parsed + validated configuration for the Iteration-Aware Review
    subsystem. Built exactly once per run via `build_iar_config()`.

    IAR runs on every review; consumers tune the four knobs below
    (policy, round cap, cap multiplier, escape label). The pipeline
    itself is wrapped in `try/except` at each `main()` call site so an
    IAR bug degrades to the baseline review path (empty IAR outputs,
    tracking marker without the annotation) — the reviewer never fails
    because of IAR.
    """

    policy: str
    max_review_rounds: int
    cap_multiplier: int
    escape_label: str


def build_iar_config(env: dict[str, str]) -> IARConfig:
    """Read the 4 IAR env vars from `env` and return a validated IARConfig.

    Defaults: `first-pass-exhaustive` policy, unlimited rounds,
    3× cap multiplier, `full-review-please` escape label — matches the
    shipped `action.yml` defaults, so a consumer who sets nothing gets
    the recommended convergence profile.

    Unknown policy values fall back to `first-pass-exhaustive` silently;
    negative integers are clamped to sane values. All parsing is lenient
    so a misconfigured input never crashes the run.
    """
    policy_raw: str = (
        env.get("AIPRR_CONVERGENCE_POLICY", "").strip()
        or IAR_POLICY_FIRST_PASS_EXHAUSTIVE
    )
    if policy_raw not in IAR_VALID_POLICIES:
        # Silent fallback keeps the runtime safe even under a misconfiguration.
        # main() emits a debug log line so the miswiring is visible in the
        # workflow log.
        policy: str = IAR_POLICY_FIRST_PASS_EXHAUSTIVE
    else:
        policy = policy_raw
    max_review_rounds_raw: str = (
        env.get("AIPRR_MAX_REVIEW_ROUNDS", "").strip() or "0"
    )
    try:
        max_review_rounds: int = int(max_review_rounds_raw)
    except ValueError:
        max_review_rounds = 0
    if max_review_rounds < 0:
        max_review_rounds = 0
    cap_multiplier_raw: str = (
        env.get("AIPRR_EXHAUSTIVE_FIRST_PASS_CAP_MULTIPLIER", "").strip()
        or str(IAR_DEFAULT_CAP_MULTIPLIER)
    )
    try:
        cap_multiplier: int = int(cap_multiplier_raw)
    except ValueError:
        cap_multiplier = IAR_DEFAULT_CAP_MULTIPLIER
    if cap_multiplier < 1:
        cap_multiplier = 1
    escape_label: str = (
        env.get("AIPRR_ITERATION_ESCAPE_LABEL", "").strip()
        or IAR_DEFAULT_ESCAPE_LABEL
    )
    return IARConfig(
        policy=policy,
        max_review_rounds=max_review_rounds,
        cap_multiplier=cap_multiplier,
        escape_label=escape_label,
    )


def write_iar_outputs_empty() -> None:
    """Write empty-string values for all 5 IAR action outputs.

    Called on every code path where IAR could not populate its own
    values — the review was skipped before IAR ran, the pre-LLM or
    post-LLM helper raised (caught by main()'s try/except), or the review
    aborted early. Guarantees downstream steps that read
    `steps.review.outputs.iteration-*` always see a defined string
    (never a missing key or a null value).

    `write_iar_outputs_populated()` is called after a successful IAR
    pipeline execution and overwrites these empty strings with real
    values — last-write-wins on `$GITHUB_OUTPUT`.
    """
    write_action_output("iteration-round", "")
    write_action_output("iteration-generation", "")
    write_action_output("iteration-policy-applied", "")
    write_action_output("iteration-tokens-used", "")
    write_action_output("iteration-cost-vs-baseline-estimate", "")


# ---------------------------------------------------------------------------
# Iteration-Aware Review (IAR) — state layer
# ---------------------------------------------------------------------------
# The IAR runtime persists a small JSON blob inside the existing tracking
# marker comment (an HTML-comment block delimited by IAR_STATE_TAG_OPEN /
# IAR_STATE_TAG_CLOSE, nested inside REVIEW_MARKER). Zero external state,
# zero new files on disk. Every parse/read failure falls back to `None`
# (treated as "first review, no prior state") with a debug log — the
# subsystem must never crash the reviewer on a malformed marker.
#
# See docs/ITERATION_AWARENESS.md § 12 for the JSON schema (version 1).


class IterationStateParseError(Exception):
    """Raised inside `_parse_state_from_marker_body` when the embedded JSON
    is malformed or its schema version is unknown. NEVER propagates outside
    the IAR module — callers catch and fall back to `None`."""


@dataclass
class IterationState:
    """Persisted IAR state (version 1). Read from the last marker, updated
    in-memory during the run, and re-embedded into the new marker at the
    end. See docs/ITERATION_AWARENESS.md § 12 for the schema contract.

    Field notes:
    - `version`: schema version; matches IAR_STATE_SCHEMA_VERSION.
    - `generation`: monotonic counter; increments on new commits or rebase.
    - `generation_range_hash`: 16-char SHA256 hex slice of the diff
      *content* between `base_sha` and `head_sha` (i.e. the output of
      `git diff base_sha...head_sha` — THREE-dot, matching
      `fetch_pr_context`'s `origin/<base>...HEAD` PR payload — not
      the commit-SHA list, and NEVER two-dot; see
      docs/ITERATION_AWARENESS.md § 4.3 for the rationale). Two
      commits producing byte-identical diffs produce byte-identical
      hashes so cosmetic rebases that don't change what the reviewer
      would see don't advance the generation. Detecting a change
      advances the generation.
    - `round_in_generation`: how many reviews have run in this generation.
      Resets to 1 on generation change.
    - `policy_applied`: which policy actually fired on the last review
      (usually matches configured policy; safety net or escape label can
      override).
    - `resolved_fingerprints`: fingerprints reported in prior rounds AND
      not present in the current round → treated as resolved.
      `iterative` / `first-pass-exhaustive` re-surface these when they
      reappear; `critical-gate` silences them unless critical.
    - `open_fingerprints_this_gen`: fingerprints reported in the current
      generation and NOT yet reported as resolved. Used by dedup engine.
    - `history`: append-only per-generation summary rows. Bounded to the
      last N generations (see IAR_HISTORY_MAX_ENTRIES) so the marker body
      cannot grow unboundedly.
    """

    version: int
    generation: int
    generation_range_hash: str
    round_in_generation: int
    policy_applied: str
    resolved_fingerprints: list[str]
    open_fingerprints_this_gen: list[str]
    history: list[dict[str, Any]]
    # Optional in v1 schema — populated by Task 4 (generation tracking).
    # An empty string means "unknown prior base" and forces `detect_generation
    # _change` to fall back to NEW_COMMITS on any hash mismatch (safe: extra
    # exhaustive review, never silent silencing).
    base_sha: str = ""
    # Optional in v1 schema — populated by Task 8 (observability). Stores
    # the head SHA of the last review so `compute_new_lines_pct` can measure
    # what has been added since. Empty string means "unknown prior head" →
    # safety net degrades to no-op (compute_new_lines_pct returns 0.0), which
    # is the safe conservative fallback (never silences a review that would
    # have benefited from an exhaustive pass; just skips the boost).
    head_sha: str = ""
    # Load-bearing arming signal for USER_FORCED_RESET on the NEXT run.
    # Computed by `compute_reviewed_label_applied` as the OR of three
    # signals: (1) this run's `gh_apply_label` call succeeded,
    # (2) the label is currently on the PR at trigger time (a prior
    # run stamped it and it's still there — no one manually removed
    # it yet), or (3) the previous run's state recorded a successful
    # stamp AND this run took a path (blocked, escape-label, etc.)
    # that does not remove the label. This three-signal OR is
    # deliberately stronger than "this run stamped the label" —
    # otherwise a blocked follow-up would silently clear the arming
    # bit and disarm a legitimate reset gesture on the run after
    # that. Defaults to `False` for back-compat with older marker
    # bodies that predate this field (safe conservative fallback:
    # users on old state must complete one successful review before
    # the reset gesture becomes armed). Full contract in
    # docs/ITERATION_AWARENESS.md § 8.5.
    reviewed_label_applied: bool = False


# Cap on `history` list length. 20 generations is plenty for the lifetime
# of a single PR while keeping the marker body under ~10KB even in
# pathological cases. See docs/ITERATION_AWARENESS.md § 12.
IAR_HISTORY_MAX_ENTRIES: int = 20


def new_iteration_state(
    *,
    generation: int = 1,
    generation_range_hash: str = "",
    round_in_generation: int = 1,
    policy_applied: str = IAR_POLICY_ITERATIVE,
    base_sha: str = "",
    head_sha: str = "",
) -> IterationState:
    """Construct a fresh IterationState with schema version + empty lists.
    Used on first review of a PR (no prior marker found)."""
    return IterationState(
        version=IAR_STATE_SCHEMA_VERSION,
        generation=generation,
        generation_range_hash=generation_range_hash,
        round_in_generation=round_in_generation,
        policy_applied=policy_applied,
        resolved_fingerprints=[],
        open_fingerprints_this_gen=[],
        history=[],
        base_sha=base_sha,
        head_sha=head_sha,
    )


GIT_SHA_PATTERN: "re.Pattern[str]" = re.compile(r"[0-9a-f]{4,64}")


def _coerce_git_sha(raw: Any) -> str:
    """Accept only a lowercase hex object id (4–64 chars) from persisted
    marker state; anything else becomes `""`. The value is later passed to
    `git diff` / `git merge-base` as its own argv token, so a poisoned
    marker must never be able to smuggle an option such as
    `--output=<path>` (argument injection) — `""` simply disables the
    delta fast path, which is the safe direction (over-review)."""
    if not isinstance(raw, str):
        return ""
    value: str = raw.strip().lower()
    return value if GIT_SHA_PATTERN.fullmatch(value) else ""


def _parse_state_from_marker_body(
    marker_body: str,
) -> IterationState | None:
    """Extract + parse the IAR state block from a marker body string.

    Returns:
    - `IterationState` on success.
    - `None` on any failure (no block, malformed JSON, unknown version,
      shape mismatch). Failure is logged via `log()` with an `IAR:` prefix
      so miswiring is visible in the workflow log.
    """
    if not marker_body or IAR_STATE_TAG_OPEN not in marker_body:
        return None
    pattern: re.Pattern[str] = re.compile(
        re.escape(IAR_STATE_TAG_OPEN)
        + r"(.*?)"
        + re.escape(IAR_STATE_TAG_CLOSE),
        re.DOTALL,
    )
    matches: list[str] = pattern.findall(marker_body)
    if not matches:
        return None
    raw_block: str = matches[-1].strip()
    try:
        data: Any = json.loads(raw_block)
        if not isinstance(data, dict):
            raise IterationStateParseError(
                f"expected JSON object at root, got {type(data).__name__}"
            )
        version: Any = data.get("version")
        if version != IAR_STATE_SCHEMA_VERSION:
            raise IterationStateParseError(
                f"unknown schema version {version!r} "
                f"(runtime supports {IAR_STATE_SCHEMA_VERSION})"
            )
        # Fingerprint lists MUST contain only strings — anything else
        # crashes IAR into a sticky DoS on convergence. `set(prior_state
        # .resolved_fingerprints)` in `dedupe_findings_against_prior`
        # raises `TypeError: unhashable type: 'dict'` on a poisoned
        # marker containing e.g. `[{"x": 1}]`, so `run_iar_pre_llm`
        # crashes → `main()`'s try/except falls back to baseline →
        # every subsequent run keeps failing until the poisoned marker
        # ages out of the fetch window. Coerce here (drop non-string
        # entries silently — over-review is the safe direction).
        def _coerce_fingerprints(raw: Any) -> list[str]:
            if not isinstance(raw, list):
                return []
            return [x for x in raw if isinstance(x, str)]

        # `history` MUST contain only dicts — the accumulator writer
        # in `run_iar_post_llm` mutates `state.history[-1]`, which
        # blows up if the entry is a scalar. Coerce here too.
        def _coerce_history(raw: Any) -> list[dict[str, Any]]:
            if not isinstance(raw, list):
                return []
            return [x for x in raw if isinstance(x, dict)]

        # `bool()` on JSON is a foot-gun: `bool("false") is True`,
        # `bool("no") is True`, `bool("0") is True`. Only accept the
        # actual JSON booleans (or, for lenient upgrades, integer 0/1);
        # everything else falls back to `False` (the safe default —
        # missing bit disarms USER_FORCED_RESET rather than firing it
        # spuriously). Trust-boundary rule per docs/SECURITY.md § IAR.
        raw_rla: Any = data.get("reviewed_label_applied", False)
        if isinstance(raw_rla, bool):
            reviewed_label_applied: bool = raw_rla
        elif isinstance(raw_rla, int) and raw_rla in (0, 1):
            reviewed_label_applied = bool(raw_rla)
        else:
            reviewed_label_applied = False

        return IterationState(
            version=int(version),
            generation=int(data.get("generation", 1)),
            generation_range_hash=str(data.get("generation_range_hash", "")),
            round_in_generation=int(data.get("round_in_generation", 1)),
            policy_applied=str(
                data.get("policy_applied", IAR_POLICY_ITERATIVE)
            ),
            resolved_fingerprints=_coerce_fingerprints(
                data.get("resolved_fingerprints", [])
            ),
            open_fingerprints_this_gen=_coerce_fingerprints(
                data.get("open_fingerprints_this_gen", [])
            ),
            history=_coerce_history(data.get("history", [])),
            base_sha=_coerce_git_sha(data.get("base_sha", "")),
            head_sha=_coerce_git_sha(data.get("head_sha", "")),
            reviewed_label_applied=reviewed_label_applied,
        )
    except IterationStateParseError as exc:
        log(f"IAR: state parse failed: {exc}; treating as first review.")
        return None
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        log(
            f"IAR: state parse failed with {type(exc).__name__}: {exc}; "
            "treating as first review."
        )
        return None


def _fetch_latest_marker_body(
    *,
    repo: str,
    pr_number: int,
    token: str,
    provider_id: str = "",
    bot_login: str = "",
) -> str | None:
    """Fetch the most recent tracking-marker issue comment on the PR that
    carries an embedded IAR state block, and return its body. Returns
    `None` if no such marker is found or on any API failure.

    Uses GraphQL because REST issue comments do not expose `isMinimized`,
    which the ordering fallback below reads.

    When `bot_login` is non-empty, filters markers to those authored by
    that GitHub identity (matching the same `[bot]` / no-suffix
    normalisation used by `gh_collapse_previous_reviews`). This is a
    **load-bearing security control**: without the author filter, ANY
    PR participant who can comment could forge a marker carrying
    fabricated `open_fingerprints_this_gen` values, and — under the
    shipped default `collapse-previous: true` — the real bot marker
    is minimized while the attacker's fresh forgery is visible,
    winning tier 1 and silencing genuine non-critical findings on the
    next run. Author filtering closes that trust-boundary hole (the
    critical-always-surfaces rail continues to make sure `critical`
    findings surface regardless, but IAR would still lose warnings
    and infos). When `bot_login` is empty, filtering is skipped —
    kept as an escape hatch for tests and unusual callers, and for
    the rare case where `gh_get_authenticated_login` fails to resolve
    an identity at all.

    When `provider_id` is non-empty, filters markers to those carrying
    the matching `<!-- ai-pr-reviewer-provider: <provider_id> -->` tag
    (see `PROVIDER_MARKER_PREFIX`). This is load-bearing for
    multi-provider setups (e.g. a self-review matrix running both
    `cursor` and `anthropic` legs on the same PR): without the filter,
    each provider would read the OTHER provider's IAR state, cross-
    poisoning fingerprint memory, generation hashes, and round
    counters. Untagged legacy markers (posted before the provider
    marker was introduced, or by callers that omit it) match every
    provider — preserves back-compat.

    Ordering rule (load-bearing — see docs/ITERATION_AWARENESS.md § 7):

        1. Prefer the latest **non-minimized** marker that contains an
           IAR state block. This is the common path: the tracking
           comment posted at the end of the last successful review.
        2. Fall back to the latest **minimized** marker that contains
           an IAR state block. This is the collapse-previous case: the
           consumer opted into `collapse-previous: true` (the shipped
           default), so between runs the previous marker gets
           minimized by `gh_collapse_previous_reviews` — but the state
           block itself is still in the body. Without this fallback,
           every collapse-previous consumer would see IAR reset to
           `first_review` on every run and never dedup findings.
        3. Fall back to any marker (state block or not) — matches the
           legacy semantics used by callers other than IAR.

    Tier (2) rescues state across the collapse boundary so IAR's
    generation-tracking / dedup engine actually engages on the default
    config. Tier (3) preserves back-compat for the non-IAR call sites
    that just want "the last marker we posted."

    Provider filtering is applied BEFORE the three-tier ordering rule,
    so provider isolation composes cleanly with the collapse-previous
    fallback (each provider gets its own three-tier search over its
    own marker chain).
    """
    if not repo or "/" not in repo or pr_number <= 0:
        return None
    owner: str
    name: str
    owner, name = repo.split("/", 1)
    query: str = (
        "query($owner: String!, $name: String!, $pr: Int!) {\n"
        "  repository(owner: $owner, name: $name) {\n"
        "    pullRequest(number: $pr) {\n"
        "      comments(last: 100) {\n"
        "        nodes {\n"
        "          body\n"
        "          isMinimized\n"
        "          createdAt\n"
        "          author { login }\n"
        "        }\n"
        "      }\n"
        "    }\n"
        "  }\n"
        "}"
    )
    # `last: 100` is the GraphQL v4 hard cap on the `pullRequest.comments`
    # connection (server returns `EXCESSIVE_PAGINATION` for anything
    # higher). On very busy PRs where 100+ human/bot comments accumulate
    # AFTER the last state-bearing marker was minimized, IAR can fail
    # to find a state-bearing marker in the window and treat the run
    # as `first_review`. Failure mode is SAFE (over-review, never
    # under-surface) — the reviewer re-fires round-1 exhaustive rather
    # than silencing findings. See docs/ITERATION_AWARENESS.md § 7.3
    # for the follow-up cursor-pagination path; deliberately deferred
    # here to keep the runtime stdlib-only and the code path simple.
    try:
        data: Any = gh_graphql(
            query,
            {"owner": owner, "name": name, "pr": pr_number},
            token=token,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort GH API call
        log(f"IAR: _fetch_latest_marker_body GraphQL failed: {exc!r}.")
        return None
    try:
        nodes: list[dict[str, Any]] = (
            data.get("repository", {})
            .get("pullRequest", {})
            .get("comments", {})
            .get("nodes", [])
            or []
        )
    except AttributeError:
        return None
    # Author-isolation predicate (SECURITY — see docstring).
    # Match the same `[bot]` / no-suffix normalisation
    # `gh_collapse_previous_reviews` applies so `github-actions[bot]`
    # and `github-actions` both count as the same identity.
    accepted_logins: set[str] = set()
    if bot_login:
        accepted_logins.add(bot_login)
        if bot_login.endswith("[bot]"):
            accepted_logins.add(bot_login[: -len("[bot]")])

    def _author_matches(node_: dict[str, Any]) -> bool:
        if not accepted_logins:
            return True
        author: Any = node_.get("author")
        if not isinstance(author, dict):
            # `author` is null when the commenter's GitHub account
            # was deleted — never our bot; drop.
            return False
        login: str = str(author.get("login") or "")
        return login in accepted_logins

    # Provider-isolation predicate. When `provider_id` is set, only
    # markers whose body carries the exact provider marker for that
    # id (or has NO provider marker at all — the untagged legacy
    # case) participate in the three-tier search. Multiple providers
    # running the same PR (e.g. self-review matrix) therefore each
    # read only their OWN state chain — no cross-poisoning of
    # fingerprints, generation hashes, or round counters.
    expected_provider_marker: str = (
        provider_marker(provider_id) if provider_id else ""
    )

    def _provider_matches(body_: str) -> bool:
        if not expected_provider_marker:
            return True
        if expected_provider_marker in body_:
            return True
        # Untagged legacy markers (posted before the provider marker
        # was introduced, or by callers that omit it) match every
        # provider — preserves back-compat.
        return provider_id in DEFAULT_MODELS and PROVIDER_MARKER_PREFIX not in body_

    non_minimized_with_state: list[dict[str, Any]] = []
    minimized_with_state: list[dict[str, Any]] = []
    any_marker: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        body: str = str(node.get("body") or "")
        if REVIEW_MARKER not in body:
            continue
        if not _author_matches(node):
            continue
        if not _provider_matches(body):
            continue
        any_marker.append(node)
        has_state_block: bool = IAR_STATE_TAG_OPEN in body
        is_minimized: bool = node.get("isMinimized") is True
        if has_state_block and not is_minimized:
            non_minimized_with_state.append(node)
        elif has_state_block and is_minimized:
            minimized_with_state.append(node)

    def _newest(nodes_: list[dict[str, Any]]) -> dict[str, Any]:
        return sorted(nodes_, key=lambda n: str(n.get("createdAt") or ""))[-1]

    if non_minimized_with_state:
        return str(_newest(non_minimized_with_state).get("body") or "")
    if minimized_with_state:
        log(
            "IAR: no visible marker carries state; falling back to the "
            "latest minimized marker with an embedded state block "
            "(this is expected under `collapse-previous: true` — the "
            "prior tracking comment was minimized between runs)."
        )
        return str(_newest(minimized_with_state).get("body") or "")
    if any_marker:
        return str(_newest(any_marker).get("body") or "")
    return None


def read_prior_iteration_state(
    *,
    repo: str,
    pr_number: int,
    token: str,
    provider_id: str = "",
    bot_login: str = "",
) -> IterationState | None:
    """Public entry point: fetch the last marker on the PR that carries
    an IAR state block and extract it. Returns `None` on any failure —
    treated by callers as "first review of this PR".

    Reads MINIMIZED markers too when no visible marker carries state, so
    IAR persistence survives `collapse-previous: true` (the shipped
    default). See `_fetch_latest_marker_body` for the full ordering rule.

    Pass `bot_login` to enforce marker-author isolation (SECURITY —
    prevents PR participants from forging state markers that silence
    non-critical findings by supplying fake `open_fingerprints_this_gen`
    lists). Callers SHOULD always pass a resolved bot login;
    `_fetch_latest_marker_body` treats empty as "filter disabled".

    Pass `provider_id` in multi-provider setups (e.g. a self-review
    matrix running `cursor` + `anthropic` legs on the same PR) so each
    provider's IAR state chain stays isolated — otherwise the two
    providers would cross-poison each other's fingerprint memory,
    generation hashes, and round counters.
    """
    marker_body: str | None = _fetch_latest_marker_body(
        repo=repo,
        pr_number=pr_number,
        token=token,
        provider_id=provider_id,
        bot_login=bot_login,
    )
    if marker_body is None:
        log("IAR: no prior marker found; treating as first review.")
        return None
    return _parse_state_from_marker_body(marker_body)


def embed_iteration_state(
    marker_body: str, state: IterationState
) -> str:
    """Inject or replace the IAR state HTML-comment block in a marker
    body string. Deterministic: same inputs produce byte-identical output
    (JSON is dumped with `sort_keys=True`).

    Truncates `state.history` to the last IAR_HISTORY_MAX_ENTRIES entries
    at embed time so the marker body cannot grow unboundedly across many
    generations.
    """
    bounded_history: list[dict[str, Any]] = (
        state.history[-IAR_HISTORY_MAX_ENTRIES:]
        if len(state.history) > IAR_HISTORY_MAX_ENTRIES
        else state.history
    )
    bounded_state: IterationState = IterationState(
        version=state.version,
        generation=state.generation,
        generation_range_hash=state.generation_range_hash,
        round_in_generation=state.round_in_generation,
        policy_applied=state.policy_applied,
        resolved_fingerprints=list(state.resolved_fingerprints),
        open_fingerprints_this_gen=list(state.open_fingerprints_this_gen),
        history=bounded_history,
        base_sha=state.base_sha,
        head_sha=state.head_sha,
        reviewed_label_applied=state.reviewed_label_applied,
    )
    state_json: str = json.dumps(
        asdict(bounded_state), indent=2, sort_keys=True
    )
    block: str = (
        f"\n\n{IAR_STATE_TAG_OPEN}\n{state_json}\n{IAR_STATE_TAG_CLOSE}\n"
    )
    pattern: re.Pattern[str] = re.compile(
        re.escape(IAR_STATE_TAG_OPEN)
        + r".*?"
        + re.escape(IAR_STATE_TAG_CLOSE)
        + r"\n?",
        re.DOTALL,
    )
    stripped_body: str = pattern.sub("", marker_body).rstrip()
    return stripped_body + block


# ---------------------------------------------------------------------------
# Iteration-Aware Review (IAR) — generation tracking
# ---------------------------------------------------------------------------
# A "generation" is a stable diff-content window. When the developer
# pushes new commits or rebases, the content window changes → a new
# generation begins. The round counter resets; convergence policies
# re-activate (e.g. first-pass-exhaustive fires again on the fresh
# content). See docs/ITERATION_AWARENESS.md § 4.
#
# The generation counter is stored in `IterationState.generation` and
# incremented by `advance_generation()`. Detection reads
# `IterationState.generation_range_hash` + `IterationState.base_sha`
# and compares them to the current values from the diff being reviewed.


class GenerationTransition(str, Enum):
    """Which of the possible transitions the current run represents.

    Values match the strings persisted in `IterationState.policy_applied`
    when relevant, and the debug-log tags. Surfaced to developers only
    through the marker annotation (e.g. `(user_forced_reset)` after the
    round/policy tags); consumers never see them in action outputs.

    `USER_FORCED_RESET` fires ONLY when ALL FIVE conditions hold:
    (1) the consumer's `applied-label` (the "reviewed" label the
    action stamps on a successful review) is configured; (2) a prior
    IAR state exists in the tracking marker; (3) that prior state's
    `reviewed_label_applied` bit is `True` (recording that the
    reviewer previously stamped the label successfully — the
    load-bearing guard that prevents a blocked review's natural
    re-trigger from being misclassified as a deliberate reset);
    (4) the PR-labels fetch succeeded (`label_fetch_ok is True` —
    a transient GitHub 5xx returning an empty list from
    `_fetch_pr_labels` must NOT be misread as "label absent" or
    the reset gesture would silently wipe fingerprint memory on
    every transient outage — round-14 F1); and (5) the label is
    absent from the returned PR-labels list. Semantically identical
    to `FIRST_REVIEW` downstream (fresh state, no dedup memory,
    round-1 exhaustive under the default policy) — separated out
    only so the log + marker can tell developers that the reset was
    a deliberate gesture, not the first-ever review of the PR. Full
    contract in docs/ITERATION_AWARENESS.md § 8.5.
    """

    FIRST_REVIEW = "first_review"
    SAME_GENERATION = "same_generation"
    NEW_COMMITS = "new_commits"
    REBASED = "rebased"
    USER_FORCED_RESET = "user_forced_reset"


def compute_generation_range_hash(
    *,
    base_sha: str,
    head_sha: str,
    repo_root: str | None = None,
) -> str:
    """Deterministic 16-hex-char hash of the diff content between
    `base_sha` and `head_sha`.

    Two commits producing the same diff content produce the same hash →
    used to detect content-window changes across runs. Empty string is
    returned when the git subprocess fails (network hiccup, missing
    refs, sparse checkout) — callers treat that as "unknown" and
    fall back to conservative behavior (typically FIRST_REVIEW).

    Uses THREE-dot `base_sha...head_sha` (not two-dot `base_sha..head_sha`)
    so the hash mirrors the exact diff the review payload sees via
    `fetch_pr_context` (`origin/<base>...HEAD`). Two-dot would recompute
    the hash every time `origin/<base>` advanced upstream even though
    the PR-visible diff is unchanged — that produced false REBASED /
    NEW_COMMITS transitions on any label-gated re-review after the
    base branch moved, burning a full exhaustive pass and re-surfacing
    already-open warnings. Three-dot pins the comparison to the merge
    base of the two commits, matching the PR contract.
    """
    if not base_sha or not head_sha:
        return ""
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["git", "diff", f"{base_sha}...{head_sha}"],
            capture_output=True,
            check=True,
            text=True,
            cwd=repo_root,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        log(
            f"IAR: compute_generation_range_hash failed "
            f"(base={base_sha[:8]}, head={head_sha[:8]}): {exc}. "
            "Returning empty hash; caller falls back to FIRST_REVIEW."
        )
        return ""
    digest: str = hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()
    return digest[:16]


def detect_generation_change(
    *,
    prior_state: IterationState | None,
    current_range_hash: str,
    current_base_sha: str,
) -> GenerationTransition:
    """Classify what kind of transition the current run represents.

    Precedence:
    1. No prior state → `FIRST_REVIEW`.
    2. Range hash matches prior → `SAME_GENERATION` (adds a round).
    3. Base SHA changed (and we know both) → `REBASED`.
    4. Otherwise (hash mismatch, same or unknown base) → `NEW_COMMITS`.

    When `prior_state.base_sha` is empty (older marker from a prior IAR
    version that didn't persist base_sha), rebase detection is impossible
    → we default to NEW_COMMITS. This is the safest fallback: NEW_COMMITS
    still advances the generation and re-activates first-pass-exhaustive.
    """
    if prior_state is None:
        return GenerationTransition.FIRST_REVIEW
    if current_range_hash and prior_state.generation_range_hash == current_range_hash:
        return GenerationTransition.SAME_GENERATION
    prior_base: str = prior_state.base_sha
    if prior_base and current_base_sha and prior_base != current_base_sha:
        return GenerationTransition.REBASED
    return GenerationTransition.NEW_COMMITS


def advance_generation(
    *,
    prior_state: IterationState | None,
    transition: GenerationTransition,
    new_range_hash: str,
    new_base_sha: str,
    policy: str,
    new_head_sha: str = "",
) -> IterationState:
    """Return a fresh `IterationState` reflecting a generation change.

    Preserves `resolved_fingerprints` across generations (they carry
    audit-trail value across the whole PR lifetime). Appends a summary
    entry to `history` describing the closed-out generation (Task 8
    populates `tokens_used` + `wall_clock_ms`).

    For `SAME_GENERATION`, callers do NOT invoke this function — they
    just increment `round_in_generation` in place via
    `increment_round_in_generation()`.
    """
    if transition in (
        GenerationTransition.FIRST_REVIEW,
        GenerationTransition.USER_FORCED_RESET,
    ) or prior_state is None:
        log(
            f"IAR: {transition.value} — starting generation 1 "
            f"(range_hash={new_range_hash!r}, base_sha={new_base_sha[:8]!r})."
        )
        return new_iteration_state(
            generation=1,
            generation_range_hash=new_range_hash,
            round_in_generation=1,
            policy_applied=policy,
            base_sha=new_base_sha,
            head_sha=new_head_sha,
        )
    closed_gen_entry: dict[str, Any] = {
        "gen": prior_state.generation,
        "range_hash": prior_state.generation_range_hash,
        "rounds_ran": prior_state.round_in_generation,
        "converged": len(prior_state.open_fingerprints_this_gen) == 0,
        "tokens_used": 0,     # populated by Task 8 (observability)
        "wall_clock_ms": 0,   # populated by Task 8 (observability)
    }
    new_history: list[dict[str, Any]] = list(prior_state.history) + [
        closed_gen_entry
    ]
    log(
        f"IAR: generation change detected ({transition.value}). "
        f"Prior: gen={prior_state.generation}, "
        f"rounds={prior_state.round_in_generation}, "
        f"range_hash={prior_state.generation_range_hash!r}, "
        f"converged={closed_gen_entry['converged']}. "
        f"New: gen={prior_state.generation + 1}, "
        f"range_hash={new_range_hash!r}, "
        f"base_sha={new_base_sha[:8]!r}."
    )
    return IterationState(
        version=IAR_STATE_SCHEMA_VERSION,
        generation=prior_state.generation + 1,
        generation_range_hash=new_range_hash,
        round_in_generation=1,
        policy_applied=policy,
        # resolved_fingerprints crosses generations for cross-gen dedup
        # (used by `critical-gate` policy and audit trail).
        resolved_fingerprints=list(prior_state.resolved_fingerprints),
        # open_fingerprints_this_gen resets — repopulated by Task 5 dedup.
        open_fingerprints_this_gen=[],
        history=new_history,
        base_sha=new_base_sha,
        head_sha=new_head_sha,
    )


def increment_round_in_generation(
    *,
    prior_state: IterationState,
    policy: str,
    new_head_sha: str = "",
) -> IterationState:
    """For `SAME_GENERATION` transitions: bump `round_in_generation` and
    refresh `policy_applied` without touching fingerprints or history.

    `new_head_sha` refreshes the persisted head_sha so subsequent runs
    measure new-lines-pct against the most recent reviewed head, not the
    first one in the generation. When empty, the prior head is preserved.
    """
    log(
        f"IAR: SAME_GENERATION — advancing round "
        f"{prior_state.round_in_generation} → "
        f"{prior_state.round_in_generation + 1} "
        f"(gen={prior_state.generation})."
    )
    return IterationState(
        version=prior_state.version,
        generation=prior_state.generation,
        generation_range_hash=prior_state.generation_range_hash,
        round_in_generation=prior_state.round_in_generation + 1,
        policy_applied=policy,
        resolved_fingerprints=list(prior_state.resolved_fingerprints),
        open_fingerprints_this_gen=list(
            prior_state.open_fingerprints_this_gen
        ),
        history=list(prior_state.history),
        base_sha=prior_state.base_sha,
        head_sha=new_head_sha or prior_state.head_sha,
    )


# ---------------------------------------------------------------------------
# Provider-independent review payload
# ---------------------------------------------------------------------------
#
# The IAR dedup engine consumes `Finding` instances (defined immediately
# below) — so the fingerprinting + dedup helpers live AFTER the `Finding`
# dataclass to avoid forward references. See the "Iteration-Aware Review
# (IAR) — fingerprinting + dedup engine" block further down the file.


@dataclass
class Finding:
    """A single inline finding, provider-independent.

    Both provider families (chat-completions via `Provider` and agent-runner
    via `AgentRunnerProvider`) surface findings as this dataclass so the
    downstream submission / label / strictness paths never need to know
    which provider produced the review.
    """

    path: str
    line: int
    body: str
    severity: str = SEVERITY_INFO
    start_line: int | None = None
    side: str | None = "RIGHT"
    # Content-anchored fingerprint (set by the IAR post-LLM step). When
    # present, the inline comment carries it in a hidden marker so the next
    # round can match the finding back from the PR thread.
    fingerprint: str | None = None


@dataclass
class ReviewResult:
    """Provider-independent review payload consumed by the submission path."""

    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    overall_severity: str = SEVERITY_NONE
    # Token/cost usage captured for this review (None = not captured).
    usage: UsageTelemetry | None = None
    # Incremental mode: the model's verdict per prior finding fingerprint.
    prior_finding_updates: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Optional PR-level metadata from chat-completions tools or agent-runner
    # findings.json (see `parse_complexity_level`, `resolve_pr_complexity`).
    complexity: str | None = None
    # Agent-runner degrade (v2.2.0+): the CLI exited 0 without writing its
    # findings file. The summary explains it; `main()` never lets an
    # incomplete review green the check or stamp the reviewed label.
    incomplete: bool = False


def incomplete_review_gate(strictness: str, cli_name: str) -> tuple[bool, str]:
    """Gate verdict for an incomplete agent-runner review.

    A review that never produced the contract output is not a clean review:
    every blocking strictness fails the check (the PR was not reviewed);
    only `lenient` — "never blocks" — stays green, and even then the
    reviewed label is not stamped.
    """
    reason: str = (
        f"incomplete review — {cli_name} ended without writing its findings "
        "file; re-run the review"
    )
    if strictness == STRICTNESS_LENIENT:
        return False, reason + " (lenient — check stays green)"
    return True, reason


# ---------------------------------------------------------------------------
# Iteration-Aware Review (IAR) — fingerprinting + dedup engine
# ---------------------------------------------------------------------------
# The dedup engine consumes `Finding` (defined above) and prior
# `IterationState` (defined near the top of the file). Every convergence
# policy in Tasks 6/7 flows through `dedupe_findings_against_prior`; the
# critical-always-surfaces safety rail is hardcoded INSIDE that function
# and MUST NOT be moved into a policy — that's the load-bearing
# correctness invariant of the whole subsystem
# (docs/ITERATION_AWARENESS.md § 7.1).


@dataclass(frozen=True)
class CodeContext:
    """Immutable snapshot of a file's contents at a specific SHA. Used to
    ground the finding fingerprint in the actual code around the anchor
    line — so a small refactor around a warning shifts the fingerprint
    and the warning re-surfaces (correct behavior)."""

    path: str
    lines: tuple[str, ...]

    def lines_around(self, line: int, radius: int) -> list[str]:
        """Return up to `2*radius + 1` lines centered on the 1-indexed
        anchor. Handles boundary cases (near start / end of file) by
        truncating rather than raising."""
        if not self.lines:
            return []
        start: int = max(1, line - radius)
        end: int = min(len(self.lines), line + radius)
        return list(self.lines[start - 1:end])


def load_code_context(
    *, path: str, review_sha: str, repo_root: str | None = None
) -> CodeContext | None:
    """Read a file's contents at a specific SHA via `git show <sha>:<path>`.

    Returns `None` if the file didn't exist at that SHA (deleted,
    pre-add, or the SHA doesn't resolve). Uses `safe_repo_path` to
    reject any path that escapes the workspace (DO #7).
    """
    if not path or not review_sha:
        return None
    try:
        safe_target: Path = safe_repo_path(path)
    except ValueError as exc:
        log(f"IAR: load_code_context refused path {path!r}: {exc}.")
        return None
    repo_root_path: Path = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()
    try:
        rel_str: str = str(safe_target.relative_to(repo_root_path))
    except ValueError:
        rel_str = path
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["git", "show", f"{review_sha}:{rel_str}"],
            capture_output=True,
            check=True,
            text=True,
            cwd=repo_root,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        # File missing at this SHA is a normal case (e.g. newly-added
        # file where review_sha predates the add); log at debug level.
        log(
            f"IAR: load_code_context({rel_str}@{review_sha[:8]}) "
            f"unavailable: {exc}."
        )
        return None
    return CodeContext(
        path=rel_str, lines=tuple(result.stdout.splitlines())
    )


def finding_fingerprint(
    *, finding: Finding, code_context: CodeContext | None
) -> str:
    """Deterministic 16-hex-char content-anchored hash of a single
    finding.

    Two runs producing the same finding on the same code produce the
    same fingerprint. Code changes around the anchor produce a
    different fingerprint (correct re-surfacing when new commits land).

    Fingerprint inputs:
    - `path` + `line` + `severity` — the coarse anchor.
    - First 200 chars of `body` — the finding identity (dedupes
      re-worded restatements of the same finding).
    - Hash of `2 * IAR_CONTEXT_HASH_RADIUS + 1` lines around the anchor
      — content-anchored. When `code_context` is missing (file didn't
      exist at review SHA), falls back to the string "no_context" so
      the fingerprint stays deterministic across runs.
    """
    if code_context is not None:
        context_lines: list[str] = code_context.lines_around(
            finding.line, IAR_CONTEXT_HASH_RADIUS
        )
        context_hash: str = hashlib.sha256(
            "\n".join(context_lines).encode("utf-8")
        ).hexdigest()[:16]
    else:
        context_hash = "no_context"
    payload: str = (
        f"{finding.path}|{finding.line}|{finding.severity}|"
        f"{finding.body[:IAR_FINGERPRINT_BODY_PREFIX_CHARS]}|{context_hash}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class SilencedFinding:
    """A finding that the dedup engine chose NOT to surface, with a
    machine-readable reason. Aggregate surfaced/silenced counts are
    rendered in the marker annotation and the post-LLM debug log
    (`run_iar_post_llm`).
    """

    finding: Finding
    reason: str


@dataclass(frozen=True)
class DedupResult:
    """Typed return of `dedupe_findings_against_prior`.

    - `surfaced`: findings the reviewer will submit to GitHub.
    - `silenced`: findings suppressed by dedup (with reasons).
    - `fingerprints_by_finding`: index → fingerprint for the caller to
      write into the updated `IterationState.open_fingerprints_this_gen`.
    """

    surfaced: list[Finding]
    silenced: list[SilencedFinding]
    fingerprints_by_finding: dict[int, str]


def dedupe_findings_against_prior(
    *,
    new_findings: list[Finding],
    prior_state: IterationState | None,
    code_contexts: dict[str, CodeContext | None],
    strict_cross_gen: bool = False,
) -> DedupResult:
    """Filter `new_findings` against prior IAR state.

    CRITICAL SAFETY RAIL (docs/ITERATION_AWARENESS.md § 7.1):
    Findings with `severity == "critical"` ALWAYS surface, unconditionally,
    regardless of whether their fingerprint matches a prior open/resolved
    finding. This rule is HARDCODED inside this function and MUST NOT be
    moved into a policy, made configurable, or moved to a caller. Doing
    so is a critical safety bug. Every policy path in Tasks 6/7 goes
    through this function precisely so this safety rail cannot be
    accidentally bypassed.

    Non-critical dedup behavior (default — `strict_cross_gen=False`):
    - `first review` (prior_state is None) → all findings surface.
    - Fingerprint matches `prior_state.open_fingerprints_this_gen` →
      silence with reason "already reported in gen N, unresolved".
    - Fingerprint matches `prior_state.resolved_fingerprints` → surface
      (regression signal — the finding was resolved but re-appeared).
      Caller may attach a "previously resolved" annotation.
    - Otherwise → surface.

    Strict cross-generation dedup (`strict_cross_gen=True`, used by the
    `critical-gate` policy in Task 7):
    - Fingerprint matches `prior_state.resolved_fingerprints` → silence
      instead of surfacing (treats resolved status as permanent for
      non-critical findings across generations). Critical severity
      still surfaces via the hardcoded safety rail above.
    """
    fingerprints_by_finding: dict[int, str] = {}
    surfaced: list[Finding] = []
    silenced: list[SilencedFinding] = []
    if prior_state is None:
        for i, finding in enumerate(new_findings):
            fingerprints_by_finding[i] = finding_fingerprint(
                finding=finding,
                code_context=code_contexts.get(finding.path),
            )
        return DedupResult(
            surfaced=list(new_findings),
            silenced=[],
            fingerprints_by_finding=fingerprints_by_finding,
        )
    known_open: set[str] = set(prior_state.open_fingerprints_this_gen)
    known_resolved: set[str] = set(prior_state.resolved_fingerprints)
    for i, finding in enumerate(new_findings):
        fp: str = finding_fingerprint(
            finding=finding,
            code_context=code_contexts.get(finding.path),
        )
        fingerprints_by_finding[i] = fp
        # >>> CRITICAL SAFETY RAIL — DO NOT MOVE, DO NOT GATE, DO NOT WEAKEN.
        # docs/ITERATION_AWARENESS.md § 7.1 pins this behavior. Every
        # convergence policy in Tasks 6/7 relies on this branch being
        # here and being unconditional.
        if finding.severity == SEVERITY_CRITICAL:
            surfaced.append(finding)
            continue
        # <<< end critical safety rail.
        if fp in known_open:
            silenced.append(
                SilencedFinding(
                    finding=finding,
                    reason=(
                        f"already reported in gen "
                        f"{prior_state.generation}, unresolved"
                    ),
                )
            )
            continue
        if strict_cross_gen and fp in known_resolved:
            silenced.append(
                SilencedFinding(
                    finding=finding,
                    reason=(
                        "previously resolved in an earlier generation; "
                        "cross-generation dedup active (critical-gate policy)"
                    ),
                )
            )
            continue
        # Default: `known_resolved` matches surface (regression signal);
        # the caller may attach a "previously resolved" annotation.
        surfaced.append(finding)
    return DedupResult(
        surfaced=surfaced,
        silenced=silenced,
        fingerprints_by_finding=fingerprints_by_finding,
    )


def resolve_finding_status(
    *,
    prior_open_fingerprints: list[str],
    current_fps: dict[int, str],
) -> tuple[list[str], list[str]]:
    """Given the prior generation's open fingerprints and the current
    run's fingerprints (indexed by finding), return
    `(still_open, newly_resolved)` fingerprint lists.

    A fingerprint is:
    - `still_open` if the current run produced a matching one → the
      finding is still present.
    - `newly_resolved` if the prior fingerprint has no match in the
      current run → the finding was fixed OR the code around it changed
      enough that the fingerprint no longer matches. Either way, it is
      no longer reported and moves to `resolved_fingerprints`.

    Deterministic ordering — sorts both lists so the marker embed step
    produces byte-identical output for byte-identical inputs.
    """
    current_fp_set: set[str] = set(current_fps.values())
    still_open: list[str] = sorted(
        fp for fp in prior_open_fingerprints if fp in current_fp_set
    )
    newly_resolved: list[str] = sorted(
        fp
        for fp in prior_open_fingerprints
        if fp not in current_fp_set
    )
    return still_open, newly_resolved


# ---------------------------------------------------------------------------
# Iteration-Aware Review (IAR) — convergence policies (Tasks 6 + 7)
# ---------------------------------------------------------------------------
# Every policy returns a `PolicyResult` — a small typed struct that
# carries the two things a policy can influence:
# * `effective_max_inline_comments` + `prompt_addendum` (consumed BEFORE
#   the LLM call, to shape the prompt + cap).
# * `findings_to_surface` + `findings_silenced` (consumed AFTER the LLM
#   call, to filter its output).
#
# All policies flow through `dedupe_findings_against_prior` (Task 5) so
# the hardcoded critical-always-surfaces safety rail is respected — no
# policy can bypass or weaken it.
#
# Cap multiplication raises the tool-call ceiling (max-inline-comments),
# not `max_tokens` or `MAX_TURNS`. See AGENTS.md DON'T #9.


@dataclass(frozen=True)
class PolicyResult:
    """Return type of every `apply_*_policy` function. Consumed by
    Task 8's `main()` integration — the two `prompt_*` / `effective_*`
    fields shape the LLM call, the two `findings_*` fields shape the
    submission."""

    findings_to_surface: list[Finding]
    findings_silenced: list[SilencedFinding]
    effective_max_inline_comments: int
    prompt_addendum: str
    policy_applied: str


def apply_iterative_policy(
    *,
    findings: list[Finding],
    prior_state: IterationState | None,
    code_contexts: dict[str, "CodeContext | None"],
    base_max_inline_comments: int,
) -> PolicyResult:
    """The default IAR policy: dedup only. Findings whose fingerprint
    matches `prior_state.open_fingerprints_this_gen` are silenced;
    everything else surfaces. `severity == critical` always surfaces
    (Task 5's safety rail).

    Steady-state cost is close to a non-dedup baseline: the LLM produces
    the same set of findings, but the reviewer only submits deltas —
    saving tokens on the GitHub API submission side (small) and reducing
    developer noise (large)."""
    dedup: DedupResult = dedupe_findings_against_prior(
        new_findings=findings,
        prior_state=prior_state,
        code_contexts=code_contexts,
    )
    return PolicyResult(
        findings_to_surface=list(dedup.surfaced),
        findings_silenced=list(dedup.silenced),
        effective_max_inline_comments=base_max_inline_comments,
        prompt_addendum="",
        policy_applied=IAR_POLICY_ITERATIVE,
    )


def apply_first_pass_exhaustive_policy(
    *,
    findings: list[Finding],
    prior_state: IterationState | None,
    code_contexts: dict[str, "CodeContext | None"],
    base_max_inline_comments: int,
    cap_multiplier: int,
    is_round_1_of_generation: bool,
) -> PolicyResult:
    """Round 1 of each generation: exhaustive prompt splicing + cap
    multiplication. Rounds 2+: delegate to `apply_iterative_policy`
    (dedup only).

    "Round 1 of each generation" means either:
    - First-ever review of the PR (FIRST_REVIEW), OR
    - First review after `advance_generation()` was called for a
      NEW_COMMITS / REBASED transition.

    On round 1, the caller MUST also splice `PolicyResult.prompt_addendum`
    into the system prompt AND raise the LLM's max-inline-comments to
    `PolicyResult.effective_max_inline_comments` BEFORE invoking the
    model. This function's post-LLM job is to truncate the model's
    output at the increased cap — nothing more. (Critical-always-
    surfaces still applies via dedup path on rounds 2+.)
    """
    if is_round_1_of_generation:
        # Round-1 exhaustive: raise the inline-comments ceiling and
        # splice the addendum. `findings` at this point is already the
        # LLM's output (produced with the raised cap upstream); we
        # truncate defensively in case the model produced more.
        #
        # Criticals-first sort BEFORE truncation is load-bearing for
        # the hardcoded critical-always-surfaces safety rail (docs
        # § 7.1). A naive `findings[:effective_cap]` would drop
        # criticals if the model happened to emit them past position N,
        # silently bypassing the rail on round-1 of every generation.
        # `_sort_findings_criticals_first` preserves the model's
        # within-tier ordering (so info/warning ordering stays intact
        # within their tiers) while lifting all criticals to the front —
        # the tail truncation then only ever sheds warnings/infos.
        effective_cap: int = base_max_inline_comments * cap_multiplier
        prioritized: list[Finding] = _sort_findings_criticals_first(findings)
        return PolicyResult(
            findings_to_surface=list(prioritized[:effective_cap]),
            findings_silenced=[],
            effective_max_inline_comments=effective_cap,
            prompt_addendum=IAR_EXHAUSTIVE_PROMPT_ADDENDUM,
            policy_applied=IAR_POLICY_FIRST_PASS_EXHAUSTIVE,
        )
    # Rounds 2+ of the same generation: iterative dedup takes over.
    # The critical-always-surfaces safety rail lives inside the dedup
    # engine, so it applies transparently here.
    iterative_result: PolicyResult = apply_iterative_policy(
        findings=findings,
        prior_state=prior_state,
        code_contexts=code_contexts,
        base_max_inline_comments=base_max_inline_comments,
    )
    # Preserve the policy name for observability — on rounds 2+ the
    # user configured `first-pass-exhaustive` even though today's run
    # applied iterative internally. Marker state records what actually
    # ran, so we return "first-pass-exhaustive" for the audit trail
    # while the behavior is identical to iterative.
    return PolicyResult(
        findings_to_surface=iterative_result.findings_to_surface,
        findings_silenced=iterative_result.findings_silenced,
        effective_max_inline_comments=iterative_result.effective_max_inline_comments,
        prompt_addendum="",
        policy_applied=IAR_POLICY_FIRST_PASS_EXHAUSTIVE,
    )


# The two `policy_applied` string values below are outputs of the
# round-capped policy so consumers can distinguish "still under cap"
# from "cap reached, only criticals surfacing".
IAR_POLICY_ROUND_CAPPED_PRE_CAP: str = "round-capped-pre-cap"
IAR_POLICY_ROUND_CAPPED_POST_CAP: str = "round-capped-post-cap"
# When the escape label short-circuits dedup for one run.
IAR_POLICY_ESCAPE_LABEL_FORCED: str = "escape-label-forced-full-review"
# When the 30% new-lines safety net forces first-pass-exhaustive.
IAR_POLICY_SAFETY_NET_FORCED: str = "safety-net-forced-first-pass-exhaustive"


def apply_round_capped_policy(
    *,
    findings: list[Finding],
    prior_state: IterationState | None,
    code_contexts: dict[str, "CodeContext | None"],
    base_max_inline_comments: int,
    max_rounds: int,
    is_round_1_of_generation: bool,
) -> PolicyResult:
    """After N rounds in the current generation, only critical findings
    surface — non-critical warnings/infos are silenced with a "cap
    reached" reason.

    Pre-cap: behaves like `iterative` (dedup only via
    `dedupe_findings_against_prior`).
    Post-cap: this function itself filters to
    `severity == SEVERITY_CRITICAL` and silences the rest with a
    "cap reached" reason — it does NOT call the dedup engine. The
    critical-always-surfaces invariant still holds because the
    filter keeps every critical; the dedup rail is simply not on
    this path (generation-fresh fingerprints + prior resolved set
    are irrelevant once only criticals remain).

    `max_rounds == 0` means unlimited (post-cap never triggers).

    `is_round_1_of_generation` is load-bearing when transitions happen:
    on `NEW_COMMITS`, `REBASED`, `USER_FORCED_RESET`, or `FIRST_REVIEW`,
    the round counter resets to 1 for the new generation. Without this
    parameter, `current_round` would be computed from the prior gen's
    counter (`prior_state.round_in_generation + 1`) and a consumer with
    e.g. `max_rounds=3` who pushed new commits after a converged
    generation would land in the post-cap path on run 1 of the new
    generation and see all non-critical findings silenced. `dispatch_policy`
    computes and passes the flag exactly as it does to
    `apply_first_pass_exhaustive_policy` — the two policies must agree
    on when a round-1 restart is happening.
    """
    if is_round_1_of_generation:
        # New generation → round counter restarts at 1, regardless of
        # the prior state's counter. Never lands in post-cap on the
        # first round of a fresh generation.
        current_round: int = 1
    else:
        # +1 because this run IS a round in the current generation; if
        # prior_state.round_in_generation == max_rounds, THIS run is the
        # first one past the cap. `prior_state is None` is impossible
        # here (would have set is_round_1_of_generation=True upstream)
        # but keep the defensive guard so a future refactor can't
        # silently reintroduce a NoneType access.
        current_round = (
            1 if prior_state is None else prior_state.round_in_generation + 1
        )
    if max_rounds > 0 and current_round > max_rounds:
        critical_only: list[Finding] = [
            f for f in findings if f.severity == SEVERITY_CRITICAL
        ]
        silenced: list[SilencedFinding] = [
            SilencedFinding(
                finding=f,
                reason=(
                    f"round cap ({max_rounds}) reached; non-critical "
                    "suppressed"
                ),
            )
            for f in findings
            if f.severity != SEVERITY_CRITICAL
        ]
        return PolicyResult(
            findings_to_surface=critical_only,
            findings_silenced=silenced,
            effective_max_inline_comments=base_max_inline_comments,
            prompt_addendum="",
            policy_applied=IAR_POLICY_ROUND_CAPPED_POST_CAP,
        )
    iterative_result: PolicyResult = apply_iterative_policy(
        findings=findings,
        prior_state=prior_state,
        code_contexts=code_contexts,
        base_max_inline_comments=base_max_inline_comments,
    )
    return PolicyResult(
        findings_to_surface=iterative_result.findings_to_surface,
        findings_silenced=iterative_result.findings_silenced,
        effective_max_inline_comments=iterative_result.effective_max_inline_comments,
        prompt_addendum="",
        policy_applied=IAR_POLICY_ROUND_CAPPED_PRE_CAP,
    )


def apply_critical_gate_policy(
    *,
    findings: list[Finding],
    prior_state: IterationState | None,
    code_contexts: dict[str, "CodeContext | None"],
    base_max_inline_comments: int,
) -> PolicyResult:
    """Strict cross-generation dedup. Same as `iterative` for the
    open-fingerprints path, but also silences findings whose fingerprint
    matches `prior_state.resolved_fingerprints` (treating resolved
    status as permanent across generations).

    Critical severity findings still surface unconditionally via the
    hardcoded safety rail in `dedupe_findings_against_prior`.
    """
    dedup: DedupResult = dedupe_findings_against_prior(
        new_findings=findings,
        prior_state=prior_state,
        code_contexts=code_contexts,
        strict_cross_gen=True,
    )
    return PolicyResult(
        findings_to_surface=list(dedup.surfaced),
        findings_silenced=list(dedup.silenced),
        effective_max_inline_comments=base_max_inline_comments,
        prompt_addendum="",
        policy_applied=IAR_POLICY_CRITICAL_GATE,
    )


def should_force_exhaustive_via_safety_net(
    *,
    transition: GenerationTransition,
    new_lines_pct: float,
    threshold_pct: int = IAR_SAFETY_NET_NEW_LINES_PCT,
) -> bool:
    """Returns True when the current run represents a NEW_COMMITS or
    REBASED transition AND the generation change brought at least
    `threshold_pct` new lines relative to the total diff.

    When True, the dispatcher overrides the configured policy back to
    `first-pass-exhaustive` for this run's round-1 pass — protecting
    against the "PR grew significantly; critical findings in new code
    might otherwise get silenced" scenario. Safety net never fires on
    SAME_GENERATION or FIRST_REVIEW.
    """
    if transition not in (
        GenerationTransition.NEW_COMMITS,
        GenerationTransition.REBASED,
    ):
        return False
    return new_lines_pct >= float(threshold_pct)


def compute_new_lines_pct(
    *,
    prior_base_sha: str,
    prior_head_sha: str,
    current_base_sha: str,
    current_head_sha: str,
    repo_root: str | None = None,
) -> float:
    """Estimate the percentage of net-new lines introduced by the
    current generation vs the prior one.

    Formula: `new_added / max(total_current, 1) * 100`, where
    `total_current = added + removed` across all files in the
    three-dot diff `current_base_sha...current_head_sha` (matching
    the PR-visible diff pinned to the merge base — see
    docs/ITERATION_AWARENESS.md § 4.3 for why three-dot), and
    `new_added` counts only lines added since `prior_head..current_head`
    (net new — this one is two-dot on purpose because both SHAs are
    head SHAs on the same branch, no merge-base semantics apply).

    Best-effort: returns `0.0` on any git subprocess failure so the
    safety net defaults to "no override" rather than crashing the run.
    """
    if not current_base_sha or not current_head_sha:
        return 0.0
    try:
        total_stat: subprocess.CompletedProcess[str] = subprocess.run(
            [
                "git", "diff", "--numstat",
                # Three-dot: same convention as compute_generation_range_hash
                # and fetch_pr_context — pins the diff to the merge base
                # so upstream base-branch movement doesn't inflate the
                # denominator (see docs/ITERATION_AWARENESS.md § 4.3).
                f"{current_base_sha}...{current_head_sha}",
            ],
            capture_output=True,
            check=True,
            text=True,
            cwd=repo_root,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return 0.0
    total_added: int = 0
    total_removed: int = 0
    for line in total_stat.stdout.splitlines():
        parts: list[str] = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            total_added += int(parts[0]) if parts[0] != "-" else 0
            total_removed += int(parts[1]) if parts[1] != "-" else 0
        except ValueError:
            continue
    total: int = total_added + total_removed
    if total <= 0:
        return 0.0
    # Net-new since the prior run — only relevant when we have a prior
    # head to diff against. Fall back to the whole current diff when we
    # don't (first review; the safety net won't fire anyway because the
    # transition will be FIRST_REVIEW).
    if not prior_head_sha:
        return 0.0
    try:
        new_stat: subprocess.CompletedProcess[str] = subprocess.run(
            [
                "git", "diff", "--numstat",
                f"{prior_head_sha}..{current_head_sha}",
            ],
            capture_output=True,
            check=True,
            text=True,
            cwd=repo_root,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return 0.0
    new_added: int = 0
    for line in new_stat.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            new_added += int(parts[0]) if parts[0] != "-" else 0
        except ValueError:
            continue
    return (new_added / float(total)) * 100.0


def _labels_contain_ci(*, needle: str, haystack: list[str]) -> bool:
    """Case-insensitive, whitespace-trimmed label membership check.

    GitHub labels preserve the case they were created with but the
    reviewer's public contract (like `label-gate`) treats them
    case-insensitively — see `resolve_trigger_action` (`gating on
    exact case is a foot-gun`). This helper centralises the same
    normalisation for the three OTHER label comparisons that
    influence review behaviour: `iteration-escape-label`,
    `skip-review-label`, and the reviewed-label-based
    USER_FORCED_RESET / `compute_reviewed_label_applied` path.

    Consistency here matters most for USER_FORCED_RESET: a casing
    mismatch between the configured `applied-label` and the label
    GitHub returns on the PR would look identical to "reviewed
    label deliberately removed" and silently wipe fingerprint
    memory on the next run — the opposite of what the developer
    intended (round-8 F3).
    """
    needle_norm: str = needle.strip().lower()
    if not needle_norm:
        return False
    return any(lbl.strip().lower() == needle_norm for lbl in haystack)


def detect_skip_label_collisions(
    *,
    skip_review_label: str,
    label_gate: str,
    applied_label: str,
    iteration_escape_label: str,
) -> list[str]:
    """Return a list of human-readable collision descriptions when
    `skip-review-label` matches any of the runtime's other semantic
    labels. Empty list means safe to proceed. Used by `main()` to
    fail loudly on misconfiguration before the reviewer runs, rather
    than silently converting every trigger into a skip.

    The three collision cases:
      - **label-gate:** the label that gates whether the reviewer
        runs at all. If `skip-review-label == label-gate`, then
        applying the gate label to request a review IMMEDIATELY
        cancels the request. Every gated review silently skips.
      - **applied-label:** the label stamped by the reviewer on
        successful completion. If `skip-review-label == applied-
        label`, then the first successful review arms the skip on
        every subsequent trigger — state freezes at round 1
        forever, IAR never advances, no new findings ever surface.
      - **iteration-escape-label:** the "force a full un-deduped
        review" gesture. If `skip-review-label == escape-label`,
        the developer's request for a thorough re-review is
        silently converted into a skip — the exact opposite of
        the requested behaviour.

    Empty strings for `label-gate` / `applied-label` are ignored
    (means "not configured"). The escape label always has a value
    (`IAR_DEFAULT_ESCAPE_LABEL` if unset) so it is always checked.
    All comparisons are case-insensitive to match `_labels_contain_ci`
    semantics at the runtime check sites.
    """
    skip_norm: str = skip_review_label.strip().lower()
    if not skip_norm:
        return []
    collisions: list[str] = []
    if label_gate and skip_norm == label_gate.strip().lower():
        collisions.append(f"label-gate ({label_gate!r})")
    if applied_label and skip_norm == applied_label.strip().lower():
        collisions.append(f"applied-label ({applied_label!r})")
    escape_norm: str = iteration_escape_label.strip().lower()
    if escape_norm and skip_norm == escape_norm:
        collisions.append(
            f"iteration-escape-label ({iteration_escape_label!r})"
        )
    return collisions


def check_escape_label(
    *, pr_labels: list[str], escape_label: str
) -> bool:
    """Returns True when a human has applied the escape label to the
    PR. When True, the dispatcher short-circuits dedup for THIS run
    only — persisted state is NOT mutated so subsequent normal runs
    resume from where they left off. Removing the label restores
    normal IAR behavior. Match is case-insensitive (see
    `_labels_contain_ci`)."""
    return _labels_contain_ci(needle=escape_label, haystack=pr_labels)


def dispatch_policy(
    *,
    iar_config: IARConfig,
    findings: list[Finding],
    prior_state: IterationState | None,
    code_contexts: dict[str, "CodeContext | None"],
    base_max_inline_comments: int,
    transition: GenerationTransition,
    new_lines_pct: float,
    pr_labels: list[str],
) -> PolicyResult:
    """Top-level IAR policy dispatch. Order of precedence (highest → lowest):

    1. USER_FORCED_RESET transition → falls through to normal policy
       dispatch with the reset already applied upstream (prior_state
       has been cleared to None by `run_iar_pre_llm`). The reset is
       the stronger of the two exhaustive-triggering gestures — it
       DISCARDS state, whereas the escape label only bypasses dedup
       for one run with state preserved. When a user applies BOTH
       gestures the intent is "start clean," so we defer to the
       reset semantics and skip the escape-label short-circuit
       (docs/ITERATION_AWARENESS.md § 8.5 precedence).
    2. Escape label short-circuit → surface all findings; no dedup;
       NO state mutation for this run.
    3. Safety net (>= 30% new lines on NEW_COMMITS or REBASED) → force
       `first-pass-exhaustive` for this round regardless of configured
       policy.
    4. Configured `iar_config.policy` → one of iterative,
       first-pass-exhaustive, round-capped, critical-gate.
    5. Unknown policy (should be unreachable — `build_iar_config`
       already falls back) → iterative + warning log.
    """
    if transition != GenerationTransition.USER_FORCED_RESET and check_escape_label(
        pr_labels=pr_labels, escape_label=iar_config.escape_label
    ):
        log(
            f"IAR: escape label {iar_config.escape_label!r} detected — "
            "bypassing dedup for this run only. Persisted state unchanged."
        )
        return PolicyResult(
            findings_to_surface=list(findings),
            findings_silenced=[],
            effective_max_inline_comments=base_max_inline_comments,
            prompt_addendum="",
            policy_applied=IAR_POLICY_ESCAPE_LABEL_FORCED,
        )
    is_round_1_of_generation: bool = (
        prior_state is None
        or transition != GenerationTransition.SAME_GENERATION
    )
    if should_force_exhaustive_via_safety_net(
        transition=transition, new_lines_pct=new_lines_pct
    ):
        log(
            f"IAR: safety net triggered ({new_lines_pct:.1f}% new lines "
            f">= {IAR_SAFETY_NET_NEW_LINES_PCT}% threshold on "
            f"{transition.value}) — forcing "
            f"{IAR_POLICY_FIRST_PASS_EXHAUSTIVE}."
        )
        result: PolicyResult = apply_first_pass_exhaustive_policy(
            findings=findings,
            prior_state=prior_state,
            code_contexts=code_contexts,
            base_max_inline_comments=base_max_inline_comments,
            cap_multiplier=iar_config.cap_multiplier,
            is_round_1_of_generation=True,
        )
        return PolicyResult(
            findings_to_surface=result.findings_to_surface,
            findings_silenced=result.findings_silenced,
            effective_max_inline_comments=result.effective_max_inline_comments,
            prompt_addendum=result.prompt_addendum,
            policy_applied=IAR_POLICY_SAFETY_NET_FORCED,
        )
    if iar_config.policy == IAR_POLICY_ITERATIVE:
        return apply_iterative_policy(
            findings=findings,
            prior_state=prior_state,
            code_contexts=code_contexts,
            base_max_inline_comments=base_max_inline_comments,
        )
    if iar_config.policy == IAR_POLICY_FIRST_PASS_EXHAUSTIVE:
        return apply_first_pass_exhaustive_policy(
            findings=findings,
            prior_state=prior_state,
            code_contexts=code_contexts,
            base_max_inline_comments=base_max_inline_comments,
            cap_multiplier=iar_config.cap_multiplier,
            is_round_1_of_generation=is_round_1_of_generation,
        )
    if iar_config.policy == IAR_POLICY_ROUND_CAPPED:
        return apply_round_capped_policy(
            findings=findings,
            prior_state=prior_state,
            code_contexts=code_contexts,
            base_max_inline_comments=base_max_inline_comments,
            max_rounds=iar_config.max_review_rounds,
            is_round_1_of_generation=is_round_1_of_generation,
        )
    if iar_config.policy == IAR_POLICY_CRITICAL_GATE:
        return apply_critical_gate_policy(
            findings=findings,
            prior_state=prior_state,
            code_contexts=code_contexts,
            base_max_inline_comments=base_max_inline_comments,
        )
    log(
        f"IAR: unreachable — unknown convergence-policy "
        f"{iar_config.policy!r}; falling back to iterative."
    )
    return apply_iterative_policy(
        findings=findings,
        prior_state=prior_state,
        code_contexts=code_contexts,
        base_max_inline_comments=base_max_inline_comments,
    )


# ---------------------------------------------------------------------------
# Iteration-Aware Review (IAR) — observability + main() integration (Task 8)
# ---------------------------------------------------------------------------
# Wires the engine (Tasks 1–7) into the reviewer's `main()`. Two touchpoints:
#
#   1. Pre-LLM: `run_iar_pre_llm()` reads prior state, computes the
#      generation transition, calls `dispatch_policy` with empty findings
#      to extract `effective_max_inline_comments` + `prompt_addendum`, and
#      returns a bundle the caller uses to shape the LLM call.
#
#   2. Post-LLM: `run_iar_post_llm()` re-runs `dispatch_policy` with the
#      LLM's actual findings to get the surfacing decision, mutates
#      `result.findings` in place, and returns the new IterationState to
#      embed in the tracking marker + telemetry to write to outputs.
#
# Both touchpoints are wrapped in `try/except` at the `main()` call site
# (see `tests/test_iar_failure_fallback.py` for the safety contract). On
# any IAR failure the reviewer logs the exception, leaves the 5 IAR
# outputs as empty strings (populated by `write_iar_outputs_empty()`),
# and falls through to the baseline review path — the CI check still
# gets a review, IAR just skips that run.
#
# `tokens_used` is a best-effort field. Populating it accurately requires
# per-provider instrumentation (Anthropic's `usage.input_tokens`/`output_tokens`,
# OpenAI's `usage.prompt_tokens`/`completion_tokens`, etc.), which is out of
# scope for Task 8 — the field ships as `0` for now with the schema pinned
# so a future provider-hook PR can populate it without changing the
# public output contract. `wall_clock_ms` is always populated (monotonic).


@dataclass
class RunTelemetry:
    """Mutable telemetry populated across a single run. Consumed by the
    IAR post-LLM step to write action outputs and the `history` entry.

    - `start_time_monotonic`: seconds from `time.monotonic()` at run start.
      `wall_clock_ms` is computed at write-time so the caller doesn't have
      to remember to call `.finalize()`.
    - `tokens_used`: best-effort token estimate. See module comment.
    - `estimated_baseline_tokens`: what the LLM would have consumed WITHOUT
      IAR (i.e. with the baseline `max_inline_comments` cap and no prompt
      addendum). Used to compute the cost-vs-baseline output.
    """

    start_time_monotonic: float = 0.0
    tokens_used: int = 0
    estimated_baseline_tokens: int = 0
    # Real usage captured this run (v2.1.0+); `tokens_used` mirrors its total.
    usage: UsageTelemetry = field(default_factory=UsageTelemetry)

    def wall_clock_ms(self) -> int:
        """Elapsed wall-clock ms since `start_time_monotonic` was set."""
        if not self.start_time_monotonic:
            return 0
        return int((time.monotonic() - self.start_time_monotonic) * 1000)


@dataclass(frozen=True)
class PriorFinding:
    """One of the bot's own inline findings still open on the PR, read back
    from a review thread (v2.1.0+ incremental mode)."""

    thread_id: str
    comment_id: str          # GraphQL node id
    comment_database_id: int  # REST id (for `/replies`)
    path: str
    line: int
    severity: str
    fingerprint: str
    body_excerpt: str
    is_outdated: bool


def _bot_login_matches(bot_login: str, author_login: str) -> bool:
    """GraphQL Bot nodes report `github-actions` while REST reports
    `github-actions[bot]`; accept both (same rule as collapse-previous).
    An empty `bot_login` disables the filter (escape hatch for tests)."""
    if not bot_login:
        return True
    accepted: set[str] = {bot_login}
    if bot_login.endswith("[bot]"):
        accepted.add(bot_login[: -len("[bot]")])
    return author_login in accepted


def fetch_prior_findings(
    *,
    token: str,
    repo: str,
    pr_number: int,
    bot_login: str,
    provider_marker_text: str = "",
) -> list[PriorFinding]:
    """Read the bot's still-open inline findings from the PR's review threads.

    Filters: first comment authored by the bot, parent review body carrying
    this provider's marker (when given), inline marker present (older
    comments without one are skipped), thread not resolved. Best-effort:
    returns `[]` on any API failure (the caller falls back to full mode).
    """
    if "/" not in repo or pr_number <= 0:
        return []
    owner, name = repo.split("/", 1)
    query: str = (
        "query($owner:String!, $repo:String!, $number:Int!, $page:Int!, $after:String) {"
        "  repository(owner:$owner, name:$repo) {"
        "    pullRequest(number:$number) {"
        "      reviewThreads(first:$page, after:$after) {"
        "        pageInfo { hasNextPage endCursor }"
        "        nodes {"
        "          id isResolved isOutdated path line originalLine"
        "          comments(first:1) {"
        "            nodes { id databaseId body author { login } pullRequestReview { body } }"
        "          }"
        "        }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    threads: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _page in range(GH_MAX_REVIEW_THREAD_PAGES):
        try:
            data: Any = gh_graphql(
                query,
                {"owner": owner, "repo": name, "number": pr_number,
                 "page": GH_CONNECTION_PAGE_SIZE, "after": cursor},
                token=token,
            )
            threads_conn: dict[str, Any] = (
                ((data or {}).get("repository") or {}).get("pullRequest") or {}
            ).get("reviewThreads") or {}
            threads.extend(threads_conn.get("nodes") or [])
            page_info: dict[str, Any] = threads_conn.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            next_cursor: Any = page_info.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                log("IAR: incomplete review-thread pagination — falling back to full review.")
                return []
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        except Exception as e:  # noqa: BLE001 — best-effort GH API call
            log(f"IAR: could not list complete prior review threads: {e}")
            return []
    else:
        log("IAR: review-thread page limit reached — falling back to full review.")
        return []
    out: list[PriorFinding] = []
    skipped_unmarked: int = 0
    for thread in threads:
        if not isinstance(thread, dict) or thread.get("isResolved"):
            continue
        first: list[dict[str, Any]] = (
            (thread.get("comments") or {}).get("nodes") or []
        )
        if not first:
            continue
        comment: dict[str, Any] = first[0] or {}
        author: str = str((comment.get("author") or {}).get("login") or "")
        if not _bot_login_matches(bot_login, author):
            continue
        review_body: str = str(
            (comment.get("pullRequestReview") or {}).get("body") or ""
        )
        if provider_marker_text and provider_marker_text not in review_body:
            continue
        body: str = str(comment.get("body") or "")
        parsed: tuple[str, str] | None = parse_inline_finding_marker(body)
        if parsed is None:
            skipped_unmarked += 1
            continue
        fingerprint, severity = parsed
        excerpt: str = body.split(INLINE_FINDING_MARKER_PREFIX, 1)[0].strip()
        excerpt = " ".join(excerpt.split())[:160]
        line_value: Any = thread.get("line")
        if line_value is None:
            line_value = thread.get("originalLine")
        out.append(
            PriorFinding(
                thread_id=str(thread.get("id") or ""),
                comment_id=str(comment.get("id") or ""),
                comment_database_id=_as_int(comment.get("databaseId")),
                path=str(thread.get("path") or ""),
                line=_as_int(line_value),
                severity=severity,
                fingerprint=fingerprint,
                body_excerpt=excerpt,
                is_outdated=bool(thread.get("isOutdated")),
            )
        )
    if skipped_unmarked:
        log(
            f"IAR: skipped {skipped_unmarked} prior bot comment(s) without an "
            "inline finding marker (posted before v2.1.0)."
        )
    log(f"IAR: {len(out)} prior open finding(s) read from review threads.")
    return out


@dataclass(frozen=True)
class IncrementalDelta:
    """What changed since the last reviewed head (v2.1.0+)."""

    prior_head_sha: str
    head_sha: str
    changed_files: tuple[str, ...]
    delta_ratio: float  # 0..1 — share of the PR's lines that are new
    diff: str | None = None  # two-tree delta, before full-PR truncation


def compute_incremental_delta(
    *,
    prior_head_sha: str,
    head_sha: str,
    new_lines_pct: float,
    repo_root: str | None = None,
) -> IncrementalDelta | None:
    """Return the trusted delta since `prior_head_sha`, or None when the
    delta cannot be trusted (unknown prior head; prior head is not an
    ancestor of HEAD after a rebase / force-push / amend; git failure)."""
    if not prior_head_sha or not head_sha:
        return None
    try:
        ancestor: subprocess.CompletedProcess[str] = run_cmd(
            ["git", "merge-base", "--is-ancestor", prior_head_sha, head_sha],
            cwd=repo_root,
            check=False,
        )
        if ancestor.returncode != 0:
            log(
                f"IAR: prior head {prior_head_sha[:8]} is not an ancestor of "
                f"{head_sha[:8]} (rebase / force-push) — full review."
            )
            return None
        names: subprocess.CompletedProcess[str] = run_cmd(
            ["git", "diff", "--name-only", "-z", prior_head_sha, head_sha, "--"],
            cwd=repo_root,
            check=True,
        )
        diff: subprocess.CompletedProcess[str] = run_cmd(
            ["git", "diff", "--no-color", "--unified=3", prior_head_sha, head_sha, "--"],
            cwd=repo_root,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log(f"IAR: could not compute the incremental delta ({e}) — full review.")
        return None
    files: tuple[str, ...] = tuple(
        path for path in names.stdout.split("\0") if path
    )
    ratio: float = max(0.0, min(1.0, float(new_lines_pct) / 100.0))
    return IncrementalDelta(
        prior_head_sha=prior_head_sha,
        head_sha=head_sha,
        changed_files=files,
        delta_ratio=ratio,
        diff=diff.stdout,
    )


def select_iar_mode(
    *,
    prior_state: IterationState | None,
    transition: "GenerationTransition",
    pre_policy_result: "PolicyResult",
    prior_findings: list[PriorFinding],
    delta: IncrementalDelta | None,
) -> tuple[str, str]:
    """Decide full vs incremental. Returns `(mode, reason)`.

    Incremental only when: a prior state exists, the transition is not a
    fresh start, no policy override forced an exhaustive pass (escape
    label, 30 % safety net), the delta is trusted, and there is at least
    one prior open finding to carry forward. Everything else → full.
    """
    if prior_state is None:
        return IAR_MODE_FULL, "no prior state"
    if transition in (
        GenerationTransition.FIRST_REVIEW,
        GenerationTransition.USER_FORCED_RESET,
        GenerationTransition.REBASED,
    ):
        return IAR_MODE_FULL, f"transition {transition.value}"
    if pre_policy_result.policy_applied in (
        IAR_POLICY_ESCAPE_LABEL_FORCED,
        IAR_POLICY_SAFETY_NET_FORCED,
    ):
        return IAR_MODE_FULL, f"policy override {pre_policy_result.policy_applied}"
    if delta is None:
        return IAR_MODE_FULL, "delta not trusted"
    if not prior_findings:
        return IAR_MODE_FULL, "no prior open findings to carry forward"
    return IAR_MODE_INCREMENTAL, (
        f"{len(prior_findings)} prior open finding(s), "
        f"{len(delta.changed_files)} file(s) changed since {delta.prior_head_sha[:8]}"
    )


def scale_incremental_budget(
    *, base_cap: int, base_turns: int, delta_ratio: float, prior_critical: int
) -> tuple[int, int]:
    """Delta-scaled inline cap and turn budget with floors (criticals never
    starve). Returns `(effective_cap, effective_turns)`."""
    ratio: float = max(IAR_INCREMENTAL_MIN_DELTA_RATIO, min(1.0, delta_ratio))
    cap: int = max(
        IAR_INCREMENTAL_MIN_CAP,
        int(-(-base_cap * ratio // 1)),
        prior_critical,
    )
    turns: int = max(IAR_INCREMENTAL_MIN_TURNS, int(-(-base_turns * ratio // 1)))
    return min(cap, max(base_cap, IAR_INCREMENTAL_MIN_CAP)), min(turns, max(base_turns, IAR_INCREMENTAL_MIN_TURNS))


@dataclass(frozen=True)
class IARPreLLMContext:
    """Bundle returned by `run_iar_pre_llm()`. Carries everything the
    caller needs to (a) shape the LLM call and (b) hand back to
    `run_iar_post_llm()` for the surfacing decision.

    `pre_policy_result` is produced by `dispatch_policy` with
    `findings=[]` so its `findings_to_surface`/`findings_silenced` are
    always empty; only `effective_max_inline_comments`, `prompt_addendum`,
    and `policy_applied` are meaningful at this stage.
    """

    prior_state: IterationState | None
    transition: GenerationTransition
    base_sha: str
    head_sha: str
    range_hash: str
    new_lines_pct: float
    pr_labels: list[str]
    pre_policy_result: PolicyResult
    # Incremental mode (v2.1.0+). Defaults keep every existing caller on
    # the full-review path.
    mode: str = IAR_MODE_FULL
    mode_reason: str = ""
    delta: IncrementalDelta | None = None
    prior_findings: tuple[PriorFinding, ...] = ()
    effective_max_turns: int = 0  # 0 = leave the caller's max_turns as is


def _resolve_base_sha(*, base_ref: str, repo_root: str | None = None) -> str:
    """Best-effort `git rev-parse origin/<base_ref>`. Returns empty
    string on any failure (missing remote, unresolved ref, sparse
    checkout). Empty base_sha degrades `detect_generation_change` to
    NEW_COMMITS on any hash mismatch — safe conservative fallback."""
    if not base_ref:
        return ""
    for ref_candidate in (f"origin/{base_ref}", base_ref):
        try:
            result: subprocess.CompletedProcess[str] = subprocess.run(
                ["git", "rev-parse", ref_candidate],
                capture_output=True,
                check=True,
                text=True,
                cwd=repo_root,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        sha: str = result.stdout.strip()
        if sha:
            return sha
    log(
        f"IAR: could not resolve base SHA for ref {base_ref!r} "
        "(tried origin/<ref> and <ref>). Range hash + rebase detection "
        "will fall back to conservative defaults."
    )
    return ""


def _fetch_pr_labels(
    *, token: str, repo: str, pr_number: int
) -> tuple[list[str], bool]:
    """Fetch PR labels via REST. Returns `(labels, ok)`:
      - `(labels, True)` — API call succeeded; `labels` is the
        authoritative list (possibly empty because the PR has no
        labels).
      - `([], False)` — API call failed; `labels` is empty as a
        conservative default and `ok=False` warns the caller not
        to distinguish "no labels" from "unknown".

    The `ok` bit is load-bearing for anywhere that would take an
    IRREVERSIBLE action on the "label absent" branch — most notably
    USER_FORCED_RESET, which wipes IAR dedup memory + resets the
    generation counter (round-14 F1). Without the bit, a transient
    GitHub 5xx during label fetch would look identical to "user
    deliberately removed the reviewed label" and silently wipe
    dedup state on the next run — the exact "infinite loop" symptom
    IAR is designed to prevent.

    Escape-label detection is a REVERSIBLE side-effect (skip dedup
    for THIS run only) so it can safely treat `ok=False` as "escape
    label not applied" — the next successful fetch restores the
    proper behaviour. USER_FORCED_RESET cannot degrade the same
    way: once state is wiped, the marker no longer records it,
    and the fingerprint memory is unrecoverable.
    """
    if "/" not in repo or pr_number <= 0:
        return [], False
    owner: str
    name: str
    owner, name = repo.split("/", 1)
    try:
        pr: Any = gh_request(
            "GET", f"/repos/{owner}/{name}/pulls/{pr_number}", token=token
        )
    except Exception as exc:  # noqa: BLE001 — best-effort GH API call:
        # transient network / rate-limit / 5xx failures are expected;
        # we return `ok=False` so callers can distinguish "PR has no
        # labels" from "we don't know if it has labels" — see
        # docstring for why that distinction is load-bearing.
        log(f"IAR: _fetch_pr_labels failed: {exc!r}. Returning empty list.")
        return [], False
    raw: list[dict[str, Any]] = pr.get("labels", []) or []
    labels: list[str] = [
        str(lbl.get("name") or "") for lbl in raw if lbl.get("name")
    ]
    return labels, True


def _load_code_contexts_for_findings(
    *, findings: list[Finding], review_sha: str
) -> dict[str, "CodeContext | None"]:
    """Load one CodeContext per unique file path. Missing / read-error
    files map to `None` — `finding_fingerprint` handles that by falling
    back to a context-less hash (still deterministic; just less resilient
    to nearby refactors)."""
    unique_paths: set[str] = {f.path for f in findings if f.path}
    contexts: dict[str, "CodeContext | None"] = {}
    for path in unique_paths:
        contexts[path] = load_code_context(path=path, review_sha=review_sha)
    return contexts


def _estimate_cost_vs_baseline(
    *,
    effective_cap: int,
    base_cap: int,
    prompt_addendum: str,
    silenced_count: int,
    surfaced_count: int,
) -> str:
    """Return a short human-readable cost-vs-baseline estimate string
    (e.g. `"+25%"`, `"0%"`). Best-effort heuristic — the true number
    requires per-provider token accounting.

    Today's function only models two effects:
    - Cap expansion (`effective_cap / base_cap - 1`) increases LLM
      generation cost roughly proportionally (more tool calls, more
      output tokens per call).
    - Prompt addendum adds a small fixed overhead per turn (~5%).

    Both effects are non-negative, so the returned string is always
    `"0%"` (baseline path — iterative / round-capped / cap not raised)
    or `"+N%"` (round 1 of `first-pass-exhaustive` or safety net
    override raising the cap). Silenced findings are a NET SAVE on
    the submission side (fewer GitHub API calls, less user noise) but
    do NOT affect LLM cost and are NOT modelled here — see
    `docs/ITERATION_AWARENESS.md § 13.3` for the follow-up plan to
    extend this to a `"-N%"` / `"unknown"` heuristic. Downstream
    consumers today MUST NOT gate CI on `== '-N%'`; the condition
    will never fire under the current implementation.

    `silenced_count` and `surfaced_count` are accepted but not yet
    consumed — the signature is stable so the future silence-savings
    extension does not force a call-site sweep.

    Returned string is safe to embed in a workflow log or output.
    Never raises; unknown inputs collapse to `"0%"`.
    """
    if base_cap <= 0:
        return "0%"
    cap_delta: float = (effective_cap / base_cap) - 1.0
    addendum_delta: float = 0.05 if prompt_addendum else 0.0
    total_delta: float = cap_delta + addendum_delta
    pct: int = int(round(total_delta * 100))
    sign: str = "+" if pct > 0 else ""
    return f"{sign}{pct}%"


def compute_reviewed_label_applied(
    *,
    applied_label: str,
    label_stamped: bool,
    current_labels: list[str],
    prior_state: "IterationState | None",
) -> bool:
    """Compute the `reviewed_label_applied` bit that gets embedded in
    the outgoing IAR state block at the end of a run.

    This is the arming signal for USER_FORCED_RESET on the NEXT run
    (docs/ITERATION_AWARENESS.md § 8.5): the reset gesture only fires
    when the prior state's `reviewed_label_applied` was `True` AND the
    label is now absent from the PR. So this function's job is to
    answer: "is the reviewed label (going to be) on the PR at the end
    of this run — such that its removal on a future run means the
    developer deliberately took it off?"

    Returns `True` if ANY of:
      1. `label_stamped` — this run's `gh_apply_label` call succeeded.
      2. `_labels_contain_ci(current_labels, applied_label)` — the
         label was already on the PR at trigger time (a prior run
         stamped it; this run may be a blocked follow-up or a no-op
         re-trigger, but the label is still present). Uses the
         same case-insensitive helper as `label-gate`, the
         escape-label check, and the skip-review-label check, so
         a casing mismatch between the configured `applied-label`
         and the GitHub-returned name can never falsely clear the
         arming bit and wrongly disarm a legitimate reset gesture.
      3. `prior_state.reviewed_label_applied is True` — the previous
         run's marker recorded a successful stamp AND this run took
         a path (blocked, escape-label, etc.) that does not remove
         the label. Preserving the prior bit here prevents a blocked
         follow-up from silently clearing the arming signal for a
         later legitimate reset gesture.

    Returns `False` only when NONE of these hold — the reviewer has
    never successfully stamped the label AND it is not currently on
    the PR AND prior state does not record a successful stamp. In
    that case there is nothing meaningful to "reset from" and
    USER_FORCED_RESET on the next run correctly no-ops.

    Also returns `False` when `applied_label` is empty (consumer opted
    out of the reviewed-label workflow entirely).
    """
    if not applied_label:
        return False
    prior_bit: bool = (
        prior_state is not None and prior_state.reviewed_label_applied
    )
    label_currently_on_pr: bool = _labels_contain_ci(
        needle=applied_label, haystack=current_labels
    )
    return label_stamped or label_currently_on_pr or prior_bit


@dataclass
class PriorFindingReconciliation:
    """Outcome of `reconcile_prior_findings` (incremental mode)."""

    resolved: list[PriorFinding] = field(default_factory=list)
    still_open: list[PriorFinding] = field(default_factory=list)
    regressed: list[PriorFinding] = field(default_factory=list)
    unverified: list[PriorFinding] = field(default_factory=list)  # claimed resolved, not verified


def parse_resolution_policy(raw: str) -> str:
    """`prior-findings-resolution` input → policy id. Empty → advisory;
    anything else must be one of `RESOLUTION_POLICIES` (case-insensitive)."""
    value: str = (raw or "").strip().lower()
    if not value:
        return RESOLUTION_POLICY_ADVISORY
    if value not in RESOLUTION_POLICIES:
        raise ValueError(
            f"prior-findings-resolution must be one of "
            f"{', '.join(RESOLUTION_POLICIES)}; got {raw!r}."
        )
    return value


def reconcile_prior_findings(
    *,
    prior_findings: tuple[PriorFinding, ...] | list[PriorFinding],
    updates: dict[str, tuple[str, str]],
    current_fingerprints: set[str],
    delta: IncrementalDelta | None,
    workspace: Path | None = None,
    policy: str = RESOLUTION_POLICY_ADVISORY,
) -> PriorFindingReconciliation:
    """Classify the model's verdicts on prior findings.

    `advisory` (default): a `resolved` claim is recorded as *unverified* and
    the finding stays open — a diff change is not proof that the concrete
    failure disappeared; a maintainer resolves the thread.

    `verified`: a `resolved` claim is honoured only when the runtime can
    corroborate it — the fingerprint is absent from this round AND the file
    changed since the last reviewed head (or no longer exists). Anything the
    runtime cannot corroborate stays open and is listed as unverified.
    `regressed` is model-asserted in both policies; no verdict → still open.
    """
    changed: set[str] = set(delta.changed_files) if delta is not None else set()
    root: Path = workspace if workspace is not None else Path.cwd()
    out = PriorFindingReconciliation()
    for pf in prior_findings:
        status, _note = updates.get(pf.fingerprint, ("", ""))
        if status == PRIOR_FINDING_STATUS_REGRESSED:
            out.regressed.append(pf)
            continue
        if status == PRIOR_FINDING_STATUS_RESOLVED:
            if policy == RESOLUTION_POLICY_VERIFIED:
                file_changed: bool = pf.path in changed
                # `pf.path` comes from a GitHub review thread; keep the
                # repo-relative invariant anyway (never join an absolute or
                # `..` path onto the workspace).
                rel: Path = Path(pf.path) if pf.path else Path()
                path_ok: bool = bool(pf.path) and not rel.is_absolute() and ".." not in rel.parts
                file_gone: bool = path_ok and not (root / rel).exists()
                if pf.fingerprint not in current_fingerprints and (
                    file_changed or file_gone
                ):
                    out.resolved.append(pf)
                    continue
            out.unverified.append(pf)
        out.still_open.append(pf)
    return out


def gh_resolve_review_thread(*, token: str, thread_id: str) -> bool:
    """Best-effort GraphQL `resolveReviewThread`. Returns True on success."""
    if not thread_id:
        return False
    mutation: str = (
        "mutation($id:ID!) {"
        "  resolveReviewThread(input:{threadId:$id}) { thread { isResolved } }"
        "}"
    )
    try:
        data: Any = gh_graphql(mutation, {"id": thread_id}, token=token)
    except Exception as e:  # noqa: BLE001 — best-effort GH API call
        log(f"IAR: could not resolve thread {thread_id}: {e}")
        return False
    return bool(
        (((data or {}).get("resolveReviewThread") or {}).get("thread") or {}).get(
            "isResolved"
        )
    )


def gh_reply_to_review_comment(
    *, token: str, repo: str, pr_number: int, comment_database_id: int, body: str
) -> bool:
    """Best-effort REST reply on a review-comment thread."""
    if comment_database_id <= 0 or "/" not in repo:
        return False
    owner, name = repo.split("/", 1)
    try:
        gh_request(
            "POST",
            f"/repos/{owner}/{name}/pulls/{pr_number}/comments/"
            f"{comment_database_id}/replies",
            token=token,
            body={"body": body},
        )
    except Exception as e:  # noqa: BLE001 — best-effort GH API call
        log(f"IAR: could not reply on comment {comment_database_id}: {e}")
        return False
    return True


def close_resolved_prior_findings(
    *,
    token: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    reconciliation: PriorFindingReconciliation,
) -> int:
    """Reply + resolve every verified-resolved prior thread. Returns the
    number of threads resolved. Never raises."""
    resolved_count: int = 0
    reply: str = (
        f"✅ Resolved in `{head_sha[:7]}` — verified by the reviewer: the file "
        "changed since the previous review and the finding was not reported again."
    )
    for pf in reconciliation.resolved:
        gh_reply_to_review_comment(
            token=token,
            repo=repo,
            pr_number=pr_number,
            comment_database_id=pf.comment_database_id,
            body=reply,
        )
        if gh_resolve_review_thread(token=token, thread_id=pf.thread_id):
            resolved_count += 1
    if reconciliation.resolved:
        log(
            f"IAR: resolved {resolved_count}/{len(reconciliation.resolved)} "
            "prior finding thread(s)."
        )
    return resolved_count


def render_incremental_footer(
    *,
    delta: IncrementalDelta,
    reconciliation: PriorFindingReconciliation,
    new_findings: int,
    policy: str = RESOLUTION_POLICY_ADVISORY,
) -> str:
    """One-line summary footer for incremental rounds."""
    unverified_note: str = (
        f" · {len(reconciliation.unverified)} claimed resolved but unverified"
        if reconciliation.unverified
        else ""
    )
    policy_note: str = (
        f" · policy: {policy}" if policy != RESOLUTION_POLICY_ADVISORY else ""
    )
    return (
        f"\n\n---\n\n_Since last review (`{delta.prior_head_sha[:7]}` → "
        f"`{delta.head_sha[:7]}`): resolved {len(reconciliation.resolved)} · "
        f"still open {len(reconciliation.still_open)} · regressed "
        f"{len(reconciliation.regressed)} · new {new_findings}{unverified_note}{policy_note}._"
    )


def apply_resolution_policy(
    *,
    policy: str,
    reconciliation: PriorFindingReconciliation,
    token: str,
    repo: str,
    pr_number: int,
    head_sha: str,
) -> int:
    """Side effects of the resolution policy on GitHub. `advisory` never
    touches review threads; `verified` replies on and resolves every thread
    the runtime corroborated (best-effort). Returns threads resolved."""
    if policy != RESOLUTION_POLICY_VERIFIED or not reconciliation.resolved:
        return 0
    return close_resolved_prior_findings(
        token=token,
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        reconciliation=reconciliation,
    )


def _render_iar_marker_annotation(
    *,
    state: IterationState,
    policy_result: PolicyResult,
    transition: GenerationTransition,
    mode: str = IAR_MODE_FULL,
) -> str:
    """Short human-readable line appended to the tracking marker body so a
    developer glancing at the comment sees the iteration status without
    having to inspect the embedded JSON state block. Kept to one line +
    optional detail line so the marker stays scannable."""
    surfaced: int = len(policy_result.findings_to_surface)
    silenced: int = len(policy_result.findings_silenced)
    critical_silenced: int = sum(
        1 for sf in policy_result.findings_silenced
        if sf.finding.severity == SEVERITY_CRITICAL
    )
    # This should always be 0 — the safety rail guarantees it. Log if
    # not, and expose the count as a visible red flag in the marker.
    critical_note: str = ""
    if critical_silenced > 0:
        critical_note = (
            f" ⚠️ **{critical_silenced} critical finding(s) silenced — "
            "this violates the IAR safety rail; please file a bug.**"
        )
    detail: str = ""
    if silenced > 0:
        detail = f", {silenced} deduplicated from prior rounds"
    return (
        f"\n\n_Iteration-Aware Review: gen {state.generation}, "
        f"round {state.round_in_generation}, "
        f"policy=`{policy_result.policy_applied}` "
        f"({transition.value}"
        + (", mode=incremental" if mode == IAR_MODE_INCREMENTAL else "")
        + f") — {surfaced} surfaced{detail}._"
        f"{critical_note}"
    )


def write_iar_outputs_populated(
    *,
    state: IterationState,
    policy_result: PolicyResult,
    telemetry: RunTelemetry,
    effective_cap: int,
    base_cap: int,
) -> None:
    """Overwrite the five IAR action outputs with real values. Called
    after `write_all_outputs` (which writes empty strings) so the
    last-write-wins semantics of `$GITHUB_OUTPUT` land the populated
    values on the downstream step.
    """
    write_action_output("iteration-round", str(state.round_in_generation))
    write_action_output("iteration-generation", str(state.generation))
    # Same rule as _render_iar_marker_annotation (see round-7 fix):
    # emit the current run's `policy_result.policy_applied`, NOT the
    # preserved-state's `state.policy_applied`. On an escape-label run
    # `run_iar_post_llm` returns the prior state unchanged (contract:
    # no mutation) while `policy_result.policy_applied` carries the
    # override (`escape-label-forced-full-review` or a `safety-net-*`
    # variant). Consumers keying downstream steps on this output MUST
    # see the current run's actual effective policy, or they will
    # miss escape / safety-net firings entirely.
    write_action_output(
        "iteration-policy-applied", policy_result.policy_applied
    )
    write_action_output("iteration-tokens-used", str(telemetry.tokens_used))
    write_action_output(
        "iteration-cost-vs-baseline-estimate",
        _estimate_cost_vs_baseline(
            effective_cap=effective_cap,
            base_cap=base_cap,
            prompt_addendum=policy_result.prompt_addendum,
            silenced_count=len(policy_result.findings_silenced),
            surfaced_count=len(policy_result.findings_to_surface),
        ),
    )


def run_iar_pre_llm(
    *,
    iar_config: IARConfig,
    repo: str,
    pr_number: int,
    gh_token: str,
    base_ref: str,
    head_sha: str,
    base_max_inline_comments: int,
    applied_label: str = "",
    provider_id: str = "",
    bot_login: str = "",
    max_turns: int = 0,
) -> IARPreLLMContext:
    """Prepare IAR context BEFORE the LLM call.

    Computes prior state, detects generation transition, loads PR labels
    for escape-label check, computes new-lines-pct for safety net, and
    runs `dispatch_policy` with an empty findings list to extract the
    prompt addendum + effective cap the caller will use to shape the
    LLM call.

    User-forced reset: fires when ALL FIVE conditions hold — (a)
    `applied_label` (the consumer's "reviewed" label — the one the
    action stamps on a successful review) is configured, (b) prior IAR
    state exists in the tracking marker, (c) the prior state records
    that the reviewer had previously stamped that label
    (`prior_state.reviewed_label_applied is True`), (d) the PR-labels
    fetch succeeded (`label_fetch_ok is True` — a transient GitHub
    5xx returning an empty list CANNOT be misread as "label absent"
    or the reset gesture would falsely fire and wipe fingerprint
    memory), and (e) that label is absent from the returned list.
    Downstream this behaves identically to `FIRST_REVIEW`: prior
    state is discarded, dedup memory is wiped, round-1 exhaustive
    fires under the default policy. The only reason the transition
    is a distinct enum value is so the log + marker annotation can
    tell developers the reset was a deliberate gesture (they removed
    the reviewed label before re-triggering) rather than a first-ever
    review of the PR.

    Condition (c) is load-bearing: without it, any blocked review
    (`block-on-critical` fired, so the label was never stamped)
    followed by the natural re-trigger would look identical to a
    deliberate reset and wipe fingerprint memory. Condition (d)
    (round-14 F1) is load-bearing for the same reason: transient
    API failures cannot silently look like a reset gesture. See
    `docs/ITERATION_AWARENESS.md` § 8.5 for the full contract.

    Caller SHOULD wrap this in `try/except` — the function does full
    GH API + git work and any failure should degrade to the baseline
    review path (IAR outputs stay empty, review still ships).
    """
    prior_state: IterationState | None = read_prior_iteration_state(
        repo=repo,
        pr_number=pr_number,
        token=gh_token,
        provider_id=provider_id,
        bot_login=bot_login,
    )
    base_sha: str = _resolve_base_sha(base_ref=base_ref)
    range_hash: str = compute_generation_range_hash(
        base_sha=base_sha, head_sha=head_sha
    )
    transition: GenerationTransition = detect_generation_change(
        prior_state=prior_state,
        current_range_hash=range_hash,
        current_base_sha=base_sha,
    )
    new_lines_pct: float = 0.0
    if transition in (
        GenerationTransition.NEW_COMMITS,
        GenerationTransition.REBASED,
    ) and prior_state is not None:
        new_lines_pct = compute_new_lines_pct(
            prior_base_sha=prior_state.base_sha,
            prior_head_sha=prior_state.head_sha,
            current_base_sha=base_sha,
            current_head_sha=head_sha,
        )
    pr_labels: list[str]
    label_fetch_ok: bool
    pr_labels, label_fetch_ok = _fetch_pr_labels(
        token=gh_token, repo=repo, pr_number=pr_number
    )
    # User-forced reset detection — see docstring. Overrides both
    # `transition` and `prior_state` so every downstream code path
    # (dedup, dispatch, advance_generation, safety-net) behaves as if
    # this were a first review. Fires only when the FIVE conditions in
    # the docstring all hold — the `reviewed_label_applied` guard is
    # the safety net that stops any blocked review's natural re-trigger
    # from being misclassified as a deliberate reset. The
    # `label_fetch_ok` guard (round-14 F1) prevents a transient GitHub
    # 5xx from silently wiping fingerprint memory — we can only trust
    # "reviewed label absent" when the API said it's absent, not when
    # we couldn't ask.
    if (
        applied_label
        and prior_state is not None
        and prior_state.reviewed_label_applied
        and label_fetch_ok
        and not _labels_contain_ci(
            needle=applied_label, haystack=pr_labels
        )
    ):
        log(
            f"IAR: user-forced reset detected — reviewed label "
            f"{applied_label!r} previously stamped (recorded in prior "
            f"state) but now absent from PR (prior gen="
            f"{prior_state.generation}, "
            f"round={prior_state.round_in_generation}). Treating this "
            "run as USER_FORCED_RESET: dedup memory wiped, generation "
            "counter reset to 1, round-1 exhaustive fires under the "
            "default policy."
        )
        transition = GenerationTransition.USER_FORCED_RESET
        prior_state = None
        new_lines_pct = 0.0
    # Dispatch with empty findings — extracts cap + addendum only.
    pre_policy_result: PolicyResult = dispatch_policy(
        iar_config=iar_config,
        findings=[],
        prior_state=prior_state,
        code_contexts={},
        base_max_inline_comments=base_max_inline_comments,
        transition=transition,
        new_lines_pct=new_lines_pct,
        pr_labels=pr_labels,
    )
    # ---- Incremental mode selection (v2.1.0+) ----
    prior_findings: list[PriorFinding] = []
    delta: IncrementalDelta | None = None
    if prior_state is not None and transition not in (
        GenerationTransition.FIRST_REVIEW,
        GenerationTransition.USER_FORCED_RESET,
    ):
        prior_findings = fetch_prior_findings(
            token=gh_token,
            repo=repo,
            pr_number=pr_number,
            bot_login=bot_login,
            provider_marker_text=provider_marker(provider_id) if provider_id else "",
        )
        delta = compute_incremental_delta(
            prior_head_sha=prior_state.head_sha,
            head_sha=head_sha,
            new_lines_pct=new_lines_pct,
        )
    mode, mode_reason = select_iar_mode(
        prior_state=prior_state,
        transition=transition,
        pre_policy_result=pre_policy_result,
        prior_findings=prior_findings,
        delta=delta,
    )
    effective_max_turns: int = 0
    if mode == IAR_MODE_INCREMENTAL and delta is not None:
        prior_critical: int = sum(
            1 for pf in prior_findings if pf.severity == SEVERITY_CRITICAL
        )
        cap, turns = scale_incremental_budget(
            base_cap=base_max_inline_comments,
            base_turns=max_turns,
            delta_ratio=delta.delta_ratio,
            prior_critical=prior_critical,
        )
        effective_max_turns = turns if max_turns else 0
        # Replace the exhaustive addendum (if any) with the incremental one
        # and the cap with the delta-scaled one; the policy label is kept
        # so dedup semantics downstream are unchanged.
        pre_policy_result = PolicyResult(
            findings_to_surface=[],
            findings_silenced=[],
            effective_max_inline_comments=cap,
            prompt_addendum=IAR_INCREMENTAL_PROMPT_ADDENDUM,
            policy_applied=pre_policy_result.policy_applied,
        )
    log(
        f"IAR pre-LLM: transition={transition.value}, "
        f"gen={prior_state.generation if prior_state else 0}, "
        f"prior_round={prior_state.round_in_generation if prior_state else 0}, "
        f"policy={pre_policy_result.policy_applied}, "
        f"effective_cap={pre_policy_result.effective_max_inline_comments} "
        f"(base={base_max_inline_comments}), "
        f"prompt_addendum={'yes' if pre_policy_result.prompt_addendum else 'no'}, "
        f"new_lines_pct={new_lines_pct:.1f}%, "
        f"mode={mode} ({mode_reason})"
        + (f", effective_max_turns={effective_max_turns}" if effective_max_turns else "")
        + "."
    )
    return IARPreLLMContext(
        prior_state=prior_state,
        transition=transition,
        base_sha=base_sha,
        head_sha=head_sha,
        range_hash=range_hash,
        new_lines_pct=new_lines_pct,
        pr_labels=pr_labels,
        pre_policy_result=pre_policy_result,
        mode=mode,
        mode_reason=mode_reason,
        delta=delta,
        prior_findings=tuple(prior_findings),
        effective_max_turns=effective_max_turns,
    )


def run_iar_post_llm(
    *,
    iar_config: IARConfig,
    pre_context: IARPreLLMContext,
    result: ReviewResult,
    base_max_inline_comments: int,
    telemetry: RunTelemetry,
    surface_cap: int = 0,
    resolution_policy: str = RESOLUTION_POLICY_ADVISORY,
    workspace: Path | None = None,
) -> tuple[IterationState, PolicyResult]:
    """Apply IAR filtering AFTER the LLM call and return the state to
    embed + the surfacing decision.

    `resolution_policy` (v2.2.0+): under `verified`, prior findings the
    runtime can corroborate as resolved (see `reconcile_prior_findings`)
    leave the outstanding set and stop contributing to the gate; under
    `advisory` (default) every prior finding stays outstanding.

    `surface_cap` (v2.1.0+, agent-runner path): the effective inline cap
    is enforced HERE, after fingerprinting, so overflow findings are still
    recorded as open (docs/ITERATION_AWARENESS.md § 13.1) instead of
    silently dropped before IAR sees them. `0` = no cap (chat-completions
    enforces the cap in the tool handler).

    Side effects:
    - Mutates `result.findings` in place to the surfaced subset.
    - Recomputes `result.overall_severity` if any findings were dropped.

    Escape-label runs return the prior state unchanged (Task 7 contract:
    persisted state is NOT mutated for escape-label runs so the next
    normal run resumes where it left off). Everything else advances or
    increments the state and populates telemetry.
    """
    # Load code contexts only for finding paths — one git-show per unique
    # file; a warmup penalty scoped to the number of findings, not the
    # size of the diff.
    code_contexts: dict[str, "CodeContext | None"] = (
        _load_code_contexts_for_findings(
            findings=result.findings, review_sha=pre_context.head_sha
        )
    )
    policy_result: PolicyResult = dispatch_policy(
        iar_config=iar_config,
        findings=result.findings,
        prior_state=pre_context.prior_state,
        code_contexts=code_contexts,
        base_max_inline_comments=base_max_inline_comments,
        transition=pre_context.transition,
        new_lines_pct=pre_context.new_lines_pct,
        pr_labels=pre_context.pr_labels,
    )
    original_finding_count: int = len(result.findings)
    surfaced: list[Finding] = list(policy_result.findings_to_surface)
    overflow: list[Finding] = []
    if surface_cap > 0 and len(surfaced) > surface_cap:
        prioritized: list[Finding] = _sort_findings_criticals_first(surfaced)
        surfaced, overflow = prioritized[:surface_cap], prioritized[surface_cap:]
        log(
            f"IAR post-LLM: capped {len(prioritized)} surfaced findings to "
            f"{surface_cap} (criticals first); {len(overflow)} overflow "
            "finding(s) recorded as open for dedup."
        )
    result.findings = surfaced
    # Recompute severity when the filter dropped findings — the strictness
    # gate downstream reads `overall_severity`, so a silenced warning
    # would otherwise still block the check.
    if len(result.findings) != original_finding_count:
        result.overall_severity = overall_severity(
            [f.severity for f in result.findings]
        )
    # The incremental prompt explicitly forbids reposting prior findings.
    # Their absence from this run's new comments must not clear the gate —
    # except, under the `verified` policy, for findings the runtime can
    # corroborate as resolved.
    verified_resolved_fps: set[str] = set()
    if (
        pre_context.prior_findings
        and resolution_policy == RESOLUTION_POLICY_VERIFIED
        and pre_context.mode == IAR_MODE_INCREMENTAL
    ):
        # `Finding.fingerprint` is stamped further down; corroboration needs
        # this round's fingerprints NOW, over every finding the model
        # produced (surfaced, overflow and silenced) — a re-posted issue is
        # never "absent this round".
        round_fps: set[str] = {
            finding_fingerprint(finding=f, code_context=code_contexts.get(f.path))
            for f in list(surfaced)
            + list(overflow)
            + [sf.finding for sf in policy_result.findings_silenced]
        }
        verified_resolved_fps = {
            pf.fingerprint
            for pf in reconcile_prior_findings(
                prior_findings=pre_context.prior_findings,
                updates=result.prior_finding_updates,
                current_fingerprints=round_fps,
                delta=pre_context.delta,
                workspace=workspace,
                policy=resolution_policy,
            ).resolved
        }
    if pre_context.prior_findings:
        result.overall_severity = overall_severity(
            [result.overall_severity]
            + [
                pf.severity
                for pf in pre_context.prior_findings
                if pf.fingerprint not in verified_resolved_fps
            ]
        )
    # Escape-label short-circuit: preserve prior state exactly, no
    # mutations. This is the contract from Task 7 — persisted state must
    # survive an escape-label run so the next normal run resumes the
    # dedup timeline as if the escape never happened.
    if policy_result.policy_applied == IAR_POLICY_ESCAPE_LABEL_FORCED:
        for finding in result.findings:
            finding.fingerprint = finding_fingerprint(
                finding=finding, code_context=code_contexts.get(finding.path)
            )
        log(
            "IAR post-LLM: escape-label run — persisted state unchanged. "
            f"Surfaced {len(policy_result.findings_to_surface)} "
            f"(silenced {len(policy_result.findings_silenced)})."
        )
        return (
            pre_context.prior_state
            or new_iteration_state(
                generation_range_hash=pre_context.range_hash,
                base_sha=pre_context.base_sha,
                head_sha=pre_context.head_sha,
                policy_applied=policy_result.policy_applied,
            ),
            policy_result,
        )
    # Compute the state for the NEXT round based on transition.
    state_before_fp_update: IterationState
    if pre_context.transition == GenerationTransition.SAME_GENERATION:
        assert pre_context.prior_state is not None  # transition guarantees it
        state_before_fp_update = increment_round_in_generation(
            prior_state=pre_context.prior_state,
            policy=policy_result.policy_applied,
            new_head_sha=pre_context.head_sha,
        )
    else:
        state_before_fp_update = advance_generation(
            prior_state=pre_context.prior_state,
            transition=pre_context.transition,
            new_range_hash=pre_context.range_hash,
            new_base_sha=pre_context.base_sha,
            new_head_sha=pre_context.head_sha,
            policy=policy_result.policy_applied,
        )
    # Update open + resolved fingerprint sets. Re-fingerprint on the
    # ORIGINAL LLM findings (surfaced + silenced) — a silenced finding
    # is still "open in reality"; only findings the LLM stopped producing
    # count as resolved.
    all_original_findings: list[Finding] = (
        list(surfaced)
        + list(overflow)
        + [sf.finding for sf in policy_result.findings_silenced]
    )
    current_fps: dict[int, str] = {}
    for i, finding in enumerate(all_original_findings):
        current_fps[i] = finding_fingerprint(
            finding=finding, code_context=code_contexts.get(finding.path)
        )
        # Stamp surfaced findings so their inline comments carry the hidden
        # marker the next round matches against (incremental mode).
        finding.fingerprint = current_fps[i]
    current_fp_set: set[str] = set(current_fps.values())
    next_open: list[str] = sorted(current_fp_set)
    # `newly_resolved` = prior open that are no longer in the current run.
    _still_open: list[str]
    newly_resolved: list[str]
    if pre_context.prior_state is not None:
        _still_open, newly_resolved = resolve_finding_status(
            prior_open_fingerprints=pre_context.prior_state.open_fingerprints_this_gen,
            current_fps=current_fps,
        )
    else:
        newly_resolved = []
    if pre_context.mode == IAR_MODE_INCREMENTAL:
        # A focused pass did not re-review every old finding. Do not infer
        # resolution from absence; retain the full outstanding fingerprint set.
        outstanding: set[str] = {
            pf.fingerprint for pf in pre_context.prior_findings
        }
        if pre_context.prior_state is not None:
            outstanding.update(pre_context.prior_state.open_fingerprints_this_gen)
        outstanding -= verified_resolved_fps
        next_open = sorted((set(next_open) | outstanding) - verified_resolved_fps)
        newly_resolved = sorted(verified_resolved_fps)
    next_resolved: list[str] = sorted(
        (set(state_before_fp_update.resolved_fingerprints) | set(newly_resolved))
        - set(next_open)
    )
    state_final: IterationState = IterationState(
        version=state_before_fp_update.version,
        generation=state_before_fp_update.generation,
        generation_range_hash=state_before_fp_update.generation_range_hash,
        round_in_generation=state_before_fp_update.round_in_generation,
        policy_applied=state_before_fp_update.policy_applied,
        resolved_fingerprints=next_resolved,
        open_fingerprints_this_gen=next_open,
        history=list(state_before_fp_update.history),
        base_sha=state_before_fp_update.base_sha,
        head_sha=state_before_fp_update.head_sha,
    )
    # NOTE on generation-history telemetry attribution: the closed
    # prior-generation entry in `state_final.history[-1]` (created by
    # `advance_generation` on NEW_COMMITS/REBASED transitions) holds
    # `tokens_used=0` + `wall_clock_ms=0` placeholders. We do NOT
    # backfill those placeholders from THIS run's telemetry, because
    # this run is round 1 of the NEW generation — attributing its
    # tokens/wall-clock to the closed prior generation misreports
    # per-generation cost history and poisons the cost-vs-baseline
    # estimate once token accounting lands (`tokens_used` is 0 today
    # so the harm is currently limited to `wall_clock_ms`, but the
    # semantics need to be right before that changes).
    #
    # The current run's telemetry surfaces as-is via `write_iar_outputs_populated`
    # (`iteration-tokens-used`, `iteration-cost-vs-baseline-estimate`,
    # observability marker annotation). Attributing per-round telemetry
    # to individual `history[]` entries would require accumulating
    # across a generation's rounds and only close the entry when the
    # generation itself closes — a bigger refactor tracked as a
    # follow-up (docs § 13.3 to be added).
    log(
        f"IAR post-LLM: policy={policy_result.policy_applied}, "
        f"surfaced={len(policy_result.findings_to_surface)}, "
        f"silenced={len(policy_result.findings_silenced)}, "
        f"newly_resolved={len(newly_resolved)}, "
        f"open_next={len(next_open)}, "
        f"tokens={telemetry.tokens_used}, "
        f"wall_clock_ms={telemetry.wall_clock_ms()}."
    )
    return state_final, policy_result


# ---------------------------------------------------------------------------
# PR context (the user message the model sees first)
# ---------------------------------------------------------------------------


@dataclass
class PRContext:
    """Snapshot of everything the model needs to start reviewing."""

    title: str
    author: str
    head_ref: str
    base_ref: str
    state: str
    additions: int
    deletions: int
    commits: int
    body: str
    changed_files: list[dict[str, Any]] = field(default_factory=list)
    diff: str = ""
    # (path, line_count) for diff sections removed by `shape_diff`.
    omitted_files: list[tuple[str, int]] = field(default_factory=list)
    # Incremental mode (v2.1.0+): the IAR pre-LLM context, when the run is
    # a follow-up review. `render_user_prompt` reads it when its own
    # `incremental` argument is None, so agent-runner providers need no
    # signature change.
    incremental: "IARPreLLMContext | None" = None


def parse_ignore_paths(raw: str) -> tuple[str, ...]:
    """`ignore-paths` input → globs (comma/newline separated, trimmed,
    de-duplicated, order preserved). Empty → `()`. Additive to the built-in
    `DEFAULT_IGNORE_PATH_GLOBS` (the caller concatenates)."""
    out: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[,\n]", raw or ""):
        glob: str = chunk.strip().strip("\"'")
        if not glob or glob.startswith("#") or glob in seen:
            continue
        if len(glob) > MAX_IGNORE_GLOB_LEN:
            log(
                f"ignore-paths: dropping glob longer than {MAX_IGNORE_GLOB_LEN} "
                f"characters ({glob[:40]!r}…)."
            )
            continue
        seen.add(glob)
        out.append(glob)
        if len(out) >= MAX_IGNORE_GLOBS:
            log(
                f"ignore-paths: keeping the first {MAX_IGNORE_GLOBS} globs; "
                "the rest are ignored."
            )
            break
    return tuple(out)


class _GlobMatcher:
    """A gitignore-style glob compiled into a backtracking-free matcher.

    Semantics: `**` as a whole segment spans zero or more directories; `*`
    and `?` never cross `/`; a pattern without `/` matches the basename
    anywhere in the tree; a leading `/` anchors to the repo root; a
    trailing `/` matches everything under that directory. Matching is a
    small dynamic programme over path segments plus the classic two-pointer
    wildcard match inside a segment — worst case O(segments² · chars), never
    exponential, so a PR-controlled file name cannot stall the run
    (regex-based compilers, including `fnmatch.translate`, backtrack
    catastrophically on `*.*.*.*…` patterns).
    """

    __slots__ = ("segments", "anchored", "dir_only")

    def __init__(self, glob: str) -> None:
        pattern: str = glob.strip()
        self.anchored: bool = pattern.startswith("/")
        pattern = pattern.strip("/")
        self.dir_only: bool = glob.strip().endswith("/") and bool(pattern)
        raw_segments: list[str] = [seg for seg in pattern.split("/") if seg]
        segments: list[str] = []
        for seg in raw_segments:
            # `**` is special only as a whole segment; inside a segment any
            # run of `*` is a single `*`. Consecutive `**` segments collapse.
            normalised: str = seg if seg == GLOB_ANY_DIRS else re.sub(r"\*{2,}", "*", seg)
            if normalised == GLOB_ANY_DIRS and segments and segments[-1] == GLOB_ANY_DIRS:
                continue
            segments.append(normalised)
        if not self.anchored and len(segments) == 1:
            segments.insert(0, GLOB_ANY_DIRS)
        self.segments: tuple[str, ...] = tuple(segments)

    @staticmethod
    def _segment_match(pat: str, text: str) -> bool:
        """`*` / `?` wildcard match within one path segment (two-pointer)."""
        p: int = 0
        t: int = 0
        star: int = -1
        mark: int = 0
        while t < len(text):
            if p < len(pat) and (pat[p] == "?" or pat[p] == text[t]):
                p += 1
                t += 1
            elif p < len(pat) and pat[p] == "*":
                star = p
                mark = t
                p += 1
            elif star != -1:
                p = star + 1
                mark += 1
                t = mark
            else:
                return False
        while p < len(pat) and pat[p] == "*":
            p += 1
        return p == len(pat)

    def match(self, path: str) -> bool:
        parts: list[str] = [seg for seg in path.split("/") if seg]
        segs: tuple[str, ...] = self.segments
        m: int = len(segs)
        n: int = len(parts)
        if not segs:
            return False
        # dp[i][j]: segs[i:] matches parts[j:].
        dp: list[list[bool]] = [[False] * (n + 1) for _ in range(m + 1)]
        for j in range(n + 1):
            dp[m][j] = (j < n) if self.dir_only else (j == n)
        for i in range(m - 1, -1, -1):
            seg: str = segs[i]
            for j in range(n, -1, -1):
                if seg == GLOB_ANY_DIRS:
                    dp[i][j] = dp[i + 1][j] or (j < n and dp[i][j + 1])
                else:
                    dp[i][j] = (
                        j < n
                        and self._segment_match(seg, parts[j])
                        and dp[i + 1][j + 1]
                    )
        return dp[0][0]


@functools.lru_cache(maxsize=1024)
def _compile_glob(glob: str) -> _GlobMatcher:
    """Compile (and memoise) one `ignore-paths` glob."""
    return _GlobMatcher(glob)


# Historical name kept for callers/tests written against the regex version.
_glob_to_regex = _compile_glob


def path_is_ignored(path: str, globs: tuple[str, ...]) -> bool:
    """True when `path` (repo-relative, POSIX) matches any glob."""
    normalized: str = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    for glob in globs:
        if _compile_glob(glob).match(normalized):
            return True
    return False


def _diff_section_path(header_line: str) -> str:
    """Extract the post-image path from a `diff --git a/x b/y` header."""
    rest: str = header_line[len(DIFF_SECTION_HEADER_PREFIX):].strip()
    marker: str = " b/"
    idx: int = rest.rfind(marker)
    if idx == -1:
        return rest
    return rest[idx + len(marker):].strip().strip('"')


def shape_diff(
    diff_text: str, globs: tuple[str, ...]
) -> tuple[str, list[tuple[str, int]]]:
    """Drop per-file sections whose path matches `globs`.

    Returns `(kept_diff, omitted)` where `omitted` is an ordered list of
    `(path, line_count)` for every removed section (line count of the whole
    section, header included). Sections are split on `diff --git` headers;
    text before the first header (normally empty) is kept verbatim.
    """
    if not diff_text or not globs:
        return diff_text, []
    lines: list[str] = diff_text.splitlines(keepends=True)
    kept: list[str] = []
    omitted: list[tuple[str, int]] = []
    section: list[str] = []
    section_path: str | None = None

    def flush() -> None:
        if not section:
            return
        if section_path is not None and path_is_ignored(section_path, globs):
            omitted.append((section_path, len(section)))
        else:
            kept.extend(section)

    for line in lines:
        if line.startswith(DIFF_SECTION_HEADER_PREFIX):
            flush()
            section = [line]
            section_path = _diff_section_path(line.rstrip("\n"))
        else:
            section.append(line)
    flush()
    return "".join(kept), omitted


def filter_diff_to_paths(diff_text: str, paths: set[str]) -> str:
    """Keep only the `diff --git` sections whose post-image path is in
    `paths` (incremental mode: the files that changed since the last
    reviewed head). Text before the first header is dropped."""
    if not diff_text or not paths:
        return ""
    kept: list[str] = []
    section: list[str] = []
    section_path: str | None = None

    def flush() -> None:
        if section and section_path is not None and section_path in paths:
            kept.extend(section)

    for line in diff_text.splitlines(keepends=True):
        if line.startswith(DIFF_SECTION_HEADER_PREFIX):
            flush()
            section = [line]
            section_path = _diff_section_path(line.rstrip("\n"))
        else:
            section.append(line)
    flush()
    return "".join(kept)


def render_prior_findings_block(
    prior_findings: tuple[PriorFinding, ...] | list[PriorFinding],
    *,
    changed_files: set[str],
) -> str:
    """The `## Your prior findings still open` table (criticals first,
    capped at `PRIOR_FINDINGS_MAX_LISTED`)."""
    if not prior_findings:
        return ""
    ordered: list[PriorFinding] = sorted(
        prior_findings,
        key=lambda pf: (-SEVERITY_RANK.get(pf.severity, 0), pf.path, pf.line),
    )
    rows: list[str] = [
        "| # | fingerprint | severity | location | summary | file changed since? |",
        "|---|---|---|---|---|---|",
    ]
    for index, pf in enumerate(ordered[:PRIOR_FINDINGS_MAX_LISTED], start=1):
        changed: str = "yes" if pf.path in changed_files else "no"
        summary: str = pf.body_excerpt.replace("|", "\\|")[:120]
        rows.append(
            f"| {index} | `{pf.fingerprint}` | {pf.severity} | "
            f"`{pf.path}:{pf.line}` | {summary} | {changed} |"
        )
    more: int = len(ordered) - PRIOR_FINDINGS_MAX_LISTED
    tail: str = f"\n\n… and {more} more (listed on the PR threads)." if more > 0 else ""
    return (
        f"{PRIOR_FINDINGS_HEADING} ({len(ordered)})\n\n"
        "For EACH row decide `resolved` (the new commits fixed it), `open` "
        "(still present) or `regressed` (worse now), citing the fingerprint. "
        "Do not re-post an open one as a new finding. Prior `critical` rows "
        "come first and must be addressed.\n\n"
        + "\n".join(rows)
        + tail
        + "\n\n"
    )


def render_incremental_sections(
    ctx: "PRContext", pre: "IARPreLLMContext"
) -> str:
    """Replacement for the `## Full Diff` section in incremental mode:
    delta hunks in full, other PR files as one-liners, prior findings."""
    delta: IncrementalDelta | None = pre.delta
    assert delta is not None  # callers check pre.mode first
    changed: set[str] = set(delta.changed_files)
    omitted: set[str] = {
        str(f.get("path")) for f in ctx.changed_files if f.get("omitted")
    }
    # Filtering an already-truncated full PR diff can lose new edits entirely
    # and includes old hunks in every touched file. Use the actual tree delta.
    delta_diff: str = filter_diff_to_paths(
        delta.diff if delta.diff is not None else ctx.diff, changed - omitted
    )
    if len(delta_diff) > MAX_DIFF_CHARS:
        delta_diff = (
            delta_diff[:MAX_DIFF_CHARS]
            + f"\n\n[diff truncated at {MAX_DIFF_CHARS} characters — use your "
            "file-reading tool to inspect specific changed files in full]"
        )
    unchanged_lines: list[str] = [
        f"- {f['path']} ({f['status']}) +{f['additions']}/-{f['deletions']}"
        for f in ctx.changed_files
        if f.get("path") not in changed and not f.get("omitted")
    ]
    heading_range: str = f"({delta.prior_head_sha[:7]} → {delta.head_sha[:7]})"
    out: list[str] = [
        f"{IAR_INCREMENTAL_DIFF_HEADING} {heading_range}\n\n"
        + (
            f"```diff\n{delta_diff}\n```\n\n"
            if delta_diff.strip()
            else "_No code changes since your last review — only verify the prior findings below._\n\n"
        )
    ]
    if unchanged_lines:
        out.append(
            f"{IAR_UNCHANGED_FILES_HEADING}\n\n"
            "Not shown again; read them with your file tools only if a prior "
            "finding or a new hunk depends on them.\n\n"
            + "\n".join(unchanged_lines)
            + "\n\n"
        )
    out.append(render_prior_findings_block(pre.prior_findings, changed_files=changed))
    return "".join(out)


def fetch_pr_context(
    *,
    repo: str,
    pr_number: int,
    base_ref: str,
    token: str,
    ignore_globs: tuple[str, ...] = DEFAULT_IGNORE_PATH_GLOBS,
) -> PRContext:
    """Pull PR metadata + diff once and shape it into a single dataclass.

    `ignore_globs` sections are removed from the diff body (and reported in
    `PRContext.omitted_files`) BEFORE the `MAX_DIFF_CHARS` truncation, so
    lockfiles never crowd real changes out of the window.
    """
    owner, name = repo.split("/", 1)
    pr: dict[str, Any] = gh_request(
        "GET", f"/repos/{owner}/{name}/pulls/{pr_number}", token=token
    )
    files_resp: list[dict[str, Any]] = []
    page: int = 1
    while True:
        chunk: Any = gh_request(
            "GET",
            f"/repos/{owner}/{name}/pulls/{pr_number}/files"
            f"?per_page={GH_CONNECTION_PAGE_SIZE}&page={page}",
            token=token,
        )
        if not chunk or not isinstance(chunk, list):
            break
        files_resp.extend(chunk)
        if len(chunk) < GH_CONNECTION_PAGE_SIZE:
            break
        page += 1

    # `git diff origin/<base>...HEAD` matches what reviewers see in the PR
    # diff tab, so the model's line numbers match GitHub's RIGHT-side diff
    # numbers. The consumer's checkout step needs `fetch-depth: 0` for this
    # to resolve — actions/checkout's default shallow clone won't have the
    # base ref locally.
    diff_proc = run_cmd(
        ["git", "diff", f"origin/{base_ref}...HEAD", "--no-color", "--unified=3"],
    )
    if diff_proc.returncode != 0:
        # Most common cause: a shallow checkout without `fetch-depth: 0`, so
        # `origin/<base>` isn't present locally. Surface it in the log rather
        # than silently feeding the model an empty diff.
        log(
            f"`git diff origin/{base_ref}...HEAD` failed "
            f"(exit {diff_proc.returncode}): "
            f"{diff_proc.stderr.strip()[:MAX_ERROR_BODY_CHARS]} — the consumer "
            "checkout likely needs `fetch-depth: 0`. Proceeding with whatever "
            "diff git produced."
        )
    diff_text, omitted_files = shape_diff(diff_proc.stdout, ignore_globs)
    if omitted_files:
        log(
            "Diff shaping: omitted "
            f"{len(omitted_files)} file(s) / "
            f"{sum(n for _, n in omitted_files)} diff line(s) "
            f"(generated / lock globs); kept {len(diff_text)} chars."
        )
    if len(diff_text) > MAX_DIFF_CHARS:
        diff_text = (
            diff_text[:MAX_DIFF_CHARS]
            + f"\n\n[diff truncated at {MAX_DIFF_CHARS} characters — use the "
            "read_file tool to inspect specific changed files in full]"
        )

    return PRContext(
        title=pr.get("title", ""),
        author=(pr.get("user") or {}).get("login", ""),
        head_ref=(pr.get("head") or {}).get("ref", ""),
        base_ref=(pr.get("base") or {}).get("ref", base_ref),
        state=pr.get("state", ""),
        additions=pr.get("additions", 0),
        deletions=pr.get("deletions", 0),
        commits=pr.get("commits", 0),
        body=pr.get("body") or "",
        changed_files=[
            {
                "path": f.get("filename", ""),
                "status": f.get("status", ""),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "omitted": path_is_ignored(f.get("filename", ""), ignore_globs),
            }
            for f in files_resp
        ],
        diff=diff_text,
        omitted_files=omitted_files,
    )


def render_user_prompt(
    ctx: PRContext,
    *,
    for_agent_runner: bool = False,
    incremental: "IARPreLLMContext | None" = None,
) -> str:
    """Produce the first user message — PR metadata + diff.

    In incremental mode (`incremental.mode == IAR_MODE_INCREMENTAL`) the
    `## Full Diff` section is replaced by the delta since the last reviewed
    head, one-line summaries of the other files, and the prior-findings
    table (`render_incremental_sections`).

    The closing paragraph differs by provider family:
      - Chat-completions (`for_agent_runner=False`): references the built-in
        `read_file`/`grep`/`glob`/`post_inline_comment`/`submit_review` tools
        that this action owns.
      - Agent-runner (`for_agent_runner=True`): those tools do NOT exist for a
        vendor CLI, which uses its own file/search tools and returns findings
        via the `findings.json` output contract (see
        `write_findings_prompt_directive`). Emitting the chat-completions tool
        names here would give the CLI contradictory, unfollowable instructions.
    """
    files_block: str = "\n".join(
        f"- {f['path']} ({f['status']}) +{f['additions']}/-{f['deletions']}"
        + (
            " — omitted from the diff below (generated / lock file)"
            if f.get("omitted")
            else ""
        )
        for f in ctx.changed_files
    )
    omitted_block: str = ""
    if ctx.omitted_files:
        listing: str = "\n".join(
            f"- `{path}` ({count} diff lines)"
            for path, count in ctx.omitted_files
        )
        omitted_block = (
            f"{OMITTED_FILES_HEADING}\n\n"
            "These files changed in the PR but their diff sections were not "
            "included (lockfiles, minified bundles, source maps, vendored or "
            "generated content). Do not review or guess their contents; you may "
            "note in the summary when their presence or absence is itself a "
            "problem, or when a kept change clearly depends on one.\n\n"
            + listing + "\n\n"
        )
    body_block: str = ctx.body.strip() or "(no body)"
    if for_agent_runner:
        closing: str = (
            "Review this PR using the rubric in the instructions above: triage "
            "the changed files by risk first, then use your own file-reading "
            "and search tools to verify findings against the broader codebase "
            "before reporting them — read slices, not whole trees. Only comment "
            "on lines that appear in the diff, and set each finding's "
            "`severity` honestly — it drives the gating behaviour configured "
            "by the consumer. When you're done, write your review to the "
            "findings file exactly as described in the output contract."
        )
    else:
        closing = (
            "Review this PR using the system prompt's rubric. Use `read_file`, "
            "`grep`, and `glob` to verify findings against the broader "
            "codebase before reporting them. Queue inline comments with "
            "`post_inline_comment` (only on lines that appear in the diff) and "
            "set the `severity` argument honestly — it drives the gating "
            "behaviour configured by the consumer. When you're done, call "
            "`submit_review` exactly once with the summary markdown — that "
            "signals the end of the session and posts the review."
        )
    if incremental is None:
        incremental = ctx.incremental
    diff_section: str
    if (
        incremental is not None
        and incremental.mode == IAR_MODE_INCREMENTAL
        and incremental.delta is not None
    ):
        diff_section = render_incremental_sections(ctx, incremental)
    else:
        diff_section = f"## Full Diff\n\n```diff\n{ctx.diff}\n```\n\n"
    return (
        f"# PR Context\n\n"
        f"**Title:** {ctx.title}\n"
        f"**Author:** {ctx.author}\n"
        f"**Branch:** `{ctx.head_ref}` → `{ctx.base_ref}`\n"
        f"**Stats:** +{ctx.additions}/-{ctx.deletions} across "
        f"{len(ctx.changed_files)} files in {ctx.commits} commit(s)\n\n"
        f"## Description\n\n{body_block}\n\n"
        f"## Changed Files\n\n{files_block or '(none)'}\n\n"
        + diff_section
        + omitted_block
        + "---\n\n"
        + closing
    )


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def tools_schema(
    max_inline_comments: int,
    *,
    allow_set_pr_description: bool = False,
    allow_set_pr_complexity: bool = False,
    allow_update_prior_finding: bool = False,
) -> list[dict[str, Any]]:
    """JSONSchema for every tool the model can call.

    `set_pr_description` is exposed only when `allow_set_pr_description`
    is True (i.e. `pr-description-mode: autocomplete`). Similarly for
    `set_pr_complexity` and the complexity-labeling feature. The base
    five tools are always present.
    """
    base: list[dict[str, Any]] = [
        {
            "name": "read_file",
            "description": (
                "Read a file from the repository. Use this to verify "
                "findings against full file context (the diff alone often "
                "lacks surrounding code). Output is capped to "
                f"{MAX_FILE_READ_LINES} lines per call — use `offset` and "
                "`limit` to paginate if needed."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repository-relative file path.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "1-indexed starting line. Default 1.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"Max lines to return. Default "
                            f"{MAX_FILE_READ_LINES}."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "grep",
            "description": (
                "Search for a regex pattern in the repository. Returns "
                "file:line:match lines (up to 200). Pattern is POSIX "
                "extended regex (no PCRE features like lookahead/`\\b`). "
                "Use to verify whether a pattern exists elsewhere before "
                "flagging an issue as novel."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "POSIX extended regex pattern.",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "Optional path or glob to scope the search."
                        ),
                    },
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "glob",
            "description": (
                "List repository files matching a glob (e.g. "
                "`src/**/*.ts`). Honors `.gitignore`. Returns up to 200 paths."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern relative to repo root.",
                    }
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "post_inline_comment",
            "description": (
                "Queue a single inline review comment. Comments are batched "
                "and submitted with the final review. The line you "
                "reference MUST appear in the PR diff (RIGHT side for new "
                "lines, LEFT for removed lines). For multi-line, set "
                "`start_line` < `line`. Set `severity` honestly: it drives "
                "the GitHub check status via the consumer's strictness "
                f"setting. Cap: {max_inline_comments} comments per review."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repository-relative file path.",
                    },
                    "line": {
                        "type": "integer",
                        "description": (
                            "Line number (end line for multi-line)."
                        ),
                    },
                    "body": {
                        "type": "string",
                        "description": (
                            "Markdown body. Supports GitHub suggestion "
                            "blocks via ```suggestion ... ``` — those "
                            "replace the entire commented line range."
                        ),
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "warning", "info"],
                        "description": (
                            "`critical` = correctness/security/data-loss/"
                            "broken-API. `warning` = bug-prone, perf, "
                            "maintainability. `info` = style/nit/"
                            "improvement. Default `info`."
                        ),
                    },
                    "start_line": {
                        "type": "integer",
                        "description": (
                            "Optional. Start line for multi-line comments."
                        ),
                    },
                    "side": {
                        "type": "string",
                        "enum": ["LEFT", "RIGHT"],
                        "description": (
                            "RIGHT (new code, default) or LEFT (removed code)."
                        ),
                    },
                },
                "required": ["path", "line", "body"],
            },
        },
        {
            "name": "submit_review",
            "description": (
                "Submit the final PR review. Call exactly once at the end. "
                "Provide the full summary markdown. Any queued inline "
                "comments post atomically with this review."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "The full review markdown body.",
                    },
                },
                "required": ["summary"],
            },
        },
    ]
    if allow_set_pr_description:
        base.append(
            {
                "name": "set_pr_description",
                "description": (
                    "Set the PR body to a new markdown value. Call this "
                    "AT MOST ONCE, only when the current PR body is missing "
                    "or too vague. Do NOT call it if the current body "
                    f"already carries the `{PR_DESC_AUTOCOMPLETE_MARKER}` "
                    "marker (that means a previous run already wrote it). "
                    "Do NOT include environment variables, tokens, or "
                    "secrets in the body."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "body": {
                            "type": "string",
                            "description": (
                                "New markdown for the PR body. Should NOT "
                                "include the autocomplete marker — the "
                                "action appends it automatically."
                            ),
                        }
                    },
                    "required": ["body"],
                },
            }
        )
    if allow_set_pr_complexity:
        base.append(
            {
                "name": "set_pr_complexity",
                "description": (
                    "Assess and record the PR's overall complexity. Call "
                    "this AT MOST ONCE, near the end of the review. The "
                    "value drives a `complexity:*` label on the PR. Assess "
                    "based on total change (files touched, cognitive load, "
                    "cross-cutting concerns, security surface, test "
                    "coverage delta) — NOT line count."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "level": {
                            "type": "string",
                            "enum": list(PR_COMPLEXITY_LEVELS),
                            "description": (
                                "`low` = self-contained, easy to review "
                                "(e.g. typo fix, doc, isolated helper). "
                                "`medium` = one subsystem, moderate "
                                "cognitive load. `high` = multiple "
                                "subsystems, security-adjacent code, "
                                "novel abstraction, or requires "
                                "cross-team review."
                            ),
                        }
                    },
                    "required": ["level"],
                },
            }
        )
    if allow_update_prior_finding:
        base.append(
            {
                "name": "update_prior_finding",
                "description": (
                    "Incremental follow-up mode only. Record your verdict on "
                    "ONE prior finding from the `Your prior findings still "
                    "open` table: `resolved` (the new commits fixed it), "
                    "`open` (still present — do NOT re-post it as a new "
                    "comment) or `regressed` (worse now). Call once per row, "
                    "citing the fingerprint verbatim."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "fingerprint": {
                            "type": "string",
                            "description": "The fingerprint column of the row.",
                        },
                        "status": {
                            "type": "string",
                            "enum": list(PRIOR_FINDING_STATUSES),
                        },
                        "note": {
                            "type": "string",
                            "description": (
                                "One line of evidence (which hunk fixed it, "
                                "or why it is still open)."
                            ),
                        },
                    },
                    "required": ["fingerprint", "status"],
                },
            }
        )
    return base


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


@dataclass
class ReviewState:
    """Mutable state shared by tool handlers."""

    inline_comments: list[dict[str, Any]] = field(default_factory=list)
    severities: list[str] = field(default_factory=list)
    final_summary: str | None = None
    max_inline_comments: int = DEFAULT_MAX_INLINE_COMMENTS
    # Populated by `set_pr_description` tool (only in `autocomplete` mode).
    # None = model did not propose a description. `""` is treated identically
    # to None so an accidental empty-string tool call is a no-op.
    proposed_pr_description: str | None = None
    # Populated by `set_pr_complexity` tool (only when complexity labeling
    # is enabled). Values: `low`, `medium`, `high`. None = not proposed.
    proposed_pr_complexity: str | None = None
    # Accumulated API usage across the chat-completions loop (v2.1.0+).
    usage: UsageTelemetry = field(default_factory=UsageTelemetry)
    # Incremental mode: fingerprint → (status, note) from `update_prior_finding`.
    prior_finding_updates: dict[str, tuple[str, str]] = field(default_factory=dict)


def safe_repo_path(rel: str) -> Path:
    """Resolve a repo-relative path, refusing to escape the workspace.

    Uses `Path.relative_to` (component-wise comparison) so a sibling
    directory that string-prefixes the repo root — e.g. workspace
    `/x/repo` and target `/x/repo_evil/file` — does not bypass the check.
    `Path.resolve()` follows symlinks, so a symlinked path that escapes
    the workspace is also caught.
    """
    repo_root: Path = Path.cwd().resolve()
    target: Path = (repo_root / rel).resolve()
    try:
        target.relative_to(repo_root)
    except ValueError as e:
        raise ValueError(f"Path escapes the workspace: {rel}") from e
    return target


def tool_read_file(args: dict[str, Any]) -> str:
    rel: str = args["path"]
    offset: int = max(1, int(args.get("offset", 1)))
    limit: int = min(
        MAX_FILE_READ_LINES, int(args.get("limit", MAX_FILE_READ_LINES))
    )
    try:
        path: Path = safe_repo_path(rel)
    except ValueError as e:
        return f"Error: {e}"
    if not path.exists() or not path.is_file():
        return f"Error: file not found: {rel}"
    with path.open("r", encoding="utf-8", errors="replace") as f:
        all_lines: list[str] = f.readlines()
    selected: list[str] = all_lines[offset - 1 : offset - 1 + limit]
    numbered: str = "".join(
        f"{i + offset:>6}\t{line}" for i, line in enumerate(selected)
    )
    header: str = (
        f"# {rel}  (lines {offset}–{offset + len(selected) - 1} of "
        f"{len(all_lines)})\n"
    )
    return truncate_for_tool(header + numbered, label="read_file")


def tool_grep(args: dict[str, Any]) -> str:
    pattern: str = args["pattern"]
    scope: str | None = args.get("path")
    cmd: list[str] = ["grep", "-rIn", "-E", "--", pattern]
    if scope:
        # Validate the scope path the same way `tool_read_file` does so a
        # caller cannot smuggle `../../etc/passwd`-style traversal. The `--`
        # separator only protects the pattern from flag injection; it does
        # NOT restrict which filesystem paths grep will read.
        try:
            scope = str(safe_repo_path(scope))
        except ValueError as e:
            return f"Error: {e}"
        cmd.append(scope)
    else:
        cmd.append(".")
    proc = run_cmd(cmd)
    if proc.returncode not in (0, 1):  # 1 = no matches, fine
        return (
            f"grep error (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:MAX_ERROR_BODY_CHARS]}"
        )
    lines: list[str] = proc.stdout.splitlines()
    if not lines:
        return f"(no matches for /{pattern}/)"
    if len(lines) > MAX_SEARCH_RESULTS:
        lines = lines[:MAX_SEARCH_RESULTS] + [
            f"... [{len(lines) - MAX_SEARCH_RESULTS} more matches truncated]"
        ]
    return truncate_for_tool("\n".join(lines), label="grep")


def tool_glob(args: dict[str, Any]) -> str:
    pattern: str = args["pattern"]
    proc = run_cmd(["git", "ls-files", "--", pattern])
    if proc.returncode != 0:
        return (
            f"glob error (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:MAX_ERROR_BODY_CHARS]}"
        )
    paths: list[str] = proc.stdout.splitlines()
    if not paths:
        return f"(no files match {pattern})"
    if len(paths) > MAX_SEARCH_RESULTS:
        paths = paths[:MAX_SEARCH_RESULTS] + [
            f"... [{len(paths) - MAX_SEARCH_RESULTS} more paths truncated]"
        ]
    return truncate_for_tool("\n".join(paths), label="glob")


def tool_post_inline_comment(args: dict[str, Any], state: ReviewState) -> str:
    if len(state.inline_comments) >= state.max_inline_comments:
        return (
            f"Error: inline-comment cap reached ({state.max_inline_comments}). "
            "Drop or merge less-critical comments before adding more."
        )
    severity: str = (args.get("severity") or SEVERITY_INFO).lower()
    if severity not in SEVERITY_RANK or severity == SEVERITY_NONE:
        severity = SEVERITY_INFO
    comment: dict[str, Any] = {
        "path": args["path"],
        "body": args["body"],
        "line": int(args["line"]),
        "side": args.get("side", "RIGHT"),
    }
    if "start_line" in args and args["start_line"] is not None:
        comment["start_line"] = int(args["start_line"])
        comment["start_side"] = args.get("side", "RIGHT")
    state.inline_comments.append(comment)
    state.severities.append(severity)
    return (
        f"Queued inline comment #{len(state.inline_comments)} on "
        f"{comment['path']}:{comment['line']} (severity={severity}). It will "
        "post with the final review when you call submit_review."
    )


def tool_submit_review(args: dict[str, Any], state: ReviewState) -> str:
    if state.final_summary is not None:
        # Idempotency guard — models occasionally re-call across multi-tool
        # turns. Keep the first articulation; surface a clear error so the
        # model stops trying.
        return (
            "Error: submit_review was already called this session and your "
            "review summary has been recorded. Do not call submit_review "
            "again — end your turn so the script can post the review."
        )
    state.final_summary = args["summary"]
    return (
        "Review accepted. End your turn now — the script will post the review "
        "with the queued inline comments. Do not call any more tools."
    )


def tool_set_pr_description(
    args: dict[str, Any], state: ReviewState
) -> str:
    """Record a proposed PR body. The actual PATCH happens in `main()`
    after the loop terminates, so the whole lifecycle stays atomic and
    the marker check can inspect the final resolved body.
    """
    body: str = args.get("body", "")
    if not isinstance(body, str) or not body.strip():
        return (
            "Error: `body` must be a non-empty string. Skipped — the "
            "current PR body will be left unchanged."
        )
    if state.proposed_pr_description is not None:
        return (
            "Error: set_pr_description was already called this session. "
            "The first proposal is retained; do not call it again."
        )
    state.proposed_pr_description = body
    return (
        "PR description proposal recorded. The action will PATCH the PR "
        "body after this run if the current body is missing/vague and "
        "does not already carry the autocomplete marker."
    )


def tool_set_pr_complexity(
    args: dict[str, Any], state: ReviewState
) -> str:
    """Record an AI-assessed complexity level. The actual label
    application happens in `main()` after the loop terminates.
    """
    level: str = str(args.get("level", "")).strip().lower()
    if level not in PR_COMPLEXITY_LEVELS:
        return (
            f"Error: `level` must be one of {list(PR_COMPLEXITY_LEVELS)}. "
            f"Got: {level!r}. Skipped."
        )
    if state.proposed_pr_complexity is not None:
        return (
            "Error: set_pr_complexity was already called this session. "
            "The first assessment is retained; do not call it again."
        )
    state.proposed_pr_complexity = level
    return (
        f"PR complexity `{level}` recorded. The action will apply the "
        "corresponding label after this run."
    )


def tool_update_prior_finding(args: dict[str, Any], state: ReviewState) -> str:
    """Record the model's verdict on one prior finding (incremental mode)."""
    fingerprint: str = str(args.get("fingerprint") or "").strip()
    status: str = str(args.get("status") or "").strip().lower()
    note: str = str(args.get("note") or "").strip()[:300]
    if not fingerprint:
        return "Error: `fingerprint` is required (copy it from the prior-findings table)."
    if status not in PRIOR_FINDING_STATUSES:
        return (
            f"Error: status {status!r} is not one of "
            f"{', '.join(PRIOR_FINDING_STATUSES)}."
        )
    state.prior_finding_updates[fingerprint] = (status, note)
    return f"Recorded prior finding {fingerprint} as {status}."


def execute_tool(name: str, args: dict[str, Any], state: ReviewState) -> str:
    """Dispatch a tool call to its handler and return a tool_result string."""
    try:
        if name == "read_file":
            return tool_read_file(args)
        if name == "grep":
            return tool_grep(args)
        if name == "glob":
            return tool_glob(args)
        if name == "post_inline_comment":
            return tool_post_inline_comment(args, state)
        if name == "submit_review":
            return tool_submit_review(args, state)
        if name == "set_pr_description":
            return tool_set_pr_description(args, state)
        if name == "set_pr_complexity":
            return tool_set_pr_complexity(args, state)
        if name == "update_prior_finding":
            return tool_update_prior_finding(args, state)
        return f"Error: unknown tool `{name}`"
    except Exception as e:  # noqa: BLE001 — surface to model rather than crash
        return f"Tool `{name}` raised {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Author-association gate (v1.3.0+): the first / cheapest gate. Runs before
# `trigger-mode` so a rejected PR never consumes a single API call.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorAssociationDecision:
    """Result of `resolve_author_association_gate()` / enhanced resolver.

    - `should_run` — True when the PR author is allowed through the gate.
    - `reason` — one-line explanation for the runtime log.
    - `author_association` — the webhook association compared, uppercase.
    - `allowed_associations` — the parsed whitelist, for downstream log
      messages (`Skipping: author X not in [OWNER, MEMBER, …]`).
    - `collaborator_permission` — resolved REST permission when looked up.
    - `repo_visibility` — `public`, `private`, `internal`, or `unknown`.
    """

    should_run: bool
    reason: str
    author_association: str
    allowed_associations: tuple[str, ...]
    collaborator_permission: str = ""
    repo_visibility: str = "unknown"


def resolve_author_association_gate(
    *, gate: str, actual_association: str
) -> AuthorAssociationDecision:
    """Decide whether the review should run given the author-association
    whitelist `gate` and the PR's actual `author_association`.

    Semantics:

    - **Empty `gate`** — gate disabled, every author allowed.
    - **Empty `actual_association`** — no PR context (local run,
      `workflow_dispatch`, malformed event payload). Fail-open: the
      operator running locally already has write access, and CI-time
      failures to read the payload are already logged elsewhere.
    - **`actual_association` in whitelist** — allowed.
    - **`actual_association` not in whitelist** — denied.

    Parsing is case-insensitive and tolerates whitespace between commas.
    Unknown values in the whitelist are logged as a warning and can
    never match (fail-safe).
    """
    normalized_gate: str = gate.strip()
    if not normalized_gate:
        return AuthorAssociationDecision(
            should_run=True,
            reason="no author-association gate configured",
            author_association=(actual_association or "").upper(),
            allowed_associations=(),
        )

    allowed: tuple[str, ...] = tuple(
        piece.strip().upper()
        for piece in normalized_gate.split(",")
        if piece.strip()
    )
    unknown: list[str] = [
        a for a in allowed if a not in VALID_AUTHOR_ASSOCIATIONS
    ]
    if unknown:
        log(
            f"WARNING: author-association gate lists unknown value(s) "
            f"{unknown}; they will never match. Valid values: "
            f"{list(VALID_AUTHOR_ASSOCIATIONS)}."
        )

    normalized_actual: str = (actual_association or "").upper()

    if not normalized_actual:
        return AuthorAssociationDecision(
            should_run=True,
            reason=(
                "author-association gate fail-open: no PR "
                "author_association in event payload (likely a local "
                "run, workflow_dispatch, or malformed event)"
            ),
            author_association="",
            allowed_associations=allowed,
        )

    if normalized_actual in allowed:
        return AuthorAssociationDecision(
            should_run=True,
            reason=(
                f"author_association '{normalized_actual}' matches "
                f"gate {list(allowed)}"
            ),
            author_association=normalized_actual,
            allowed_associations=allowed,
        )

    return AuthorAssociationDecision(
        should_run=False,
        reason=(
            f"author_association '{normalized_actual}' not in gate "
            f"{list(allowed)}"
        ),
        author_association=normalized_actual,
        allowed_associations=allowed,
    )


def _author_gate_needs_permission_lookup(
    *, gate: str, webhook_association: str
) -> bool:
    """Return True when the collaborator-permission API should be called."""
    if not gate.strip():
        return False
    if not (webhook_association or "").strip():
        return False
    base: AuthorAssociationDecision = resolve_author_association_gate(
        gate=gate,
        actual_association=webhook_association,
    )
    return not base.should_run


def _is_private_or_internal_visibility(repo_visibility: str) -> bool:
    normalized: str = (repo_visibility or "").strip().lower()
    return normalized in ("private", "internal")


def resolve_author_association_gate_enhanced(
    *,
    gate: str,
    webhook_association: str,
    collaborator_permission: str | None = None,
    permission_lookup_failed: bool = False,
    repo_visibility: str = "unknown",
) -> AuthorAssociationDecision:
    """Permission-aware author gate — extends webhook-only resolution.

    When the webhook ``author_association`` is not in the allow-list on a
    **private or internal** repo, a collaborator permission of ``admin``,
    ``maintain``, or ``write`` still allows the review (fixes GitHub
    under-reporting on private org repos). On public repos the gate stays
    association-only so narrowed presets like ``OWNER,MEMBER`` remain
    strict. Permission lookup failures fail-open on private/internal repos
    and fail-closed on public repos.
    """
    visibility: str = (repo_visibility or "unknown").lower() or "unknown"
    base: AuthorAssociationDecision = resolve_author_association_gate(
        gate=gate,
        actual_association=webhook_association,
    )
    metadata: dict[str, str] = {
        "collaborator_permission": collaborator_permission or "",
        "repo_visibility": visibility,
    }

    if base.should_run:
        return AuthorAssociationDecision(
            should_run=base.should_run,
            reason=base.reason,
            author_association=base.author_association,
            allowed_associations=base.allowed_associations,
            **metadata,
        )

    if not gate.strip():
        return AuthorAssociationDecision(
            should_run=base.should_run,
            reason=base.reason,
            author_association=base.author_association,
            allowed_associations=base.allowed_associations,
            **metadata,
        )

    if permission_lookup_failed:
        if _is_private_or_internal_visibility(visibility):
            return AuthorAssociationDecision(
                should_run=True,
                reason=(
                    f"permission lookup failed; fail-open on {visibility}"
                ),
                author_association=base.author_association,
                allowed_associations=base.allowed_associations,
                collaborator_permission="unknown",
                repo_visibility=visibility,
            )
        return AuthorAssociationDecision(
            should_run=False,
            reason="permission lookup failed; fail-closed on public",
            author_association=base.author_association,
            allowed_associations=base.allowed_associations,
            collaborator_permission="unknown",
            repo_visibility=visibility,
        )

    permission: str = (collaborator_permission or "").lower()
    if (
        permission in COLLABORATOR_PERMISSION_WRITE_TIER
        and _is_private_or_internal_visibility(visibility)
    ):
        return AuthorAssociationDecision(
            should_run=True,
            reason=(
                f"permission={permission} overrides "
                f"webhook={base.author_association}"
            ),
            author_association=base.author_association,
            allowed_associations=base.allowed_associations,
            collaborator_permission=permission,
            repo_visibility=visibility,
        )

    return AuthorAssociationDecision(
        should_run=False,
        reason=(
            f"webhook={base.author_association} permission={permission or 'unknown'} "
            f"not in gate {list(base.allowed_associations)}"
        ),
        author_association=base.author_association,
        allowed_associations=base.allowed_associations,
        collaborator_permission=permission or "unknown",
        repo_visibility=visibility,
    )


def format_author_gate_log_line(
    decision: AuthorAssociationDecision, *, gate_raw: str
) -> str:
    """Emit the actionable author-gate log line required by operators."""
    webhook: str = decision.author_association or "(none)"
    permission: str = decision.collaborator_permission or "not_fetched"
    visibility: str = decision.repo_visibility or "unknown"
    allowlist: str = gate_raw.strip() or "(disabled)"
    verdict: str = "allow" if decision.should_run else "deny"
    return (
        f"Author gate: webhook={webhook} permission={permission} "
        f"visibility={visibility} allowlist={allowlist} → {verdict} "
        f"({decision.reason})"
    )


# ---------------------------------------------------------------------------
# Trigger modes (v1.2.0+): decide whether to run based on webhook event
# + label state + prior-run marker generation.
# ---------------------------------------------------------------------------


@dataclass
class TriggerDecision:
    """Result of `resolve_trigger_action()`."""

    should_run: bool
    reason: str  # log line + optional tracking-comment note


COUNT_LABEL_EVENTS_MAX_PAGES: int = 20


def count_label_events(
    *, token: str, repo: str, pr_number: int, label: str
) -> int:
    """Return the number of times `label` was applied to the PR.

    Uses `/issues/{n}/timeline`, filtered on `labeled` events with the
    matching label name. Best-effort: any error returns the count
    accumulated so far (possibly 0) — callers use the "was the label
    present at all?" signal to decide whether that 0 is meaningful.

    Pagination is capped at `COUNT_LABEL_EVENTS_MAX_PAGES` (~2000
    timeline events) to bound cost on long-lived, high-chatter PRs.
    When the cap is hit a `WARNING:` is logged; `label-once` may
    undercount the generation on such PRs, in which case toggling the
    label twice or switching to `label-added-only` are the documented
    workarounds (see docs/TRIGGER_MODES.md § "Edge cases").
    """
    if not label:
        return 0
    owner, name = repo.split("/", 1)
    count: int = 0
    page: int = 1
    while True:
        try:
            events: list[dict[str, Any]] = gh_request(
                "GET",
                (
                    f"/repos/{owner}/{name}/issues/{pr_number}/timeline"
                    f"?per_page=100&page={page}"
                ),
                token=token,
            )
        except Exception as e:  # noqa: BLE001 — best-effort GH API call
            log(f"count_label_events: could not read timeline page {page}: {e}")
            return count
        if not isinstance(events, list) or not events:
            break
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if ev.get("event") != "labeled":
                continue
            lbl_field: dict[str, Any] = ev.get("label") or {}
            # Case-insensitive match (`ready` == `Ready`) — consistent with
            # gh_pr_has_label / resolve_trigger_action.
            if (lbl_field.get("name") or "").strip().lower() == label.strip().lower():
                count += 1
        if len(events) < 100:
            break
        page += 1
        if page > COUNT_LABEL_EVENTS_MAX_PAGES:
            log(
                f"WARNING: count_label_events hit the "
                f"{COUNT_LABEL_EVENTS_MAX_PAGES}-page pagination cap for "
                f"label {label!r} on PR #{pr_number}. `label-once` "
                f"generation may be undercounted; if a re-review does not "
                f"fire, toggle {label!r} off/on twice or switch to "
                f"`trigger-mode: label-added-only`."
            )
            break
    return count


def read_trigger_state(tracking_comment_body: str) -> dict[str, Any]:
    """Parse the ai-pr-reviewer-state HTML comment, or `{}` if absent."""
    if not tracking_comment_body:
        return {}
    body: str = tracking_comment_body
    open_at: int = body.find(TRIGGER_STATE_MARKER_OPEN)
    if open_at < 0:
        return {}
    close_at: int = body.find(
        TRIGGER_STATE_MARKER_CLOSE, open_at + len(TRIGGER_STATE_MARKER_OPEN)
    )
    if close_at < 0:
        return {}
    raw: str = body[
        open_at + len(TRIGGER_STATE_MARKER_OPEN) : close_at
    ].strip()
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def write_trigger_state(body: str, state: dict[str, Any]) -> str:
    """Emit `body` with the ai-pr-reviewer-state marker inserted/updated.

    The state block sits on a line by itself just after the runtime's
    canonical `<!-- ai-pr-reviewer-marker -->` marker (or at the top if
    that marker is absent). Round-trips cleanly through `read_trigger_state`.
    """
    payload: str = (
        TRIGGER_STATE_MARKER_OPEN + json.dumps(state, sort_keys=True)
        + TRIGGER_STATE_MARKER_CLOSE
    )
    # Strip any pre-existing state block to keep the body idempotent.
    prior_open: int = body.find(TRIGGER_STATE_MARKER_OPEN)
    if prior_open >= 0:
        prior_close: int = body.find(
            TRIGGER_STATE_MARKER_CLOSE,
            prior_open + len(TRIGGER_STATE_MARKER_OPEN),
        )
        if prior_close >= 0:
            body = (
                body[:prior_open]
                + body[prior_close + len(TRIGGER_STATE_MARKER_CLOSE) :]
            )
            body = body.lstrip("\n")
    canonical_marker: str = "<!-- ai-pr-reviewer-marker -->"
    marker_at: int = body.find(canonical_marker)
    if marker_at < 0:
        return payload + "\n" + body
    insert_at: int = marker_at + len(canonical_marker)
    return body[:insert_at] + "\n" + payload + body[insert_at:]


def _read_github_event_payload() -> dict[str, Any]:
    """Return the current GitHub event payload as a dict, or `{}`.

    Reads `GITHUB_EVENT_PATH` — a JSON file provided by the runner for
    every workflow event. Best-effort: parsing errors return `{}` so
    the trigger resolver treats the event as generic.
    """
    path: str = os.environ.get("GITHUB_EVENT_PATH", "")
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload: Any = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        log(f"Could not read GITHUB_EVENT_PATH ({path!r}): {e}")
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _read_github_event_action() -> str:
    """Return the `action` field of the current GitHub event, or `""`."""
    payload = _read_github_event_payload()
    return str(payload.get("action", "") or "")


def _read_github_event_label() -> str:
    """Return the `label.name` field of the current event, or `""`.

    Only meaningful for `labeled` / `unlabeled` webhook events, where
    GitHub attaches the specific label that triggered the event to the
    payload. Any other event returns `""`. Used by
    `label-added-only` to reject webhooks fired by unrelated labels.
    """
    payload = _read_github_event_payload()
    label = payload.get("label")
    if not isinstance(label, dict):
        return ""
    return str(label.get("name", "") or "")


def _read_github_event_pr_author_association() -> str:
    """Return `pull_request.author_association` from the event payload
    in uppercase, or `""` when unavailable.

    GitHub attaches this field to every `pull_request` /
    `pull_request_target` webhook — it is derived server-side and
    cannot be spoofed by the PR author. When empty, the caller is
    outside a PR-event context (local run, `workflow_dispatch`, etc.)
    and the caller should fail-open on the author gate.
    """
    payload = _read_github_event_payload()
    pr = payload.get("pull_request")
    if not isinstance(pr, dict):
        return ""
    return str(pr.get("author_association", "") or "").upper()


def _read_github_event_pr_author_login() -> str:
    """Return `pull_request.user.login` from the event payload, or `""`."""
    payload = _read_github_event_payload()
    pr = payload.get("pull_request")
    if not isinstance(pr, dict):
        return ""
    user = pr.get("user")
    if not isinstance(user, dict):
        return ""
    return str(user.get("login", "") or "")


def _read_github_event_repo_visibility() -> str:
    """Return `repository.visibility` from the event payload, or `unknown`."""
    payload = _read_github_event_payload()
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        return "unknown"
    visibility: str = str(repository.get("visibility", "") or "").lower()
    return visibility or "unknown"


def _read_existing_tracking_state(
    *, token: str, repo: str, pr_number: int, provider_id: str = ""
) -> dict[str, Any]:
    """Fetch prior ai-pr-reviewer tracking-comment state, or `{}`.

    Looks for an issue comment carrying the canonical
    `<!-- ai-pr-reviewer-marker -->` marker. Returns the parsed state
    JSON from the same body, or `{}` when no prior comment exists.
    """
    owner, name = repo.split("/", 1)
    try:
        comments: list[dict[str, Any]] = gh_request(
            "GET",
            f"/repos/{owner}/{name}/issues/{pr_number}/comments?per_page=100",
            token=token,
        )
    except Exception as e:  # noqa: BLE001 — best-effort GH API call
        log(f"Could not list issue comments for trigger state: {e}")
        return {}
    if not isinstance(comments, list):
        return {}
    marker: str = "<!-- ai-pr-reviewer-marker -->"
    # Iterate in reverse — the most recent tracking comment wins.
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        body: str = str(comment.get("body") or "")
        if marker not in body:
            continue
        if provider_id and provider_marker(provider_id) not in body:
            # Only historical default lanes may adopt an untagged marker.
            if provider_id not in DEFAULT_MODELS or PROVIDER_MARKER_PREFIX in body:
                continue
        return read_trigger_state(body)
    return {}


def resolve_trigger_action(
    *,
    trigger_mode: str,
    event_action: str,
    label_gate: str,
    current_labels: list[str],
    label_toggle_generation: int,
    last_reviewed_generation: int,
    event_label: str = "",
) -> TriggerDecision:
    """Decide whether to run the review for the current event.

    `event_label` is the specific label attached to `labeled`/`unlabeled`
    webhook payloads (from `event.label.name`). It's used by
    `label-added-only` to distinguish "this label was just added" from
    "some unrelated label was just added while `label_gate` was already
    present."

    See docs/TRIGGER_MODES.md for the full semantics per mode.
    """
    if trigger_mode == TRIGGER_ALWAYS:
        return TriggerDecision(True, "trigger-mode=always")

    if not label_gate:
        return TriggerDecision(
            True,
            f"trigger-mode={trigger_mode} requires label-gate; "
            "no label-gate set → treating as always.",
        )

    # Label matching is CASE-INSENSITIVE: `ready`, `Ready`, and `READY` all
    # satisfy `label-gate: ready`. GitHub label names are case-sensitive as
    # stored, but gating on exact case is a foot-gun, so we compare on a
    # lowercased, whitespace-trimmed basis throughout. (Display strings below
    # keep the configured casing via `label_gate!r`.)
    label_gate_lc: str = label_gate.strip().lower()
    current_labels_lc: list[str] = [c.strip().lower() for c in current_labels]
    event_label_lc: str = event_label.strip().lower()
    label_present: bool = label_gate_lc in current_labels_lc

    if trigger_mode == TRIGGER_LABEL_REQUIRED:
        if not label_present:
            return TriggerDecision(
                False, f"label {label_gate!r} not present"
            )
        return TriggerDecision(True, f"label {label_gate!r} present")

    if trigger_mode == TRIGGER_LABEL_ONCE:
        if not label_present:
            return TriggerDecision(
                False, f"label {label_gate!r} not present"
            )
        # Only skip on a stale generation if we actually counted at least
        # one `labeled` event. Otherwise `count_label_events()` returned 0
        # (transient API error, permissions issue, or empty timeline) —
        # skipping would silently mask "the label is present but we can't
        # tell how many times it's been applied." Better to run than to
        # deliver nothing. Regression for PR #9 self-review comment #4.
        if (
            label_toggle_generation > 0
            and label_toggle_generation <= last_reviewed_generation
        ):
            return TriggerDecision(
                False,
                (
                    f"already reviewed label generation "
                    f"{last_reviewed_generation} — toggle "
                    f"{label_gate!r} off/on to re-run"
                ),
            )
        return TriggerDecision(
            True,
            (
                f"new label generation ({label_toggle_generation} "
                f"vs. last reviewed {last_reviewed_generation})"
            ),
        )

    if trigger_mode == TRIGGER_LABEL_ADDED_ONLY:
        if event_action != "labeled":
            return TriggerDecision(
                False,
                (
                    f"event action {event_action!r} is not 'labeled' — "
                    "workflow must subscribe with `types: [labeled]`"
                ),
            )
        if not label_present:
            return TriggerDecision(
                False, f"label {label_gate!r} not present"
            )
        # `labeled` fires for ANY label — reject when it's not our gate.
        # Without this, adding an unrelated label (e.g. `bug`) triggers
        # a full review as long as `label_gate` was already present.
        if event_label_lc and event_label_lc != label_gate_lc:
            return TriggerDecision(
                False,
                (
                    f"labeled event was for {event_label!r}, "
                    f"not {label_gate!r}"
                ),
            )
        return TriggerDecision(True, "labeled event fired")

    return TriggerDecision(
        False, f"unknown trigger-mode {trigger_mode!r} — no action taken"
    )


# ---------------------------------------------------------------------------
# PR metadata checks (v1.2.0+): description review + complexity labeling.
# ---------------------------------------------------------------------------


@dataclass
class DescriptionVerdict:
    """Result of `evaluate_pr_description()`."""

    is_adequate: bool
    reason: str  # empty when adequate, otherwise a short human-readable reason


def build_agent_runner_noop_warning(
    *,
    provider_id: str,
    is_agent_runner: bool,
    pr_desc_mode: str,
    complexity_labels_enabled: bool,
) -> str:
    """Return the WARNING log line for v1.2 features that silently no-op
    on agent-runner providers, or `""` if none apply.

    Extracted from `main()` for unit-testability. `set_pr_description` is
    exposed via `tools_schema()` only on the chat-completions path, so
    `pr-description-mode=autocomplete` never populates `state.proposed_*`
    on agent-runner providers → the post-loop PATCH block silently no-ops.
    Complexity labeling is bridged via optional `complexity` in
    `findings.json` when `complexity-labels-enabled=true`. See
    docs/PR_METADATA_CHECKS.md § "Provider support matrix".
    """
    if not is_agent_runner:
        return ""
    skips: list[str] = []
    if pr_desc_mode == PR_DESC_MODE_AUTOCOMPLETE:
        skips.append("pr-description-mode=autocomplete")
    if not skips:
        return ""
    return (
        "WARNING: "
        + ", ".join(skips)
        + f" requested but provider={provider_id!r} is an "
        "agent-runner CLI. These features are chat-completions-only "
        "in v1.2 and will silently no-op. See "
        "docs/PR_METADATA_CHECKS.md § 'Provider support matrix'."
    )


def evaluate_pr_description(
    body: str, *, min_length: int
) -> DescriptionVerdict:
    """Cheap heuristic — 'missing' if body is empty/whitespace after strip,
    'vague' if under `min_length` after stripping the autocomplete marker.

    The heuristic is intentionally simple; a smart LLM check would burn
    tokens on trivia. The maintainer decides the minimum length; the
    action just enforces it.
    """
    stripped: str = (
        (body or "").replace(PR_DESC_AUTOCOMPLETE_MARKER, "").strip()
    )
    if not stripped:
        return DescriptionVerdict(
            is_adequate=False, reason="PR description is empty."
        )
    if len(stripped) < min_length:
        return DescriptionVerdict(
            is_adequate=False,
            reason=(
                f"PR description is too short ({len(stripped)} chars); "
                f"minimum is {min_length}."
            ),
        )
    return DescriptionVerdict(is_adequate=True, reason="")


def gh_patch_pr_body(
    *, token: str, repo: str, pr_number: int, new_body: str
) -> None:
    """PATCH the PR body via the GitHub REST API.

    Raises on non-2xx. Callers are expected to wrap this in try/except
    so PR-description autocomplete failures do not crash the review.
    """
    owner, name = repo.split("/", 1)
    gh_request(
        "PATCH",
        f"/repos/{owner}/{name}/pulls/{pr_number}",
        token=token,
        body={"body": new_body},
    )


# ---------------------------------------------------------------------------
# Severity / strictness
# ---------------------------------------------------------------------------


def overall_severity(severities: list[str]) -> str:
    """Return the highest severity in the list, or `none` if empty."""
    if not severities:
        return SEVERITY_NONE
    ranked: list[tuple[int, str]] = [
        (SEVERITY_RANK.get(s, 0), s) for s in severities
    ]
    return max(ranked)[1]


def state_to_review_result(state: "ReviewState") -> ReviewResult:
    """Adapt a `ReviewState` (populated by `drive_review`) into a `ReviewResult`.

    Bridges the chat-completions provider family into the provider-independent
    shape the submission path consumes. The CLI (agent-runner) providers
    produce `ReviewResult` directly via `parse_findings_file`, so the two
    families converge at this dataclass.
    """
    findings: list[Finding] = []
    for i, comment in enumerate(state.inline_comments):
        severity: str = (
            state.severities[i] if i < len(state.severities) else SEVERITY_INFO
        )
        findings.append(
            Finding(
                path=str(comment.get("path", "")),
                line=int(comment.get("line", 0)),
                body=str(comment.get("body", "")),
                severity=severity,
                start_line=(
                    int(comment["start_line"])
                    if "start_line" in comment
                    and comment["start_line"] is not None
                    else None
                ),
                side=comment.get("side", "RIGHT"),
            )
        )
    severities: list[str] = [f.severity for f in findings]
    return ReviewResult(
        usage=state.usage if state.usage.turns else None,
        prior_finding_updates=dict(state.prior_finding_updates),
        summary=state.final_summary or "",
        findings=findings,
        overall_severity=overall_severity(severities),
    )


def _extract_summary_from_malformed_findings(raw_text: str) -> str | None:
    """Best-effort extraction for malformed agent-runner JSON.

    Some vendor CLIs occasionally hand-write invalid JSON while still leaving
    a valid top-level `summary` string. Recovering that summary lets the action
    post a review instead of failing the whole check; inline findings are
    intentionally not recovered from malformed JSON.
    """
    key_index: int = raw_text.find('"summary"')
    if key_index < 0:
        return None
    colon_index: int = raw_text.find(":", key_index + len('"summary"'))
    if colon_index < 0:
        return None
    value_start: int = colon_index + 1
    while value_start < len(raw_text) and raw_text[value_start].isspace():
        value_start += 1
    if value_start >= len(raw_text) or raw_text[value_start] != '"':
        return None

    decoder = json.JSONDecoder()
    try:
        value, _end_index = decoder.raw_decode(raw_text[value_start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(value, str):
        return None
    return value


def parse_complexity_level(raw_value: Any) -> str | None:
    """Normalise an optional complexity level from tools or findings.json.

    Returns a validated `low`/`medium`/`high` string, or `None` when the
    value is absent or not a recognised level.
    """
    if raw_value is None:
        return None
    level: str = str(raw_value).strip().lower()
    if level not in PR_COMPLEXITY_LEVELS:
        return None
    return level


def resolve_pr_complexity(
    *,
    state: "ReviewState",
    result: ReviewResult,
) -> str | None:
    """Return the PR complexity level from either provider family."""
    return state.proposed_pr_complexity or result.complexity


# Path substrings that bump heuristic complexity (security / runtime surface).
_COMPLEXITY_HIGH_PATH_MARKERS: tuple[str, ...] = (
    "auth",
    "crypto",
    "secret",
    "password",
    "token",
    "security",
    "scripts/reviewer.py",
    "action.yml",
)
_COMPLEXITY_DOC_SUFFIXES: tuple[str, ...] = (".md", ".mdx", ".rst", ".txt")


def infer_pr_complexity_fallback(pr_ctx: PRContext) -> str:
    """Heuristic complexity when the model omits an explicit level.

    Used only when `complexity-labels-enabled` is on but neither
    `set_pr_complexity` nor findings.json `complexity` was recorded.
    Keeps labeling provider-agnostic even when an agent-runner CLI skips
    the output contract.
    """
    paths: list[str] = [
        str(f.get("filename") or "") for f in pr_ctx.changed_files
    ]
    if not paths:
        return PR_COMPLEXITY_LOW

    lowered: list[str] = [p.lower() for p in paths]
    if any(
        marker in path
        for path in lowered
        for marker in _COMPLEXITY_HIGH_PATH_MARKERS
    ):
        return PR_COMPLEXITY_HIGH

    if len(paths) == 1 and paths[0].endswith(_COMPLEXITY_DOC_SUFFIXES):
        return PR_COMPLEXITY_LOW

    line_delta: int = pr_ctx.additions + pr_ctx.deletions
    if len(paths) >= 8 or line_delta > 500:
        return PR_COMPLEXITY_HIGH
    if len(paths) >= 3 or line_delta > 100:
        return PR_COMPLEXITY_MEDIUM

    return PR_COMPLEXITY_LOW


def parse_findings_file(
    path: Path, *, allow_malformed_summary_fallback: bool = False
) -> ReviewResult:
    """Parse an agent-runner `findings.json` into a `ReviewResult`.

    Strict validation:
      - Root MUST be a JSON object.
      - `findings` MUST be a list (may be empty).
      - Every finding MUST carry non-empty `path`, integer `line`, non-empty
        `body`. Missing severity defaults to `info`; unknown severities raise.
      - Optional `start_line` is coerced to int; optional `side` MUST be one
        of LEFT/RIGHT (case-normalised).
      - Unknown top-level or per-finding keys are silently ignored (forward-
        compat with vendor extensions).

    Raises:
      - `FileNotFoundError` with an actionable message if the file is missing.
      - `ValueError` for malformed JSON or schema violations, quoting the
        offending path/index/value so the caller can surface it to the model.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Agent-runner provider did not write {path}. "
            "The CLI may have crashed, the review-instruction prompt may be "
            "missing the write-to-file directive, or the workspace path is "
            "wrong. See docs/PROVIDERS.md for the contract."
        )
    size: int = path.stat().st_size
    if size > MAX_FINDINGS_FILE_BYTES:
        raise ValueError(
            f"Agent-runner findings file {path} is {size} bytes, above the "
            f"{MAX_FINDINGS_FILE_BYTES}-byte cap; refusing to parse it. A "
            "review never needs a findings file this large — the CLI was "
            "likely tricked into dumping content into it."
        )
    raw_text: str = path.read_text(encoding="utf-8")
    try:
        raw: Any = json.loads(raw_text)
    except json.JSONDecodeError as e:
        if allow_malformed_summary_fallback:
            recovered_summary: str | None = (
                _extract_summary_from_malformed_findings(raw_text)
            )
            if recovered_summary:
                log(
                    "WARNING: Agent-runner provider wrote malformed "
                    f"findings.json ({e}). Posting summary-only review; "
                    "inline findings were dropped because the JSON could "
                    "not be trusted."
                )
                summary: str = (
                    recovered_summary.rstrip()
                    + "\n\n---\n\n"
                    + "**AI Diff Reviewer note:** The CLI wrote malformed "
                    + "`findings.json`, so this run posted the recovered "
                    + "summary only and dropped inline findings."
                )
                return ReviewResult(
                    summary=summary,
                    findings=[],
                    overall_severity=SEVERITY_NONE,
                )
        snippet: str = raw_text[:MAX_ERROR_BODY_CHARS]
        raise ValueError(
            f"Malformed findings.json ({e}). Content head: {snippet!r}"
        ) from e

    if not isinstance(raw, dict):
        raise ValueError(
            f"findings.json root must be an object, got {type(raw).__name__}"
        )

    summary: str = str(raw.get("summary") or "")
    raw_findings: Any = raw.get("findings") if raw.get("findings") is not None else []
    if not isinstance(raw_findings, list):
        raise ValueError(
            f"'findings' must be a list, got {type(raw_findings).__name__}"
        )

    findings: list[Finding] = []
    for i, item in enumerate(raw_findings):
        if not isinstance(item, dict):
            raise ValueError(f"finding[{i}] must be an object")
        try:
            path_val: str = str(item["path"])
            line_val: int = int(item["line"])
            body_val: str = str(item["body"])
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(
                f"finding[{i}] missing or invalid required field: {e}"
            ) from e
        if not path_val:
            raise ValueError(f"finding[{i}].path is empty")
        if not body_val.strip():
            raise ValueError(f"finding[{i}].body is empty")

        severity_val: str = str(item.get("severity") or SEVERITY_INFO).lower()
        if severity_val not in ALLOWED_SEVERITIES:
            raise ValueError(
                f"finding[{i}].severity={severity_val!r} not in "
                f"{ALLOWED_SEVERITIES}"
            )

        start_line_val: int | None = None
        if item.get("start_line") is not None:
            try:
                start_line_val = int(item["start_line"])
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"finding[{i}].start_line must be an integer: {e}"
                ) from e

        side_val: str | None = "RIGHT"
        if item.get("side") is not None:
            side_val = str(item["side"]).upper()
            if side_val not in ALLOWED_SIDES:
                raise ValueError(
                    f"finding[{i}].side={side_val!r} not in {ALLOWED_SIDES}"
                )

        findings.append(
            Finding(
                path=path_val,
                line=line_val,
                body=body_val,
                severity=severity_val,
                start_line=start_line_val,
                side=side_val,
            )
        )

    # Incremental mode (v2.1.0+): optional `prior_findings` verdicts.
    prior_updates: dict[str, tuple[str, str]] = {}
    raw_prior: Any = raw.get("prior_findings")
    if isinstance(raw_prior, list):
        for entry in raw_prior:
            if not isinstance(entry, dict):
                continue
            pf_fingerprint: str = str(entry.get("fingerprint") or "").strip()
            pf_status: str = str(entry.get("status") or "").strip().lower()
            if not pf_fingerprint or pf_status not in PRIOR_FINDING_STATUSES:
                log(
                    "findings.json: ignoring prior_findings entry with "
                    f"fingerprint={pf_fingerprint!r} status={pf_status!r}."
                )
                continue
            prior_updates[pf_fingerprint] = (
                pf_status,
                str(entry.get("note") or "")[:300],
            )
    elif raw_prior is not None:
        log("findings.json: `prior_findings` must be a list — ignored.")
    severities: list[str] = [f.severity for f in findings]
    complexity_level: str | None = parse_complexity_level(raw.get("complexity"))
    if raw.get("complexity") is not None and complexity_level is None:
        log(
            "WARNING: findings.json 'complexity' field was present but "
            f"not a recognised level ({list(PR_COMPLEXITY_LEVELS)}); "
            "ignoring."
        )
    return ReviewResult(
        prior_finding_updates=prior_updates,
        summary=summary,
        findings=findings,
        overall_severity=overall_severity(severities),
        complexity=complexity_level,
    )


def write_findings_prompt_directive(
    review_instructions: str,
    findings_path: Path,
    *,
    require_complexity: bool = False,
    prior_findings_expected: bool = False,
    max_inline_comments: int = 0,
) -> str:
    """Append the "write your findings to this file" directive to the
    review instructions handed to an agent-runner CLI.

    `max_inline_comments` (v2.2.0+): the effective inline cap for this
    round, stated to the agent so it prioritises instead of being truncated
    after the fact (0 = not stated).

    Standardised so every CLI provider emits the same schema — the receiving
    parser (`parse_findings_file`) is a single implementation shared across
    all providers.
    """
    # The example must stay valid JSON (no `//` comments): whether the
    # field is optional is stated by ``complexity_rule`` right below it.
    complexity_schema: str = ',\n  "complexity": "low | medium | high"\n'
    complexity_rule: str = (
        "\n- `complexity` is **required** for this run. Assess the PR's "
        "overall review difficulty based on cognitive load, files touched, "
        "cross-cutting concerns, security surface, and test-coverage "
        "delta — NOT line count. Use `low` for self-contained changes "
        "(docs, typos, isolated helpers), `medium` for one subsystem, "
        "`high` for multiple subsystems, security-adjacent code, or "
        "novel abstractions."
        if require_complexity
        else (
            "\n- `complexity` is optional PR-level metadata (`low`, `medium`, "
            "or `high`). Include it when you assess overall review difficulty."
        )
    )
    return (
        review_instructions
        + "\n\n---\n\n"
        + "## Output contract (MANDATORY)\n\n"
        + "Before ending your turn, write your review to the file:\n\n"
        + f"    {findings_path}\n\n"
        + "as JSON matching this schema (the outer fence is four backticks so "
        + "the three-backtick suggestion example inside stays part of it):\n\n"
        + "````json\n"
        + "{\n"
        + '  "summary": "markdown body of the overall review",\n'
        + '  "findings": [\n'
        + "    {\n"
        + '      "path": "repo-relative file path (must appear in the PR diff)",\n'
        + '      "line": 123,\n'
        + '      "body": "markdown body of this inline comment; a short fix goes in a suggestion block, escaped for JSON: \\n\\n```suggestion\\nfixed line\\n```",\n'
        + '      "severity": "critical | warning | info",\n'
        + '      "start_line": 121,\n'
        + '      "side": "RIGHT"\n'
        + "    }\n"
        + "  ]"
        + complexity_schema
        + (
            ',\n  "prior_findings": [\n'
            '    {"fingerprint": "<from the prior-findings table>", '
            '"status": "resolved | open | regressed", "note": "one line of evidence"}\n'
            "  ]\n"
            if prior_findings_expected
            else ""
        )
        + "}\n"
        + "````\n\n"
        + "Rules:\n"
        + "- `path` and `line` MUST reference a line that appears in the PR "
        + "diff. Off-diff lines are rejected by GitHub with HTTP 422 and lose "
        + "the whole review.\n"
        + "- `severity` MUST be exactly one of `critical`, `warning`, `info` "
        + "(lowercase). Choose honestly — it drives the strictness gate.\n"
        + "- `start_line` and `side` are optional. `side` defaults to `RIGHT` "
        + "(new code); use `LEFT` for removed code.\n"
        + "- Empty `findings` is valid — it means "
        + '"no issues found; just the summary".\n'
        + (
            f"- At most {max_inline_comments} findings are posted inline this "
            "round: list the most severe first; anything beyond the cap is "
            "kept for later rounds, not posted.\n"
            if max_inline_comments > 0
            else ""
        )
        + "- Only write the file once, at the end. Do NOT stream partials.\n"
        + "- Never modify any file other than the findings file (the review "
        + "instructions above carry the triage and verification budget).\n"
        + "- The file MUST parse with Python `json.load()`. Do not hand-write "
        + "JSON when the content contains Markdown, quotes, or code blocks; "
        + "use a JSON serializer so strings are escaped correctly."
        + complexity_rule
        + (
            "\n- `prior_findings` is **required** for this run: one entry per "
            "row of the `Your prior findings still open` table, with the "
            "fingerprint copied verbatim. Do not re-post an `open` prior "
            "finding inside `findings`."
            if prior_findings_expected
            else ""
        )
    )


def pr_context_is_incremental(ctx: "PRContext") -> bool:
    """True when the run is an incremental follow-up review."""
    pre: Any = getattr(ctx, "incremental", None)
    return bool(
        pre is not None and pre.mode == IAR_MODE_INCREMENTAL and pre.delta is not None
    )


def render_inline_finding_marker(fingerprint: str | None, severity: str) -> str:
    """The hidden per-comment marker: `<!-- ai-pr-reviewer-finding: fp=… sev=… -->`."""
    if not fingerprint:
        return ""
    return (
        f"\n\n{INLINE_FINDING_MARKER_PREFIX} fp={fingerprint} "
        f"sev={severity}{INLINE_FINDING_MARKER_CLOSE}"
    )


def parse_inline_finding_marker(body: str) -> tuple[str, str] | None:
    """Extract `(fingerprint, severity)` from an inline comment body, or
    None when the comment predates the marker."""
    if not body or INLINE_FINDING_MARKER_PREFIX not in body:
        return None
    match = re.search(
        re.escape(INLINE_FINDING_MARKER_PREFIX)
        + r"\s*fp=([0-9a-f]{8,64})\s+sev=([a-z]+)\s*-->",
        body,
    )
    if not match:
        return None
    severity: str = match.group(2)
    if severity not in ALLOWED_SEVERITIES:
        severity = SEVERITY_INFO
    return match.group(1), severity


def findings_to_gh_inline_comments(
    findings: list[Finding],
) -> list[dict[str, Any]]:
    """Convert a `list[Finding]` into the GitHub Reviews API inline shape.

    Kept separate from `state_to_review_result` so agent-runner providers
    (which produce `Finding`s directly from `.aiprr/findings.json`) can reuse
    the same encoder without round-tripping through `ReviewState`.
    """
    out: list[dict[str, Any]] = []
    for f in findings:
        comment: dict[str, Any] = {
            "path": f.path,
            "body": (
                f.body + render_inline_finding_marker(f.fingerprint, f.severity)
                if f.fingerprint
                else f.body
            ),
            "line": f.line,
            "side": f.side or "RIGHT",
        }
        if f.start_line is not None:
            comment["start_line"] = f.start_line
            comment["start_side"] = f.side or "RIGHT"
        out.append(comment)
    return out


def compose_system_prompt(base: str, extension: str) -> str:
    """Compose the effective system prompt from a base + optional extension.

    - `extension` empty → returns `base` unchanged.
    - `extension` non-empty → returns `base.rstrip() + "\\n\\n---\\n\\n" +
      extension.lstrip()`. The `---` separator gives the model an
      unambiguous boundary between the base prompt and the consumer's
      overrides so overrides can safely contradict the base.
    """
    if not extension:
        return base
    return base.rstrip() + "\n\n---\n\n" + extension.lstrip()


def evaluate_strictness(
    severity: str, strictness: str
) -> tuple[bool, str]:
    """Decide whether the configured strictness blocks the check.

    Returns `(blocked, reason)`. `reason` is a short human-readable string
    that goes into both the workflow log and the tracking comment.
    """
    if strictness not in VALID_STRICTNESS:
        # Defensive fallback — invalid input becomes lenient so a typo can
        # never fail the check unexpectedly.
        return False, f"unknown strictness {strictness!r} → treated as lenient"
    if strictness == STRICTNESS_LENIENT:
        return False, "lenient — never blocks"
    rank: int = SEVERITY_RANK.get(severity, 0)
    if strictness == STRICTNESS_BLOCK_CRITICAL:
        if rank >= SEVERITY_RANK[SEVERITY_CRITICAL]:
            return True, "found `critical` severity — block-on-critical fired"
        return False, f"highest severity `{severity}` ≤ critical threshold"
    if strictness == STRICTNESS_BLOCK_WARNING:
        if rank >= SEVERITY_RANK[SEVERITY_WARNING]:
            return True, (
                f"found `{severity}` severity — block-on-warning fired"
            )
        return False, f"highest severity `{severity}` ≤ warning threshold"
    if strictness == STRICTNESS_BLOCK_ANY:
        # Zero-tolerance: blocks on any finding, including `info`. The gate
        # fires whenever a comment was posted (i.e. severity is not `none`).
        if severity != SEVERITY_NONE:
            return True, (
                f"found `{severity}` severity — block-on-any fired"
            )
        return False, "no findings — block-on-any passes"
    return False, "unhandled strictness branch"


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------


def drive_review(
    *,
    provider: Provider,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    state: ReviewState,
    max_turns: int,
) -> None:
    """Drive the agentic tool-use loop until submit_review or end_turn.

    Mutates `messages` and `state` in place; raises if the API or a tool
    call surfaces an uncaught exception.
    """
    for turn in range(1, max_turns + 1):
        log(f"Turn {turn}/{max_turns} — calling provider")
        resp: dict[str, Any] = provider.complete(
            system_prompt=system_prompt, messages=messages, tools=tools
        )
        stop_reason: str = resp.get("stop_reason", "")
        content_blocks: list[dict[str, Any]] = resp.get("content", [])
        turn_usage: UsageTelemetry | None = normalise_usage(resp.get("usage"))
        if turn_usage is not None:
            state.usage.add(turn_usage)

        # Append assistant turn verbatim — the API requires us to echo back
        # the same content blocks (including tool_use ids) on the next call.
        messages.append({"role": "assistant", "content": content_blocks})

        tool_uses: list[dict[str, Any]] = [
            b for b in content_blocks if b.get("type") == "tool_use"
        ]
        if not tool_uses:
            log(f"Stop reason: {stop_reason} (no tool calls — ending)")
            break

        tool_results: list[dict[str, Any]] = []
        for use in tool_uses:
            tool_name: str = use.get("name", "")
            tool_args: dict[str, Any] = use.get("input", {})
            log(
                f"  → {tool_name}("
                f"{json.dumps(redact_for_log(tool_args))[:MAX_TOOL_LOG_PREVIEW_CHARS]})"
            )
            result_text: str = execute_tool(tool_name, tool_args, state)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": use.get("id"),
                    "content": result_text,
                }
            )

        # Prune BEFORE appending the new tool_results so the just-arrived
        # turn-pair is never at risk of being dropped on the boundary, AND
        # always drop in pairs of 2 (assistant + tool_results) so we don't
        # leave an orphan tool_result whose `tool_use_id` no longer has a
        # matching `tool_use` block in any preceding message — which the
        # Anthropic API rejects with `messages.X.content.Y: unexpected
        # tool_use_id found in tool_result blocks`.
        pair_target: int = 2 * MAX_CONVERSATION_TURNS_RETAINED
        while len(messages) > 1 + pair_target:
            del messages[1:3]
            log("Pruned 1 turn-pair (2 messages) to bound token usage")

        messages.append({"role": "user", "content": tool_results})

        if state.final_summary is not None:
            log("submit_review captured — terminating loop")
            break
    else:
        log(f"Reached MAX_TURNS={max_turns} without an explicit submit_review")


# ---------------------------------------------------------------------------
# Tracking comment
# ---------------------------------------------------------------------------


def _tracking_marker_header(provider: str) -> str:
    """First line(s) of every tracking comment: the review marker, plus the
    per-provider marker when `provider` is set (enables provider-scoped
    `collapse-previous`)."""
    if provider:
        return f"{REVIEW_MARKER}\n{provider_marker(provider)}"
    return REVIEW_MARKER


def render_tracking_body_working(
    head_sha: str, *, collapse_previous: bool, provider: str = ""
) -> str:
    """The initial 'Working…' tracking-comment body."""
    collapsed_note: str = (
        " Previous reviews on this PR have been collapsed as outdated."
        if collapse_previous
        else ""
    )
    return (
        f"{_tracking_marker_header(provider)}\n"
        f"### AI review for `{head_sha[:7]}` — _Working…_\n\n"
        f"Full SHA: `{head_sha}`\n\n"
        f"Reviewing the latest pushed changes.{collapsed_note}"
    )


def render_tracking_body_done(
    *,
    head_sha: str,
    review_url: str,
    inline_attached: int,
    inline_dropped: int,
    severity: str,
    blocked: bool,
    block_reason: str,
    provider: str = "",
    usage_line: str = "",
) -> str:
    """The terminal 'done' tracking-comment body. `usage_line` (v2.1.0+) is
    the pre-formatted `**Usage:** …` line from `format_usage_line`."""
    status_emoji: str = "✅" if not blocked else "🚫"
    block_line: str = (
        f"\n\n**Strictness gate:** 🚫 {block_reason}"
        if blocked
        else f"\n\n**Strictness gate:** ✅ {block_reason}"
    )
    inline_line: str
    if inline_dropped:
        inline_line = (
            f"_{inline_attached} inline comment(s) attached; "
            f"{inline_dropped} dropped — GitHub rejected them with HTTP 422 "
            "(line outside the diff). See the workflow logs for the original "
            "payload._"
        )
    else:
        inline_line = f"_{inline_attached} inline comment(s) attached._"
    return (
        f"{_tracking_marker_header(provider)}\n"
        f"### AI review for `{head_sha[:7]}` — {status_emoji} done\n\n"
        f"[View review →]({review_url})\n\n"
        f"**Highest severity:** `{severity}`{block_line}\n\n"
        f"{inline_line}"
        + (f"\n\n{usage_line}" if usage_line else "")
    )


def render_tracking_body_failed(
    *, head_sha: str, error: str, provider: str = ""
) -> str:
    """The terminal 'failed' tracking-comment body.

    The error text can carry CLI stderr/stdout tails (see `_invoke_cli_agent`),
    so it is passed through `scrub_secrets` before being embedded in this
    public comment.
    """
    safe_error: str = scrub_secrets(error)[:MAX_TRACKING_ERROR_CHARS]
    return (
        f"{_tracking_marker_header(provider)}\n"
        f"### AI review for `{head_sha[:7]}` — ❌ failed\n\n"
        f"```\n{safe_error}\n```\n\n"
        "_See the workflow logs for the full traceback._"
    )


def render_tracking_body_skipped_by_label(
    *, head_sha: str, skip_label: str, provider: str = ""
) -> str:
    """The terminal 'skipped by label' tracking-comment body.

    Posted when the developer applied the `skip-review-label` alongside the
    normal trigger — the reviewer short-circuits before the LLM call so the
    merge can proceed without burning tokens. The comment carries the same
    `<!-- ai-pr-reviewer-marker -->` header as any other terminal comment so
    downstream tooling (dashboards, `collapse-previous` on the next run,
    audit scripts) treats it uniformly.

    The GitHub check reports `success` (exit 0). No IAR state is written; the
    next real review starts from wherever the pipeline left off before this
    skip. The `applied-label` is NOT stamped on skip runs — applying it would
    misrepresent an unreviewed PR as reviewed.
    """
    return (
        f"{_tracking_marker_header(provider)}\n"
        f"### AI review for `{head_sha[:7]}` — ⏭️ skipped\n\n"
        f"Full SHA: `{head_sha}`\n\n"
        f"The `{skip_label}` label was applied to this PR, so the AI "
        "reviewer short-circuited: **no LLM call, no findings, no state "
        "mutation.** The GitHub check reports success so the merge can "
        "proceed.\n\n"
        f"_This is the intended behaviour when `skip-review-label` is "
        "configured — use it deliberately for hotfixes, rollbacks, or "
        "changes where an LLM review would burn tokens for no "
        "incremental value. Remove the label + push a new commit to "
        "get a real review on the follow-up work._"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    # ------------------------------------------------------------------
    # Load + validate environment
    # ------------------------------------------------------------------
    provider_id: str = os.environ.get("AIPRR_PROVIDER", "anthropic").strip()
    api_key: str = os.environ.get("AIPRR_API_KEY", "").strip()
    gh_token: str = os.environ.get("AIPRR_GH_TOKEN", "").strip()
    repo: str = os.environ.get("AIPRR_REPO", "").strip()
    pr_number_raw: str = os.environ.get("AIPRR_PR_NUMBER", "").strip()
    head_sha: str = os.environ.get("AIPRR_HEAD_SHA", "").strip()
    base_ref: str = (
        os.environ.get("AIPRR_BASE_REF", "").strip() or DEFAULT_BASE_REF
    )
    action_path: str = os.environ.get("AIPRR_ACTION_PATH", "").strip()

    if not (api_key and gh_token and repo and pr_number_raw and head_sha):
        log(
            "Missing required env (AIPRR_API_KEY, AIPRR_GH_TOKEN, AIPRR_REPO, "
            "AIPRR_PR_NUMBER, AIPRR_HEAD_SHA). Aborting."
        )
        write_all_outputs(skipped=False)
        return 1
    # Register the two secrets so their literal values are scrubbed from any
    # text that reaches a public PR comment / review body (see scrub_secrets).
    register_secret(api_key)
    register_secret(gh_token)
    pr_number: int = int(pr_number_raw)

    # Backend selection (v2.1.0+). Validate before anything outward-facing
    # happens: the credential in `api-key` will be sent to this host.
    try:
        api_base: str = validate_api_base(os.environ.get(API_BASE_ENV, ""))
    except ValueError as e:
        log(f"CONFIGURATION ERROR: {e} Aborting.")
        write_all_outputs(skipped=False)
        return 1
    backend_profile: EndpointProfile = resolve_endpoint_profile(
        api_base, provider_id
    )
    review_scope: str = review_scope_id(provider_id, api_base)
    log_backend_selection(backend_profile)

    # Model: empty → provider default; tier word → cost-controls table;
    # anything else → explicit id (v2.1.0+ tier aliases).
    try:
        model: str = resolve_model(
            provider_id, backend_profile, os.environ.get("AIPRR_MODEL", "")
        )
    except ValueError as e:
        log(f"CONFIGURATION ERROR: {e} Aborting.")
        write_all_outputs(skipped=False)
        return 1
    try:
        resolution_policy: str = parse_resolution_policy(
            os.environ.get(PRIOR_FINDINGS_RESOLUTION_ENV, "")
        )
    except ValueError as e:
        log(f"CONFIGURATION ERROR: {e} Aborting.")
        write_all_outputs(skipped=False)
        return 1
    if resolution_policy != RESOLUTION_POLICY_ADVISORY:
        log(f"Prior-finding resolution policy: {resolution_policy}")
    if not model:
        log(f"No default model for provider {provider_id!r} — aborting.")
        write_all_outputs(skipped=False)
        return 1

    prompt_file: str = os.environ.get("AIPRR_PROMPT_FILE", "").strip()
    prompt_extension_file: str = os.environ.get(
        "AIPRR_PROMPT_EXTENSION_FILE", ""
    ).strip()
    label_gate: str = os.environ.get("AIPRR_LABEL_GATE", "").strip()
    applied_label: str = os.environ.get("AIPRR_APPLIED_LABEL", "").strip()
    skip_review_label: str = os.environ.get(
        "AIPRR_SKIP_REVIEW_LABEL", ""
    ).strip()

    # Load-bearing misconfiguration guard: `skip-review-label` is an
    # emergency-bypass hatch (silently skipping the review). If it
    # collides with any of the runtime's other semantic labels,
    # every normal trigger silently becomes a skip. Abort loudly.
    # See detect_skip_label_collisions() for the collision matrix.
    if skip_review_label:
        _iar_escape_label_default: str = (
            os.environ.get("AIPRR_ITERATION_ESCAPE_LABEL", "").strip()
            or IAR_DEFAULT_ESCAPE_LABEL
        )
        _collisions: list[str] = detect_skip_label_collisions(
            skip_review_label=skip_review_label,
            label_gate=label_gate,
            applied_label=applied_label,
            iteration_escape_label=_iar_escape_label_default,
        )
        if _collisions:
            log(
                f"CONFIGURATION ERROR: skip-review-label "
                f"{skip_review_label!r} collides with: "
                f"{', '.join(_collisions)}. "
                "This would cause every normal review trigger to be "
                "silently skipped. Rename skip-review-label to a "
                "distinct value (recommended: 'skip-ai-review', "
                "'hotfix-no-review', or similar). Aborting."
            )
            write_all_outputs(skipped=False)
            return 1

    collapse_previous: bool = parse_bool(
        os.environ.get("AIPRR_COLLAPSE_PREVIOUS", "true"), default=True
    )
    tracking_comment_enabled: bool = parse_bool(
        os.environ.get("AIPRR_TRACKING_COMMENT", "true"), default=True
    )
    strictness: str = (
        os.environ.get("AIPRR_STRICTNESS", STRICTNESS_LENIENT).strip()
        or STRICTNESS_LENIENT
    )
    max_inline_comments: int = int(
        os.environ.get("AIPRR_MAX_INLINE_COMMENTS", DEFAULT_MAX_INLINE_COMMENTS)
        or DEFAULT_MAX_INLINE_COMMENTS
    )
    max_turns: int = int(
        os.environ.get("AIPRR_MAX_TURNS", DEFAULT_MAX_TURNS)
        or DEFAULT_MAX_TURNS
    )

    # Iteration-Aware Review (IAR). Every review runs the IAR pipeline;
    # the four tunable inputs (convergence-policy, max-review-rounds,
    # exhaustive-first-pass-cap-multiplier, iteration-escape-label)
    # shape it. Pre-LLM / post-LLM helpers are wrapped in try/except at
    # their call sites — an IAR failure degrades to the baseline review
    # path (5 IAR outputs stay empty via write_iar_outputs_empty(),
    # tracking marker skips the annotation). See docs/ITERATION_AWARENESS.md.
    iar_config: IARConfig = build_iar_config(dict(os.environ))
    iar_telemetry: RunTelemetry = RunTelemetry(
        start_time_monotonic=time.monotonic()
    )
    iar_pre_context: IARPreLLMContext | None = None
    iar_state_final: IterationState | None = None
    iar_policy_final: PolicyResult | None = None
    iar_effective_cap: int = 0  # populated pre-LLM; used by cost estimate
    # Detect silently-fallback-corrected policy inputs so miswiring is
    # visible in the workflow log rather than swallowed. The build_iar_config
    # helper rewrote the value; we compare raw vs effective.
    raw_policy: str = (
        os.environ.get("AIPRR_CONVERGENCE_POLICY", "").strip()
        or IAR_POLICY_FIRST_PASS_EXHAUSTIVE
    )
    if raw_policy not in IAR_VALID_POLICIES:
        log(
            f"IAR: unknown convergence-policy {raw_policy!r}; falling back "
            f"to {IAR_POLICY_FIRST_PASS_EXHAUSTIVE!r}. "
            f"Valid values: {list(IAR_VALID_POLICIES)}."
        )
    log(
        f"IAR: policy={iar_config.policy}, "
        f"max-rounds={iar_config.max_review_rounds}, "
        f"cap-multiplier={iar_config.cap_multiplier}, "
        f"escape-label={iar_config.escape_label!r}."
    )

    # PR description review (v1.2.0+)
    pr_desc_mode: str = (
        os.environ.get(
            "AIPRR_PR_DESCRIPTION_MODE", PR_DESC_MODE_OFF
        ).strip()
        or PR_DESC_MODE_OFF
    )
    if pr_desc_mode not in PR_DESC_MODES:
        log(
            f"Unknown pr-description-mode {pr_desc_mode!r} — falling back "
            f"to {PR_DESC_MODE_OFF}"
        )
        pr_desc_mode = PR_DESC_MODE_OFF
    pr_desc_min_length: int = int(
        os.environ.get(
            "AIPRR_PR_DESCRIPTION_MIN_LENGTH",
            str(PR_DESC_MIN_LENGTH_DEFAULT),
        )
        or PR_DESC_MIN_LENGTH_DEFAULT
    )

    # PR complexity labeling (v1.2.0+)
    complexity_labels_enabled: bool = parse_bool(
        os.environ.get("AIPRR_COMPLEXITY_LABELS_ENABLED", "false"),
        default=False,
    )
    complexity_label_prefix: str = (
        os.environ.get(
            "AIPRR_COMPLEXITY_LABEL_PREFIX",
            PR_COMPLEXITY_LABEL_PREFIX_DEFAULT,
        ).strip()
        or PR_COMPLEXITY_LABEL_PREFIX_DEFAULT
    )

    # Trigger-mode resolution (v1.2.0+). Empty default falls back to
    # `always` (or `label-required` when `label-gate` is set) for full
    # back-compat with v1.1 workflows.
    trigger_mode_raw: str = (
        os.environ.get("AIPRR_TRIGGER_MODE", "").strip()
    )
    if trigger_mode_raw:
        trigger_mode: str = trigger_mode_raw
        if trigger_mode not in TRIGGER_MODES:
            log(
                f"Unknown trigger-mode {trigger_mode!r} — falling back to "
                f"{TRIGGER_ALWAYS}"
            )
            trigger_mode = TRIGGER_ALWAYS
    else:
        trigger_mode = (
            TRIGGER_LABEL_REQUIRED if label_gate else TRIGGER_ALWAYS
        )

    event_action: str = _read_github_event_action()
    event_label: str = _read_github_event_label()

    log(
        f"Reviewing {repo}#{pr_number} @ {head_sha[:7]} with "
        f"{provider_id}/{model} (strictness={strictness}, "
        f"trigger-mode={trigger_mode})"
    )

    # ------------------------------------------------------------------
    # Author-association gate (v1.3.0+) — cheapest gate, runs first so a
    # denied PR never consumes an LLM API call. Defaults to write-tier
    # only, which is the safe baseline for public open-source repos.
    # ------------------------------------------------------------------
    author_gate_raw: str = os.environ.get(
        "AIPRR_AUTHOR_ASSOCIATION",
        ",".join(AUTHOR_ASSOCIATION_WRITE_TIER),
    )
    pr_author_association: str = _read_github_event_pr_author_association()
    repo_visibility: str = _read_github_event_repo_visibility()
    collaborator_permission: str | None = None
    permission_lookup_failed: bool = False
    if _author_gate_needs_permission_lookup(
        gate=author_gate_raw,
        webhook_association=pr_author_association,
    ):
        author_login: str = _read_github_event_pr_author_login()
        if author_login and "/" in repo:
            owner, name = repo.split("/", 1)
            collaborator_permission, permission_lookup_failed = (
                gh_get_collaborator_permission(
                    token=gh_token,
                    owner=owner,
                    repo=name,
                    username=author_login,
                )
            )
        else:
            permission_lookup_failed = True
    author_decision: AuthorAssociationDecision = (
        resolve_author_association_gate_enhanced(
            gate=author_gate_raw,
            webhook_association=pr_author_association,
            collaborator_permission=collaborator_permission,
            permission_lookup_failed=permission_lookup_failed,
            repo_visibility=repo_visibility,
        )
    )
    log(
        format_author_gate_log_line(
            author_decision, gate_raw=author_gate_raw
        )
    )
    if not author_decision.should_run:
        log(
            "Skipping review — author not allowed by the association gate. "
            "On public repos this is the abuse-prevention default. To allow "
            "this author, add their association to `author-association` "
            "(see docs/SECURITY.md § 'Author-association gate'), widen the "
            "allow-list, or set the input to an empty string to disable the "
            "gate entirely."
        )
        write_all_outputs(skipped=True)
        return 0

    # ------------------------------------------------------------------
    # Trigger evaluation (v1.2.0+) — subsumes the v1.x `label-gate` block
    # ------------------------------------------------------------------
    label_toggle_generation: int = 0
    last_reviewed_generation: int = 0
    if trigger_mode in (TRIGGER_LABEL_REQUIRED, TRIGGER_LABEL_ONCE):
        try:
            label_toggle_generation = count_label_events(
                token=gh_token,
                repo=repo,
                pr_number=pr_number,
                label=label_gate,
            )
        except Exception as e:  # noqa: BLE001 — best-effort GH API call
            log(f"Could not count label events (assuming 0): {e}")

    if trigger_mode == TRIGGER_LABEL_ONCE:
        prior_state: dict[str, Any] = _read_existing_tracking_state(
            token=gh_token, repo=repo, pr_number=pr_number, provider_id=review_scope
        )
        try:
            last_reviewed_generation = int(
                prior_state.get("label_toggle_generation", 0) or 0
            )
        except (TypeError, ValueError):
            last_reviewed_generation = 0

    try:
        current_labels_raw: list[dict[str, Any]] = (
            gh_request(
                "GET",
                f"/repos/{repo.split('/')[0]}/{repo.split('/')[1]}"
                f"/pulls/{pr_number}",
                token=gh_token,
            )
            .get("labels", [])
            or []
        )
        current_labels: list[str] = [
            (lbl.get("name") or "") for lbl in current_labels_raw
        ]
    except Exception as e:  # noqa: BLE001 — best-effort GH API call
        log(f"Could not read PR labels for trigger check: {e}")
        current_labels = []

    trigger_decision: TriggerDecision = resolve_trigger_action(
        trigger_mode=trigger_mode,
        event_action=event_action,
        event_label=event_label,
        label_gate=label_gate,
        current_labels=current_labels,
        label_toggle_generation=label_toggle_generation,
        last_reviewed_generation=last_reviewed_generation,
    )
    log(
        f"Trigger decision: should_run={trigger_decision.should_run} "
        f"({trigger_decision.reason})"
    )
    if not trigger_decision.should_run:
        write_all_outputs(skipped=True)
        return 0

    # ------------------------------------------------------------------
    # Skip-review-label short-circuit (emergency-bypass hatch)
    # ------------------------------------------------------------------
    # Opt-in escape: when `skip-review-label` is configured AND that label
    # is present on the PR at trigger time, the reviewer short-circuits to
    # success without touching the LLM, IAR state, or any other side
    # effects. Intended for hotfixes / rollbacks / trivially safe changes
    # where an LLM review would burn tokens for no incremental value.
    #
    # Contract (mirrors action.yml + docs/TRIGGER_MODES.md § "Emergency-bypass label"):
    #   1. No LLM call — the reviewer never enters `run_agentic_loop`.
    #   2. No IAR state mutation — persisted state is left exactly as it
    #      was; the next non-skip run resumes from where the pipeline
    #      last left off.
    #   3. No `applied-label` stamp — applying it would misrepresent an
    #      unreviewed PR as reviewed. Anyone dashboarding on that label
    #      keeps seeing the truth.
    #   4. No collapse-previous — the skip is meant to be minimal; prior
    #      reviews (if any) stay visible so the human reviewer still
    #      has context before merging. The next real review will
    #      collapse them as usual.
    #   5. A tracking comment IS posted (subject to `tracking-comment`)
    #      so the audit trail records WHY the review was skipped, and
    #      so `collapse-previous` on the next real run treats this like
    #      any other terminal comment.
    #   6. Outputs: `skipped=true`, `severity=none`, `blocked=false`.
    #      The GitHub check reports success (exit 0) so the merge
    #      proceeds.
    #
    # SECURITY NOTE: anyone who can label a PR can bypass code review via
    # this gesture. Consumers who care must combine this input with a
    # ruleset / CODEOWNERS rule restricting who can apply the label.
    #
    # The `if skip_review_label:` guard is defensive: `_labels_contain_ci`
    # already returns False for an empty needle (documented contract),
    # so the behaviour is correct without it — but the explicit guard
    # makes the "feature disabled when input is empty" contract visible
    # at the call site rather than relying on knowledge of the helper's
    # semantics one level down. If the helper's contract ever changes
    # (e.g. someone adds an `if not needle: return True` optimization
    # for a legitimate but unrelated reason), this guard prevents the
    # skip-review short-circuit from silently activating on every
    # trigger for consumers who don't use the feature.
    if skip_review_label and _labels_contain_ci(
        needle=skip_review_label, haystack=current_labels
    ):
        log(
            f"Skip-review-label {skip_review_label!r} present on PR — "
            "short-circuiting to success without invoking the LLM. No "
            "findings, no IAR state mutation, no reviewed-label stamp."
        )
        if tracking_comment_enabled:
            try:
                gh_post_issue_comment(
                    token=gh_token,
                    repo=repo,
                    pr_number=pr_number,
                    body=render_tracking_body_skipped_by_label(
                        head_sha=head_sha,
                        skip_label=skip_review_label,
                        provider=review_scope,
                    ),
                )
            except Exception as e:  # noqa: BLE001 — audit trail is
                # best-effort; the skip must still succeed even if the
                # tracking comment fails to post (network hiccup,
                # permissions revoked mid-run, etc.).
                log(
                    f"Could not post skip-review tracking comment "
                    f"(non-fatal): {e}"
                )
        write_all_outputs(skipped=True)
        return 0

    # ------------------------------------------------------------------
    # Resolve the reviewer's own bot identity — always, regardless of
    # `collapse-previous`. Two independent downstream consumers need it:
    # (1) `gh_collapse_previous_reviews` (below, guarded by
    # `collapse_previous`) filters comments to authors matching this
    # login; (2) the IAR marker-author filter in
    # `_fetch_latest_marker_body` (via `run_iar_pre_llm` below) uses
    # it to reject forged state markers from non-bot commenters
    # (round-10 F1 security fix). Prior to round-11 this was scoped
    # inside `if collapse_previous:` so consumers with
    # `collapse-previous: false` had IAR's author filter permanently
    # disabled. Failure mode is safe on both sides — `""` disables
    # the collapse loop's filter (already documented) and disables
    # the IAR author filter (falls back to pre-round-10 behaviour —
    # over-review, never under-surface).
    bot_login: str = ""
    try:
        # v1.2.0+: pass repo + pr_number so the fallback chain in
        # `gh_get_authenticated_login` can marker-scan for the prior
        # bot's login when the built-in `GITHUB_TOKEN` refuses
        # `/user` (the fix for the silent 403 that broke this
        # feature for every workflow-token consumer).
        bot_login = gh_get_authenticated_login(
            gh_token, repo=repo, pr_number=pr_number
        )
        log(f"Authenticated as: {bot_login}")
    except Exception as e:  # noqa: BLE001 — best-effort GH API call:
        # bot identity resolution is optional context (used by the
        # collapse-previous loop's author filter AND the round-10 IAR
        # marker author filter). If it fails (permission problem, API
        # outage, marker-scan fallback exhausted, etc.), both consumers
        # degrade to their pre-filter behaviour (over-review, never
        # under-surface) rather than crashing the whole review — the
        # documented safe fallback for the whole reviewer's identity path.
        log(f"bot-login lookup failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # Collapse previous bot reviews/comments as outdated
    # ------------------------------------------------------------------
    if collapse_previous:
        try:
            gh_collapse_previous_reviews(
                token=gh_token,
                repo=repo,
                pr_number=pr_number,
                bot_login=bot_login,
                # Scope collapsing to THIS provider's prior artefacts so
                # concurrent multi-provider reviews don't collapse each other.
                provider_marker_text=provider_marker(review_scope),
            )
        except Exception as e:  # noqa: BLE001
            log(f"Collapse-previous step failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # Tracking spinner comment
    # ------------------------------------------------------------------
    tracking_id: int = 0
    if tracking_comment_enabled:
        try:
            tracking_id = gh_post_issue_comment(
                token=gh_token,
                repo=repo,
                pr_number=pr_number,
                body=render_tracking_body_working(
                    head_sha,
                    collapse_previous=collapse_previous,
                    provider=review_scope,
                ),
            )
            log(f"Tracking comment id: {tracking_id}")
        except Exception as e:  # noqa: BLE001
            log(f"Could not post tracking comment (non-fatal): {e}")
            tracking_id = 0

    # ------------------------------------------------------------------
    # Resolve and read system prompt
    # ------------------------------------------------------------------
    # Composition matrix:
    #   1) neither set              → bundled default
    #   2) prompt_file only         → prompt_file replaces default
    #   3) prompt_extension only    → default + "\n\n---\n\n" + extension
    #   4) both set                 → prompt_file + "\n\n---\n\n" + extension
    # The `---` separator gives the model an unambiguous boundary between
    # the base prompt and the consumer's overrides.
    resolved_prompt_path: Path
    if prompt_file:
        resolved_prompt_path = Path(prompt_file)
    else:
        resolved_prompt_path = Path(action_path) / "prompts" / "default.md"
    try:
        base_prompt: str = resolved_prompt_path.read_text(encoding="utf-8")
        log(f"Base prompt loaded from {resolved_prompt_path}")
    except OSError as e:
        log(f"Failed to read prompt file {resolved_prompt_path!r}: {e}")
        gh_update_issue_comment(
            token=gh_token,
            repo=repo,
            comment_id=tracking_id,
            body=render_tracking_body_failed(
                head_sha=head_sha,
                error=f"Could not read prompt file: {e}",
                provider=review_scope,
            ),
        )
        write_all_outputs(skipped=False)
        return 1

    extension_text: str = ""
    if prompt_extension_file:
        extension_path: Path = Path(prompt_extension_file)
        try:
            extension_text = extension_path.read_text(encoding="utf-8")
            log(f"Prompt extension appended from {extension_path}")
        except OSError as e:
            log(
                f"Failed to read prompt extension file "
                f"{extension_path!r}: {e}"
            )
            gh_update_issue_comment(
                token=gh_token,
                repo=repo,
                comment_id=tracking_id,
                body=render_tracking_body_failed(
                    head_sha=head_sha,
                    error=f"Could not read prompt extension file: {e}",
                    provider=review_scope,
                ),
            )
            write_all_outputs(skipped=False)
            return 1
    system_prompt: str = compose_system_prompt(base_prompt, extension_text)

    # ------------------------------------------------------------------
    # IAR pre-LLM: shape the LLM call.
    #
    # Reads the prior state, detects generation transition, and dispatches
    # to the configured policy with empty findings to extract the effective
    # cap + optional prompt addendum. Wrapped in try/except so any IAR
    # failure degrades to the baseline review path (effective cap =
    # base cap, system_prompt unchanged, outputs stay empty) — the safety
    # contract locked by tests/test_iar_failure_fallback.py.
    # ------------------------------------------------------------------
    effective_max_inline_comments: int = max_inline_comments
    try:
        iar_pre_context = run_iar_pre_llm(
            iar_config=iar_config,
            repo=repo,
            pr_number=pr_number,
            gh_token=gh_token,
            base_ref=base_ref,
            head_sha=head_sha,
            base_max_inline_comments=max_inline_comments,
            applied_label=applied_label,
            provider_id=review_scope,
            bot_login=bot_login,
            max_turns=max_turns,
        )
        effective_max_inline_comments = (
            iar_pre_context.pre_policy_result.effective_max_inline_comments
        )
        if iar_pre_context.effective_max_turns:
            max_turns = iar_pre_context.effective_max_turns
        iar_effective_cap = effective_max_inline_comments
        if iar_pre_context.pre_policy_result.prompt_addendum:
            system_prompt = compose_system_prompt(
                system_prompt,
                iar_pre_context.pre_policy_result.prompt_addendum,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort IAR wrap
        # IAR must never crash the reviewer. On any pre-LLM error we
        # log and continue with baseline behavior — the review still
        # runs (IAR simply won't populate outputs or state this run).
        log(
            f"IAR pre-LLM crashed: {type(exc).__name__}: {exc}. "
            "Continuing with baseline (non-IAR) review path."
        )
        iar_pre_context = None
        effective_max_inline_comments = max_inline_comments

    # ------------------------------------------------------------------
    # Fetch PR + run agentic loop, all wrapped so failures hit the spinner
    # ------------------------------------------------------------------
    state: ReviewState = ReviewState(
        max_inline_comments=effective_max_inline_comments
    )
    try:
        ignore_globs: tuple[str, ...] = DEFAULT_IGNORE_PATH_GLOBS + tuple(
            g
            for g in parse_ignore_paths(os.environ.get(IGNORE_PATHS_ENV, ""))
            if g not in DEFAULT_IGNORE_PATH_GLOBS
        )
        pr_ctx: PRContext = fetch_pr_context(
            repo=repo,
            pr_number=pr_number,
            base_ref=base_ref,
            token=gh_token,
            ignore_globs=ignore_globs,
        )
        log(
            f"PR loaded: +{pr_ctx.additions}/-{pr_ctx.deletions} across "
            f"{len(pr_ctx.changed_files)} files"
        )
        # Incremental follow-up (v2.1.0+): hand the pre-LLM context to the
        # prompt renderer (agent-runners render inside their providers).
        if iar_pre_context is not None and iar_pre_context.mode == IAR_MODE_INCREMENTAL:
            pr_ctx.incremental = iar_pre_context
            log(
                f"IAR: incremental mode — {iar_pre_context.mode_reason}; "
                f"cap={effective_max_inline_comments}, max_turns={max_turns}."
            )

        # PR description verdict (only computed when the mode is not `off`,
        # to keep the log clean when the feature is disabled).
        description_verdict: DescriptionVerdict = DescriptionVerdict(
            is_adequate=True, reason=""
        )
        if pr_desc_mode != PR_DESC_MODE_OFF:
            description_verdict = evaluate_pr_description(
                pr_ctx.body, min_length=pr_desc_min_length
            )
            log(
                f"PR description: mode={pr_desc_mode}, "
                f"adequate={description_verdict.is_adequate}"
            )

        provider: Provider | AgentRunnerProvider = build_provider(
            provider_id, api_key=api_key, model=model, api_base=api_base
        )

        # v1.2.0 dispatch caveat: `set_pr_description` autocomplete is
        # chat-completions-only (tool-use loop). Complexity labeling is
        # bridged on agent-runners via optional `complexity` in findings.json.
        agent_runner_warning: str = build_agent_runner_noop_warning(
            provider_id=provider_id,
            is_agent_runner=isinstance(provider, AgentRunnerProvider),
            pr_desc_mode=pr_desc_mode,
            complexity_labels_enabled=complexity_labels_enabled,
        )
        if agent_runner_warning:
            log(agent_runner_warning)

        if isinstance(provider, AgentRunnerProvider):
            # Agent-runner path: vendor CLI owns the tool-use loop. Verify the
            # CLI is on PATH (defensive — the composite step should have
            # installed it), then invoke and parse findings.json.
            provider.install()
            workspace: Path = Path.cwd()
            result: ReviewResult = provider.run_review(
                pr_context=pr_ctx,
                review_instructions=system_prompt,
                workspace=workspace,
                output_dir=workspace,
                require_complexity_in_findings=complexity_labels_enabled,
                max_inline_comments=effective_max_inline_comments,
            )
            # The inline cap for the agent-runner path is enforced in
            # `run_iar_post_llm` AFTER fingerprinting (single path; overflow
            # findings stay known to IAR — docs/ITERATION_AWARENESS.md
            # § 13.1). If the IAR pipeline is unavailable this run, the
            # fallback further down caps here instead.
        else:
            # Chat-completions path: this action owns the tool-use loop.
            messages: list[dict[str, Any]] = [
                {"role": "user", "content": render_user_prompt(pr_ctx)}
            ]
            # Expose set_pr_description only in autocomplete mode; expose
            # set_pr_complexity only when complexity labeling is enabled.
            # `effective_max_inline_comments` == `max_inline_comments` when
            # the IAR pre-LLM step didn't amplify the cap (i.e. NOT round 1
            # of a new generation under first-pass-exhaustive / safety net,
            # OR the pre-LLM step crashed and the try/except fell back to
            # the baseline cap — see docs/ITERATION_AWARENESS.md § 2).
            # On round 1 of a new generation the multiplier raises the cap
            # so the LLM can surface an exhaustive initial pass.
            tools: list[dict[str, Any]] = tools_schema(
                effective_max_inline_comments,
                allow_set_pr_description=(
                    pr_desc_mode == PR_DESC_MODE_AUTOCOMPLETE
                    and not description_verdict.is_adequate
                    and PR_DESC_AUTOCOMPLETE_MARKER not in (pr_ctx.body or "")
                ),
                allow_set_pr_complexity=complexity_labels_enabled,
                allow_update_prior_finding=pr_context_is_incremental(pr_ctx),
            )

            drive_review(
                provider=provider,
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                state=state,
                max_turns=max_turns,
            )
            result = state_to_review_result(state)
    except Exception as e:  # noqa: BLE001
        log(f"Agentic loop crashed: {type(e).__name__}: {e}")
        gh_update_issue_comment(
            token=gh_token,
            repo=repo,
            comment_id=tracking_id,
            body=render_tracking_body_failed(
                head_sha=head_sha,
                error=f"{type(e).__name__}: {e}",
                provider=review_scope,
            ),
        )
        write_all_outputs(skipped=False)
        return 1

    # ------------------------------------------------------------------
    # Usage telemetry (v2.1.0+): real numbers from the provider, indicative
    # cost when the vendor did not report one. Never fatal, never gated on.
    # ------------------------------------------------------------------
    run_usage: UsageTelemetry = result.usage or UsageTelemetry()
    if (
        run_usage.source != USAGE_SOURCE_UNAVAILABLE
        and run_usage.cost_usd is None
    ):
        estimated: float | None = estimate_cost_usd(model, run_usage)
        if estimated is not None:
            run_usage.cost_usd = estimated
            if run_usage.source == USAGE_SOURCE_API:
                run_usage.source = USAGE_SOURCE_ESTIMATED
    iar_telemetry.usage = run_usage
    iar_telemetry.tokens_used = run_usage.total_tokens
    log(
        f"Usage: source={run_usage.source} in={run_usage.input_tokens} "
        f"cache_read={run_usage.cache_read_tokens} "
        f"cache_write={run_usage.cache_write_tokens} "
        f"out={run_usage.output_tokens} turns={run_usage.turns} "
        f"cost_usd={run_usage.cost_usd}"
    )

    # ------------------------------------------------------------------
    # IAR post-LLM: filter findings + build the state to persist.
    #
    # When the pre-LLM step failed (`iar_pre_context is None`) this block
    # is a no-op — the submission path sees exactly what the LLM produced.
    # Otherwise we mutate `result.findings` to the surfaced subset and
    # stash the new state + policy result for marker embedding + output
    # writing further down.
    # ------------------------------------------------------------------
    if (
        iar_pre_context is None
        and isinstance(provider, AgentRunnerProvider)
        and len(result.findings) > effective_max_inline_comments
    ):
        # IAR unavailable this run — keep the documented safety control.
        result.findings = _sort_findings_criticals_first(result.findings)[
            :effective_max_inline_comments
        ]
        result.overall_severity = overall_severity(
            [f.severity for f in result.findings]
        )
    if iar_pre_context is not None and result.incomplete:
        # No findings were produced, so nothing was resolved: re-embed the
        # prior state unchanged (as the escape-label path does) instead of
        # recording an empty round that would retire every open finding.
        log("IAR post-LLM: incomplete review — persisted state unchanged.")
        iar_state_final = iar_pre_context.prior_state
        iar_policy_final = iar_pre_context.pre_policy_result
        if iar_pre_context.prior_findings:
            result.overall_severity = overall_severity(
                [result.overall_severity]
                + [pf.severity for pf in iar_pre_context.prior_findings]
            )
    elif iar_pre_context is not None:
        try:
            iar_state_final, iar_policy_final = run_iar_post_llm(
                iar_config=iar_config,
                pre_context=iar_pre_context,
                result=result,
                base_max_inline_comments=max_inline_comments,
                telemetry=iar_telemetry,
                surface_cap=(
                    effective_max_inline_comments
                    if isinstance(provider, AgentRunnerProvider)
                    else 0
                ),
                resolution_policy=resolution_policy,
                workspace=Path.cwd(),
            )
        except Exception as exc:  # noqa: BLE001 — best-effort IAR wrap
            log(
                f"IAR post-LLM crashed: {type(exc).__name__}: {exc}. "
                "Submitting the review with the raw LLM findings (IAR "
                "skipped for this run)."
            )
            iar_state_final = None
            iar_policy_final = None
            # The model may have omitted known findings as instructed even
            # when post-processing fails. A bookkeeping error must not turn
            # that omission into a passing strictness gate.
            result.overall_severity = overall_severity(
                [result.overall_severity]
                + [pf.severity for pf in iar_pre_context.prior_findings]
            )

    # ------------------------------------------------------------------
    # Incremental mode: classify advisory verdicts and append the footer.
    # Human thread resolution is required to retire an outstanding finding.
    # ------------------------------------------------------------------
    if (
        iar_pre_context is not None
        and iar_pre_context.mode == IAR_MODE_INCREMENTAL
        and iar_pre_context.delta is not None
    ):
        try:
            current_fps: set[str] = set(
                iar_state_final.open_fingerprints_this_gen
                if iar_state_final is not None
                else [f.fingerprint for f in result.findings if f.fingerprint]
            )
            reconciliation: PriorFindingReconciliation = reconcile_prior_findings(
                prior_findings=iar_pre_context.prior_findings,
                updates=result.prior_finding_updates,
                current_fingerprints=current_fps,
                delta=iar_pre_context.delta,
                workspace=Path.cwd(),
                policy=resolution_policy,
            )
            # `advisory` (default): model-only resolution never mutates human
            # review threads. `verified`: reply on + resolve the threads the
            # runtime corroborated (best-effort).
            apply_resolution_policy(
                policy=resolution_policy,
                reconciliation=reconciliation,
                token=gh_token,
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha,
            )
            result.summary = (result.summary or "").rstrip() + render_incremental_footer(
                delta=iar_pre_context.delta,
                reconciliation=reconciliation,
                new_findings=len(result.findings),
                policy=resolution_policy,
            )
            log(
                f"IAR incremental: resolved={len(reconciliation.resolved)} "
                f"open={len(reconciliation.still_open)} "
                f"regressed={len(reconciliation.regressed)} "
                f"unverified={len(reconciliation.unverified)} new={len(result.findings)}"
            )
        except Exception as exc:  # noqa: BLE001 — never block the review on bookkeeping
            log(f"IAR incremental reconciliation failed (non-fatal): {exc}")

    # ------------------------------------------------------------------
    # Post the review (with 422 fallback)
    # ------------------------------------------------------------------
    if not result.summary:
        result.summary = (
            "## Code Review Summary\n\n"
            "_The reviewer hit the turn cap without producing a structured "
            "summary. Inline comments (if any) are still attached below._"
        )
        log("No submit_review captured — posting fallback summary")

    # Append the PR description verdict to the summary when warn/block mode
    # flagged the description. In autocomplete mode we don't warn — the
    # feature *fixed* the description.
    if (
        pr_desc_mode in (PR_DESC_MODE_WARN, PR_DESC_MODE_BLOCK)
        and not description_verdict.is_adequate
    ):
        result.summary = (
            result.summary.rstrip()
            + "\n\n---\n\n"
            + "> **PR description check**: "
            + description_verdict.reason
            + (
                "  (mode: `block` — the check will fail on this)"
                if pr_desc_mode == PR_DESC_MODE_BLOCK
                else "  (mode: `warn` — advisory only)"
            )
        )

    # Scrub any registered secret value out of everything that is about to be
    # posted publicly — the summary and each inline-comment body. On the
    # agent-runner path these strings originate from a vendor CLI that holds
    # an API key in its env; this is the last line of defence before a leaked
    # key could land in a public comment (see docs/SECURITY.md).
    result.summary = scrub_secrets(result.summary)
    for _finding in result.findings:
        _finding.body = scrub_secrets(_finding.body)

    # Embed the provider marker (an invisible HTML comment) at the top of the
    # review body so `collapse-previous` can scope to this provider's own
    # prior reviews — see provider_marker / gh_collapse_previous_reviews.
    result.summary = f"{provider_marker(review_scope)}\n\n{result.summary}"

    log(
        f"Submitting review: {len(result.findings)} inline comment(s), "
        f"{len(result.summary)} chars of summary"
    )

    try:
        review, dropped_inline = gh_submit_review_with_fallback(
            token=gh_token,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            result=result,
        )
    except Exception as e:  # noqa: BLE001
        log(f"Failed to post review: {e}")
        gh_update_issue_comment(
            token=gh_token,
            repo=repo,
            comment_id=tracking_id,
            body=render_tracking_body_failed(
                head_sha=head_sha,
                error=f"Could not post the review: {e}",
                provider=review_scope,
            ),
        )
        write_all_outputs(skipped=False)
        return 1

    review_url: str = str(review.get("html_url", ""))
    log(f"Review posted: {review_url}")

    # ------------------------------------------------------------------
    # PR description autocomplete (v1.2.0+) — best-effort PATCH.
    # ------------------------------------------------------------------
    if (
        pr_desc_mode == PR_DESC_MODE_AUTOCOMPLETE
        and not description_verdict.is_adequate
        and PR_DESC_AUTOCOMPLETE_MARKER not in (pr_ctx.body or "")
        and state.proposed_pr_description
    ):
        new_body: str = (
            state.proposed_pr_description.rstrip()
            + "\n\n"
            + PR_DESC_AUTOCOMPLETE_MARKER
        )
        try:
            gh_patch_pr_body(
                token=gh_token,
                repo=repo,
                pr_number=pr_number,
                new_body=new_body,
            )
            log("PR description autocompleted by the reviewer.")
        except Exception as e:  # noqa: BLE001 — best-effort GH API call
            log(f"Could not PATCH PR body (non-fatal): {e}")

    # ------------------------------------------------------------------
    # PR complexity labeling (v1.2.0+) — best-effort label update.
    # ------------------------------------------------------------------
    complexity_level: str | None = resolve_pr_complexity(
        state=state, result=result
    )
    if complexity_labels_enabled and not complexity_level:
        complexity_level = infer_pr_complexity_fallback(pr_ctx)
        log(
            "WARNING: complexity-labels-enabled=true but the reviewer did not "
            f"record a complexity level — applied heuristic fallback "
            f"{complexity_level!r}. Prefer an explicit model assessment via "
            "set_pr_complexity (chat-completions) or findings.json "
            "'complexity' (agent-runner)."
        )
    if complexity_labels_enabled and complexity_level:
        new_label: str = f"{complexity_label_prefix}{complexity_level}"
        try:
            # Remove any prior `complexity:*` label so the labels reflect
            # the current review's assessment, not a stale one.
            gh_remove_labels_by_prefix(
                token=gh_token,
                repo=repo,
                pr_number=pr_number,
                prefix=complexity_label_prefix,
                except_label=new_label,
            )
            gh_apply_label(
                token=gh_token,
                repo=repo,
                pr_number=pr_number,
                label=new_label,
            )
            log(f"Applied complexity label {new_label!r}")
        except Exception as e:  # noqa: BLE001 — best-effort GH API call
            log(f"Could not apply complexity label (non-fatal): {e}")

    # ------------------------------------------------------------------
    # Strictness gate
    # ------------------------------------------------------------------
    severity: str = result.overall_severity
    blocked, block_reason = evaluate_strictness(severity, strictness)
    if result.incomplete:
        # An incomplete agent-runner review must not green the check.
        incomplete_blocked, incomplete_reason = incomplete_review_gate(
            strictness, getattr(provider, "CLI_NAME", provider_id)
        )
        if incomplete_blocked or not blocked:
            blocked, block_reason = incomplete_blocked or blocked, incomplete_reason

    # PR description gate — orthogonal to the strictness gate. When
    # `pr-description-mode: block`, an inadequate description forces
    # `blocked=True` regardless of inline-comment severity.
    if (
        pr_desc_mode == PR_DESC_MODE_BLOCK
        and not description_verdict.is_adequate
    ):
        blocked = True
        block_reason = (
            f"pr-description-mode=block: {description_verdict.reason}"
        )
        log(f"PR description gate: blocking — {description_verdict.reason}")
    elif (
        pr_desc_mode in (PR_DESC_MODE_WARN, PR_DESC_MODE_BLOCK)
        and not description_verdict.is_adequate
    ):
        log(f"PR description gate: warning — {description_verdict.reason}")

    log(
        f"Severity: {severity}; strictness: {strictness}; blocked: {blocked} "
        f"({block_reason})"
    )

    attached_inline: int = len(result.findings) - dropped_inline
    tracking_body: str = render_tracking_body_done(
        head_sha=head_sha,
        review_url=review_url,
        inline_attached=attached_inline,
        inline_dropped=dropped_inline,
        severity=severity,
        blocked=blocked,
        block_reason=block_reason,
        provider=review_scope,
        usage_line=format_usage_line(
            run_usage, model=model, wall_clock_ms=iar_telemetry.wall_clock_ms()
        ),
    )
    # For `label-once` mode, embed the label-toggle generation so the
    # next run can detect "already reviewed this label application".
    if trigger_mode == TRIGGER_LABEL_ONCE and not blocked and not result.incomplete:
        tracking_body = write_trigger_state(
            tracking_body,
            {"label_toggle_generation": label_toggle_generation},
        )
    # ------------------------------------------------------------------
    # Apply success label (only if not blocked) — must happen BEFORE the
    # IAR state embed so `reviewed_label_applied` reflects the ACTUAL
    # outcome of the label stamp, not the intent. If we set the bit to
    # `True` before the stamp and the stamp then fails (network hiccup,
    # revoked permissions, deleted-label race, etc.), the next run's
    # USER_FORCED_RESET detection sees `reviewed_label_applied=True` +
    # label absent → wrongly fires a reset that wipes dedup memory.
    # Attempting the stamp first and recording the observed outcome
    # keeps the marker honest.
    # ------------------------------------------------------------------
    label_stamped: bool = False
    if applied_label and result.incomplete:
        log(f"Skipped applying {applied_label!r} — incomplete review")
    elif applied_label and not blocked:
        try:
            gh_apply_label(
                token=gh_token,
                repo=repo,
                pr_number=pr_number,
                label=applied_label,
            )
            log(f"Applied label {applied_label!r}")
            label_stamped = True
        except Exception as e:  # noqa: BLE001 — best-effort GH API call;
            # a label-stamp failure MUST NOT crash the reviewer (the
            # review has already posted successfully), but we must record
            # the failure so USER_FORCED_RESET's guard reads the truth.
            log(
                f"Failed to apply label {applied_label!r} (non-fatal, "
                f"marker will record reviewed_label_applied=False): {e}"
            )
    elif applied_label and blocked:
        log(f"Skipped applying {applied_label!r} — strictness gate blocked")

    # IAR marker embed: append a one-line annotation for developers who
    # skim the marker + embed the machine-readable state block that the
    # next run will parse. Skipped only if the pre-LLM or post-LLM step
    # crashed (state/policy will be None in that case) — the review still
    # ships, IAR just doesn't annotate this specific marker.
    if (
        iar_state_final is not None
        and iar_policy_final is not None
        and iar_pre_context is not None
    ):
        # Load-bearing for USER_FORCED_RESET: the arming bit reflects
        # whether the applied label is (or should be treated as) on the
        # PR at the end of this run — see `compute_reviewed_label_applied`.
        iar_state_final.reviewed_label_applied = (
            compute_reviewed_label_applied(
                applied_label=applied_label,
                label_stamped=label_stamped,
                current_labels=current_labels,
                prior_state=iar_pre_context.prior_state,
            )
        )
        tracking_body = tracking_body + _render_iar_marker_annotation(
            state=iar_state_final,
            policy_result=iar_policy_final,
            transition=iar_pre_context.transition,
            mode=iar_pre_context.mode,
        )
        tracking_body = embed_iteration_state(tracking_body, iar_state_final)
    gh_update_issue_comment(
        token=gh_token,
        repo=repo,
        comment_id=tracking_id,
        body=tracking_body,
    )

    # ------------------------------------------------------------------
    # Action outputs
    # ------------------------------------------------------------------
    write_all_outputs(
        skipped=False,
        severity=severity,
        inline_attached=attached_inline,
        inline_dropped=dropped_inline,
        blocked=blocked,
        review_url=review_url,
    )
    # IAR outputs: overwrite the five empty defaults from write_all_outputs
    # with real values ($GITHUB_OUTPUT is append-only; last write wins).
    # Only fires when the full IAR pipeline succeeded — a mid-flight
    # crash leaves the empty defaults in place so downstream steps still
    # see defined values.
    if (
        iar_state_final is not None
        and iar_policy_final is not None
    ):
        write_iar_outputs_populated(
            state=iar_state_final,
            policy_result=iar_policy_final,
            telemetry=iar_telemetry,
            effective_cap=iar_effective_cap or max_inline_comments,
            base_cap=max_inline_comments,
        )

    # Exit code 2 = blocked, so the GitHub check turns red but we keep
    # exit code 1 reserved for hard failures.
    return 2 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
