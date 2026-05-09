"""Unit tests for the reactions registry and detector logic.

We avoid running anything that requires ares / a real BotAI — these tests
exercise the registry's bookkeeping and the ling_flood detector against
duck-typed mock bot objects."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from bot import reactions


# ---------------------------------------------------------------------------
# Mock bot helpers
# ---------------------------------------------------------------------------

class _MockUnits(list):
    """Tiny stand-in for python-sc2's Units that supports of_type / closer_than /
    .amount, just enough for the ling-flood detector to work in tests."""

    def of_type(self, type_id):
        return _MockUnits([u for u in self if u.type_id == type_id])

    def closer_than(self, radius, target):
        # Squared-distance comparison; mock units carry a .position tuple.
        tx, ty = target.position
        return _MockUnits(
            u for u in self
            if (u.position[0] - tx) ** 2 + (u.position[1] - ty) ** 2 <= radius ** 2
        )

    @property
    def amount(self):
        return len(self)


def _mock_bot(time: float = 0.0, lings_at: list[tuple[float, float]] | None = None,
              townhall_at: tuple[float, float] | None = (50.0, 50.0)) -> Any:
    """Build a lightweight mock with just the attributes ling_flood touches."""
    try:
        from sc2.ids.unit_typeid import UnitTypeId
    except ImportError:
        pytest.skip("python-sc2 not installed; ling-flood detector can't import UnitTypeId")
    enemy_units = _MockUnits()
    if lings_at:
        for x, y in lings_at:
            enemy_units.append(SimpleNamespace(
                type_id=UnitTypeId.ZERGLING, position=(x, y),
            ))
    townhalls = _MockUnits()
    if townhall_at:
        townhalls.append(SimpleNamespace(position=townhall_at))
    return SimpleNamespace(
        time=time,
        enemy_units=enemy_units,
        townhalls=townhalls,
        chat_messages=[],
        chat_send=lambda msg, team_only=False: None,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

async def _no_op_responder(bot, reaction):
    pass


def test_register_stores_reactions_by_name():
    registry = reactions.Registry(bot=object())
    r = reactions.Reaction("foo", lambda b: False, _no_op_responder)
    registry.register(r)
    assert registry.reactions["foo"] is r


def test_prepop_below_confidence_floor_is_ignored():
    registry = reactions.Registry(bot=object())
    registry.register(reactions.Reaction("foo", lambda b: False, _no_op_responder))
    registry.prepop(["foo"], confidence=0.2)
    assert "foo" not in registry.fired


def test_prepop_above_floor_marks_fired():
    registry = reactions.Registry(bot=object())
    registry.register(reactions.Reaction("foo", lambda b: False, _no_op_responder))
    registry.prepop(["foo"], confidence=0.5)
    assert "foo" in registry.fired


def test_prepop_unknown_name_skipped_with_warning():
    registry = reactions.Registry(bot=object())
    # No reaction named 'foo' is registered. prepop should not raise and
    # should not add anything to fired.
    registry.prepop(["foo"], confidence=0.9)
    assert registry.fired == set()


def test_update_runs_responder_for_newly_triggered():
    fired_responses: list[str] = []

    async def responder(bot, reaction):
        fired_responses.append(reaction.name)

    registry = reactions.Registry(bot=object())
    registry.register(reactions.Reaction("foo", lambda b: True, responder))
    triggered = asyncio.run(registry.update())
    assert triggered == ["foo"]
    assert fired_responses == ["foo"]
    # Second update: no new triggers, no new responder calls
    fired_responses.clear()
    triggered = asyncio.run(registry.update())
    assert triggered == []
    assert fired_responses == []


def test_update_skips_already_fired_reactions():
    calls: list[str] = []

    async def responder(bot, reaction):
        calls.append(reaction.name)

    registry = reactions.Registry(bot=object())
    registry.register(reactions.Reaction("foo", lambda b: True, responder))
    registry.fired.add("foo")  # simulate prepop
    triggered = asyncio.run(registry.update())
    assert triggered == []
    assert calls == []


def test_buggy_detector_does_not_crash_update():
    def boom(_b):
        raise RuntimeError("scout was off the map")

    registry = reactions.Registry(bot=object())
    registry.register(reactions.Reaction("crashy", boom, _no_op_responder))
    # Should NOT raise.
    triggered = asyncio.run(registry.update())
    assert triggered == []
    assert "crashy" not in registry.fired


# ---------------------------------------------------------------------------
# ling_flood detector
# ---------------------------------------------------------------------------

def test_ling_flood_fires_on_six_lings_near_townhall():
    bot = _mock_bot(time=180, lings_at=[(50, 50)] * 6)
    assert reactions.detect_ling_flood(bot) is True


def test_ling_flood_quiet_with_few_lings():
    bot = _mock_bot(time=180, lings_at=[(50, 50)] * 3)
    assert reactions.detect_ling_flood(bot) is False


def test_ling_flood_quiet_after_time_window():
    bot = _mock_bot(time=300, lings_at=[(50, 50)] * 8)
    assert reactions.detect_ling_flood(bot) is False


def test_ling_flood_quiet_when_lings_far_from_base():
    bot = _mock_bot(time=180, lings_at=[(5, 5)] * 8, townhall_at=(150.0, 150.0))
    assert reactions.detect_ling_flood(bot) is False


def test_ling_flood_quiet_when_no_townhalls():
    bot = _mock_bot(time=180, lings_at=[(50, 50)] * 8, townhall_at=None)
    assert reactions.detect_ling_flood(bot) is False


# ---------------------------------------------------------------------------
# Default registry construction
# ---------------------------------------------------------------------------

def test_build_default_registry_includes_ling_flood():
    registry = reactions.build_default_registry(bot=object())
    assert "ling_flood" in registry.reactions
    # Other v0 vocabulary entries are stubbed but registered:
    for name in ("early_pool", "mass_roach", "mutas", "nydus", "baneling_drop"):
        assert name in registry.reactions
