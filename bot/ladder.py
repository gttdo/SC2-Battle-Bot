"""aiarena LadderManager bridge — boilerplate adapted from the ares-sc2
template. Lets python-sc2 connect to a LadderManager game running on the
aiarena container.

Source / inspiration:
  - https://github.com/AresSC2/ares-sc2-bot-template/blob/main/ladder.py
  - https://github.com/Cryptyc/Sc2LadderServer
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import aiohttp
import sc2
from sc2.client import Client
from sc2.protocol import ConnectionAlreadyClosed


def run_ladder_game(bot):
    """Parse aiarena's CLI args, set the bot's opponent_id, join the
    LadderManager-hosted game, and return (result, opponent_id)."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--GamePort", type=int, nargs="?", help="Game port")
    parser.add_argument("--StartPort", type=int, nargs="?", help="Start port")
    parser.add_argument("--LadderServer", type=str, nargs="?", help="Ladder server")
    parser.add_argument("--ComputerOpponent", type=str, nargs="?")
    parser.add_argument("--ComputerRace", type=str, nargs="?")
    parser.add_argument("--ComputerDifficulty", type=str, nargs="?")
    parser.add_argument("--OpponentId", type=str, nargs="?", help="Opponent ID")
    parser.add_argument("--RealTime", action="store_true", help="real time flag")
    args, _unknown = parser.parse_known_args()

    host = args.LadderServer or "127.0.0.1"
    host_port = args.GamePort
    lan_port = args.StartPort

    # The bot reads opponent_id off self at runtime to load opponent priors.
    bot.ai.opponent_id = args.OpponentId

    ports = [lan_port + p for p in range(1, 6)]
    portconfig = sc2.portconfig.Portconfig()
    portconfig.shared = ports[0]  # not used by python-sc2, kept for compatibility
    portconfig.server = [ports[1], ports[2]]
    portconfig.players = [[ports[3], ports[4]]]

    g = _join_ladder_game(
        host=host,
        port=host_port,
        players=[bot],
        realtime=args.RealTime,
        portconfig=portconfig,
    )
    result = asyncio.get_event_loop().run_until_complete(g)
    return result, args.OpponentId


async def _join_ladder_game(
    host,
    port,
    players,
    realtime,
    portconfig,
    save_replay_as=None,
    step_time_limit=None,
    game_time_limit=None,
):
    """Connect to a running LadderManager game without spawning our own
    sc2 process. Modified from sc2.main._join_game."""
    ws_url = f"ws://{host}:{port}/sc2api"
    ws_connection = await aiohttp.ClientSession().ws_connect(ws_url, timeout=120)

    client = Client(ws_connection)
    try:
        result = await sc2.main._play_game(
            players[0], client, realtime, portconfig, step_time_limit, game_time_limit
        )
        if save_replay_as is not None:
            await client.save_replay(save_replay_as)
    except ConnectionAlreadyClosed:
        logging.error("Connection was closed before the game ended")
        return None
    finally:
        await ws_connection.close()

    return result
