"""Unit tests for the per-opponent priors module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot import opponent_memory


# ---------------------------------------------------------------------------
# Filename / path helpers
# ---------------------------------------------------------------------------

def test_sanitize_name_strips_unsafe_chars():
    assert opponent_memory.sanitize_name("BlinkerBot") == "BlinkerBot"
    assert opponent_memory.sanitize_name("foo bar/baz") == "foo_bar_baz"
    assert opponent_memory.sanitize_name("") == "unknown"
    assert opponent_memory.sanitize_name("///") == "___"  # at least it's a stable string


def test_file_path_combines_name_and_matchup(tmp_path):
    p = opponent_memory.file_path("Spiny", "TvZ", data_dir=tmp_path)
    assert p == tmp_path / "Spiny_TvZ.json"


# ---------------------------------------------------------------------------
# Load / save / round-trip
# ---------------------------------------------------------------------------

def test_load_returns_none_for_missing_file(tmp_path):
    assert opponent_memory.load("Nobody", "TvZ", data_dir=tmp_path) is None


def test_save_then_load_roundtrips(tmp_path):
    state = opponent_memory.empty_state("Spiny", "TvZ")
    opponent_memory.save(state, data_dir=tmp_path)
    loaded = opponent_memory.load("Spiny", "TvZ", data_dir=tmp_path)
    assert loaded is not None
    assert loaded["opponent_name"] == "Spiny"
    assert loaded["matchup"] == "TvZ"
    assert loaded["match_count"] == 0


def test_save_is_atomic_no_tmp_left_behind(tmp_path):
    state = opponent_memory.empty_state("Spiny", "TvZ")
    opponent_memory.save(state, data_dir=tmp_path)
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == [], f"atomic write left tmp files: {leftovers}"


def test_load_rejects_corrupt_json(tmp_path):
    bad = tmp_path / "Crash_TvZ.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not valid json", encoding="utf-8")
    assert opponent_memory.load("Crash", "TvZ", data_dir=tmp_path) is None


def test_load_rejects_matchup_mismatch(tmp_path):
    state = opponent_memory.empty_state("Spiny", "TvZ")
    opponent_memory.save(state, data_dir=tmp_path)
    # Trying to load it as TvP should refuse — wrong matchup.
    assert opponent_memory.load("Spiny", "TvP", data_dir=tmp_path) is None


def test_load_rejects_missing_required_keys(tmp_path):
    bad = tmp_path / "Old_TvZ.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(json.dumps({"opponent_name": "Old"}), encoding="utf-8")
    assert opponent_memory.load("Old", "TvZ", data_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# record_observation + compute_derived
# ---------------------------------------------------------------------------

def _make_obs(result: str, **overrides):
    base = {
        "timestamp": "2026-05-08T00:00:00Z",
        "result": result,
        "map": "Equilibrium LE",
        "duration_seconds": 600,
        "our_playbook_version": "0.2+manual",
        "their_first_attack": {
            "seconds": 280,
            "composition": {"Roach": 8, "Zergling": 6},
        },
        "their_opening": {"first_expansion_seconds": 92},
        "our_scouted_reactions_fired": ["mass_roach"],
        "our_critical_event": "engaged_roach_ball_below_critical_mass",
    }
    base.update(overrides)
    return base


def test_record_observation_increments_counters():
    state = opponent_memory.empty_state("Spiny", "TvZ")
    opponent_memory.record_observation(state, _make_obs("loss"))
    opponent_memory.record_observation(state, _make_obs("loss"))
    opponent_memory.record_observation(state, _make_obs("win"))
    assert state["match_count"] == 3
    assert state["losses"] == 2
    assert state["wins"] == 1
    assert state["ties"] == 0


def test_record_observation_trims_to_max():
    state = opponent_memory.empty_state("Spiny", "TvZ")
    for _ in range(opponent_memory.MAX_OBSERVATIONS + 5):
        opponent_memory.record_observation(state, _make_obs("loss"))
    assert len(state["observations"]) == opponent_memory.MAX_OBSERVATIONS


def test_compute_derived_confidence_scales_with_count():
    # Empty
    assert opponent_memory.compute_derived([])["confidence"] == 0.0
    # Half-saturated at half the threshold
    half = opponent_memory.compute_derived(
        [_make_obs("loss") for _ in range(opponent_memory.CONFIDENCE_FULL_AT // 2)]
    )
    assert 0.4 < half["confidence"] < 0.6
    # Saturated above the threshold
    full = opponent_memory.compute_derived(
        [_make_obs("loss") for _ in range(opponent_memory.CONFIDENCE_FULL_AT * 2)]
    )
    assert full["confidence"] == 1.0


def test_compute_derived_first_attack_median():
    obs = [
        _make_obs("loss", their_first_attack={"seconds": 200, "composition": {"Roach": 5}}),
        _make_obs("loss", their_first_attack={"seconds": 280, "composition": {"Roach": 8}}),
        _make_obs("win",  their_first_attack={"seconds": 360, "composition": {"Roach": 10}}),
    ]
    derived = opponent_memory.compute_derived(obs)
    assert derived["expected_first_attack_seconds"] == 280.0


def test_compute_derived_prepop_reactions_from_wins_only():
    obs = [
        # Won when mass_roach fired; reaction should pre-pop
        _make_obs("win",  our_scouted_reactions_fired=["mass_roach"]),
        _make_obs("win",  our_scouted_reactions_fired=["mass_roach", "ling_flood"]),
        # ling_flood only fired in 1 of 2 wins -> below half threshold
        # Losses don't count toward prepop
        _make_obs("loss", our_scouted_reactions_fired=["nydus", "mutas"]),
    ]
    derived = opponent_memory.compute_derived(obs)
    assert "mass_roach" in derived["prepop_reactions"]
    # nydus/mutas only appeared in losses — should not pre-pop
    assert "nydus" not in derived["prepop_reactions"]
    assert "mutas" not in derived["prepop_reactions"]


def test_compute_derived_loss_patterns_threshold():
    obs = [
        _make_obs("loss", our_critical_event="engaged_below_critical_mass"),
        _make_obs("loss", our_critical_event="engaged_below_critical_mass"),
        _make_obs("loss", our_critical_event="other_thing"),
    ]
    derived = opponent_memory.compute_derived(obs)
    assert derived["loss_patterns"] == ["engaged_below_critical_mass"]


def test_compute_derived_expected_composition_normalizes_to_ratios():
    obs = [
        _make_obs("loss", their_first_attack={
            "seconds": 280, "composition": {"Roach": 8, "Zergling": 2},
        }),
        _make_obs("loss", their_first_attack={
            "seconds": 290, "composition": {"Roach": 6, "Zergling": 4},
        }),
    ]
    derived = opponent_memory.compute_derived(obs)
    comp = derived["expected_composition"]
    assert abs(sum(comp.values()) - 1.0) < 0.01
    # Roach should dominate (14/20 of total units)
    assert comp["Roach"] > comp["Zergling"]


def test_compute_derived_all_null_expansion_marks_allin():
    obs = [
        _make_obs("loss", their_opening={"first_expansion_seconds": None}),
        _make_obs("loss", their_opening={"first_expansion_seconds": None}),
    ]
    derived = opponent_memory.compute_derived(obs)
    assert derived["expected_expansion_seconds"] is None


def test_record_then_save_then_reload_preserves_derived(tmp_path):
    state = opponent_memory.empty_state("Spiny", "TvZ")
    opponent_memory.record_observation(state, _make_obs("loss"))
    opponent_memory.record_observation(state, _make_obs("win"))
    opponent_memory.save(state, data_dir=tmp_path)

    loaded = opponent_memory.load("Spiny", "TvZ", data_dir=tmp_path)
    assert loaded is not None
    assert loaded["match_count"] == 2
    assert loaded["wins"] == 1
    assert loaded["losses"] == 1
    assert loaded["derived"]["confidence"] > 0
