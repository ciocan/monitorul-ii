#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "openai>=1.50",
#   "jsonschema>=4.20",
#   "json-repair>=0.30",
#   "python-dotenv>=1.0",
# ]
# ///
"""Pilot benchmark harness for the discourse-analysis prompts.

Walks `validation/pilot_speeches.jsonl`, runs the three prompt pipeline
(hawkins → voice → dqi) against every selected model, captures structured
outputs, and writes per-(model, speech) result files plus per-model and
cross-model summary files.

Anthropic models run via the **Claude Code CLI** (subprocess against `claude
--print --output-format json --model <alias>`; uses the user's existing
authentication). Open-source models run via **OpenRouter**'s OpenAI-compatible
API (set `OPENROUTER_API_KEY`). Both paths consume the same prompt files +
companion JSON Schema files; the schema is enforced natively by OpenRouter
and inlined as a suffix-instruction for Claude Code.

Output offsets are recovered downstream: prompts emit only `evidence.text`,
the harness `str.find()`s each fragment in the speech to recover
`[start, end)`, and missing fragments are recorded as errors per call.

Usage:

    uv run python tools/pilot_benchmark.py \\
        --speeches validation/pilot_speeches.jsonl \\
        --out data/pilot_results/ \\
        --models opus,sonnet,haiku,llama-3.3-70b \\
        --prompts hawkins,voice,dqi \\
        --limit 3                # test on 3 speeches first

Environment:

    OPENROUTER_API_KEY            # required for any OpenRouter model

Exit codes:

    0   all calls completed (some may have per-call errors)
    1   harness-level failure (no models reachable, missing files)
    2   bad CLI arguments
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import jsonschema

# Auto-load .env so OPENROUTER_API_KEY (and any other secrets the project
# already keeps there alongside PROXY_URL / S3_* / ES_*) is visible to the
# OpenRouter call adapter without needing the user to manually `export` it.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

# Each entry: (launcher, model_arg, label, supports_strict_json_schema)
# `model_arg` is the value passed to claude --model (alias or full id) for
# claude_code, or the OpenRouter model slug for openrouter.
# `supports_strict_json_schema` is set to False for OpenRouter models that
# only support JSON mode (not strict json_schema); the harness falls back
# to schema-in-prompt + JSON mode for those.
MODELS: dict[str, dict[str, Any]] = {
    "opus": {
        "launcher": "claude_code",
        "model_arg": "opus",
        "label": "claude-opus-4.7",
        "supports_strict_json_schema": False,  # we inline schema in prompt
    },
    "sonnet": {
        "launcher": "claude_code",
        "model_arg": "sonnet",
        "label": "claude-sonnet-4.6",
        "supports_strict_json_schema": False,
    },
    "haiku": {
        "launcher": "claude_code",
        "model_arg": "haiku",
        "label": "claude-haiku-4.5",
        "supports_strict_json_schema": False,
    },
    # OpenRouter — Google Gemma 4 family
    "gemma-4-31b": {
        "launcher": "openrouter",
        "model_arg": "google/gemma-4-31b-it",
        "label": "gemma-4-31b-it",
        "supports_strict_json_schema": True,
    },
    "gemma-4-26b-a4b": {
        "launcher": "openrouter",
        "model_arg": "google/gemma-4-26b-a4b-it",
        "label": "gemma-4-26b-a4b-it",
        "supports_strict_json_schema": True,
    },
    # OpenRouter — Alibaba Qwen 3.6
    "qwen-3.6-27b": {
        "launcher": "openrouter",
        "model_arg": "qwen/qwen3.6-27b",
        "label": "qwen-3.6-27b",
        "supports_strict_json_schema": True,
    },
    "qwen-3.6-35b-a3b": {
        "launcher": "openrouter",
        "model_arg": "qwen/qwen3.6-35b-a3b",
        "label": "qwen-3.6-35b-a3b",
        "supports_strict_json_schema": True,
    },
    # OpenRouter — Google Gemini 3.1
    "gemini-3.1-pro": {
        "launcher": "openrouter",
        "model_arg": "google/gemini-3.1-pro-preview",
        "label": "gemini-3.1-pro-preview",
        "supports_strict_json_schema": True,
    },
    "gemini-3.1-flash-lite": {
        "launcher": "openrouter",
        "model_arg": "google/gemini-3.1-flash-lite",
        "label": "gemini-3.1-flash-lite",
        "supports_strict_json_schema": True,
    },
    # OpenRouter — OpenAI GPT-5
    "gpt-5.4": {
        "launcher": "openrouter",
        "model_arg": "openai/gpt-5.4",
        "label": "gpt-5.4",
        "supports_strict_json_schema": True,
    },
    "gpt-5.5": {
        "launcher": "openrouter",
        "model_arg": "openai/gpt-5.5",
        "label": "gpt-5.5",
        "supports_strict_json_schema": True,
    },
    "gpt-5.4-mini": {
        "launcher": "openrouter",
        "model_arg": "openai/gpt-5.4-mini",
        "label": "gpt-5.4-mini",
        "supports_strict_json_schema": True,
    },
    # OpenRouter — NVIDIA Nemotron 3
    "nemotron-3-120b": {
        "launcher": "openrouter",
        "model_arg": "nvidia/nemotron-3-super-120b-a12b",
        "label": "nemotron-3-super-120b-a12b",
        "supports_strict_json_schema": True,
    },
    # OpenRouter — Moonshot Kimi K2.6
    "kimi-k2.6": {
        "launcher": "openrouter",
        "model_arg": "moonshotai/kimi-k2.6",
        "label": "kimi-k2.6",
        "supports_strict_json_schema": True,
    },
    # OpenRouter — Z-AI GLM 5.1
    "glm-5.1": {
        "launcher": "openrouter",
        "model_arg": "z-ai/glm-5.1",
        "label": "glm-5.1",
        "supports_strict_json_schema": True,
    },
    # OpenRouter — OpenAI GPT-OSS 120B
    "gpt-oss-120b": {
        "launcher": "openrouter",
        "model_arg": "openai/gpt-oss-120b",
        "label": "gpt-oss-120b",
        "supports_strict_json_schema": True,
    },
    # OpenRouter — DeepSeek V4 Pro
    "deepseek-v4-pro": {
        "launcher": "openrouter",
        "model_arg": "deepseek/deepseek-v4-pro",
        "label": "deepseek-v4-pro",
        "supports_strict_json_schema": True,
    },
    # OpenRouter — DeepSeek V4 Flash
    "deepseek-v4-flash": {
        "launcher": "openrouter",
        "model_arg": "deepseek/deepseek-v4-flash",
        "label": "deepseek-v4-flash",
        "supports_strict_json_schema": True,
    },
}

PROMPT_FILES: dict[str, tuple[Path, Path]] = {
    "hawkins": (
        Path("prompts/hawkins_populism_v1.md"),
        Path("prompts/hawkins_populism_v1.schema.json"),
    ),
    "voice": (
        Path("prompts/voice_classifier_v1.md"),
        Path("prompts/voice_classifier_v1.schema.json"),
    ),
    "dqi": (
        Path("prompts/dqi_v1.md"),
        Path("prompts/dqi_v1.schema.json"),
    ),
    "vparty": (
        Path("prompts/vparty_antipluralism_v2.md"),
        Path("prompts/vparty_antipluralism_v2.schema.json"),
    ),
}

# Order matters — voice depends on hawkins (uses its emitted markers as input).
# dqi and vparty are independent classifiers (no marker dependency on hawkins).
PROMPT_SEQUENCE: list[str] = ["hawkins", "voice", "dqi", "vparty"]

CLAUDE_CALL_TIMEOUT_SECONDS = 300
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class CallResult:
    """One round-trip to one model for one prompt, on one speech."""

    model_key: str
    prompt_kind: str  # "hawkins" | "voice" | "dqi"
    speech_id: str
    output: (
        dict[str, Any] | None
    )  # parsed structured output; None on parse/validation failure
    raw_response: str | None  # the model's exact text response (for debugging)
    error: (
        str | None
    )  # short error class: "json_parse" | "schema_invalid" | "subprocess" | "transport"
    error_detail: str | None  # human-readable error
    fragments_not_found: list[
        str
    ]  # evidence.text values that couldn't be located in the speech
    parse_repaired: bool  # True when strict json.loads failed but json-repair succeeded
    tokens_in: int | None  # total input across fresh + cache_creation + cache_read
    tokens_in_breakdown: (
        dict[str, int] | None
    )  # {"fresh": int, "cache_creation": int, "cache_read": int}
    tokens_out: int | None
    cost_usd: float | None
    latency_ms: int
    retries_used: int = 0  # 0 = first attempt succeeded; >0 = retry-on-error fired


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

# Errors with these substrings reflect configuration / environment problems
# that retrying cannot fix; the harness short-circuits them so the retry
# budget is spent on transient transport / subprocess / parsing failures.
_NON_RETRYABLE_ERROR_DETAIL_SUBSTRINGS: tuple[str, ...] = (
    "openai package not installed",
    "OPENROUTER_API_KEY env var not set",
    "`claude` CLI not on PATH",
)


def _is_retryable(error_class: str | None, error_detail: str | None) -> bool:
    """Decide whether a failed CallResult warrants another attempt.

    A None error_class means success — never retried. Configuration-shaped
    failures (missing CLI, missing env var, missing package) are
    short-circuited as non-retryable. Everything else (transport timeouts,
    subprocess-wrapper parse failures, json_parse, schema_invalid) is
    retried until the budget is exhausted.
    """
    if not error_class:
        return False
    if error_detail:
        for snippet in _NON_RETRYABLE_ERROR_DETAIL_SUBSTRINGS:
            if snippet in error_detail:
                return False
    return True


def _retry_backoff_seconds(attempt: int) -> float:
    """Exponential backoff before retry attempt N (1-indexed: 1, 2, 4, 8s, capped 8s)."""
    return float(min(2 ** (attempt - 1), 8))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def slug_speech_id(speech_id: str) -> str:
    """Filesystem-safe slug from a speech_id like `mo://2005/II/136#agenda-3#act-64`."""
    return (
        speech_id.replace("mo://", "")
        .replace("/", "_")
        .replace("#", "-")
        .replace(":", "_")
    )


def find_text_offsets(speech_text: str, fragment: str) -> list[int] | None:
    """Locate `fragment` in `speech_text`; return `[start, end)` or None.

    The prompts instruct models to emit `evidence.text` as an exact substring;
    this helper recovers the offsets the schema doesn't carry. A return of
    None signals a paraphrased / inexact quote — the harness records this as
    an error and the marker is unrenderable downstream.
    """
    idx = speech_text.find(fragment)
    if idx < 0:
        return None
    return [idx, idx + len(fragment)]


def build_prompt_for_speech(
    prompt_text: str, speech_excerpt: str, schema: dict[str, Any]
) -> str:
    """Compose the user message for hawkins / dqi (single-speech input)."""
    schema_block = json.dumps(schema, ensure_ascii=False, indent=2)
    return f"""{prompt_text}

