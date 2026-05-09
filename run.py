"""Entry point for both local testing and aiarena ladder play.

Local: `python run.py` — picks a random map from your SC2 installation and
plays vs a built-in CheatVision Macro AI.

Ladder: aiarena's LadderManager invokes this script with `--LadderServer
<host> --GamePort <p> --StartPort <p> --OpponentId <id>`; we route to
bot/ladder.py to join the running game.

Adapted from the ares-sc2 template, with our SC2Agent class wired up.
"""

from __future__ import annotations

import os
import platform
import random
import sys
from os import path
from pathlib import Path
from typing import List

import yaml
from loguru import logger

# Ares-sc2 looks for both `config.yml` and `<race>_builds.yml` in
# path.abspath(".") at import / on_before_start time. Our config and compiled
# YAML live under bot/, so we change cwd to that directory BEFORE any ares
# imports happen and BEFORE instantiating SC2Agent. Maps and ares-sc2 source
# paths are resolved as absolute paths below, so chdir doesn't break them.
_REPO_ROOT = Path(__file__).resolve().parent
os.chdir(_REPO_ROOT / "bot")

# Ares-sc2 is git-cloned alongside this project (not pip-installed). Add its
# source dirs to sys.path BEFORE importing anything that pulls from `ares`.
# Use absolute paths so they work regardless of cwd.
_ARES_ROOT = (_REPO_ROOT.parent / "ares-sc2").resolve()
sys.path.append(str(_ARES_ROOT / "src" / "ares"))
sys.path.append(str(_ARES_ROOT / "src"))
sys.path.append(str(_ARES_ROOT))

from sc2 import maps
from sc2.data import AIBuild, Difficulty, Race
from sc2.main import run_game
from sc2.player import Bot, Computer

from bot.ladder import run_ladder_game
from bot.main import SC2Agent

CONFIG_FILE: str = "bot/config.yml"
MAP_FILE_EXT: str = "SC2Map"
CONFIG_KEY_NAME: str = "MyBotName"
CONFIG_KEY_RACE: str = "MyBotRace"

# Candidate Maps directories by OS. We pick the first one that exists and
# has any *.SC2Map files. The user-folder paths come first because they
# don't require admin rights to write to (handy when dropping in a fresh
# aiarena map pack).
plt = platform.system()
if plt == "Windows":
    # OneDrive often hijacks Documents on modern Windows; check those paths
    # first so we don't hit "no maps" when files are sitting in OneDrive.
    MAPS_CANDIDATES: list[str] = [
        path.expandvars(r"%OneDrive%\Documents\StarCraft II\Maps"),
        path.expanduser(r"~\OneDrive\Documents\StarCraft II\Maps"),
        path.expanduser(r"~\Documents\StarCraft II\Maps"),
        r"C:\Program Files (x86)\StarCraft II\Maps",
        r"C:\Program Files\StarCraft II\Maps",
    ]
elif plt == "Darwin":
    MAPS_CANDIDATES = [
        path.expanduser("~/Library/Application Support/Blizzard/StarCraft II/Maps"),
        "/Applications/StarCraft II/Maps",
    ]
elif plt == "Linux":
    MAPS_CANDIDATES = [
        path.expanduser("~/Games/battlenet/drive_c/Program Files (x86)/StarCraft II/Maps"),
        path.expanduser("~/StarCraftII/Maps"),
    ]
else:
    logger.error(f"{plt} not supported")
    sys.exit(1)


def _find_maps_dir() -> str | None:
    for candidate in MAPS_CANDIDATES:
        if path.isdir(candidate):
            has_maps = any(
                p.suffix == f".{MAP_FILE_EXT}" for p in Path(candidate).glob(f"*.{MAP_FILE_EXT}")
            )
            if has_maps:
                return candidate
    # Return the first candidate even if empty so the warning message is informative
    return MAPS_CANDIDATES[0] if MAPS_CANDIDATES else None


def _read_bot_identity() -> tuple[str, Race]:
    """Parse bot/config.yml for the bot name + race; defaults to SC2Agent /
    Terran if config is absent."""
    bot_name = "SC2Agent"
    race = Race.Terran

    config_path = path.abspath(CONFIG_FILE)
    if path.isfile(config_path):
        with open(config_path, encoding="utf-8") as fh:
            config: dict = yaml.safe_load(fh) or {}
        if CONFIG_KEY_NAME in config:
            bot_name = config[CONFIG_KEY_NAME]
        if CONFIG_KEY_RACE in config:
            race = Race[str(config[CONFIG_KEY_RACE]).title()]
    return bot_name, race


