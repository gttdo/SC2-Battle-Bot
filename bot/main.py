"""SC2 Agent — in-game executor.

Subclass of ares-sc2's AresBot. ares does the heavy lifting (build runner,
production controller, mediator); we layer on top of it:

  - Matchup-aware build selection at on_start (TvT / TvP / TvZ).
  - Tier-2 adaptation: load opponent priors from ./data/opponents/, apply
    via prepop reactions and (eventually) timing shifts.
  - Tier-1 adaptation: the reactions Registry runs each on_step, firing
    detector-driven scouted-pattern responses.
  - Match observation recording: first-seen enemy structures, first attack,
    reactions fired. Written back to ./data/opponents/ at on_end.

This file ASSUMES ares-sc2 is on the Python path (cloned alongside the
project and wired up in run.py). It does NOT run if ares-sc2 is missing —
that's intentional. The pure-Python modules (opponent_memory, reactions,
match_observation) are testable on their own.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from ares import AresBot

from bot import match_observation, opponent_memory, reactions

if TYPE_CHECKING:
    from sc2.data import Result
    from sc2.unit import Unit

logger = logging.getLogger(__name__)

PLAYBOOK_VERSION = "0.2+manual"


class SC2Agent(AresBot):
    """The bot. Named SC2Agent to match the project's external branding;
    extends AresBot so we get ares's BuildRunner + managers for free."""

    def __init__(self, game_step_override: Optional[int] = None) -> None:
        super().__init__(game_step_override)
        # State initialized in on_start once the game environment is ready.
        self.matchup: str = "TvZ"
        self.opponent_priors: Optional[dict] = None
        self.match_recorder: Optional[match_observation.Recorder] = None
        self.reactions_registry: Optional[reactions.Registry] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_start(self) -> None:
        await super().on_start()

        self.matchup = self._matchup_label()
        opp_id = getattr(self, "opponent_id", None) or "unknown"
        logger.info("on_start: matchup=%s, opponent_id=%s", self.matchup, opp_id)

        # ---- Tier-2: load opponent priors -------------------------------
        self.opponent_priors = opponent_memory.load(opp_id, self.matchup)
        if self.opponent_priors:
            n = self.opponent_priors.get("match_count", 0)
            conf = self.opponent_priors.get("derived", {}).get("confidence", 0.0)
            logger.info("priors loaded: %d prior matches, confidence=%.2f", n, conf)

        # ---- Initialize match recorder ---------------------------------
        self.match_recorder = match_observation.Recorder(
            map_name=getattr(self.game_info, "map_name", "unknown"),
        )

        # ---- Tier-1: build reaction registry & apply priors -------------
        self.reactions_registry = reactions.build_default_registry(self)
        if self.opponent_priors:
            derived = self.opponent_priors.get("derived", {})
            prepop = list(derived.get("prepop_reactions", []) or [])
            confidence = float(derived.get("confidence", 0.0))
            self.reactions_registry.prepop(prepop, confidence)

        # ---- Switch ares to the matchup-named build, with fallback -----
        self._switch_to_matchup_build()

        # Diagnostic: log how many steps ares actually parsed from our YAML.
        # If this is 0, something in compile.py is producing strings ares
        # rejects, and the build will silently complete on the first step.
        runner = getattr(self, "build_order_runner", None)
        parsed = list(getattr(runner, "build_order", []) or [])
        chosen = getattr(runner, "chosen_opening", "?")
        logger.info(
            "build_runner: chosen_opening=%s, parsed_steps=%d", chosen, len(parsed),
        )
        if parsed:
            preview = [
                f"{getattr(s, 'start_at_supply', '?')}@{getattr(s, 'command', '?')}"
                for s in parsed[:6]
            ]
            logger.info("build_runner: first steps = %s", preview)

    async def on_step(self, iteration: int) -> None:
        await super().on_step(iteration)

        if self.reactions_registry is not None:
            await self.reactions_registry.update()

        # Record first sightings of enemy structures. We sweep what's currently
        # in vision each step; the recorder is idempotent on already-seen names.
        if self.match_recorder is not None:
            our_supply = int(getattr(self, "supply_used", 0))
            for unit in getattr(self, "enemy_structures", []):
                # `unit.type_id.name` is the python-sc2 PascalCase string.
                name = getattr(getattr(unit, "type_id", None), "name", None)
                if name:
                    self.match_recorder.see_enemy_structure(name, our_supply)

    async def on_end(self, game_result: "Result") -> None:
        try:
            self._persist_observation(game_result)
        except Exception:  # pragma: no cover — never block on_end on a write failure
            logger.exception("on_end: failed to persist opponent observation")
        await super().on_end(game_result)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _matchup_label(self) -> str:
        """Map our race + enemy_race to TvT/TvP/TvZ. We're Terran-specialist
        for v0; non-Terran-self is unsupported and falls back to TvZ."""
        # python-sc2's Race enum: Terran, Protoss, Zerg, Random
        try:
            from sc2.data import Race
        except ImportError:
            return "TvZ"

        enemy = getattr(self, "enemy_race", None)
        if enemy == Race.Terran:
            return "TvT"
        if enemy == Race.Protoss:
            return "TvP"
        if enemy == Race.Zerg:
            return "TvZ"
        # Random opponents resolve at game start; if we can't tell yet, default
        # to TvZ (the playbook we have hand-authored for v0).
        return "TvZ"

    def _switch_to_matchup_build(self) -> None:
        """Tell ares's BuildRunner to use the matchup-named opening. Falls
        back to whatever's available if our matchup playbook hasn't been
        compiled into terran_builds.yml yet."""
        runner = getattr(self, "build_order_runner", None)
        if runner is None:
            logger.warning("build_order_runner missing; ares may not be initialized")
            return

        target = self.matchup
        try:
            runner.switch_opening(target)
            logger.info("build: switched to %s", target)
        except Exception:
            logger.warning(
                "build: switch_opening(%r) failed; ares will use Cycle/Winrate default",
                target,
                exc_info=True,
            )

    def _persist_observation(self, game_result: "Result") -> None:
        """Write the match observation to the opponent priors file."""
        if self.match_recorder is None:
            return

        opp_id = getattr(self, "opponent_id", None) or "unknown"
        result_label = self._result_label(game_result)

        # Mark the actually-fired (i.e. scouted-and-triggered) reactions on
        # the recorder. Prepop reactions are not "scouted" — the registry
        # tracks both in `fired`, but we want only the ones that flipped
        # because of game state, not priors. Approximate by subtracting the
        # priors' prepop list.
        registry_fired = (
            list(self.reactions_registry.fired) if self.reactions_registry else []
        )
        prepop = []
        if self.opponent_priors:
            prepop = list(
                self.opponent_priors.get("derived", {}).get("prepop_reactions", []) or []
            )
        self.match_recorder.reactions_fired = sorted(set(registry_fired) - set(prepop))

        observation = self.match_recorder.to_observation(
            result=result_label,
            duration_seconds=float(getattr(self, "time", 0.0)),
            playbook_version=PLAYBOOK_VERSION,
        )

        state = self.opponent_priors or opponent_memory.empty_state(opp_id, self.matchup)
        opponent_memory.record_observation(state, observation)
        opponent_memory.save(state)
        logger.info(
            "on_end: wrote observation for %s (%s) -> %s",
            opp_id, self.matchup, result_label,
        )

    @staticmethod
    def _result_label(game_result: "Result") -> str:
        """Map python-sc2's Result enum to our schema's win/loss/tie strings."""
        try:
            from sc2.data import Result
        except ImportError:
            return "tie"
        if game_result == Result.Victory:
            return "win"
        if game_result == Result.Defeat:
            return "loss"
        return "tie"
