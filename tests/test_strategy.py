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


def test_composition_for_ares_aliases_viking_to_vikingfighter():
    """Regression: 'Viking' from a strategist-generated playbook caused a
    KeyError mid-game in cy_unit_pending(VIKING) because UnitTypeId.VIKING
    is an abstract parent type, not in UNIT_TRAINED_FROM. We alias it to
    VIKINGFIGHTER (air) before passing to ares."""
    try:
        from sc2.ids.unit_typeid import UnitTypeId
    except ImportError:
        pytest.skip("python-sc2 not installed")

    out = strategy.composition_for_ares({"Viking": 0.3, "Marine": 0.7}, race="Terran")
    # Viking should land as VIKINGFIGHTER, not VIKING
    assert UnitTypeId.VIKINGFIGHTER in out
    assert UnitTypeId.VIKING not in out


def test_composition_for_ares_drops_abstract_types_not_in_unit_trained_from():
    """Defense-in-depth: if some new abstract type slips past the alias
    table, drop it silently rather than crash SpawnController."""
    try:
        from sc2.dicts.unit_trained_from import UNIT_TRAINED_FROM
        from sc2.ids.unit_typeid import UnitTypeId
    except ImportError:
        pytest.skip("python-sc2 not installed")

    # Sanity: VIKING is in the enum but not in UNIT_TRAINED_FROM. If python-sc2
    # ever adds it, this test no longer guards anything.
    assert UnitTypeId.VIKING not in UNIT_TRAINED_FROM, (
        "python-sc2 added VIKING to UNIT_TRAINED_FROM; revisit the alias logic"
    )

    # Without the alias, a hypothetical playbook with a non-Viking abstract
    # name would still be caught. Hard to construct without faking — the
    # Viking test above already exercises the dropping path implicitly via
    # the alias detour. Keeping this as a documentation guard.


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
    """Sanity check ordering: DEFEND < RETREAT < ATTACK. If RETREAT
    crossed ATTACK we'd never enter the hysteresis branch; if DEFEND
    crossed RETREAT we'd contradict the layering."""
    assert strategy.DEFEND_SUPPLY < strategy.RETREAT_SUPPLY < strategy.ATTACK_SUPPLY


# ---------------------------------------------------------------------------
# decide_army_state — hysteresis state machine
# ---------------------------------------------------------------------------

def test_decide_state_threat_always_defends():
    """Threat present beats every other state."""
    state = strategy.decide_army_state(
        threat_present=True, army_supply_value=200, prev_state="ATTACK",
    )
    assert state == "DEFEND"


def test_decide_state_threat_defends_even_with_no_army():
    state = strategy.decide_army_state(
        threat_present=True, army_supply_value=0, prev_state="HOLD",
    )
    assert state == "DEFEND"


def test_decide_state_fresh_attack_at_threshold():
    """No prior attack, army at ATTACK_SUPPLY -> commit to attack."""
    state = strategy.decide_army_state(
        threat_present=False,
        army_supply_value=strategy.ATTACK_SUPPLY,
        prev_state="HOLD",
    )
    assert state == "ATTACK"


def test_decide_state_below_attack_threshold_holds():
    """No prior attack, army below ATTACK_SUPPLY -> hold and macro."""
    state = strategy.decide_army_state(
        threat_present=False,
        army_supply_value=strategy.ATTACK_SUPPLY - 1,
        prev_state="HOLD",
    )
    assert state == "HOLD"


def test_decide_state_hysteresis_keeps_attacking_above_retreat():
    """The reinforcement fix: once attacking, KEEP attacking even if
    army has dropped below ATTACK_SUPPLY, as long as we're above
    RETREAT_SUPPLY. This is what was broken — bot would bounce to HOLD
    after a lost engagement at army=24, leaving the front empty."""
    state = strategy.decide_army_state(
        threat_present=False,
        army_supply_value=strategy.RETREAT_SUPPLY + 1,
        prev_state="ATTACK",
    )
    assert state == "ATTACK"


def test_decide_state_hysteresis_releases_below_retreat():
    """Stop attacking once army really drops — let it rebuild at home."""
    state = strategy.decide_army_state(
        threat_present=False,
        army_supply_value=strategy.RETREAT_SUPPLY - 1,
        prev_state="ATTACK",
    )
    assert state == "HOLD"


def test_decide_state_hysteresis_does_not_apply_after_defend():
    """After defending and the threat clearing, fall through to normal
    logic (don't 'remember' an old ATTACK state from before the threat).
    This keeps the state machine simple — no reset semantics."""
    state = strategy.decide_army_state(
        threat_present=False,
        army_supply_value=strategy.RETREAT_SUPPLY + 1,
        prev_state="DEFEND",
    )
    # Below ATTACK_SUPPLY and prev wasn't ATTACK -> HOLD
    assert state == "HOLD"


def test_decide_state_hysteresis_does_not_let_us_attack_under_retreat_from_hold():
    """If we WEREN'T already attacking, having army at RETREAT_SUPPLY (a low
    threshold) doesn't get us to ATTACK — we need the full ATTACK_SUPPLY
    to commit fresh. This stops the bot from suicidal mini-attacks."""
    state = strategy.decide_army_state(
        threat_present=False,
        army_supply_value=strategy.RETREAT_SUPPLY,
        prev_state="HOLD",
    )
    assert state == "HOLD"
