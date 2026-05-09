"""Tests for strategist.observations — pure functions, no API."""

from __future__ import annotations

import json
from pathlib import Path

from strategist import observations


def _write_opponent_file(
    tmp_path: Path,
    name: str,
    matchup: str,
    obs_list: list[dict],
) -> None:
    state = {
        "schema_version": "0.2",
        "opponent_name": name,
        "matchup": matchup,
        "match_count": len(obs_list),
        "wins": sum(1 for o in obs_list if o["result"] == "win"),
        "losses": sum(1 for o in obs_list if o["result"] == "loss"),
        "ties": 0,
        "last_seen": "2026-05-09T00:00:00Z",
        "observations": obs_list,
        "derived": {"confidence": 0.5},
    }
    p = tmp_path / f"{name}_{matchup}.json"
    p.write_text(json.dumps(state), encoding="utf-8")


def _obs(result: str, **overrides) -> dict:
    base = {
        "timestamp": "2026-05-09T00:00:00Z",
        "result": result,
        "map": "Pylon AIE",
        "duration_seconds": 600,
        "our_playbook_version": "0.2+manual",
        "their_first_attack": {"seconds": 280, "composition": {"Roach": 8}},
        "our_scouted_reactions_fired": [],
        "our_critical_event": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# load_observations
# ---------------------------------------------------------------------------

def test_load_observations_returns_empty_when_dir_missing(tmp_path):
    missing = tmp_path / "nope"
    assert observations.load_observations("TvZ", data_dir=missing) == []


def test_load_observations_filters_by_matchup(tmp_path):
    _write_opponent_file(tmp_path, "OppA", "TvZ", [_obs("win"), _obs("loss")])
    _write_opponent_file(tmp_path, "OppB", "TvP", [_obs("win")])
    obs = observations.load_observations("TvZ", data_dir=tmp_path)
    assert len(obs) == 2  # only TvZ file's observations


def test_load_observations_aggregates_across_files(tmp_path):
    _write_opponent_file(tmp_path, "Alpha", "TvZ", [_obs("win")])
    _write_opponent_file(tmp_path, "Bravo", "TvZ", [_obs("loss"), _obs("loss")])
    obs = observations.load_observations("TvZ", data_dir=tmp_path)
    assert len(obs) == 3


def test_load_observations_skips_corrupt_files(tmp_path):
    bad = tmp_path / "Crash_TvZ.json"
    bad.write_text("{not json", encoding="utf-8")
    _write_opponent_file(tmp_path, "Good", "TvZ", [_obs("win")])
    obs = observations.load_observations("TvZ", data_dir=tmp_path)
    assert len(obs) == 1  # corrupt file silently skipped


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------

def test_summarize_empty_returns_zero_stats():
    summary = observations.summarize([])
    assert summary["match_count"] == 0
    assert summary["winrate"] == 0.0
    assert summary["recurring_loss_tags"] == []
    assert summary["reactions_fired"] == {}


def test_summarize_winrate_arithmetic():
    obs = [_obs("win"), _obs("win"), _obs("loss")]
    summary = observations.summarize(obs)
    assert summary["match_count"] == 3
    assert summary["wins"] == 2
    assert summary["losses"] == 1
    assert abs(summary["winrate"] - 0.667) < 0.005


def test_summarize_recurring_loss_tags_threshold():
    """A tag must appear in >=2 losses to count as recurring."""
    obs = [
        _obs("loss", our_critical_event="engaged_below_critical_mass"),
        _obs("loss", our_critical_event="engaged_below_critical_mass"),
        _obs("loss", our_critical_event="rare_thing"),
    ]
    summary = observations.summarize(obs)
    assert "engaged_below_critical_mass" in summary["recurring_loss_tags"]
    assert "rare_thing" not in summary["recurring_loss_tags"]


def test_summarize_reactions_fired_counts():
    obs = [
        _obs("win", our_scouted_reactions_fired=["mass_roach"]),
        _obs("loss", our_scouted_reactions_fired=["mass_roach", "ling_flood"]),
        _obs("loss", our_scouted_reactions_fired=[]),
    ]
    summary = observations.summarize(obs)
    assert summary["reactions_fired"]["mass_roach"] == 2
    assert summary["reactions_fired"]["ling_flood"] == 1


def test_summarize_medians_pull_from_durations_and_attacks():
    obs = [
        _obs("loss", duration_seconds=300),
        _obs("loss", duration_seconds=600),
        _obs("loss", duration_seconds=900),
    ]
    summary = observations.summarize(obs)
    assert summary["median_match_seconds"] == 600.0
    # All three obs have first_attack at seconds=280 by default
    assert summary["their_first_attack_median_seconds"] == 280.0


def test_summarize_handles_missing_first_attack():
    obs = [
        _obs("loss", their_first_attack=None),
        _obs("loss", their_first_attack=None),
    ]
    summary = observations.summarize(obs)
    assert summary["their_first_attack_median_seconds"] is None