def main() -> None:
    bot_name, race = _read_bot_identity()
    bot1 = Bot(race, SC2Agent(), bot_name)

    if "--LadderServer" in sys.argv:
        logger.info("Starting ladder game...")
        result, opponent_id = run_ladder_game(bot1)
        logger.info(f"Result: {result} vs opponent {opponent_id}")
        return

    # Local game: pick a random map from whichever Maps dir actually has
    # files. If python-sc2's hardcoded SC2 install/Maps path is empty (e.g.
    # because OneDrive captured the user's Documents folder), monkey-patch
    # sc2.paths.Paths.MAPS so maps.get(name) finds files where they actually
    # live. Saves the user from needing admin rights to write into Program
    # Files.
    maps_dir = _find_maps_dir()
    if maps_dir:
        from sc2.paths import Paths as _SC2Paths
        _ = _SC2Paths.BASE  # force lazy __setup so our override isn't reset
        _SC2Paths.MAPS = Path(maps_dir)
        logger.info(f"using maps from {maps_dir}")

        # python-sc2's Map class stores `relative_path` (computed from
        # path.relative_to(Paths.MAPS)) and sends THAT to SC2. Then SC2
        # resolves the relative path against its own dataDir/Maps — which is
        # Program Files\Maps and will be empty if our maps live under
        # OneDrive. Force relative_path to be absolute so SC2 receives the
        # full path and finds the file regardless of where it lives.
        import sc2.maps as _sc2maps
        _orig_map_init = _sc2maps.Map.__init__

        def _absolute_path_map_init(self, path):  # type: ignore[no-redef]
            self.path = Path(path).absolute()
            self.relative_path = self.path

        _sc2maps.Map.__init__ = _absolute_path_map_init  # type: ignore[method-assign]

    # SC2 startup time varies a lot (observed 7s in one run, >30s in
    # another on the same machine). python-sc2 calls ws_connect() with an
    # int timeout that aiohttp's TCP connector ignores — the underlying
    # socket connect hits ~30s and the whole launch fails before SC2 is
    # actually listening. Patch SC2Process._connect to first poll the port
    # until SC2 binds it, THEN do the websocket handshake.
    import asyncio as _asyncio
    import socket as _socket

    import sc2.sc2process as _sc2process

    _orig_connect = _sc2process.SC2Process._connect

    async def _connect_with_port_wait(self):  # type: ignore[no-redef]
        deadline = _asyncio.get_event_loop().time() + 180.0
        while _asyncio.get_event_loop().time() < deadline:
            try:
                with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                    s.settimeout(1.0)
                    s.connect(("127.0.0.1", self._port))
                break  # port is listening — proceed to WS handshake
            except (ConnectionRefusedError, OSError):
                await _asyncio.sleep(0.5)
        else:
            raise TimeoutError(
                f"SC2 didn't bind port {self._port} within 180s; "
                "the binary may be hung. Try launching SC2 once via "
                "Battle.net to warm caches, then re-run."
            )
        return await _orig_connect(self)

    _sc2process.SC2Process._connect = _connect_with_port_wait  # type: ignore[method-assign]

    map_list: List[str] = []
    if maps_dir:
        map_list = [
            p.name.replace(f".{MAP_FILE_EXT}", "")
            for p in Path(maps_dir).glob(f"*.{MAP_FILE_EXT}")
            if p.is_file()
        ]
    if not map_list:
        logger.warning(
            "No maps found in any of: {}. Drop the aiarena map pack into one "
            "of those folders. Falling back to a hardcoded ladder map list, "
            "which will fail to load if those .SC2Map files aren't present.",
            MAPS_CANDIDATES,
        )
        map_list = [
            "PylonAIE_v4", "PersephoneAIE_v4", "TorchesAIE_v4",
            "IncorporealAIE_v4", "MagannathaAIE_v2", "UltraloveAIE_v2",
        ]

    enemy_race = random.choice([Race.Zerg, Race.Terran, Race.Protoss])
    # The bot is comfortably winning vs Easy after the home-defense fix —
    # bumping the default to Medium for a real challenge. Override via
    # SC2AGENT_DIFFICULTY env var to climb (Hard, Harder, VeryHard,
    # CheatVision, CheatMoney, CheatInsane) or fall back to Easy.
    difficulty_name = os.environ.get("SC2AGENT_DIFFICULTY", "Medium")
    difficulty = Difficulty[difficulty_name] if hasattr(Difficulty, difficulty_name) else Difficulty.Medium
    logger.info(
        f"Starting local game on a random map vs {enemy_race} ({difficulty.name})..."
    )
    run_game(
        maps.get(random.choice(map_list)),
        [bot1, Computer(enemy_race, difficulty, ai_build=AIBuild.Macro)],
        realtime=False,
    )


if __name__ == "__main__":
    main()
