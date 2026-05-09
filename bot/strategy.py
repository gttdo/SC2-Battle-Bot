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

# Army state-machine thresholds (combat-unit supply). With home-defense
# wired in (units recall to threats), we can be patient about pushing —
# 25 supply is ~12 marines + 4 marauders + medivac, a real first push.
ATTACK_SUPPLY = 25
DEFEND_SUPPLY = 5

# How close an enemy combat unit must be to one of our townhalls before
# we treat it as a threat and recall the army to defend. Generous radius
# so units start moving back BEFORE the enemy is shooting our buildings.
THREAT_RADIUS = 40.0

# How often (in game-seconds) to emit a strategy status log line. Avoids
# spamming once-per-step but gives us enough signal to diagnose army flow.
LOG_INTERVAL_SECONDS = 30.0
DIAG_INTERVAL_SECONDS = 15.0  # tighter cadence for production diagnostics

# Production / economy structures we count when diagnosing stalls.
TERRAN_PRODUCTION_NAMES: tuple[str, ...] = (
    "BARRACKS", "FACTORY", "STARPORT",
)

# Upgrade priority list for Terran bio. ares's UpgradeController iterates
# this in order; once it gets the first one researching it queues the next
# from the same building (e.g. WeaponsLvl1 -> WeaponsLvl2 -> WeaponsLvl3).
# Stim is already in the playbook's scripted opening but listing again is
# harmless — UpgradeController skips already-pending or complete ones.
TERRAN_BIO_UPGRADE_NAMES: tuple[str, ...] = (
    "STIMPACK",
    "SHIELDWALL",  # Combat Shield (Marine +10 HP)
    "PUNISHERGRENADES",  # Concussive Shells (Marauder slow)
    "TERRANINFANTRYWEAPONSLEVEL1",
    "TERRANINFANTRYARMORSLEVEL1",
    "TERRANINFANTRYWEAPONSLEVEL2",
    "TERRANINFANTRYARMORSLEVEL2",
    "TERRANINFANTRYWEAPONSLEVEL3",
    "TERRANINFANTRYARMORSLEVEL3",
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
    """Where to push when we hit attack threshold.

    SC2 victory = destroying ALL enemy structures, not arriving at the
    enemy spawn. Without this targeting, the bot a-moves to
    enemy_start_locations[0] and stops; if the enemy expanded or has
    proxies the army never finds them and the game stalls forever.

    Priority:
      1. The closest visible enemy structure (or memory-unit structure
         that ares is still tracking after it left vision). Townhalls
         (Hatchery / CommandCenter / Nexus) get a bias because killing
         them denies economy. Within structure types we go by raw
         distance from our start location.
      2. enemy_start_locations[0] if nothing is visible — most common
         place to find the next thing to kill.
      3. game_info.map_center as a last resort (shouldn't normally hit).
    """
    enemy_structures = getattr(bot, "enemy_structures", None)
    if enemy_structures:
        # Townhall types — race-agnostic so it works across matchups.
        townhall_names = {"Hatchery", "Lair", "Hive", "CommandCenter",
                          "OrbitalCommand", "PlanetaryFortress", "Nexus"}

        try:
            anchor = bot.start_location

            def _sort_key(s):
                # Lower key sorts first. Townhalls get -1 priority bonus
                # before distance, so they always come before non-townhalls
                # at the same distance.
                type_name = getattr(getattr(s, "type_id", None), "name", "")
                is_townhall = 0 if type_name in townhall_names else 1
                try:
                    d2 = s.position.distance_to_squared(anchor)
                except AttributeError:
                    sx, sy = s.position[0], s.position[1]
                    ax, ay = anchor[0], anchor[1]
                    d2 = (sx - ax) ** 2 + (sy - ay) ** 2
                return (is_townhall, d2)

            closest = min(enemy_structures, key=_sort_key)
            return closest.position
        except (AttributeError, ValueError):
            pass  # fall through to start-location fallback

    if getattr(bot, "enemy_start_locations", None):
        return bot.enemy_start_locations[0]
    return bot.game_info.map_center


def hold_position(bot: "AresBot") -> "Point2":
    """Where to gather while building army. v0 holds at our main — units
    walking forward to a midmap rally got picked off before reaching critical
    mass last game. Defending in the main means production buildings are
    auto-defended by the army that just spawned from them."""
    return bot.start_location


def find_threat(bot: "AresBot") -> "Point2 | None":
    """Return the position of the nearest enemy combat unit threatening one
    of our townhalls, or None if no threat. 'Threat' = an enemy unit that
    isn't a structure and can attack ground, within THREAT_RADIUS of any
    townhall. We also include observed enemy units recently in fog of war,
    via ares's enemy_units (which includes memory units)."""
    townhalls = list(getattr(bot, "townhalls", []) or [])
    enemy_units = getattr(bot, "enemy_units", None)
    if not townhalls or not enemy_units:
        return None

    nearest_pos = None
    nearest_d2 = THREAT_RADIUS * THREAT_RADIUS
    for enemy in enemy_units:
        # Skip non-combat (overlords, larvae, eggs etc). Workers count as
        # combat for v0: a worker harassing our base IS a threat.
        if getattr(enemy, "is_structure", False):
            continue
        if not getattr(enemy, "can_attack_ground", False) and not getattr(
            enemy, "is_worker", False
        ):
            continue
        for hall in townhalls:
            try:
                d2 = enemy.position.distance_to_squared(hall.position)
            except AttributeError:
                # distance_to_squared isn't always available; fall back
                ex, ey = enemy.position[0], enemy.position[1]
                hx, hy = hall.position[0], hall.position[1]
                d2 = (ex - hx) ** 2 + (ey - hy) ** 2
            if d2 < nearest_d2:
                nearest_d2 = d2
                nearest_pos = enemy.position
                break  # this enemy threatens; move to next enemy
    return nearest_pos


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

    # Macro behaviors. ares's BuildRunner stops auto-supplying and stops
    # producing workers the moment its scripted opening completes — after
    # that, every part of macro is the bot's job. We wire ares's macro
    # behaviors directly so production keeps flowing post-opening.
    #
    # `freeflow_mode=True` on SpawnController is intentional: its proportion
    # mode `break`s the queue loop the moment it can't afford the next
    # priority unit. With Reaper/Marauder high in priority and gas constrained,
    # that means cheap Marines don't queue from idle Barracks even when we
    # have 1000+ minerals. Freeflow spends what we have — strategist will
    # eventually drive smarter proportions.
    try:
        from ares.behaviors.macro.auto_supply import AutoSupply
        from ares.behaviors.macro.build_workers import BuildWorkers
        from ares.behaviors.macro.expansion_controller import ExpansionController
        from ares.behaviors.macro.gas_building_controller import GasBuildingController
        from ares.behaviors.macro.mining import Mining
        from ares.behaviors.macro.production_controller import ProductionController
        from ares.behaviors.macro.spawn_controller import SpawnController
        from ares.behaviors.macro.upgrade_controller import UpgradeController
        from sc2.ids.upgrade_id import UpgradeId
    except Exception:  # pragma: no cover
        logger.exception("strategy: failed to import ares macro behaviors")
        return

    macro = playbook.get("macro_rules", {})
    worker_cap = int(macro.get("worker_cap", 70))
    max_bases = int(macro.get("max_bases", 3))

    try:
        # Worker distribution: long-distance mining, gas saturation,
        # threatened-worker fleeing. Cheapest fix for the 13-14 idle SCVs
        # we saw mid-game when our main saturated.
        bot.register_behavior(Mining())

        # Keep the SCV count climbing toward worker_cap. ares stops doing
        # this after the opening, which is why we drained to 1 worker.
        bot.register_behavior(BuildWorkers(to_count=worker_cap))

        # Auto-build depots so we don't supply-block. AutoSupply checks
        # supply_left vs supply build time and queues a depot when the gap
        # is small. This was the original "supply=94/94" problem.
        bot.register_behavior(AutoSupply(bot.start_location))

        # Take a third / fourth base. Without this we run out of minerals
        # mid-fight (saw 14k collected and still drained on attrition).
        bot.register_behavior(
            ExpansionController(to_count=max_bases, max_pending=1)
        )

        # Two refineries per active base — gives us enough gas for sustained
        # Marauder / Medivac / tech production.
        n_townhalls = max(1, len(bot.townhalls))
        bot.register_behavior(
            GasBuildingController(to_count=n_townhalls * 2)
        )

        # Continuous army production from the playbook's composition_targets.
        bot.register_behavior(
            SpawnController(army_composition_dict=ares_comp, freeflow_mode=True)
        )
        bot.register_behavior(
            ProductionController(
                army_composition_dict=ares_comp,
                base_location=bot.start_location,
            )
        )

        # Spend gas surplus on bio upgrades. Drops UpgradeController's
        # priority gracefully — won't block army production if we can't
        # afford the upgrade right now, just queues when ready.
        upgrade_list = []
        for name in TERRAN_BIO_UPGRADE_NAMES:
            try:
                upgrade_list.append(UpgradeId[name])
            except KeyError:
                continue  # python-sc2 might rename a key; skip silently
        if upgrade_list:
            bot.register_behavior(
                UpgradeController(
                    upgrade_list=upgrade_list,
                    base_location=bot.start_location,
                )
            )
    except Exception:  # pragma: no cover — never crash the bot from strategy
        logger.exception("strategy: macro behavior register failed")

    # Army control: 3-state decision — DEFEND beats ATTACK beats HOLD.
    # Defense always wins: if anything threatens our bases, the entire army
    # collapses to that point regardless of attack threshold. Without this
    # the bot pushed all-in and lost its base to a counter-attack.
    units = army_units(bot)
    a_supply = army_supply(units)

    threat_pos = find_threat(bot)
    if threat_pos is not None:
        state = "DEFEND"
        target = threat_pos
    elif a_supply >= ATTACK_SUPPLY:
        state = "ATTACK"
        target = attack_target(bot)
    else:
        state = "HOLD"
        target = hold_position(bot)

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
    _log_status_periodically(bot, phase, a_supply, state, target, len(units))
    _log_production_diagnostics(bot)


def _log_status_periodically(
    bot: "AresBot",
    phase: str,
    a_supply: int,
    state: str,
    target: "Point2",
    unit_count: int,
) -> None:
    """Emit one status line per LOG_INTERVAL_SECONDS of in-game time."""
    now = float(getattr(bot, "time", 0.0))
    last = float(getattr(bot, "_last_strategy_log_time", -LOG_INTERVAL_SECONDS))
    if now - last < LOG_INTERVAL_SECONDS:
        return
    bot._last_strategy_log_time = now  # type: ignore[attr-defined]
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
