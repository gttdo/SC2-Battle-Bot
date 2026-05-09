# SC2-Battle-Bot

An Opus-powered StarCraft II bot for the [aiarena.net](https://aiarena.net) ladder.

## Architecture

Two-tier design driven by the ladder's no-network-during-match rule:

- **`bot/`** — in-game bot. Fast deterministic Python on top of [python-sc2](https://github.com/BurnySc2/python-sc2). Executes a playbook (JSON). No LLM calls at runtime. This is what gets zipped and uploaded.
- **`strategist/`** — offline pipeline. Pulls match results and replays via the [aiarena Data API](https://aiarena.net/wiki/data-api/), parses replays with `sc2reader`, and uses Claude Opus to regenerate the playbook between submissions. Dev-only, never ships.
- **`playbook/`** — JSON schema and reference playbooks. The contract between the strategist and the bot.
- **`tests/`** — unit + sim tests for the bot and strategist.

## Race

Single-race specialist (Protoss) for v0. Random / multi-race is a possible later expansion.

## Status

Scaffolding only. Not yet runnable.
