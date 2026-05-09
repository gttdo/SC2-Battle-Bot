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
