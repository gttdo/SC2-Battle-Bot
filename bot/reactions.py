"""Tier-1 adaptation: in-match scouted-pattern reactions.

A reaction is two named pieces of behavior:
  - a `trigger` detector that watches the bot state each step and returns True
    when a scouted enemy pattern is recognized (e.g. ling_flood, mass_roach)
  - a `response` handler that runs once when the trigger fires (e.g.
    wall_off_at_natural, tech_to_marauder)

The schema in playbook/schema.json carries reaction names as snake_case
strings; this module is where those names map to actual code. Names that
appear in a playbook but aren't registered here log a warning and are
ignored (schema enforces shape, code enforces vocabulary — same pattern as
the playbook compiler).

This file is intentionally light on python-sc2 / ares dependencies so the
registry abstraction can be unit-tested with mock bots. Detectors that
need real game APIs (UnitTypeId queries, mediator calls) import lazily.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------

class BotLike(Protocol):
    """Subset of AresBot / python-sc2 BotAI we touch from reactions.

    This is a structural contract so tests can pass plain mock objects
    without instantiating the real game classes."""

    time: float

    def chat_send(self, message: str, team_only: bool = False) -> None: ...


# Async because most response handlers will issue ares mediator commands
# that are awaitable. Sync detectors are fine.
DetectorFn = Callable[[BotLike], bool]
ResponderFn = Callable[[BotLike, "Reaction"], Awaitable[None]]


@dataclass
class Reaction:
    name: str
    detector: DetectorFn
    responder: ResponderFn
    description: str = ""


@dataclass
class Registry:
    """Tracks which reactions exist, which have already fired this match,
    and runs newly-triggered ones from on_step."""

    bot: BotLike
    reactions: dict[str, Reaction] = field(default_factory=dict)
    fired: set[str] = field(default_factory=set)

    def register(self, reaction: Reaction) -> None:
        if reaction.name in self.reactions:
            logger.warning("reactions: re-registering %s (last one wins)", reaction.name)
        self.reactions[reaction.name] = reaction

    def prepop(self, names: list[str], confidence: float) -> None:
        """Mark reactions as already-fired at game start, e.g. from opponent
        priors that say this opponent reliably does X. Below the confidence
        floor we ignore the priors entirely."""
        if confidence < 0.3:
            logger.info("reactions: priors confidence %.2f < 0.3, ignoring prepop", confidence)
            return
        for name in names:
            if name not in self.reactions:
                logger.warning(
                    "reactions: prior wants prepop %r but no such reaction registered (skipping)",
                    name,
                )
                continue
            self.fired.add(name)
            logger.info("reactions: prepop %s from priors (confidence=%.2f)", name, confidence)

    async def update(self) -> list[str]:
        """Called once per on_step. Checks every un-fired reaction's detector;
        for each that triggers, marks it fired and awaits its responder.
        Returns the list of reaction names newly triggered this call (mainly
        for tests / observation recording)."""
        newly_triggered: list[str] = []
        for name, reaction in self.reactions.items():
            if name in self.fired:
                continue
            try:
                if not reaction.detector(self.bot):
                    continue
            except Exception:  # pragma: no cover — never let a buggy detector crash the bot
                logger.exception("reactions: detector for %s raised; treating as not-fired", name)
                continue

            self.fired.add(name)
            newly_triggered.append(name)
            logger.info("reactions: %s triggered at t=%.1fs", name, getattr(self.bot, "time", 0.0))
            try:
                await reaction.responder(self.bot, reaction)
            except Exception:  # pragma: no cover
                logger.exception("reactions: responder for %s raised", name)
        return newly_triggered


# ---------------------------------------------------------------------------
# Reaction definitions (the v0 vocabulary)
#
# Detectors are sync functions over BotLike; responders are async over the
# same. Pure snake_case names match the schema's reaction trigger pattern.
# ---------------------------------------------------------------------------

LING_FLOOD_TIME_LIMIT_S = 240.0  # only fire in early game
LING_FLOOD_MIN_LINGS_NEAR_BASE = 6
LING_FLOOD_NEAR_BASE_RADIUS = 30.0


def detect_ling_flood(bot: Any) -> bool:
    """Fire when 6+ enemy zerglings are within 30 distance of any of our
    townhalls before t=4:00. This is the canonical TvZ early-game pressure
    signal (12-pool / speedling all-in / 16-hatch + lings into our face).

    Lazily imports python-sc2 enums so reactions.py is importable in
    environments where python-sc2 isn't installed (e.g. our schema-only
    test runner). Returns False on any import failure."""
    if getattr(bot, "time", 0.0) > LING_FLOOD_TIME_LIMIT_S:
        return False
    try:
        from sc2.ids.unit_typeid import UnitTypeId  # noqa: PLC0415 (lazy)
    except ImportError:
        return False

    enemy_units = getattr(bot, "enemy_units", None)
    townhalls = getattr(bot, "townhalls", None)
    if not enemy_units or not townhalls:
        return False

    enemy_lings = enemy_units.of_type(UnitTypeId.ZERGLING)
    if not enemy_lings:
        return False

    for hall in townhalls:
        near = enemy_lings.closer_than(LING_FLOOD_NEAR_BASE_RADIUS, hall)
        if near.amount >= LING_FLOOD_MIN_LINGS_NEAR_BASE:
            return True
    return False


async def respond_wall_off_at_natural(bot: Any, reaction: Reaction) -> None:
    """Place a Terran ling-tight wall at the natural's choke.

    NOT YET IMPLEMENTED. Real placement requires ares's mediator API
    (`mediator.get_building_position` / similar) to query a wall-off layout
    for the current map's natural ramp. v0 logs that the reaction fired so
    the recorder can capture it, and surfaces a chat message in-game for
    debugging. The actual SCV-level placement work is the next iteration.

    See: https://aressc2.github.io/ares-sc2/api_reference/managers/mediator.html
    """
    msg = f"[reaction] {reaction.name} fired -> wall_off_at_natural (not yet implemented)"
    logger.warning(msg)
    try:
        bot.chat_send(msg, team_only=True)
    except Exception:
        pass  # chat_send is best-effort, never crash the bot


def build_default_registry(bot: Any) -> Registry:
    """Return a Registry pre-populated with the v0 TvZ reaction palette.

    Adding a new reaction is two pieces here: a detector + a responder, plus
    a register() call. The schema doesn't change."""
    registry = Registry(bot=bot)
    registry.register(
        Reaction(
            name="ling_flood",
            detector=detect_ling_flood,
            responder=respond_wall_off_at_natural,
            description="6+ enemy zerglings near our base before 4:00 -> wall off natural.",
        )
    )
    # Stubs for the rest of the playbook's TvZ reactions. Each gets a
    # detector that always returns False (i.e. never auto-fires) until
    # implemented; prepop from priors still works because that path bypasses
    # the detector. Fill these in over time.
    for name in ("early_pool", "mass_roach", "mutas", "nydus", "baneling_drop"):
        registry.register(
            Reaction(
                name=name,
                detector=lambda _bot: False,
                responder=respond_wall_off_at_natural,  # placeholder
                description=f"{name} (detector + responder not yet implemented)",
            )
        )
    return registry
