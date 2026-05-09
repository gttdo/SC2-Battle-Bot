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

from typing import TYPE_CHECKING, Any

# loguru is used everywhere else in the bot stack (ares, run.py, sc2)
# so we route through it too — keeps log lines on a single stream and
# visible at INFO without needing stdlib logging config.
from loguru import logger

if TYPE_CHECKING:
    from ares import AresBot
    from sc2.position import Point2

# Phase boundaries in our supply count. v0 hand-tuned; the strategist will
# eventually drive these per matchup.
EARLY_END_SUPPLY = 50
MID_END_SUPPLY = 120

# Army state-machine thresholds (combat-unit supply). v0 is intentionally
# trigger-happy: 5 supply (~3 marines or 1 marauder + 1 marine + 1 reaper)
# is enough to push. Real strategy scales this up; v0 prioritizes the bot
# actually doing something visible over winning. Tune as production scales.
ATTACK_SUPPLY = 5
DEFEND_SUPPLY = 2

# How often (in game-seconds) to emit a strategy status log line. Avoids
# spamming once-per-step but gives us enough signal to diagnose army flow.
LOG_INTERVAL_SECONDS = 30.0
DIAG_INTERVAL_SECONDS = 15.0  # tighter cadence for production diagnostics

# Production / economy structures we count when diagnosing stalls.
TERRAN_PRODUCTION_NAMES: tuple[str, ...] = (
    "BARRACKS", "FACTORY", "STARPORT",
)
TERRAN_TECHLAB_NAMES: tuple[str, ...] = (
    "BARRACKSTECHLAB", "FACTORYTECHLAB", "STARPORTTECHLAB",
)
TERRAN_REACTOR_NAMES: tuple[str, ...] = (
    "BARRACKSREACTOR", "FACTORYREACTOR", "STARPORTREACTOR",
)

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
            logger.warning(f"strategy: unknown unit name {name!r} in comp_map; skipping")
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
    """Where to gather while building army. v0 holds at our main — units
    walking forward to a midmap rally got picked off before reaching critical
    mass last game. Defending in the main means production buildings are
    auto-defended by the army that just spawned from them."""
    return bot.start_location


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
    # `freeflow_mode=True` is intentional for v0: SpawnController's proportion
    # mode breaks out of the queue loop the moment it can't afford the next
    # priority unit. With a gas-cost unit (Reaper, Marauder) high in the
    # priority list and sub-50 gas income, that means cheap Marines NEVER
    # queue from idle Barracks even when sitting on 4000+ minerals. Freeflow
    # spends resources on whatever is buildable, ignoring proportions —
    # exactly what a v0 bot needs while production scales.
    try:
        from ares.behaviors.macro.spawn_controller import SpawnController
        from ares.behaviors.macro.production_controller import ProductionController

        bot.register_behavior(
            SpawnController(army_composition_dict=ares_comp, freeflow_mode=True)
        )
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
    a_supply = army_supply(units)
    attacking = a_supply >= ATTACK_SUPPLY
    target = attack_target(bot) if attacking else hold_position(bot)

    if units:
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

    # Periodic status log so we can diagnose without watching every frame.
    _log_status_periodically(bot, phase, a_supply, attacking, target, len(units))
    _log_production_diagnostics(bot)


def _log_status_periodically(
    bot: "AresBot",
    phase: str,
    a_supply: int,
    attacking: bool,
    target: "Point2",
    unit_count: int,
) -> None:
    """Emit one status line per LOG_INTERVAL_SECONDS of in-game time."""
    now = float(getattr(bot, "time", 0.0))
    last = float(getattr(bot, "_last_strategy_log_time", -LOG_INTERVAL_SECONDS))
    if now - last < LOG_INTERVAL_SECONDS:
        return
    bot._last_strategy_log_time = now  # type: ignore[attr-defined]
    state = "ATTACK" if attacking else "HOLD"
    target_str = (
        f"({target.x:.0f},{target.y:.0f})" if hasattr(target, "x") else str(target)
    )
    logger.info(
        f"strategy t={now:05.1f}s phase={phase} "
        f"army={a_supply}/{ATTACK_SUPPLY} ({state}) target={target_str} "
        f"unit_count={unit_count}"
    )


def _log_production_diagnostics(bot: "AresBot") -> None:
    """Per-DIAG_INTERVAL_SECONDS dump of production-pipeline state. Tells
    us where the bot is stalling: supply blocked, no idle production, no
    workers, low income, etc. Reads stdlib python-sc2 attributes only —
    safe to call every step (rate-limited internally)."""
    now = float(getattr(bot, "time", 0.0))
    last = float(getattr(bot, "_last_prod_log_time", -DIAG_INTERVAL_SECONDS))
    if now - last < DIAG_INTERVAL_SECONDS:
        return
    bot._last_prod_log_time = now  # type: ignore[attr-defined]

    try:
        from sc2.ids.unit_typeid import UnitTypeId
    except ImportError:
        return

    structures = getattr(bot, "structures", None)
    workers = getattr(bot, "workers", None)
    if structures is None or workers is None:
        return

    # Production buildings, by readiness + idleness.
    prod_summary: list[str] = []
    total_idle_prod = 0
    for name in TERRAN_PRODUCTION_NAMES:
        try:
            uid = UnitTypeId[name]
        except KeyError:
            continue
        all_of = structures(uid)
        ready = [s for s in all_of if s.is_ready]
        idle = [s for s in ready if s.is_idle]
        total_idle_prod += len(idle)
        if all_of:
            prod_summary.append(f"{name.lower()}={len(ready)}/{len(all_of)}(idle={len(idle)})")

    # Addons (tech-tier indicators).
    addons: list[str] = []
    for name in TERRAN_TECHLAB_NAMES + TERRAN_REACTOR_NAMES:
        try:
            uid = UnitTypeId[name]
        except KeyError:
            continue
        n = len([s for s in structures(uid) if s.is_ready])
        if n:
            addons.append(f"{name.lower()}={n}")

    # Worker accounting.
    n_workers = len(workers)
    n_idle_workers = len([w for w in workers if w.is_idle])

    minerals = int(getattr(bot, "minerals", 0))
    vespene = int(getattr(bot, "vespene", 0))
    supply_used = int(getattr(bot, "supply_used", 0))
    supply_cap = int(getattr(bot, "supply_cap", 0))
    supply_left = int(getattr(bot, "supply_left", 0))

    # Supply-blocked is one of the most common production stalls.
    supply_blocked = supply_left == 0 and supply_cap < 200

    parts = [
        f"prod t={now:05.1f}s",
        f"min={minerals} gas={vespene}",
        f"supply={supply_used}/{supply_cap}({supply_left} left)",
        f"workers={n_workers}({n_idle_workers} idle)",
    ]
    if prod_summary:
        parts.append("buildings: " + " ".join(prod_summary))
    if addons:
        parts.append("addons: " + " ".join(addons))
    if supply_blocked:
        parts.append("[SUPPLY-BLOCKED]")
    if total_idle_prod > 0 and minerals >= 50 and supply_left > 0:
        parts.append(f"[IDLE-PROD {total_idle_prod}]")

    logger.info(" | ".join(parts))
