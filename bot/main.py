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

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ares import AresBot
from loguru import logger

from bot import match_observation, opponent_memory, reactions, strategy

if TYPE_CHECKING:
    from sc2.data import Result
    from sc2.unit import Unit

PLAYBOOK_VERSION = "0.2+manual"
PLAYBOOK_DIR = Path(__file__).resolve().parent.parent / "playbook"

# First-attack detection. Tighter radius than strategy.THREAT_RADIUS (40)
# because this asks "are they literally at our base", not "should the army
# pull back". MIN_THREAT_UNITS=3 ignores single scouts / overlords.
FIRST_ATTACK_RADIUS = 30.0
MIN_THREAT_UNITS = 3


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
        # Loaded playbook JSON (composition_targets, macro_rules, reactions).
        # Strategy module reads composition_targets each step to drive
        # continuous production after the BuildRunner's opening completes.
        self.playbook: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_start(self) -> None:
        await super().on_start()

        self.matchup = self._matchup_label()
        opp_id = getattr(self, "opponent_id", None) or "unknown"
        logger.info(f"on_start: matchup={self.matchup}, opponent_id={opp_id}")

        # ---- Tier-2: load opponent priors -------------------------------
        self.opponent_priors = opponent_memory.load(opp_id, self.matchup)
        if self.opponent_priors:
            n = self.opponent_priors.get("match_count", 0)
            conf = self.opponent_priors.get("derived", {}).get("confidence", 0.0)
            logger.info(f"priors loaded: {n} prior matches, confidence={conf:.2f}")

        # ---- Load matchup playbook (drives strategy.update each step) ---
        self.playbook = self._load_playbook(self.matchup)
        if self.playbook:
            n_build = len(self.playbook.get("build_order", []))
            n_react = len(self.playbook.get("reactions", []))
            logger.info(
                f"playbook loaded: {self.matchup} "
                f"({n_build} build steps, {n_react} reactions)"
            )

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
        logger.info(f"build_runner: chosen_opening={chosen}, parsed_steps={len(parsed)}")
        if parsed:
            preview = [
                f"{getattr(s, 'start_at_supply', '?')}@{getattr(s, 'command', '?')}"
                for s in parsed[:6]
            ]
            logger.info(f"build_runner: first steps = {preview}")

    async def on_step(self, iteration: int) -> None:
        await super().on_step(iteration)

        if self.reactions_registry is not None:
            await self.reactions_registry.update()

        # Drive continuous production + army control once ares's BuildRunner
        # has executed our scripted opening. Without this, the bot builds
        # the opening then sits idle for the rest of the match.
        strategy.update(self, self.playbook)

        # Record first sightings of enemy structures. We sweep what's currently
        # in vision each step; the recorder is idempotent on already-seen names.
        if self.match_recorder is not None:
            our_supply = int(getattr(self, "supply_used", 0))
            for unit in getattr(self, "enemy_structures", []):
                # `unit.type_id.name` is the python-sc2 PascalCase string.
                name = getattr(getattr(unit, "type_id", None), "name", None)
                if name:
                    self.match_recorder.see_enemy_structure(name, our_supply)

            # Detect the first significant enemy attack on our base.
            self._record_first_attack_if_seen()

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
            logger.info(f"build: switched to {target}")
        except Exception as exc:
            logger.warning(
                f"build: switch_opening({target!r}) failed ({exc}); "
                "ares will use Cycle/Winrate default"
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

        # Fill in critical_event if no in-game code already did. Coarse but
        # consistent — gives the strategist a tag it can aggregate across
        # matches even when richer in-game detection didn't fire.
        duration = float(getattr(self, "time", 0.0))
        if self.match_recorder.critical_event is None:
            self.match_recorder.critical_event = (
                self.match_recorder.derive_critical_event_tag(
                    result=result_label, duration_seconds=duration,
                )
            )

        observation = self.match_recorder.to_observation(
            result=result_label,
            duration_seconds=duration,
            playbook_version=PLAYBOOK_VERSION,
        )

        state = self.opponent_priors or opponent_memory.empty_state(opp_id, self.matchup)
        opponent_memory.record_observation(state, observation)
        opponent_memory.save(state)
        logger.info(
            f"on_end: wrote observation for {opp_id} ({self.matchup}) -> {result_label}"
        )

    def _record_first_attack_if_seen(self) -> None:
        """Detect the first cluster of enemy combat units near our base; if
        found and not already recorded, snapshot time + composition on the
        match recorder. Idempotent — a recorder that already has a first
        attack drops out cheaply."""
        if self.match_recorder is None:
            return
        if self.match_recorder.first_attack_seconds is not None:
            return  # already recorded — no-op

        townhalls = list(getattr(self, "townhalls", []) or [])
        enemy_units = getattr(self, "enemy_units", None)
        if not townhalls or not enemy_units:
            return

        radius_sq = FIRST_ATTACK_RADIUS * FIRST_ATTACK_RADIUS
        threatening: list = []
        for enemy in enemy_units:
            # Skip structures, workers, and air-only / non-attacking units —
            # we only care about combat units that can pressure us.
            if getattr(enemy, "is_structure", False):
                continue
            if getattr(enemy, "is_worker", False):
                continue
            if not getattr(enemy, "can_attack_ground", False):
                continue
            for hall in townhalls:
                try:
                    d2 = enemy.position.distance_to_squared(hall.position)
                except AttributeError:
                    ex, ey = enemy.position[0], enemy.position[1]
                    hx, hy = hall.position[0], hall.position[1]
                    d2 = (ex - hx) ** 2 + (ey - hy) ** 2
                if d2 < radius_sq:
                    threatening.append(enemy)
                    break

        if len(threatening) < MIN_THREAT_UNITS:
            return

        # Build composition snapshot — Counter of PascalCase type names.
        from collections import Counter

        comp: Counter[str] = Counter()
        for u in threatening:
            name = getattr(getattr(u, "type_id", None), "name", None)
            if name:
                comp[name] += 1

        self.match_recorder.record_first_attack(
            time_seconds=float(getattr(self, "time", 0.0)),
            composition=dict(comp),
        )
        logger.info(
            f"observation: first attack detected at t={self.time:.1f}s "
            f"with composition={dict(comp)}"
        )

    @staticmethod
    def _load_playbook(matchup: str) -> Optional[dict[str, Any]]:
        """Load the playbook JSON for the given matchup. Returns None if
        the file is missing or unreadable. The strategy module is a no-op
        when playbook is None, so a missing file degrades gracefully to
        'execute the opening from terran_builds.yml then sit'."""
        path = PLAYBOOK_DIR / f"{matchup.lower()}.json"
        if not path.is_file():
            # Fallback: pick any *.json under playbook/ that isn't a schema/example.
            for candidate in PLAYBOOK_DIR.glob("*.json"):
                stem = candidate.stem
                if stem.endswith("_schema") or stem.endswith(".example"):
                    continue
                if stem == "schema" or stem == "opponent_schema" or "example" in stem:
                    continue
                path = candidate
                break
        try:
            with path.open(encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"playbook: could not load {path}: {e}")
            return None

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
