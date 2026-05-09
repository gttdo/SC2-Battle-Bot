"""Tests for the playbook -> ares-sc2 YAML compiler."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from playbook.compile import (
    compile_one_playbook,
    compile_to_ares_yaml,
    target_to_ares_command,
)

PLAYBOOK_DIR = Path(__file__).resolve().parent.parent / "playbook"


def _load(p: Path) -> dict:
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def test_target_to_ares_terran_aliases():
    assert target_to_ares_command("SCV", "Terran") == "worker"
    assert target_to_ares_command("SupplyDepot", "Terran") == "supply"
    assert target_to_ares_command("Refinery", "Terran") == "gas"
    assert target_to_ares_command("OrbitalCommand", "Terran") == "orbital"
    # Anything not aliased falls through to lowercase
    assert target_to_ares_command("Marine", "Terran") == "marine"
    assert target_to_ares_command("BarracksTechLab", "Terran") == "barrackstechlab"


def test_target_to_ares_protoss_aliases():
    assert target_to_ares_command("Probe", "Protoss") == "worker"
    assert target_to_ares_command("Pylon", "Protoss") == "supply"
    assert target_to_ares_command("Assimilator", "Protoss") == "gas"
    assert target_to_ares_command("Stalker", "Protoss") == "stalker"


def test_compile_tvz_top_level_yaml():
    pb = _load(PLAYBOOK_DIR / "tvz.json")
    yaml_text = compile_to_ares_yaml({"TvZ": pb}, bot_race="Terran", bot_name="SC2Agent")

    parsed = yaml.safe_load(yaml_text)

    assert parsed["UseData"] is True
    assert parsed["BuildSelection"] == "Cycle"
    # BuildChoices is keyed by ENEMY race. With only a TvZ build available,
    # all four enemy races (Protoss, Terran, Zerg, Random) should fall back
    # to TvZ rather than be missing entirely.
    for enemy_race in ("Protoss", "Terran", "Zerg", "Random"):
        assert enemy_race in parsed["BuildChoices"], \
            f"BuildChoices missing entry for vs {enemy_race}"
        assert parsed["BuildChoices"][enemy_race]["Cycle"] == ["TvZ"]
    assert "TvZ" in parsed["Builds"]


def test_compile_build_choices_picks_exact_matchup_when_available():
    """When playbooks for multiple matchups exist, BuildChoices should route
    each enemy race to its own matchup, not fall back."""
    pb_tvz = _load(PLAYBOOK_DIR / "tvz.json")
    pb_tvp = {  # synthetic minimal TvP playbook
        "metadata": {
            "schema_version": "0.2", "matchup": "TvP",
            "generated_at": "2026-01-01T00:00:00Z", "generator": "test",
        },
        "build_order": [
            {"trigger": {"supply": 14}, "action": {"kind": "produce", "target": "SupplyDepot"}},
        ],
        "composition_targets": {"early": {"Marine": 1.0}, "mid": {"Marine": 1.0}, "late": {"Marine": 1.0}},
        "macro_rules": {
            "worker_cap": 22, "gas_workers_per_geyser": 3,
            "supply_buffer_pct": 0.15, "max_bases": 3,
        },
    }
    yaml_text = compile_to_ares_yaml({"TvZ": pb_tvz, "TvP": pb_tvp}, bot_race="Terran")
    parsed = yaml.safe_load(yaml_text)

    # vs Zerg -> TvZ; vs Protoss -> TvP; Terran/Random fall back to first available.
    assert parsed["BuildChoices"]["Zerg"]["Cycle"] == ["TvZ"]
    assert parsed["BuildChoices"]["Protoss"]["Cycle"] == ["TvP"]
    # Terran has no exact match but falls back rather than being missing.
    assert "Terran" in parsed["BuildChoices"]
    assert "Random" in parsed["BuildChoices"]


def test_compile_tvz_opening_contains_expected_steps():
    pb = _load(PLAYBOOK_DIR / "tvz.json")
    build = compile_one_playbook(pb)
    opening = build["OpeningBuildOrder"]

    # First step: 14 SupplyDepot -> "14 supply"
    assert "14 supply" in opening
    # Barracks at 16
    assert "16 barracks" in opening
    # Orbital upgrade at 20
    assert "20 orbital" in opening
    # Expand at 21
    assert "21 expand" in opening
    # Marauder x2 at 30
    assert "30 marauder x2" in opening
    # Stim research at supply 18 (BarracksTechLab from event resolution +2)
    # Barracks was at 16 -> tech_lab at 18 (no event, since on:barracks_complete -> 18)
    # tech_lab itself triggers research at +2 = 20.
    # We don't pin exact supply for the research step (depends on resolution chain),
    # but `stimpack` should appear somewhere.
    assert any("stimpack" in line.lower() for line in opening)


def test_compile_resolves_event_trigger_via_producer():
    """on:barracks_complete should resolve to (barracks supply + 2)."""
    pb = {
        "metadata": {
            "schema_version": "0.2", "matchup": "TvZ",
            "generated_at": "2026-01-01T00:00:00Z", "generator": "test",
        },
        "build_order": [
            {"trigger": {"supply": 16}, "action": {"kind": "produce", "target": "Barracks"}},
            {"trigger": {"on": "barracks_complete"},
             "action": {"kind": "produce", "target": "BarracksTechLab"}},
        ],
        "composition_targets": {"early": {"Marine": 1.0}, "mid": {"Marine": 1.0}, "late": {"Marine": 1.0}},
        "macro_rules": {
            "worker_cap": 22, "gas_workers_per_geyser": 3,
            "supply_buffer_pct": 0.15, "max_bases": 3,
        },
    }
    build = compile_one_playbook(pb, race="Terran")
    opening = build["OpeningBuildOrder"]
    assert "18 barrackstechlab" in opening


def test_compile_event_resolves_to_first_producer_not_latest():
    """When two Barracks are produced, on:barracks_complete should attach to
    the FIRST one — that's what 'barracks_complete' means in a build order."""
    pb = {
        "metadata": {
            "schema_version": "0.2", "matchup": "TvZ",
            "generated_at": "2026-01-01T00:00:00Z", "generator": "test",
        },
        "build_order": [
            {"trigger": {"supply": 16}, "action": {"kind": "produce", "target": "Barracks"}},
            {"trigger": {"supply": 26}, "action": {"kind": "produce", "target": "Barracks"}},
            {"trigger": {"on": "barracks_complete"},
             "action": {"kind": "produce", "target": "BarracksTechLab"}},
        ],
        "composition_targets": {"early": {"Marine": 1.0}, "mid": {"Marine": 1.0}, "late": {"Marine": 1.0}},
        "macro_rules": {
            "worker_cap": 22, "gas_workers_per_geyser": 3,
            "supply_buffer_pct": 0.15, "max_bases": 3,
        },
    }
    build = compile_one_playbook(pb, race="Terran")
    opening = build["OpeningBuildOrder"]
    # First barracks at 16 -> tech lab at 18 (NOT 28 from the second barracks)
    assert "18 barrackstechlab" in opening
    assert "28 barrackstechlab" not in opening


