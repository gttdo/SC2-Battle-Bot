"""Records what happens during one match so we can write a structured
observation to the opponent priors file at on_end.

Tracks:
  - First-time-seen enemy structures, paired with our supply at that moment.
  - First key tech structure (an enum'd subset that signals strategy choice).
  - Reactions that fired during the match (handed in from the reactions
    Registry at game end).
  - First significant attack: time + composition snapshot the first time
    enemy combat units cluster near our base. v0 heuristic; refine later.

Like opponent_memory and reactions, this module avoids hard-importing
python-sc2 / ares so the recorder logic is unit-testable with mock bots.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# UnitTypeId names (PascalCase strings — we match by string, not enum, so
# we don't have to import python-sc2). The recorder samples the FIRST time
# we ever see each of these, paired with our supply.
KEY_BUILDINGS_TO_TRACK: tuple[str, ...] = (
    # Zerg
    "Hatchery", "SpawningPool", "Extractor", "RoachWarren", "BanelingNest",
    "HydraliskDen", "Spire", "InfestationPit", "UltraliskCavern",
    # Terran
    "CommandCenter", "SupplyDepot", "Barracks", "Refinery", "Factory",
    "Starport", "Armory", "BarracksTechLab", "FactoryTechLab", "StarportTechLab",
    # Protoss
    "Nexus", "Pylon", "Gateway", "Assimilator", "CyberneticsCore",
    "TwilightCouncil", "RoboticsFacility", "Stargate", "DarkShrine",
)

# A subset that's especially meaningful as "first tech structure" — the
# scouting moment that telegraphs which strategy the opponent is committing
# to. Order matters: first match wins.
TECH_STRUCTURES_PRIORITY: tuple[str, ...] = (
    "RoachWarren", "BanelingNest", "Spire", "InfestationPit", "HydraliskDen",
    "UltraliskCavern", "BarracksTechLab", "FactoryTechLab", "StarportTechLab",
    "Armory", "TwilightCouncil", "RoboticsFacility", "Stargate", "DarkShrine",
)


@dataclass
class Recorder:
    """Per-match recorder. One instance lives on the bot for the life of
    a single match; reset (or replaced) on each on_start."""

    map_name: str = "unknown"
    started_at: str = field(default_factory=lambda: _now_iso())

    # Building first-seen samples. value = our_supply at first sight.
    key_buildings_seen: dict[str, int] = field(default_factory=dict)

    # First major enemy attack snapshot.
    first_attack_seconds: float | None = None
    first_attack_composition: dict[str, int] | None = None

    # Reactions that fired this match. Filled in at game end from the
    # Registry's `fired` set, minus any prepop reactions that were
    # activated artificially from priors (those don't count as "scouted").
    reactions_fired: list[str] = field(default_factory=list)

    # The pivotal moment tag (free-form for v0). The bot may set this from
    # game flow; left None for v0 unless explicitly assigned.
    critical_event: str | None = None

    def see_enemy_structure(self, structure_name: str, our_supply: int) -> None:
        """Call from on_step / on_unit_destroyed / on_enemy_unit_entered_vision
        whenever we learn about an enemy structure. First write wins; later
        sightings are no-ops."""
        if structure_name not in KEY_BUILDINGS_TO_TRACK:
            return
        if structure_name in self.key_buildings_seen:
            return
        self.key_buildings_seen[structure_name] = our_supply
        logger.debug(
            "match_observation: first %s seen at our supply %d",
            structure_name, our_supply,
        )

    def record_first_attack(self, time_seconds: float, composition: dict[str, int]) -> None:
        """Call when we first detect a significant enemy engagement at our
        base. Idempotent — subsequent calls are ignored."""
        if self.first_attack_seconds is not None:
            return
        self.first_attack_seconds = float(time_seconds)
        self.first_attack_composition = dict(composition)
        logger.info(
            "match_observation: first attack at t=%.1fs, comp=%s",
            time_seconds, self.first_attack_composition,
        )

    def first_tech_structure(self) -> tuple[str | None, int | None]:
        """Return (structure_name, supply_at_first_sight) for the first
        meaningful tech structure we saw, or (None, None) if none seen."""
        for name in TECH_STRUCTURES_PRIORITY:
            if name in self.key_buildings_seen:
                return name, self.key_buildings_seen[name]
        return None, None

    def derive_critical_event_tag(
        self, result: str, duration_seconds: float
    ) -> str | None:
        """If the bot didn't manually set a critical_event, derive a coarse
        tag from result + duration + first-attack timing. Better-than-null
        signal for the strategist: across N matches, a tag like
        'early_pressure_loss_at_5m' lets Claude spot rush patterns even
        when our match recorder hasn't captured a specific event."""
        if self.critical_event is not None:
            return self.critical_event
        return derive_critical_event_tag(
            result=result,
            duration_seconds=duration_seconds,
            first_attack_seconds=self.first_attack_seconds,
        )

    def to_observation(
        self,
        result: str,
        duration_seconds: float,
        playbook_version: str,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        """Render the recorder's contents into a dict matching the
        observation schema in playbook/opponent_schema.json."""
        if result not in ("win", "loss", "tie"):
            raise ValueError(f"result must be win/loss/tie, got {result!r}")

        first_tech, first_tech_supply = self.first_tech_structure()
        # Convert the first_tech supply (we recorded as our-supply) into a
        # rough seconds estimate is out of scope. We only have supply, so
        # we record the structure name and leave seconds null.
        their_opening: dict[str, Any] = {
            "key_buildings_seen": dict(self.key_buildings_seen),
            "first_expansion_seconds": None,  # populated by caller if known
            "first_tech_structure": first_tech,
            "first_tech_seconds": None,
        }

        their_first_attack: dict[str, Any] | None = None
        if self.first_attack_seconds is not None:
            their_first_attack = {
                "seconds": self.first_attack_seconds,
                "composition": dict(self.first_attack_composition or {}),
            }

        return {
            "timestamp": timestamp or _now_iso(),
            "result": result,
            "map": self.map_name,
            "duration_seconds": float(duration_seconds),
            "our_playbook_version": playbook_version,
            "their_opening": their_opening,
            "their_first_attack": their_first_attack,
            "our_scouted_reactions_fired": list(self.reactions_fired),
            "our_critical_event": self.critical_event,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# Time buckets for the duration-based fallback tagger. Tuned so 'early' lines
# up with rushes / cheeses, 'mid' with standard pushes, and 'late' with
# attritional games. The strategist sees these tags as recurring loss
# patterns when they appear in 2+ matches.
_EARLY_END_S = 360.0   # < 6 min
_MID_END_S = 720.0     # 6-12 min
_EARLY_PRESSURE_THRESHOLD_S = 240.0  # < 4 min first attack = real pressure


def derive_critical_event_tag(
    result: str,
    duration_seconds: float,
    first_attack_seconds: float | None = None,
) -> str:
    """Pure function: coarse v0 critical-event tag from match outcome.

    Result + duration bucket + (optionally) the first_attack timing produce
    a stable string that's useful as an aggregation key when the strategist
    reads many matches. Pure so it's unit-testable without a live game."""
    duration = max(0.0, float(duration_seconds))
    minutes = max(1, int(duration // 60))

    if result == "win":
        return f"won_at_{minutes}m"

    early_pressure = (
        first_attack_seconds is not None
        and first_attack_seconds < _EARLY_PRESSURE_THRESHOLD_S
    )

    if duration < _EARLY_END_S:
        # Lost in <6 min — almost certainly a rush we didn't survive
        if early_pressure:
            return f"early_pressure_loss_at_{minutes}m"
        return f"early_loss_at_{minutes}m"
    if duration < _MID_END_S:
        # Lost mid-game — could be a standard push or a delayed rush
        if early_pressure:
            return f"mid_loss_after_early_pressure_at_{minutes}m"
        return f"mid_loss_at_{minutes}m"
    # Lost late-game — attrition, macro deficit, no early pressure
    return f"late_loss_at_{minutes}m"
