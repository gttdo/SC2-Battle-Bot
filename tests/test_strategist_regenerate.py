"""Tests for strategist.regenerate.

The Anthropic API call is mocked — we don't hit the network in CI. What
we DO test:
  - JSON extraction from code-fenced responses (the model's actual output
    shape).
  - Cache-control breakpoint placement on the request payload.
  - Schema validation rejects malformed playbooks.
  - dry_run returns the payload structure without calling anything.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from strategist import regenerate as regen


# ---------------------------------------------------------------------------
# extract_json_from_response — handles model output shape
# ---------------------------------------------------------------------------

def test_extract_json_from_code_fence():
    text = """Here's the new playbook:

```json
{"hello": "world"}
```

Done."""
    # Note: model is told NOT to add prose, but our parser tolerates it
    # since we still want to extract the JSON if it slips through.
    assert regen.extract_json_from_response(text) == {"hello": "world"}


def test_extract_json_handles_bare_fence():
    text = "```\n{\"k\": 1}\n```"
    assert regen.extract_json_from_response(text) == {"k": 1}


def test_extract_json_handles_naked_json():
    """If the model perfectly obeys the no-prose rule, output is naked JSON."""
    text = '{"naked": true}'
    assert regen.extract_json_from_response(text) == {"naked": True}


def test_extract_json_raises_on_garbage():
    with pytest.raises(json.JSONDecodeError):
        regen.extract_json_from_response("totally not json")


# ---------------------------------------------------------------------------
# build_request_payload — caching + structure
# ---------------------------------------------------------------------------

def _load_real_schema():
    project_root = Path(__file__).resolve().parent.parent
    with (project_root / "playbook" / "schema.json").open(encoding="utf-8") as f:
        return json.load(f)


def test_payload_has_two_cache_breakpoints_when_prior_playbook_exists():
    """We expect cache_control on (1) system prompt and (2) prior playbook."""
    schema = _load_real_schema()
    prior_playbook = {"metadata": {"matchup": "TvZ"}, "build_order": []}
    summary = {"match_count": 5, "wins": 2, "losses": 3}

    payload = regen.build_request_payload(
        matchup="TvZ",
        observations_summary=summary,
        schema=schema,
        prior_playbook=prior_playbook,
        timestamp="2026-05-09T12:00:00Z",
    )

    # System: one block with cache_control
    assert len(payload["system"]) == 1
    assert payload["system"][0].get("cache_control") == {"type": "ephemeral"}

    # User: prior playbook (cached) + observations (uncached)
    user_blocks = payload["messages"][0]["content"]
    assert len(user_blocks) == 2
    assert user_blocks[0].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in user_blocks[1]


def test_payload_has_one_cache_breakpoint_when_no_prior_playbook():
    schema = _load_real_schema()
    summary = {"match_count": 0, "wins": 0, "losses": 0}

    payload = regen.build_request_payload(
        matchup="TvZ",
        observations_summary=summary,
        schema=schema,
        prior_playbook=None,
        timestamp="2026-05-09T12:00:00Z",
    )

    # System still cached, but only one user block (no prior playbook)
    assert payload["system"][0].get("cache_control") == {"type": "ephemeral"}
    assert len(payload["messages"][0]["content"]) == 1
    assert "cache_control" not in payload["messages"][0]["content"][0]


def test_payload_includes_matchup_and_timestamp_in_user_message():
    schema = _load_real_schema()
    payload = regen.build_request_payload(
        matchup="TvZ",
        observations_summary={"match_count": 1, "wins": 0, "losses": 1},
        schema=schema,
        prior_playbook=None,
        timestamp="2026-05-09T12:00:00Z",
    )
    user_text = payload["messages"][0]["content"][0]["text"]
    assert "TvZ" in user_text
    assert "2026-05-09T12:00:00Z" in user_text


def test_payload_system_prompt_serializes_schema_deterministically():
    """sort_keys=True ensures byte-stable cache key across Python runs."""
    schema = _load_real_schema()
    p1 = regen.build_request_payload(
        matchup="TvZ",
        observations_summary={"match_count": 0, "wins": 0, "losses": 0},
        schema=schema,
        prior_playbook=None,
        timestamp="2026-05-09T12:00:00Z",
    )
    p2 = regen.build_request_payload(
        matchup="TvZ",
        observations_summary={"match_count": 0, "wins": 0, "losses": 0},
        schema=schema,
        prior_playbook=None,
        timestamp="2026-05-09T12:00:00Z",
    )
    assert p1["system"][0]["text"] == p2["system"][0]["text"]


# ---------------------------------------------------------------------------
# regenerate_playbook — dry-run + mocked client
# ---------------------------------------------------------------------------

def test_regenerate_dry_run_returns_payload_without_api_call():
    summary = {"match_count": 1, "wins": 0, "losses": 1}
    result = regen.regenerate_playbook("TvZ", summary, dry_run=True)
    assert result["_dry_run"] is True
    assert "system" in result
    assert "messages" in result


def test_regenerate_validates_model_output_against_schema():
    """A response that returns valid playbook JSON should round-trip cleanly."""
    project_root = Path(__file__).resolve().parent.parent
    with (project_root / "playbook" / "tvz.json").open(encoding="utf-8") as f:
        valid_playbook = json.load(f)

    fake_text_block = SimpleNamespace(
        type="text",
        text=f"```json\n{json.dumps(valid_playbook)}\n```",
    )
    fake_response = SimpleNamespace(
        content=[fake_text_block],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=200,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=1500,
        ),
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response

    result = regen.regenerate_playbook(
        "TvZ",
        {"match_count": 1, "wins": 0, "losses": 1},
        client=fake_client,
        timestamp="2026-05-09T12:00:00Z",
    )

    assert result["metadata"]["matchup"] == "TvZ"
    fake_client.messages.create.assert_called_once()


def test_regenerate_rejects_malformed_playbook():
    """If the model emits valid JSON but it doesn't match the playbook
    schema, we surface a ValidationError so the caller can decide what to
    do (retry, fall back to prior playbook, etc.)."""
    from jsonschema import ValidationError

    bad_text = SimpleNamespace(
        type="text",
        text='```json\n{"not_a_playbook": true}\n```',
    )
    fake_response = SimpleNamespace(
        content=[bad_text],
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=10,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response

    with pytest.raises(ValidationError):
        regen.regenerate_playbook(
            "TvZ",
            {"match_count": 1, "wins": 0, "losses": 1},
            client=fake_client,
            timestamp="2026-05-09T12:00:00Z",
        )


def test_regenerate_passes_correct_anthropic_kwargs():
    """Verify we're calling the API with adaptive thinking + opus 4.7 +
    the cached payload. Catches regressions if someone refactors and drops
    the cache_control or thinking config."""
    project_root = Path(__file__).resolve().parent.parent
    with (project_root / "playbook" / "tvz.json").open(encoding="utf-8") as f:
        valid_playbook = json.load(f)

    fake_text = SimpleNamespace(
        type="text",
        text=f"```json\n{json.dumps(valid_playbook)}\n```",
    )
    fake_response = SimpleNamespace(
        content=[fake_text],
        usage=SimpleNamespace(
            input_tokens=10, output_tokens=10,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response

    regen.regenerate_playbook(
        "TvZ",
        {"match_count": 1, "wins": 0, "losses": 1},
        client=fake_client,
        timestamp="2026-05-09T12:00:00Z",
    )

    call_kwargs = fake_client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == "claude-opus-4-7"
    assert call_kwargs["thinking"] == {"type": "adaptive"}
    assert call_kwargs["output_config"] == {"effort": "high"}
    # Cache breakpoints survived the call
    assert call_kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