---

## Speech to classify

```
{speech_excerpt}
```

## Required output

Return ONE JSON object that conforms to the following JSON Schema. No prose, no commentary, no markdown fences — just the JSON.

```json
{schema_block}
```
"""


def build_prompt_for_voice(
    prompt_text: str,
    speech_excerpt: str,
    markers: list[dict[str, Any]],
    schema: dict[str, Any],
) -> str:
    """Compose the voice-classifier user message: speech + per-marker payload.

    Markers come from Hawkins's output; `char_range` is recovered locally via
    `find_text_offsets`. Markers whose text isn't found in the excerpt are
    skipped (they would not be classifiable anyway).
    """
    marker_payload = []
    for i, m in enumerate(markers):
        text = (m.get("evidence") or {}).get("text") or ""
        offsets = find_text_offsets(speech_excerpt, text)
        if offsets is None:
            continue
        marker_payload.append(
            {
                "marker_id": f"m_{i}",
                "char_range": offsets,
                "marker_text": text,
            }
        )

    if not marker_payload:
        return ""  # signal to caller: no markers → skip voice call

    voice_input = {
        "speech_excerpt": speech_excerpt,
        "pre_detected_regions": [],  # Pass 1 skipped for pilot
        "markers": marker_payload,
    }
    schema_block = json.dumps(schema, ensure_ascii=False, indent=2)
    input_block = json.dumps(voice_input, ensure_ascii=False, indent=2)
    return f"""{prompt_text}

