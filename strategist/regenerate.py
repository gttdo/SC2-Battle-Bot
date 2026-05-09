"""Regenerate a playbook by asking Claude Opus to update the previous
version based on recent match observations.

Uses prompt caching: the system prompt (schema + persona + reasoning guide)
and the prior playbook are both placed at cache breakpoints, so subsequent
regenerations re-read the cached prefix at ~10% of the uncached cost. Only
the observations summary varies between calls.

Validates the model's output against the playbook JSON schema before
returning. We don't use the API's structured-output feature (`output_config.
format`) because our schema uses pattern / minimum / maximum constraints
that aren't supported by the structured-outputs validator — instead we let
the model emit JSON in a code fence and validate post-hoc with jsonschema,
which gives us the full schema's enforcement.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK_DIR = PROJECT_ROOT / "playbook"

# claude-opus-4-7 per the skill's default model guidance. The strategist is
# the LLM-heavy side of this project — quality matters more than per-call
# cost, and Opus 4.7's adaptive thinking is the right fit for "rewrite this
# playbook based on N losses."
DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_MAX_TOKENS = 16000

# JSON code-fence extractor. Handles ```json``` and bare ``` fences. The
# regex is non-greedy so it stops at the first closing fence, and DOTALL so
# multi-line JSON parses cleanly.
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _load_schema() -> dict[str, Any]:
    with (PLAYBOOK_DIR / "schema.json").open(encoding="utf-8") as f:
        return json.load(f)


def _load_prior_playbook(matchup: str) -> dict[str, Any] | None:
    path = PLAYBOOK_DIR / f"{matchup.lower()}.json"
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def extract_json_from_response(text: str) -> dict[str, Any]:
    """Find the first ```json ... ``` code fence in `text` and parse it.

    Falls back to parsing the whole string if no fence is present, since
    Claude sometimes obeys the format rule literally and emits naked JSON.
    Raises json.JSONDecodeError on failure — the caller is responsible for
    surfacing the error.
    """
    match = _CODE_FENCE_RE.search(text)
    if match:
        return json.loads(match.group(1))
    return json.loads(text.strip())


def _validate_playbook(playbook: dict[str, Any], schema: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if the playbook doesn't match schema."""
    from jsonschema import Draft202012Validator

    Draft202012Validator(schema).validate(playbook)


def build_request_payload(
    matchup: str,
    observations_summary: dict[str, Any],
    schema: dict[str, Any],
    prior_playbook: dict[str, Any] | None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Construct the (system, messages) payload — pure function, testable
    without an API call. Returns a dict with keys 'system' and 'messages'."""
    from strategist.prompts import render_system_prompt

    timestamp = timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds")

    system_prompt = render_system_prompt(schema)

    user_blocks: list[dict[str, Any]] = []
    if prior_playbook is not None:
        user_blocks.append({
            "type": "text",
            "text": (
                f"PRIOR PLAYBOOK ({matchup}):\n```json\n"
                f"{json.dumps(prior_playbook, indent=2, sort_keys=True)}\n```"
            ),
            # Cache breakpoint #2: prior playbook is stable within a session.
            # Ordering matters — render order is system → messages, so this
            # block sits AFTER the cached system prompt and creates a second
            # readable cache entry for callers that ship multiple
            # observations summaries through the same prior playbook.
            "cache_control": {"type": "ephemeral"},
        })
    user_blocks.append({
        "type": "text",
        "text": (
            f"MATCHUP: {matchup}\n"
            f"NOW (UTC): {timestamp}\n\n"
            f"OBSERVATIONS SUMMARY:\n```json\n"
            f"{json.dumps(observations_summary, indent=2, sort_keys=True)}\n```\n\n"
            f"Regenerate the playbook for {matchup}. Apply lessons from the "
            "recurring_loss_tags. Keep build orders grounded in standard "
            "Terran openings. Return only the JSON playbook in a single "
            "```json``` code fence."
        ),
        # Volatile content — no cache marker. This is what changes per call.
    })

    return {
        "system": [
            {
                "type": "text",
                "text": system_prompt,
                # Cache breakpoint #1: schema + persona is the largest stable
                # block in the prefix. Render order puts system BEFORE
                # messages, so this is the first cacheable point.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": user_blocks}],
    }


def regenerate_playbook(
    matchup: str,
    observations_summary: dict[str, Any],
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    client: Any = None,
    dry_run: bool = False,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Ask Claude to regenerate the playbook for `matchup`.

    Args:
        matchup: 'TvT', 'TvP', or 'TvZ'.
        observations_summary: Output of strategist.observations.summarize().
        model: Anthropic model ID (default Opus 4.7).
        max_tokens: Output token budget. 16k is generous for our schema.
        client: Optional pre-configured Anthropic client (used in tests).
        dry_run: If True, return the request payload without calling the API.
        timestamp: ISO-8601 timestamp to embed in the user message; defaults
            to now (UTC). Pinned in tests for stable output.

    Returns:
        The regenerated playbook dict, validated against the schema.

    Raises:
        FileNotFoundError: schema.json missing.
        jsonschema.ValidationError: model's output is malformed.
        anthropic.APIError: API failure.
    """
    schema = _load_schema()
    prior_playbook = _load_prior_playbook(matchup)

    payload = build_request_payload(
        matchup=matchup,
        observations_summary=observations_summary,
        schema=schema,
        prior_playbook=prior_playbook,
        timestamp=timestamp,
    )

    if dry_run:
        return {"_dry_run": True, **payload}

    if client is None:
        import anthropic  # noqa: PLC0415 (deferred so module imports cleanly)

        client = anthropic.Anthropic()

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=payload["system"],
        messages=payload["messages"],
    )

    text = "".join(block.text for block in response.content if block.type == "text")

    usage = getattr(response, "usage", None)
    if usage is not None:
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        logger.info(
            "strategist tokens: input=%d output=%d cache_read=%d cache_write=%d",
            usage.input_tokens,
            usage.output_tokens,
            cache_read,
            cache_write,
        )

    playbook = extract_json_from_response(text)
    _validate_playbook(playbook, schema)
    return playbook
