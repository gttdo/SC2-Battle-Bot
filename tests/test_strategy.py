"""Tests for the post-opening strategy helpers.

The strategy module's per-step entry point (`update`) is hard to unit-test
without ares-sc2 + a live game, but the supporting pure functions
(phase selection, composition mapping, supply accounting) are testable in
isolation."""

from __future__ import annotations

import pytest

from bot import strategy


# ---------------------------------------------------------------------------
# Phase selection
# ---------------------------------------------------------------------------

def test_select_phase_thresholds():
    assert strategy.select_phase(0) == "early"
    assert strategy.select_phase(strategy.EARLY_END_SUPPLY - 1) == "early"
    assert strategy.select_phase(strategy.EARLY_END_SUPPLY) == "mid"
    assert strategy.select_phase(strategy.MID_END_SUPPLY - 1) == "mid"
    assert strategy.select_phase(strategy.MID_END_SUPPLY) == "late"
    assert strategy.select_phase(200) == "late"


# ---------------------------------------------------------------------------
# composition_for_ares
# ---------------------------------------------------------------------------

def test_composition_for_ares_assigns_priority_by_proportion():
    """Highest proportion -> priority 0 (top), lowest -> highest priority int."""
    try:
        from sc2.ids.unit_typeid import UnitTypeId
    except ImportError:
        pytest.skip("python-sc2 not installed")

    out = strategy.composition_for_ares({
        "Marauder": 0.25,
        "Marine": 0.6,
        "Medivac": 0.15,
    })

    assert out[UnitTypeId.MARINE]["priority"] == 0      # biggest proportion
    assert out[UnitTypeId.MARAUDER]["priority"] == 1
    assert out[UnitTypeId.MEDIVAC]["priority"] == 2     # smallest proportion

    # Proportions preserved as floats
    assert abs(out[UnitTypeId.MARINE]["proportion"] - 0.6) < 1e-9
    assert abs(out[UnitTypeId.MEDIVAC]["proportion"] - 0.15) < 1e-9


def test_composition_for_ares_drops_unknown_units():
    try:
        from sc2.ids.unit_typeid import UnitTypeId
    except ImportError:
        pytest.skip("python-sc2 not installed")

    out = strategy.composition_for_ares({
        "Marine": 0.5,
        "FloofDragon": 0.5,  # not a real UnitTypeId
    })

    assert UnitTypeId.MARINE in out
    assert len(out) == 1


def test_composition_for_ares_empty_input_returns_empty():
    assert strategy.composition_for_ares({}) == {}


def test_composition_for_ares_caps_priority_at_10():
    """ares asserts priority < 11, so even with 15 unit types the highest
    priority value should never exceed 10."""
    try:
        from sc2.ids.unit_typeid import UnitTypeId  # noqa: F401
    except ImportError:
        pytest.skip("python-sc2 not installed")

    # 15 distinct Terran units with descending proportions
    big_comp = {name: 1.0 - i * 0.01 for i, name in enumerate([
        "Marine", "Marauder", "Medivac", "Liberator", "WidowMine",
        "Ghost", "SiegeTank", "Hellion", "Cyclone", "Thor",
        "VikingFighter", "Banshee", "Raven", "Battlecruiser", "Reaper",
    ])}
    out = strategy.composition_for_ares(big_comp)
    priorities = [info["priority"] for info in out.values()]
    assert max(priorities) == 10
    assert min(priorities) == 0


# ---------------------------------------------------------------------------
# Army-supply accounting
# ---------------------------------------------------------------------------

def test_army_supply_from_unit_names_basic():
    # 3 marines (1 supply each) + 2 marauders (2 each) + 1 medivac (2) = 9
    assert strategy.army_supply_from_unit_names(
        ["MARINE", "MARINE", "MARINE", "MARAUDER", "MARAUDER", "MEDIVAC"]
    ) == 9


def test_army_supply_from_unit_names_handles_pascal_case():
    """We accept either PascalCase or UPPERCASE name strings."""
    assert strategy.army_supply_from_unit_names(["Marine", "Marauder"]) == 3


def test_army_supply_from_unit_names_unknown_defaults_to_one():
    """An unrecognized unit name shouldn't crash; default to 1 supply so we
    don't undercount a fielded combat unit."""
    assert strategy.army_supply_from_unit_names(["FloofDragon"]) == 1


def test_army_supply_thresholds_are_consistent():
    """Sanity check that DEFEND_SUPPLY < ATTACK_SUPPLY so we don't push
    when we should be pulling back."""
    assert strategy.DEFEND_SUPPLY < strategy.ATTACK_SUPPLY