---

## Input

```json
{input_block}
```

## Required output

Return ONE JSON object that conforms to the following JSON Schema, with one classification entry per input marker (in input order). No prose, no commentary, no markdown fences — just the JSON.

```json
{schema_block}
```
"""


def parse_and_validate(
    raw_text: str, schema: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None, str | None, bool]:
    """Strip optional code fences, parse JSON (with repair fallback), validate.

    Returns `(output, error_class, error_detail, repaired)`. `output` is None
    only when both strict and repair parsing fail. `error_class` is
    `None | "json_parse" | "schema_invalid"`. `repaired` is True when strict
    json.loads failed but json-repair succeeded — typically signals an
    unescaped ASCII `"` inside a Romanian-quoted string (the most common
    cheaper-model failure mode for our prompts).
    """
    text = (raw_text or "").strip()
    # Strip a single leading code fence + optional language tag
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[: -len("```")]
        text = text.strip()
    repaired = False
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as strict_exc:
        # Repair fallback. json-repair handles unescaped quotes inside string
        # values, missing commas, trailing prose, and other common LLM-output
        # malformations without losing structure.
        try:
            from json_repair import repair_json
        except ImportError:
            return None, "json_parse", str(strict_exc), False
        try:
            repaired_text = repair_json(text)
            obj = json.loads(repaired_text)
            repaired = True
        except (json.JSONDecodeError, ValueError) as repair_exc:
            return (
                None,
                "json_parse",
                f"strict: {strict_exc}; repair: {repair_exc}",
                False,
            )
    try:
        jsonschema.validate(obj, schema)
    except jsonschema.ValidationError as exc:
        # Schema-invalid: keep the parsed object so callers can inspect what was wrong
        return obj, "schema_invalid", exc.message, repaired
    return obj, None, None, repaired


def collect_fragments_not_found(
    output: dict[str, Any] | None, speech_text: str
) -> list[str]:
    """Walk the output for any `evidence.text` (or `voice_evidence.text`) that
    can't be located in the speech text. Empty list = all fragments resolved.

    Defensive against non-conforming outputs: schema-invalid responses (e.g. a
    top-level list instead of an object — observed in the wild from some
    OpenRouter providers) are recorded with output set to whatever was parsed,
    so this function may receive non-dict values. Return empty in those cases
    rather than crashing the per-speech pipeline.
    """
    if not isinstance(output, dict):
        return []
    misses: list[str] = []

    def _check(text_value: str | None) -> None:
        if text_value and find_text_offsets(speech_text, text_value) is None:
            misses.append(text_value)

    # Hawkins / DQI: markers[].evidence.text
    markers = output.get("markers") or []
    if isinstance(markers, list):
        for marker in markers:
            if not isinstance(marker, dict):
                continue
            evidence = marker.get("evidence") or {}
            if isinstance(evidence, dict):
                _check(evidence.get("text"))
    # Voice: classifications[].voice_evidence.text (when present)
    classifications = output.get("classifications") or []
    if isinstance(classifications, list):
        for entry in classifications:
            if not isinstance(entry, dict):
                continue
            ev = entry.get("voice_evidence")
            if isinstance(ev, dict):
                _check(ev.get("text"))
    return misses


# ---------------------------------------------------------------------------
# Provider adapters
# ---------------------------------------------------------------------------


def call_claude_code(model_arg: str, full_prompt: str) -> dict[str, Any]:
    """Invoke `claude --print --output-format json --model <model_arg>` via stdin.

    Returns a dict with `result_text`, `tokens_in`, `tokens_out`, `cost_usd`,
    `latency_ms`, and (on failure) `error` / `error_detail`.
    """
    cmd = [
        "claude",
        "--print",
        "--output-format",
        "json",
        "--model",
        model_arg,
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=CLAUDE_CALL_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": int((time.time() - t0) * 1000),
            "error": "subprocess",
            "error_detail": "`claude` CLI not on PATH; is Claude Code installed and authenticated?",
        }
    except subprocess.TimeoutExpired:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": CLAUDE_CALL_TIMEOUT_SECONDS * 1000,
            "error": "subprocess",
            "error_detail": f"timeout after {CLAUDE_CALL_TIMEOUT_SECONDS}s",
        }
    latency_ms = int((time.time() - t0) * 1000)
    if proc.returncode != 0:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": latency_ms,
            "error": "subprocess",
            "error_detail": f"returncode={proc.returncode}; stderr={proc.stderr[:500]}",
        }
    try:
        wrapper = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": latency_ms,
            "error": "subprocess",
            "error_detail": f"wrapper parse: {exc}; stdout head: {proc.stdout[:300]}",
        }
    usage = wrapper.get("usage") or {}
    # Claude Code reports input tokens in three buckets — fresh (the
    # not-yet-cached portion of the user message), cache_creation (system
    # prompt + tool descriptions on first call within the cache TTL), and
    # cache_read (replay of cached input on subsequent calls). Sum for the
    # headline; keep the breakdown for cost interpretation.
    fresh_in = int(usage.get("input_tokens") or 0)
    cache_creation_in = int(usage.get("cache_creation_input_tokens") or 0)
    cache_read_in = int(usage.get("cache_read_input_tokens") or 0)
    return {
        "result_text": wrapper.get("result"),
        "tokens_in": fresh_in + cache_creation_in + cache_read_in,
        "tokens_in_breakdown": {
            "fresh": fresh_in,
            "cache_creation": cache_creation_in,
            "cache_read": cache_read_in,
        },
        "tokens_out": usage.get("output_tokens"),
        "cost_usd": wrapper.get("total_cost_usd"),
        "latency_ms": latency_ms,
        "error": None,
        "error_detail": None,
    }


# Substrings in OpenRouter / OpenAI BadRequestError messages that indicate
# the chosen model / backend doesn't honour the strict json_schema response
# format and we should fall back to plain json_object mode. The list is
# conservative: we only retry-with-fallback on errors that look schema-shaped,
# not on rate limits or auth failures.
_STRICT_SCHEMA_REJECTION_SUBSTRINGS: tuple[str, ...] = (
    "json_schema",
    "response_format",
    "structured output",
    "structured_output",
    "not support",
    "is not supported",
    "unrecognized",
    "unsupported",
    "invalid schema",
)


def _looks_like_schema_rejection(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in _STRICT_SCHEMA_REJECTION_SUBSTRINGS)


def call_openrouter(
    model_arg: str,
    full_prompt: str,
    schema: dict[str, Any],
    *,
    use_strict_schema: bool,
) -> dict[str, Any]:
    """Invoke OpenRouter via the OpenAI SDK.

    `use_strict_schema=True` requests the OpenAI Structured Outputs
    `response_format: json_schema` strict mode; on schema-rejection-shaped
    errors the call automatically retries with `response_format: json_object`
    (the schema is already inlined in the prompt body, so semantic enforcement
    happens via the prompt + the harness's downstream `parse_and_validate`
    + json-repair fallback). `use_strict_schema=False` skips the strict
    attempt entirely.
    """
    try:
        import openai  # imported lazily so `--models opus,sonnet` runs without OpenAI SDK
    except ImportError:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": 0,
            "error": "transport",
            "error_detail": "openai package not installed; run via `uv run` or `pip install openai`",
        }

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": 0,
            "error": "transport",
            "error_detail": "OPENROUTER_API_KEY env var not set",
        }

    client = openai.OpenAI(api_key=api_key, base_url=OPENROUTER_BASE_URL)

    schema_format: dict[str, Any] = {
        "type": "json_schema",
        "json_schema": {
            "name": (schema.get("$id") or "output").replace(":", "_"),
            "strict": True,
            "schema": schema,
        },
    }
    json_object_format: dict[str, Any] = {"type": "json_object"}

    def _make_call(response_format: dict[str, Any]):
        return client.chat.completions.create(
            model=model_arg,
            messages=[{"role": "user", "content": full_prompt}],
            response_format=response_format,
            temperature=0,
            # Generous ceiling: DQI rationales reach ~7K visible tokens against
            # Opus and reasoning models add hidden thinking on top. Truncated
            # JSON is the largest single class of "json_parse" failures across
            # cheaper models, so headroom matters more than rate-limit
            # conservation here.
            max_tokens=16384,
        )

    t0 = time.time()
    format_used = "json_schema" if use_strict_schema else "json_object"
    fallback_triggered = False
    resp = None
    last_exc: BaseException | None = None
    try:
        resp = _make_call(schema_format if use_strict_schema else json_object_format)
    except Exception as exc:  # noqa: BLE001 — OpenRouter raises a wide mix of openai.* errors
        last_exc = exc
        # Any 400 BadRequest while in strict mode is treated as a schema
        # rejection — providers vary widely in which JSON Schema features
        # they accept (Gemini rejects $schema/$id/anyOf; some require
        # unspecific subset). The fallback to json_object lets the prompt's
        # inlined schema + downstream json-repair handle semantic enforcement.
        is_bad_request = (
            isinstance(exc, openai.BadRequestError)
            if hasattr(openai, "BadRequestError")
            else getattr(exc, "status_code", None) == 400
        )
        should_fallback = use_strict_schema and (
            is_bad_request or _looks_like_schema_rejection(exc)
        )
        if should_fallback:
            print(
                f"  [fallback] {model_arg} rejected json_schema strict mode "
                f"({type(exc).__name__}); retrying with json_object",
                file=sys.stderr,
            )
            try:
                resp = _make_call(json_object_format)
                format_used = "json_object_after_fallback"
                fallback_triggered = True
                last_exc = None
            except Exception as exc2:  # noqa: BLE001
                last_exc = exc2

    latency_ms = int((time.time() - t0) * 1000)

    if resp is None:
        return {
            "result_text": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "latency_ms": latency_ms,
            "error": "transport",
            "error_detail": (
                f"{type(last_exc).__name__}: {last_exc}"
                + (
                    " (fallback to json_object also failed)"
                    if fallback_triggered
                    else ""
                )
            ),
        }

    usage = resp.usage
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    # OpenAI / OpenRouter standard usage block doesn't split cache reads /
    # creation across prompt_tokens; some providers expose cache stats on a
    # `prompt_tokens_details` extension, but treating the whole prompt as
    # "fresh" is the honest baseline for cross-provider comparison.
    return {
        "result_text": resp.choices[0].message.content if resp.choices else None,
        "tokens_in": prompt_tokens,
        "tokens_in_breakdown": {
            "fresh": int(prompt_tokens or 0),
            "cache_creation": 0,
            "cache_read": 0,
        }
        if prompt_tokens is not None
        else None,
        "tokens_out": getattr(usage, "completion_tokens", None),
        "cost_usd": None,  # OpenRouter doesn't return cost in the standard usage block; pricing is per-model on their side
        "latency_ms": latency_ms,
        "error": None,
        "error_detail": (
            f"transport_format={format_used}" if fallback_triggered else None
        ),
    }


def call_model(
    model_key: str, full_prompt: str, schema: dict[str, Any]
) -> dict[str, Any]:
    """Dispatch to the correct provider adapter based on the model registry."""
    model = MODELS[model_key]
    if model["launcher"] == "claude_code":
        return call_claude_code(model["model_arg"], full_prompt)
    if model["launcher"] == "openrouter":
        return call_openrouter(
            model["model_arg"],
            full_prompt,
            schema,
            use_strict_schema=model["supports_strict_json_schema"],
        )
    raise ValueError(f"unknown launcher for model {model_key}: {model['launcher']}")


# ---------------------------------------------------------------------------
# Per-speech pipeline
# ---------------------------------------------------------------------------


def run_one(
    *,
    model_key: str,
    prompt_kind: str,
    speech_id: str,
    full_prompt: str,
    schema: dict[str, Any],
    speech_text: str,
    max_retries: int = 0,
) -> CallResult:
    """One model call + parse + validate + offset recovery, with optional retry.

    The retry loop covers both transport-layer errors (network, timeout,
    subprocess wrapper-parse) and post-call output errors (json_parse,
    schema_invalid). Configuration-shaped errors (missing API key, claude
    CLI not on PATH) short-circuit and never retry. Backoff is exponential,
    capped at 8 s.

    `max_retries=0` (default) preserves the v0 single-attempt behaviour.
    """
    last: CallResult | None = None
    for attempt in range(max_retries + 1):
        if attempt > 0:
            time.sleep(_retry_backoff_seconds(attempt))
        transport = call_model(model_key, full_prompt, schema)
        raw_text = transport.get("result_text")

        if transport.get("error"):
            last = CallResult(
                model_key=model_key,
                prompt_kind=prompt_kind,
                speech_id=speech_id,
                output=None,
                raw_response=raw_text,
                error=transport["error"],
                error_detail=transport.get("error_detail"),
                fragments_not_found=[],
                parse_repaired=False,
                tokens_in=transport.get("tokens_in"),
                tokens_in_breakdown=transport.get("tokens_in_breakdown"),
                tokens_out=transport.get("tokens_out"),
                cost_usd=transport.get("cost_usd"),
                latency_ms=transport.get("latency_ms", 0),
                retries_used=attempt,
            )
        else:
            output, err_class, err_detail, repaired = parse_and_validate(
                raw_text or "", schema
            )
            fragments = collect_fragments_not_found(output, speech_text)
            last = CallResult(
                model_key=model_key,
                prompt_kind=prompt_kind,
                speech_id=speech_id,
                output=output,
                raw_response=raw_text,
                error=err_class,
                error_detail=err_detail,
                fragments_not_found=fragments,
                parse_repaired=repaired,
                tokens_in=transport.get("tokens_in"),
                tokens_in_breakdown=transport.get("tokens_in_breakdown"),
                tokens_out=transport.get("tokens_out"),
                cost_usd=transport.get("cost_usd"),
                latency_ms=transport.get("latency_ms", 0),
                retries_used=attempt,
            )

        if not last.error:
            return last
        if not _is_retryable(last.error, last.error_detail):
            return last
        # retryable error → loop continues if budget remains
    return last  # type: ignore[return-value]  # loop body always sets `last`


def run_pipeline_for_speech(
    *,
    model_key: str,
    speech: dict[str, Any],
    prompts: dict[str, str],
    schemas: dict[str, dict[str, Any]],
    enabled_kinds: set[str],
    max_retries: int = 0,
) -> dict[str, Any]:
    """Run hawkins → voice (conditional) → dqi → vparty for one speech and one model."""
    speech_text = speech["text"]
    speech_id = speech["speech_id"]
    results: dict[str, CallResult | None] = {
        "hawkins": None,
        "voice": None,
        "dqi": None,
        "vparty": None,
    }

    # 1. Hawkins
    if "hawkins" in enabled_kinds:
        prompt = build_prompt_for_speech(
            prompts["hawkins"], speech_text, schemas["hawkins"]
        )
        results["hawkins"] = run_one(
            model_key=model_key,
            prompt_kind="hawkins",
            speech_id=speech_id,
            full_prompt=prompt,
            schema=schemas["hawkins"],
            speech_text=speech_text,
            max_retries=max_retries,
        )

    # 2. Voice (conditional on Hawkins markers)
    if "voice" in enabled_kinds:
        hawkins_output = results["hawkins"].output if results["hawkins"] else None
        markers = (hawkins_output or {}).get("markers") or []
        if markers:
            voice_prompt = build_prompt_for_voice(
                prompts["voice"], speech_text, markers, schemas["voice"]
            )
            if voice_prompt:
                results["voice"] = run_one(
                    model_key=model_key,
                    prompt_kind="voice",
                    speech_id=speech_id,
                    full_prompt=voice_prompt,
                    schema=schemas["voice"],
                    speech_text=speech_text,
                    max_retries=max_retries,
                )

    # 3. DQI (independent)
    if "dqi" in enabled_kinds:
        dqi_prompt = build_prompt_for_speech(
            prompts["dqi"], speech_text, schemas["dqi"]
        )
        results["dqi"] = run_one(
            model_key=model_key,
            prompt_kind="dqi",
            speech_id=speech_id,
            full_prompt=dqi_prompt,
            schema=schemas["dqi"],
            speech_text=speech_text,
            max_retries=max_retries,
        )

    # 4. V-Party anti-pluralism (independent)
    if "vparty" in enabled_kinds:
        vparty_prompt = build_prompt_for_speech(
            prompts["vparty"], speech_text, schemas["vparty"]
        )
        results["vparty"] = run_one(
            model_key=model_key,
            prompt_kind="vparty",
            speech_id=speech_id,
            full_prompt=vparty_prompt,
            schema=schemas["vparty"],
            speech_text=speech_text,
            max_retries=max_retries,
        )

    return {
        "speech_id": speech_id,
        "tier": speech["tier"],
        "speaker": speech["speaker"],
        "metadata": speech.get("metadata", {}),
        "model_key": model_key,
        "model_label": MODELS[model_key]["label"],
        "results": {
            k: dataclasses.asdict(v) if v else None for k, v in results.items()
        },
    }


# ---------------------------------------------------------------------------
# Summarisation
# ---------------------------------------------------------------------------


def _safe_get(d: dict | None, *keys, default=None):
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def summarise_model(per_speech: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-(speech) results for one model into a summary dict."""
    n_speeches = len(per_speech)
    totals = {
        "tokens_in": 0,
        "tokens_in_fresh": 0,
        "tokens_in_cache_creation": 0,
        "tokens_in_cache_read": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "latency_ms": 0,
        "calls": 0,
        "retries_used": 0,
        "calls_with_retries": 0,
        "calls_with_repair": 0,
    }
    errors_by_kind_and_class: Counter[tuple[str, str]] = Counter()
    fragments_misses_by_kind: Counter[str] = Counter()

    hawkins_scores: Counter[int] = Counter()
    hawkins_marker_kinds: Counter[str] = Counter()
    voice_distribution: Counter[str] = Counter()
    dqi_levels: Counter[int] = Counter()
    dqi_constructive: Counter[str] = Counter()
    vparty_scores: Counter[int] = Counter()
    vparty_marker_kinds: Counter[str] = Counter()

    for entry in per_speech:
        for kind in PROMPT_SEQUENCE:
            call = entry["results"].get(kind)
            if call is None:
                continue
            totals["calls"] += 1
            totals["tokens_in"] += call.get("tokens_in") or 0
            breakdown = call.get("tokens_in_breakdown") or {}
            totals["tokens_in_fresh"] += breakdown.get("fresh") or 0
            totals["tokens_in_cache_creation"] += breakdown.get("cache_creation") or 0
            totals["tokens_in_cache_read"] += breakdown.get("cache_read") or 0
            totals["tokens_out"] += call.get("tokens_out") or 0
            cost = call.get("cost_usd")
            if cost is not None:
                totals["cost_usd"] += cost
            totals["latency_ms"] += call.get("latency_ms") or 0
            retries = call.get("retries_used") or 0
            totals["retries_used"] += retries
            if retries > 0:
                totals["calls_with_retries"] += 1
            if call.get("parse_repaired"):
                totals["calls_with_repair"] += 1
            err = call.get("error")
            if err:
                errors_by_kind_and_class[(kind, err)] += 1
            if call.get("fragments_not_found"):
                fragments_misses_by_kind[kind] += len(call["fragments_not_found"])

            output = call.get("output")
            # Defensive: schema-invalid responses can leave `output` as a list
            # or other non-dict value; skip them rather than crashing summarisation.
            if not isinstance(output, dict):
                continue
            if kind == "hawkins":
                score = output.get("score")
                if isinstance(score, int):
                    hawkins_scores[score] += 1
                markers = output.get("markers") or []
                if isinstance(markers, list):
                    for m in markers:
                        if isinstance(m, dict) and m.get("kind"):
                            hawkins_marker_kinds[m["kind"]] += 1
            elif kind == "voice":
                classifications = output.get("classifications") or []
                if isinstance(classifications, list):
                    for c in classifications:
                        if not isinstance(c, dict):
                            continue
                        voice = c.get("voice")
                        if voice:
                            voice_distribution[voice] += 1
            elif kind == "dqi":
                level = output.get("level_of_justification")
                if isinstance(level, int):
                    dqi_levels[level] += 1
                cp = output.get("constructive_politics")
                if cp:
                    dqi_constructive[cp] += 1
            elif kind == "vparty":
                score = output.get("score")
                if isinstance(score, int):
                    vparty_scores[score] += 1
                markers = output.get("markers") or []
                if isinstance(markers, list):
                    for m in markers:
                        if isinstance(m, dict) and m.get("kind"):
                            vparty_marker_kinds[m["kind"]] += 1

    return {
        "n_speeches": n_speeches,
        "totals": totals,
        "errors": {
            f"{kind}/{cls}": count
            for (kind, cls), count in sorted(errors_by_kind_and_class.items())
        },
        "fragments_not_found_by_kind": dict(fragments_misses_by_kind),
        "hawkins_score_histogram": dict(sorted(hawkins_scores.items())),
        "hawkins_marker_kinds": dict(hawkins_marker_kinds.most_common()),
        "voice_distribution": dict(voice_distribution.most_common()),
        "dqi_level_of_justification_histogram": dict(sorted(dqi_levels.items())),
        "dqi_constructive_politics": dict(dqi_constructive.most_common()),
        "vparty_score_histogram": dict(sorted(vparty_scores.items())),
        "vparty_marker_kinds": dict(vparty_marker_kinds.most_common()),
    }


