"""Compile SC2 Agent playbook JSON into ares-sc2 build runner YAML.

Our `playbook/*.json` files are the strategist's output format — richer than
ares's YAML (reactions, composition, conditional steps, event triggers). This
module emits the subset ares understands so its BuildRunner can execute the
opening, while the rest of our schema (reactions, composition_targets,
opponent priors) stays as the bot's own concern.

CLI:
    python -m playbook.compile playbook/tvz.json -o bot/terran_builds.yml --race Terran

Library:
    from playbook.compile import compile_to_ares_yaml
    yaml_text = compile_to_ares_yaml({"TvZ": tvz_dict}, bot_race="Terran")

v0 limitations (documented, not silent):
  - Only supply triggers are fully supported.
  - Event triggers (`{on: "<thing>_complete"}`) are resolved heuristically by
    walking back to the most recent step that produces <thing> and adding +2
    supply. This is a coarse build-time approximation, good enough for v0.
  - Time triggers (`{seconds: N}`) are not supported — emitted as YAML comments
    so the user sees what was dropped.
  - The `chrono` flag on a non-chrono step is ignored (correct chrono targeting
    requires structure-producer mapping; users should use explicit
    `kind: chrono` steps for now).
  - `only_if` / `skip_if` are not enforced at compile time. The step is emitted
    unconditionally, with a YAML comment noting the intended condition. The
    bot's reaction layer is responsible for runtime overrides via
    `build_order_runner.switch_opening`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml


# Race-specific aliases mapping python-sc2 UnitTypeId names (PascalCase) to
# the shorthand verbs ares-sc2's build runner accepts. Anything not aliased
# falls through as `target.lower()`, which ares interprets as a UnitTypeId.
ALIASES_PROTOSS: dict[str, str] = {
    "Probe": "worker",
    "Pylon": "supply",
    "Assimilator": "gas",
}

ALIASES_TERRAN: dict[str, str] = {
    "SCV": "worker",
    "SupplyDepot": "supply",
    "Refinery": "gas",
    "OrbitalCommand": "orbital",
    "PlanetaryFortress": "planetary",
}

ALIASES_ZERG: dict[str, str] = {
    "Drone": "worker",
    "Overlord": "supply",
    "Extractor": "gas",
}

ALIASES_BY_RACE: dict[str, dict[str, str]] = {
    "Protoss": ALIASES_PROTOSS,
    "Terran": ALIASES_TERRAN,
    "Zerg": ALIASES_ZERG,
}

# Map our matchup labels to the bot's race so a single playbook file is enough
# to infer which alias table to use without extra CLI flags.
MATCHUP_TO_RACE: dict[str, str] = {
    "TvT": "Terran", "TvP": "Terran", "TvZ": "Terran",
    "PvP": "Protoss", "PvT": "Protoss", "PvZ": "Protoss",
    "ZvZ": "Zerg",   "ZvT": "Zerg",    "ZvP": "Zerg",
}


def target_to_ares_command(target: str, race: str) -> str:
    """Map a PascalCase UnitTypeId to an ares build-runner command word."""
    return ALIASES_BY_RACE.get(race, {}).get(target, target.lower())


def _precompute_supplies(steps: list[dict]) -> list[int | None]:
    """Single forward pass that assigns a supply value to every step.

    - Supply triggers pass through unchanged.
    - Event triggers '<thing>_complete' resolve by finding the most recent
      prior `produce` step whose target matches <thing> (case + underscore
      insensitive) and adding +2 supply as a build-time offset. Because we
      walk forward, the producer's own supply has already been resolved by
      the time we look at it, so chains of event triggers resolve correctly
      (e.g. `barracks_complete` -> `barracks_tech_lab_complete` -> ...).
    - Time triggers ('seconds') and unresolvable events return None; the
      caller emits a SKIPPED comment for those.
    """
    supplies: list[int | None] = [None] * len(steps)
    for i, step in enumerate(steps):
        trigger = step["trigger"]
        if "supply" in trigger:
            supplies[i] = trigger["supply"]
            continue
        if "on" in trigger:
            event = trigger["on"]
            if not event.endswith("_complete"):
                continue
            thing = event[: -len("_complete")].replace("_", "").lower()
            # Walk FORWARD to find the FIRST producer of <thing>, matching the
            # natural reading of "X_complete" as "when the first X finishes."
            # Users wanting a later occurrence can express it with an explicit
            # supply trigger.
            for j in range(i):
                action = steps[j].get("action", {})
                if action.get("kind") != "produce":
                    continue
                if action.get("target", "").replace("_", "").lower() != thing:
                    continue
                producer_supply = supplies[j]
                if producer_supply is not None:
                    supplies[i] = producer_supply + 2
                break
    return supplies


def _step_to_command(step: dict, supply: int, race: str) -> str | None:
    """Render one playbook build_step as one ares OpeningBuildOrder line."""
    action = step["action"]
    kind = action["kind"]
    target = action["target"]
    count = action.get("count", 1)

    if kind == "produce":
        cmd = f"{supply} {target_to_ares_command(target, race)}"
    elif kind == "research":
        cmd = f"{supply} {target.lower()}"
    elif kind == "expand":
        cmd = f"{supply} expand"
    elif kind == "chrono":
        cmd = f"{supply} chrono @ {target.lower()}"
    else:
        return None

    if count > 1 and kind == "produce":
        cmd = f"{cmd} x{count}"
    return cmd


def compile_one_playbook(playbook: dict, race: str | None = None) -> dict:
    """Compile one playbook JSON dict into one ares Builds-entry dict.

    Args:
        playbook: parsed contents of a playbook/*.json file.
        race: bot's race, or None to infer from playbook.metadata.matchup.

    Returns:
        A dict suitable as a value under `Builds:` in ares's *_builds.yml.
    """
    if race is None:
        matchup = playbook["metadata"]["matchup"]
        race = MATCHUP_TO_RACE.get(matchup, "Protoss")

    macro = playbook.get("macro_rules", {})
    steps = playbook.get("build_order", [])
    supplies = _precompute_supplies(steps)

    # ares-sc2's BuildRunner parses each item in OpeningBuildOrder as a
    # command. Comment-style strings (even quoted ones starting with '#')
    # silently break the build runner — it marks the order completed and
    # the bot does nothing for the rest of the match. So we keep
    # OpeningBuildOrder pure (one valid command per item) and surface
    # NOTE / SKIPPED info via a sibling `notes` field on each Builds entry,
    # which ares ignores.
    resolved: list[tuple[int, str]] = []
    notes: list[str] = []

    for i, step in enumerate(steps):
        supply = supplies[i]
        trigger = step["trigger"]
        if supply is None:
            if "seconds" in trigger:
                notes.append(
                    f"SKIPPED time trigger ({trigger['seconds']}s) not supported in v0: "
                    f"{step['action']['kind']} {step['action']['target']}"
                )
            elif "on" in trigger:
                notes.append(
                    f"SKIPPED unresolved event '{trigger['on']}': "
                    f"{step['action']['kind']} {step['action']['target']}"
                )
            continue

        cmd = _step_to_command(step, supply, race)
        if cmd is None:
            notes.append(f"SKIPPED unsupported kind: {step['action']}")
            continue

        condition_note = ""
        if "only_if" in step:
            condition_note += f" only_if={step['only_if']}"
        if "skip_if" in step:
            condition_note += f" skip_if={step['skip_if']}"
        if condition_note:
            notes.append(
                f"NOTE conditional step (runtime-only): {cmd}{condition_note}"
            )
        resolved.append((supply, cmd))

    # Stable sort by supply preserves declared order among same-supply steps.
    resolved.sort(key=lambda pair: pair[0])
    opening: list[str] = [cmd for _, cmd in resolved]

    entry: dict[str, Any] = {
        "AutoSupplyAtSupply": 0,
        "ConstantWorkerProductionTill": macro.get("worker_cap", 22),
        "OpeningBuildOrder": opening,
    }
    # Keep the human-readable notes alongside the build for debugging /
    # round-tripping; ares ignores unknown keys on a Builds entry.
    if notes:
        entry["notes"] = notes
    return entry


# Inverse map: which enemy race does each matchup label correspond to?
# This is critical for BuildChoices construction — ares-sc2 keys BuildChoices
# by ENEMY race, not by our bot's race. Getting this wrong means ares never
# finds a build to run and silently marks the build order complete on game
# start.
MATCHUP_TO_ENEMY_RACE: dict[str, str] = {
    "TvT": "Terran", "TvP": "Protoss", "TvZ": "Zerg",
    "PvT": "Terran", "PvP": "Protoss", "PvZ": "Zerg",
    "ZvT": "Terran", "ZvP": "Protoss", "ZvZ": "Zerg",
}

ALL_ENEMY_RACES: tuple[str, ...] = ("Protoss", "Terran", "Zerg", "Random")


def _build_choices(
    builds: dict[str, dict],
    bot_name: str,
) -> dict[str, dict]:
    """Construct ares's BuildChoices section keyed by ENEMY race.

    For each of the 4 enemy races (Protoss / Terran / Zerg / Random), find the
    build whose matchup targets that race. If no exact match exists (e.g. we
    only have TvZ but the opponent is Protoss), fall back to whichever build
    we DO have — better to run the wrong matchup's build than to silently
    have ares run nothing.
    """
    choices: dict[str, dict] = {}
    matchup_for_enemy: dict[str, str | None] = {race: None for race in ALL_ENEMY_RACES}

    # First pass: exact matches.
    for matchup in builds:
        enemy = MATCHUP_TO_ENEMY_RACE.get(matchup)
        if enemy and matchup_for_enemy.get(enemy) is None:
            matchup_for_enemy[enemy] = matchup

    # Fallback for enemy races we don't have a build for: use any available
    # build. Prefer Zerg matchup (most common at top of aiarena), then Terran,
    # then Protoss.
    fallback_priority = ["TvZ", "TvT", "TvP", "PvZ", "PvT", "PvP", "ZvZ", "ZvT", "ZvP"]
    available_fallback: str | None = None
    for candidate in fallback_priority:
        if candidate in builds:
            available_fallback = candidate
            break
    if available_fallback is None and builds:
        available_fallback = next(iter(builds.keys()))

    # Random gets the same fallback as anyone we don't have a specific build for.
    for race in ALL_ENEMY_RACES:
        chosen = matchup_for_enemy[race] or available_fallback
        if chosen is None:
            continue  # no builds at all — skip this enemy race
        choices[race] = {
            "BotName": f"{bot_name}_v{race}",
            "Cycle": [chosen],
        }
    return choices


def compile_to_ares_yaml(
    playbooks_by_matchup: dict[str, dict],
    bot_race: str = "Terran",
    bot_name: str = "SC2Agent",
) -> str:
    """Compile a {matchup -> playbook} dict into a complete ares *_builds.yml string.

    Output structure:
      - Each playbook becomes a named entry under top-level Builds.
      - BuildChoices is keyed by ENEMY race (Protoss / Terran / Zerg /
        Random). Each enemy race points at the matchup-appropriate build,
        falling back to whichever build is available when we don't have one
        for that specific matchup yet (v0 ships only TvZ; PvP / TvT will be
        added later).
    """
    builds = {
        matchup: compile_one_playbook(pb, bot_race)
        for matchup, pb in playbooks_by_matchup.items()
    }
    doc = {
        "UseData": True,
        "BuildSelection": "Cycle",
        "MinGamesWinrateBased": 5,
        "BuildChoices": _build_choices(builds, bot_name),
        "Builds": builds,
    }
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, indent=2)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Compile SC2 Agent playbook JSONs into an ares-sc2 builds YAML.",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="One or more playbook JSON files. Matchup is read from each "
             "file's metadata.matchup field.",
    )
    parser.add_argument(
        "-o", "--output",
        required=True,
        help="Output YAML path (e.g. bot/terran_builds.yml).",
    )
    parser.add_argument(
        "--race",
        default="Terran",
        choices=["Protoss", "Terran", "Zerg"],
        help="Our bot's race (default Terran).",
    )
    parser.add_argument(
        "--bot-name",
        default="SC2Agent",
        help="Bot identifier written into BuildChoices.<Race>.BotName.",
    )
    args = parser.parse_args(argv)

    playbooks: dict[str, dict] = {}
    for path_str in args.inputs:
        path = Path(path_str)
        with path.open() as f:
            pb = json.load(f)
        matchup = pb["metadata"]["matchup"]
        if matchup in playbooks:
            print(
                f"warning: duplicate matchup {matchup}; last input wins",
                file=sys.stderr,
            )
        playbooks[matchup] = pb

    yaml_text = compile_to_ares_yaml(
        playbooks, bot_race=args.race, bot_name=args.bot_name,
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml_text)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
