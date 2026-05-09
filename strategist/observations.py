"""Load and summarize opponent observations from bot/data/opponents/.

Pure stdlib — no LLM here. The strategist receives a *summary* of recent
matches (winrate, recurring loss tags, which reactions fired), not the raw
observation log. Smaller payload to ship to Claude, easier for the model to
reason over.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "bot" / "data" / "opponents"


def load_observations(
    matchup: str,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> list[dict[str, Any]]:
    """Read every observation across every opponent file matching <matchup>."""
    observations: list[dict[str, Any]] = []
    if not data_dir.is_dir():
        return observations
    for path in sorted(data_dir.glob(f"*_{matchup}.json")):
        try:
            with path.open(encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        observations.extend(state.get("observations", []) or [])
    return observations


def summarize(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate observations into a compact dict for the strategist prompt."""
    if not observations:
        return {
            "match_count": 0,
            "wins": 0,
            "losses": 0,
            "winrate": 0.0,
            "recurring_loss_tags": [],
            "reactions_fired": {},
            "median_match_seconds": None,
            "their_first_attack_median_seconds": None,
        }

    wins = sum(1 for o in observations if o.get("result") == "win")
    losses = sum(1 for o in observations if o.get("result") == "loss")

    crit_events = Counter(
        o["our_critical_event"]
        for o in observations
        if o.get("our_critical_event")
    )
    recurring = [tag for tag, count in crit_events.most_common(8) if count >= 2]

    reactions_fired: Counter[str] = Counter()
    for o in observations:
        for r in o.get("our_scouted_reactions_fired", []) or []:
            reactions_fired[r] += 1

    durations = [
        float(o["duration_seconds"])
        for o in observations
        if o.get("duration_seconds")
    ]
    first_attacks = [
        float(o["their_first_attack"]["seconds"])
        for o in observations
        if o.get("their_first_attack") and o["their_first_attack"].get("seconds")
    ]

    return {
        "match_count": len(observations),
        "wins": wins,
        "losses": losses,
        "winrate": round(wins / len(observations), 3),
        "recurring_loss_tags": recurring,
        "reactions_fired": dict(reactions_fired.most_common()),
        "median_match_seconds": round(median(durations), 1) if durations else None,
        "their_first_attack_median_seconds": (
            round(median(first_attacks), 1) if first_attacks else None
        ),
    }