def print_cross_model_table(
    summaries: dict[str, dict[str, Any]], stream=sys.stdout
) -> None:
    """Print a tight side-by-side comparison table to stdout."""
    cols = list(summaries.keys())
    if not cols:
        return

    def cell(value: Any, width: int) -> str:
        s = str(value)
        return s if len(s) <= width else s[: width - 1] + "…"

    widths = max(20, max(len(MODELS[m]["label"]) for m in cols))
    label_w = max(len("metric"), 36)
    col_w = max(20, widths)

    print(
        f"\n=== Cross-model summary ({len(cols)} models, {summaries[cols[0]]['n_speeches']} speeches) ===\n",
        file=stream,
    )
    print(
        f"{'metric':<{label_w}}"
        + "".join(f"{cell(MODELS[m]['label'], col_w):>{col_w + 2}}" for m in cols),
        file=stream,
    )
    print("-" * (label_w + (col_w + 2) * len(cols)), file=stream)

    rows: list[tuple[str, Callable[[dict], Any]]] = [
        ("calls", lambda s: s["totals"]["calls"]),
        ("tokens_in total", lambda s: s["totals"]["tokens_in"]),
        ("  fresh", lambda s: s["totals"]["tokens_in_fresh"]),
        ("  cache_creation", lambda s: s["totals"]["tokens_in_cache_creation"]),
        ("  cache_read", lambda s: s["totals"]["tokens_in_cache_read"]),
        ("tokens_out", lambda s: s["totals"]["tokens_out"]),
        ("cost_usd (claude-code only)", lambda s: f"{s['totals']['cost_usd']:.4f}"),
        ("latency_ms total", lambda s: s["totals"]["latency_ms"]),
        (
            "latency_ms / call avg",
            lambda s: int(s["totals"]["latency_ms"] / max(1, s["totals"]["calls"])),
        ),
        ("errors total", lambda s: sum(s["errors"].values())),
        ("retries used total", lambda s: s["totals"]["retries_used"]),
        ("calls with retries", lambda s: s["totals"]["calls_with_retries"]),
        ("calls with json repair", lambda s: s["totals"]["calls_with_repair"]),
        (
            "fragments_not_found total",
            lambda s: sum(s["fragments_not_found_by_kind"].values()),
        ),
        ("hawkins score 0", lambda s: s["hawkins_score_histogram"].get(0, 0)),
        ("hawkins score 1", lambda s: s["hawkins_score_histogram"].get(1, 0)),
        ("hawkins score 2", lambda s: s["hawkins_score_histogram"].get(2, 0)),
        (
            "voice first_person",
            lambda s: s["voice_distribution"].get("speaker_first_person", 0),
        ),
        ("voice quoted", lambda s: s["voice_distribution"].get("quoted", 0)),
        (
            "voice apophasis_disclaimed",
            lambda s: s["voice_distribution"].get("apophasis_disclaimed", 0),
        ),
        ("voice uncertain", lambda s: s["voice_distribution"].get("uncertain", 0)),
        (
            "dqi level_of_justification 0",
            lambda s: s["dqi_level_of_justification_histogram"].get(0, 0),
        ),
        (
            "dqi level_of_justification 1",
            lambda s: s["dqi_level_of_justification_histogram"].get(1, 0),
        ),
        (
            "dqi level_of_justification 2",
            lambda s: s["dqi_level_of_justification_histogram"].get(2, 0),
        ),
        (
            "dqi level_of_justification 3",
            lambda s: s["dqi_level_of_justification_histogram"].get(3, 0),
        ),
        ("vparty score 0", lambda s: s.get("vparty_score_histogram", {}).get(0, 0)),
        ("vparty score 1", lambda s: s.get("vparty_score_histogram", {}).get(1, 0)),
        ("vparty score 2", lambda s: s.get("vparty_score_histogram", {}).get(2, 0)),
    ]

    for name, getter in rows:
        line = f"{name:<{label_w}}"
        for m in cols:
            line += f"{cell(getter(summaries[m]), col_w):>{col_w + 2}}"
        print(line, file=stream)
    print("", file=stream)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--speeches",
        type=Path,
        default=Path("validation/pilot_speeches.jsonl"),
        help="JSONL of stratified pilot speeches (default: validation/pilot_speeches.jsonl).",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("data/pilot_results"),
        help="Output directory for per-speech result files + summaries (default: data/pilot_results).",
    )
    p.add_argument(
        "--models",
        type=str,
        default="opus,sonnet,haiku",
        help=f"Comma-separated model keys; available: {','.join(MODELS.keys())}.",
    )
    p.add_argument(
        "--prompts",
        type=str,
        default="hawkins,voice,dqi",
        help="Comma-separated prompt kinds to run (hawkins,voice,dqi).",
    )
    p.add_argument(
        "--skip",
        type=int,
        default=0,
        metavar="N",
        help="Skip the first N speeches (default: 0). Combine with --limit to slice.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Run at most N speeches (after --skip is applied). Default: all.",
    )
    p.add_argument(
        "--retry-on-error",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Retry failed calls up to N times with exponential backoff "
            "(1, 2, 4, 8s, capped 8s). Covers transport / subprocess / "
            "json_parse / schema_invalid; configuration errors "
            "(missing API key, claude CLI not on PATH) short-circuit. "
            "Default 0 (single attempt)."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be called without contacting any model.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    # Force line-buffered stderr so progress lines flush immediately even
    # when the harness output is piped (`| cat` or task wrappers); otherwise
    # Python block-buffers stderr behind a 4KB threshold and progress is
    # invisible until the harness exits.
    sys.stderr.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

    args = _build_parser().parse_args(argv)

    if not args.speeches.exists():
        print(f"[error] --speeches {args.speeches} does not exist", file=sys.stderr)
        return 1

    if args.retry_on_error < 0:
        print(
            f"[error] --retry-on-error must be >= 0 (got {args.retry_on_error})",
            file=sys.stderr,
        )
        return 2

    selected_models = [m.strip() for m in args.models.split(",") if m.strip()]
    unknown = [m for m in selected_models if m not in MODELS]
    if unknown:
        print(
            f"[error] unknown model keys: {unknown}; available: {list(MODELS)}",
            file=sys.stderr,
        )
        return 2

    enabled_kinds = {k.strip() for k in args.prompts.split(",") if k.strip()}
    unknown_prompts = enabled_kinds - set(PROMPT_FILES)
    if unknown_prompts:
        print(
            f"[error] unknown prompt kinds: {unknown_prompts}; available: {list(PROMPT_FILES)}",
            file=sys.stderr,
        )
        return 2

    # Validate that any OpenRouter model has its env var available
    needs_openrouter = any(
        MODELS[m]["launcher"] == "openrouter" for m in selected_models
    )
    if (
        needs_openrouter
        and not os.environ.get("OPENROUTER_API_KEY")
        and not args.dry_run
    ):
        print(
            "[warn] OPENROUTER_API_KEY not set; OpenRouter calls will fail",
            file=sys.stderr,
        )

    prompts: dict[str, str] = {}
    schemas: dict[str, dict[str, Any]] = {}
    for kind, (prompt_path, schema_path) in PROMPT_FILES.items():
        if kind not in enabled_kinds:
            continue
        if not prompt_path.exists() or not schema_path.exists():
            print(
                f"[error] missing prompt/schema for {kind}: {prompt_path} / {schema_path}",
                file=sys.stderr,
            )
            return 1
        prompts[kind] = prompt_path.read_text(encoding="utf-8")
        schemas[kind] = json.loads(schema_path.read_text(encoding="utf-8"))

    speeches = [
        json.loads(line)
        for line in args.speeches.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.skip < 0:
        print(f"[error] --skip must be >= 0 (got {args.skip})", file=sys.stderr)
        return 2
    if args.skip:
        speeches = speeches[args.skip :]
    if args.limit is not None:
        speeches = speeches[: args.limit]

    retry_note = (
        f" (retry-on-error budget: {args.retry_on_error})"
        if args.retry_on_error > 0
        else ""
    )
    print(
        f"[info] {len(speeches)} speeches × {len(selected_models)} models × {len(enabled_kinds)} prompts{retry_note}",
        file=sys.stderr,
    )
    if args.dry_run:
        for m in selected_models:
            print(
                f"  [dry-run] would call {MODELS[m]['label']} ({MODELS[m]['launcher']}) for {len(speeches)} speeches × {sorted(enabled_kinds)}",
                file=sys.stderr,
            )
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    summaries: dict[str, dict[str, Any]] = {}

    for model_key in selected_models:
        model_dir = args.out / model_key
        model_dir.mkdir(parents=True, exist_ok=True)
        per_speech: list[dict[str, Any]] = []
        print(f"\n[info] === {MODELS[model_key]['label']} ===", file=sys.stderr)
        for i, speech in enumerate(speeches, start=1):
            t0 = time.time()
            result = run_pipeline_for_speech(
                model_key=model_key,
                speech=speech,
                prompts=prompts,
                schemas=schemas,
                enabled_kinds=enabled_kinds,
                max_retries=args.retry_on_error,
            )
            per_speech.append(result)
            out_path = model_dir / f"{slug_speech_id(speech['speech_id'])}.json"
            out_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            errs = [
                f"{kind}={result['results'][kind]['error']}"
                for kind in PROMPT_SEQUENCE
                if result["results"].get(kind) and result["results"][kind].get("error")
            ]
            err_tag = f" errors={','.join(errs)}" if errs else ""
            retries_total = sum(
                (result["results"].get(kind) or {}).get("retries_used") or 0
                for kind in PROMPT_SEQUENCE
            )
            repair_total = sum(
                1
                for kind in PROMPT_SEQUENCE
                if (result["results"].get(kind) or {}).get("parse_repaired")
            )
            retry_tag = f" retries={retries_total}" if retries_total else ""
            repair_tag = f" repaired={repair_total}" if repair_total else ""
            print(
                f"  [{i:2d}/{len(speeches)}] {slug_speech_id(speech['speech_id'])} "
                f"tier={speech['tier']:14s} {int((time.time() - t0) * 1000):5d}ms{retry_tag}{repair_tag}{err_tag}",
                file=sys.stderr,
            )
        summary = summarise_model(per_speech)
        summaries[model_key] = summary
        (args.out / f"{model_key}_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    (args.out / "_cross_model_summary.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print_cross_model_table(summaries, stream=sys.stderr)
    print(f"[info] wrote results under {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