def test_compile_event_chain_resolves_through_intermediate_event():
    """on:barracks_tech_lab_complete should chain through on:barracks_complete."""
    pb = {
        "metadata": {
            "schema_version": "0.2", "matchup": "TvZ",
            "generated_at": "2026-01-01T00:00:00Z", "generator": "test",
        },
        "build_order": [
            {"trigger": {"supply": 16}, "action": {"kind": "produce", "target": "Barracks"}},
            {"trigger": {"on": "barracks_complete"},
             "action": {"kind": "produce", "target": "BarracksTechLab"}},
            {"trigger": {"on": "barracks_tech_lab_complete"},
             "action": {"kind": "research", "target": "Stimpack"}},
        ],
        "composition_targets": {"early": {"Marine": 1.0}, "mid": {"Marine": 1.0}, "late": {"Marine": 1.0}},
        "macro_rules": {
            "worker_cap": 22, "gas_workers_per_geyser": 3,
            "supply_buffer_pct": 0.15, "max_bases": 3,
        },
    }
    build = compile_one_playbook(pb, race="Terran")
    opening = build["OpeningBuildOrder"]
    # 16 -> barracks; 18 -> tech lab; 20 -> stimpack
    assert "16 barracks" in opening
    assert "18 barrackstechlab" in opening
    assert "20 stimpack" in opening


def test_compile_unresolved_event_emits_skip_comment():
    pb = {
        "metadata": {
            "schema_version": "0.2", "matchup": "TvZ",
            "generated_at": "2026-01-01T00:00:00Z", "generator": "test",
        },
        "build_order": [
            {"trigger": {"on": "nonexistent_complete"},
             "action": {"kind": "produce", "target": "Marine"}},
        ],
        "composition_targets": {"early": {"Marine": 1.0}, "mid": {"Marine": 1.0}, "late": {"Marine": 1.0}},
        "macro_rules": {
            "worker_cap": 22, "gas_workers_per_geyser": 3,
            "supply_buffer_pct": 0.15, "max_bases": 3,
        },
    }
    build = compile_one_playbook(pb, race="Terran")
    # Skipped steps live on `notes`, not in OpeningBuildOrder, because
    # comment-style strings break ares's BuildRunner parser.
    assert all(not line.startswith("#") for line in build["OpeningBuildOrder"])
    assert any("SKIPPED" in n and "nonexistent_complete" in n for n in build.get("notes", []))


def test_compile_time_trigger_emits_skip_comment():
    pb = {
        "metadata": {
            "schema_version": "0.2", "matchup": "TvZ",
            "generated_at": "2026-01-01T00:00:00Z", "generator": "test",
        },
        "build_order": [
            {"trigger": {"seconds": 240},
             "action": {"kind": "produce", "target": "Marine"}},
        ],
        "composition_targets": {"early": {"Marine": 1.0}, "mid": {"Marine": 1.0}, "late": {"Marine": 1.0}},
        "macro_rules": {
            "worker_cap": 22, "gas_workers_per_geyser": 3,
            "supply_buffer_pct": 0.15, "max_bases": 3,
        },
    }
    build = compile_one_playbook(pb, race="Terran")
    assert all(not line.startswith("#") for line in build["OpeningBuildOrder"])
    assert any("time trigger" in n.lower() for n in build.get("notes", []))


def test_compile_skip_if_recorded_in_notes_not_in_opening():
    """ares parses every list item in OpeningBuildOrder as a command and
    silently breaks on comment-style strings, so conditional notes must
    NOT appear inline. They belong on the sibling `notes` key."""
    pb = _load(PLAYBOOK_DIR / "tvz.json")
    build = compile_one_playbook(pb)
    opening = build["OpeningBuildOrder"]
    # Every item in opening should look like a real ares command.
    for line in opening:
        assert not line.startswith("#"), f"comment leaked into OpeningBuildOrder: {line!r}"
    # The skip_if=ling_flood detail should still be captured somewhere.
    assert any("ling_flood" in n for n in build.get("notes", [])), \
        "expected ling_flood note in build['notes']"
