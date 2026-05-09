"""Post-opening strategy: continuous production, phase selection, army control.

Drives the bot after ares's BuildRunner finishes the scripted opening. Reads
`composition_targets` from the loaded playbook to keep producing units in the
right ratios as the game progresses through early -> mid -> late phases, and
manages a single combat group that holds at home until reaching an attack-
supply threshold, then pushes to the enemy main.

Imports from `ares.*` and `sc2.*` are deferred to the call sites so this
module stays importable without ares-sc2 on the path (tests can exercise
the pure helpers without a full game context).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ares import AresBot
    from sc2.position import Point2

logger = logging.getLogger(__name__)

# Phase boundaries in our supply count. v0 hand-tuned; the strategist will
# eventually drive these per matchup.
EARLY_END_SUPPLY = 50
MID_END_SUPPLY = 120

# Army state-machine thresholds (combat-unit supply).
ATTACK_SUPPLY = 60
DEFEND_SUPPLY = 25

# Approximate supply costs for fast army-supply summation. Static table is
# faster than calling calculate_supply_cost per unit per step.
TERRAN_UNIT_SUPPLY: dict[str, int] = {
    "MARINE": 1, "REAPER": 1,
    "MARAUDER": 2, "MEDIVAC": 2, "GHOST": 2,
    "WIDOWMINE": 2, "WIDOWMINEBURROWED": 2,
    "HELLION": 2, "HELLIONTANK": 2,
    "VIKINGFIGHTER": 2, "VIKINGASSAULT": 2,
    "RAVEN": 2,
    "LIBERATOR": 3, "LIBERATORAG": 3,
    "SIEGETANK": 3, "SIEGETANKSIEGED": 3,
    "CYCLONE": 3, "BANSHEE": 3,
    "THOR": 6, "THORAP": 6, "BATTLECRUISER": 6,
}

# Combat-unit type names per race (v0 = Terran only). Used to filter our
# units down to the army.
TERRAN_COMBAT_NAMES: frozenset[str] = frozenset(TERRAN_UNIT_SUPPLY.keys())


def select_phase(supply_used: int) -> str:
    """Return 'early', 'mid', or 'late' for the current supply count."""
    if supply_used < EARLY_END_SUPPLY:
        return "early"
    if supply_used < MID_END_SUPPLY:
        return "mid"
    return "late"


def composition_for_ares(
    comp_map: dict[str, float],
) -> dict[Any, dict[str, Any]]:
    """Convert our playbook's `{pascal_str: ratio}` into ares's
    `{UnitTypeId: {proportion, priority}}` format.

    Priority is assigned by descending proportion: the unit with the largest
    target ratio gets priority 0 (highest in ares's SpawnController).
    Capped at 10 because ares asserts `priority < 11`.

    Unit names that don't exist in python-sc2's UnitTypeId enum are silently
    dropped — schema validation should have caught typos upstream.
    """
    try:
        from sc2.ids.unit_typeid import UnitTypeId
    except ImportError:
        return {}

    out: dict[Any, dict[str, Any]] = {}
    sorted_items = sorted(comp_map.items(), key=lambda kv: -kv[1])
    for i, (name, prop) in enumerate(sorted_items):
        try:
            uid = UnitTypeId[name.upper()]
        except KeyError:
            logger.warning("strategy: unknown unit name %r in comp_map; skipping", name)
            continue
        out[uid] = {"proportion": float(prop), "priority": min(i, 10)}
    return out


def army_supply_from_unit_names(unit_type_names: list[str]) -> int:
    """Sum approximate supply for a list of unit-type-name strings (PascalCase
    or UPPERCASE). Pure function — testable without sc2 installed."""
    total = 0
    for name in unit_type_names:
        total += TERRAN_UNIT_SUPPLY.get(name.upper(), 1)
    return total


def army_units(bot: "AresBot") -> list:
    """Return our combat units (excludes workers, townhalls, depots, addons)."""
    return [
        u for u in bot.units
        if u.type_id is not None and u.type_id.name in TERRAN_COMBAT_NAMES
    ]


def army_supply(units: list) -> int:
    """Sum approximate supply for a list of Unit objects."""
    return army_supply_from_unit_names([u.type_id.name for u in units if u.type_id])


def attack_target(bot: "AresBot") -> "Point2":
    """Where to push when we hit attack threshold."""
    if bot.enemy_start_locations:
        return bot.enemy_start_locations[0]
    return bot.game_info.map_center


def hold_position(bot: "AresBot") -> "Point2":
    """Where to gather while building army — between our base and the enemy
    so defenders can engage incoming attacks before reaching production."""
    main = bot.start_location
    if bot.enemy_start_locations:
        return main.towards(bot.enemy_start_locations[0], distance=15)
    return main


def update(bot: "AresBot", playbook: dict[str, Any] | None) -> None:
    """Per-step driver. Registers SpawnController, ProductionController, and
    a single AMoveGroup behavior based on current state. Idempotent — ares
    expects behaviors to be re-registered each step.
    """
    if playbook is None:
        return

    phase = select_phase(int(getattr(bot, "supply_used", 0)))
    comp_targets = playbook.get("composition_targets", {}).get(phase, {})
    if not comp_targets:
        return

    ares_comp = composition_for_ares(comp_targets)
    if not ares_comp:
        return

    # Continuous unit production toward target composition.
    try:
        from ares.behaviors.macro.spawn_controller import SpawnController
        from ares.behaviors.macro.production_controller import ProductionController

        bot.register_behavior(SpawnController(army_composition_dict=ares_comp))
        bot.register_behavior(
            ProductionController(
                army_composition_dict=ares_comp,
                base_location=bot.start_location,
            )
        )
    except Exception:  # pragma: no cover — never crash the bot from strategy
        logger.exception("strategy: SpawnController/ProductionController register failed")

    # Army control: hold or attack as a single group.
    units = army_units(bot)
    if not units:
        return

    a_supply = army_supply(units)
    target = attack_target(bot) if a_supply >= ATTACK_SUPPLY else hold_position(bot)

    try:
        from ares.behaviors.combat.group.a_move_group import AMoveGroup

        bot.register_behavior(
            AMoveGroup(
                group=units,
                group_tags={u.tag for u in units},
                target=target,
            )
        )
    except Exception:  # pragma: no cover
        logger.exception("strategy: AMoveGroup register failed")
