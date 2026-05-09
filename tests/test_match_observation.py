"""Unit tests for the match_observation Recorder."""

from __future__ import annotations

import pytest

from bot import match_observation


def test_records_first_seen_structure_only():
    rec = match_observation.Recorder(map_name="Equilibrium LE")
    rec.see_enemy_structure("SpawningPool", our_supply=18)
    rec.see_enemy_structure("SpawningPool", our_supply=22)  # later sighting; ignored
    assert rec.key_buildings_seen == {"SpawningPool": 18}


def test_ignores_unknown_structure_names():
    rec = match_observation.Recorder()
    rec.see_enemy_structure("Floofdragon", our_supply=14)
    assert rec.key_buildings_seen == {}


def test_first_tech_structure_picks_priority_order():
    rec = match_observation.Recorder()
    rec.see_enemy_structure("Hatchery", our_supply=14)
    rec.see_enemy_structure("RoachWarren", our_supply=22)
    rec.see_enemy_structure("HydraliskDen", our_supply=30)
    name, supply = rec.first_tech_structure()
    # RoachWarren ranks higher in the tech priority than HydraliskDen.
    assert name == "RoachWarren"
    assert supply == 22


def test_first_attack_is_recorded_only_once():
    rec = match_observation.Recorder()
    rec.record_first_attack(280.0, {"Roach": 8})
    rec.record_first_attack(360.0, {"Roach": 12})  # ignored
    assert rec.first_attack_seconds == 280.0
    assert rec.first_attack_composition == {"Roach": 8}


def test_to_observation_shape_matches_schema():
    rec = match_observation.Recorder(map_name="Babylon LE")
    rec.see_enemy_structure("SpawningPool", our_supply=17)
    rec.see_enemy_structure("RoachWarren", our_supply=22)
    rec.record_first_attack(285.0, {"Roach": 7, "Zergling": 4})
    rec.reactions_fired = ["mass_roach"]
    rec.critical_event = "engaged_below_critical_mass"

    obs = rec.to_observation(
        result="loss",
        duration_seconds=540.0,
        playbook_version="0.2+manual",
        timestamp="2026-05-08T14:00:00Z",
    )

    assert obs["result"] == "loss"
    assert obs["map"] == "Babylon LE"
    assert obs["duration_seconds"] == 540.0
    assert obs["our_playbook_version"] == "0.2+manual"
    assert obs["timestamp"] == "2026-05-08T14:00:00Z"

    opening = obs["their_opening"]
    assert opening["key_buildings_seen"] == {"SpawningPool": 17, "RoachWarren": 22}
    assert opening["first_tech_structure"] == "RoachWarren"

    attack = obs["their_first_attack"]
    assert attack["seconds"] == 285.0
    assert attack["composition"] == {"Roach": 7, "Zergling": 4}

    assert obs["our_scouted_reactions_fired"] == ["mass_roach"]
    assert obs["our_critical_event"] == "engaged_below_critical_mass"


def test_to_observation_with_no_first_attack_returns_null():
    rec = match_observation.Recorder()
    obs = rec.to_observation(
        result="win",
        duration_seconds=300.0,
        playbook_version="0.2+manual",
    )
    assert obs["their_first_attack"] is None


def test_to_observation_rejects_invalid_result():
    rec = match_observation.Recorder()
    with pytest.raises(ValueError):
        rec.to_observation("victory", 100.0, "0.2+manual")


# ---------------------------------------------------------------------------
# derive_critical_event_tag (pure function)
# ---------------------------------------------------------------------------

def test_derive_critical_event_win():
    tag = match_observation.derive_critical_event_tag(
        result="win", duration_seconds=540, first_attack_seconds=None,
    )
    assert tag == "won_at_9m"


def test_derive_critical_event_early_loss_no_pressure():
    tag = match_observation.derive_critical_event_tag(
        result="loss", duration_seconds=300, first_attack_seconds=None,
    )
    assert tag == "early_loss_at_5m"


def test_derive_critical_event_early_pressure_loss():
    tag = match_observation.derive_critical_event_tag(
        result="loss", duration_seconds=240, first_attack_seconds=180,
    )
    assert tag == "early_pressure_loss_at_4m"


def test_derive_critical_event_mid_loss():
    tag = match_observation.derive_critical_event_tag(
        result="loss", duration_seconds=600, first_attack_seconds=None,
    )
    assert tag == "mid_loss_at_10m"


def test_derive_critical_event_mid_loss_after_early_pressure():
    """An attack at t=3:00 followed by a loss at t=10:00 — they pressured
    us early, we held but then lost the second engagement. Distinct from
    a clean mid-game macro loss."""
    tag = match_observation.derive_critical_event_tag(
        result="loss", duration_seconds=600, first_attack_seconds=180,
    )
    assert tag == "mid_loss_after_early_pressure_at_10m"


def test_derive_critical_event_late_loss():
    tag = match_observation.derive_critical_event_tag(
        result="loss", duration_seconds=900, first_attack_seconds=None,
    )
    assert tag == "late_loss_at_15m"


def test_derive_critical_event_late_loss_no_early_pressure_qualifier():
    """Late losses don't get the 'after_early_pressure' qualifier even if
    the first attack was early — by 15 minutes whether the rush hit or not
    isn't the dominant signal anymore."""
    tag = match_observation.derive_critical_event_tag(
        result="loss", duration_seconds=900, first_attack_seconds=120,
    )
    assert tag == "late_loss_at_15m"


def test_derive_critical_event_method_respects_existing_event():
    """If something already set critical_event in-game, the recorder's
    derive method should keep that value — we only fall back to the tag
    derivation when nothing was recorded."""
    rec = match_observation.Recorder()
    rec.critical_event = "lost_main_to_drop_at_8m"
    out = rec.derive_critical_event_tag(result="loss", duration_seconds=600)
    assert out == "lost_main_to_drop_at_8m"
