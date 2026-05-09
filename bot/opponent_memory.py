"""Tier-2 adaptation: per-(opponent, matchup) priors stored in aiarena's
persistent ./data directory and read at game start.

Pure standard-library module — no ares / python-sc2 dependencies — so it's
fully unit-testable on its own. The bot wires this module into on_start
(load + apply priors) and on_end (record observation, recompute, save).

Schema: see playbook/opponent_schema.json (v0.2).
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "0.2"
DEFAULT_DATA_DIR = Path("./data/opponents")
MAX_OBSERVATIONS = 50
CONFIDENCE_FULL_AT = 20  # match count at which confidence saturates to 1.0


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-]")


def sanitize_name(name: str) -> str:
    """Sanitize an opponent name for use in a filename. Anything outside
    [A-Za-z0-9_-] becomes '_'. Empty or all-bad input becomes 'unknown'."""
    cleaned = _FILENAME_SAFE_RE.sub("_", name or "")
    return cleaned or "unknown"


def file_path(opponent_name: str, matchup: str, data_dir: Path | str = DEFAULT_DATA_DIR) -> Path:
    safe = sanitize_name(opponent_name)
    return Path(data_dir) / f"{safe}_{matchup}.json"


# ---------------------------------------------------------------------------
# State construction & I/O
# ---------------------------------------------------------------------------

def empty_state(opponent_name: str, matchup: str) -> dict[str, Any]:
    """Return a fresh priors-state dict matching the v0.2 opponent schema."""
    return {
        "schema_version": SCHEMA_VERSION,
        "opponent_name": opponent_name,
        "matchup": matchup,
        "match_count": 0,
        "wins": 0,
        "losses": 0,
        "ties": 0,
        "last_seen": _now_iso(),
        "observations": [],
        "derived": {"confidence": 0.0},
    }


def load(
    opponent_name: str,
    matchup: str,
    data_dir: Path | str = DEFAULT_DATA_DIR,
) -> dict[str, Any] | None:
    """Load priors for (opponent, matchup). Returns None if the file is
    missing, unreadable, or fails a basic shape check.

    Defensive on purpose: a corrupt priors file must NEVER crash the bot
    mid-match (it'd auto-loss us). Worst case: we play without priors.
    """
    path = file_path(opponent_name, matchup, data_dir)
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("opponent_memory: failed to read %s (%s); ignoring priors", path, e)
        return None

    # Minimal shape check. The full schema lives in opponent_schema.json
    # but we don't depend on jsonschema at runtime — just check the keys
    # we actually use so a partial / older file doesn't crash us.
    required_top = ("opponent_name", "matchup", "match_count", "observations", "derived")
    if not all(k in state for k in required_top):
        logger.warning("opponent_memory: %s missing required keys; ignoring priors", path)
        return None
    if state.get("matchup") != matchup:
        logger.warning(
            "opponent_memory: %s matchup mismatch (file=%s, expected=%s); ignoring priors",
            path, state.get("matchup"), matchup,
        )
        return None
    return state


def save(state: dict[str, Any], data_dir: Path | str = DEFAULT_DATA_DIR) -> None:
    """Atomically write priors to disk. Writes to a sibling .tmp file then
    renames over the target — no half-written files even if the bot is
    killed mid-write."""
    path = file_path(state["opponent_name"], state["matchup"], data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # If anything went wrong, clean up the tmp file rather than leaving litter
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Observation recording & derivation
# ---------------------------------------------------------------------------

def record_observation(state: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    """Append an observation to state, trim history, recompute derived priors,
    and update top-level counters. Returns the same state dict (mutated)."""
    state["observations"].append(observation)
    if len(state["observations"]) > MAX_OBSERVATIONS:
        state["observations"] = state["observations"][-MAX_OBSERVATIONS:]

    state["match_count"] = state.get("match_count", 0) + 1
    result = observation.get("result")
    if result == "win":
        state["wins"] = state.get("wins", 0) + 1
    elif result == "loss":
        state["losses"] = state.get("losses", 0) + 1
    elif result == "tie":
        state["ties"] = state.get("ties", 0) + 1

    state["last_seen"] = observation.get("timestamp") or _now_iso()
    state["derived"] = compute_derived(state["observations"])
    return state


def compute_derived(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the priors the bot uses at game start from the observation log.

    Simple statistics — nothing fancy. The strategist (offline, with Claude)
    may later overwrite this with smarter analysis; the bot's tier-2
    derivation is intentionally cheap and predictable.
    """
    derived: dict[str, Any] = {"confidence": 0.0}

    n = len(observations)
    if n == 0:
        return derived

    derived["confidence"] = round(min(1.0, n / CONFIDENCE_FULL_AT), 3)

    # First-attack timings: median across observations that captured one
    first_attack_times = [
        obs["their_first_attack"]["seconds"]
        for obs in observations
        if obs.get("their_first_attack") and "seconds" in obs["their_first_attack"]
    ]
    if first_attack_times:
        derived["expected_first_attack_seconds"] = round(
            statistics.median(first_attack_times), 1
        )

    # Expansion timings: same idea, but only count observations that saw an expansion
    expansion_times = [
        obs["their_opening"]["first_expansion_seconds"]
        for obs in observations
        if (obs.get("their_opening") or {}).get("first_expansion_seconds") is not None
    ]
    if expansion_times:
        derived["expected_expansion_seconds"] = round(
            statistics.median(expansion_times), 1
        )
    elif any(
        (obs.get("their_opening") or {}).get("first_expansion_seconds") is None
        for obs in observations
    ):
        # All observations had null expansion — they're an all-iner
        derived["expected_expansion_seconds"] = None

    # Pre-pop reactions: reactions that fired in >50% of WINS only.
    # (A reaction that fired in losses isn't necessarily worth pre-loading;
    # it might've fired too late. We weight wins heavier.)
    wins = [obs for obs in observations if obs.get("result") == "win"]
    if wins:
        reaction_counter: Counter[str] = Counter()
        for obs in wins:
            for r in obs.get("our_scouted_reactions_fired", []) or []:
                reaction_counter[r] += 1
        threshold = max(1, len(wins) // 2)
        prepop = sorted(r for r, c in reaction_counter.items() if c >= threshold)
        if prepop:
            derived["prepop_reactions"] = prepop

    # Expected mid-game composition: average of first-attack composition snapshots.
    comps = [
        obs["their_first_attack"]["composition"]
        for obs in observations
        if obs.get("their_first_attack") and obs["their_first_attack"].get("composition")
    ]
    if comps:
        unit_totals: Counter[str] = Counter()
        for comp in comps:
            for unit, count in comp.items():
                unit_totals[unit] += count
        total_units = sum(unit_totals.values())
        if total_units > 0:
            derived["expected_composition"] = {
                unit: round(count / total_units, 3)
                for unit, count in unit_totals.most_common()
            }

    # Loss patterns: tags that appeared in 2+ losses.
    losses = [obs for obs in observations if obs.get("result") == "loss"]
    if losses:
        loss_tag_counter: Counter[str] = Counter()
        for obs in losses:
            tag = obs.get("our_critical_event")
            if tag:
                loss_tag_counter[tag] += 1
        recurring = sorted(tag for tag, c in loss_tag_counter.items() if c >= 2)
        if recurring:
            derived["loss_patterns"] = recurring

    return derived


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
