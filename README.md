# SC2 Agent

An Opus-powered StarCraft II agent for the [aiarena.net](https://aiarena.net) ladder.

The "agent" is the system as a whole — a long-lived offline brain that learns from ladder results, plus a fast in-game executor that plays the actual matches. The split is forced by aiarena's no-network-during-match rule: the LLM runs *between* games, not during them.

## Architecture

- **`bot/`** — in-game executor. Fast deterministic Python on top of [python-sc2](https://github.com/BurnySc2/python-sc2). Reads a per-matchup JSON playbook and plays. No LLM calls, no network. This is what gets zipped and uploaded as an aiarena "bot."
- **`strategist/`** — offline brain. Pulls match results and replays via the [aiarena Data API](https://aiarena.net/wiki/data-api/), parses replays with `sc2reader`, and uses Claude Opus to regenerate the playbook between submissions. Dev-only, never ships.
- **`playbook/`** — JSON schema and per-matchup playbooks (`pvp.json`, `pvt.json`, `pvz.json`). The contract between the strategist and the bot. Schema enforces shape; the bot enforces vocabulary.
- **`tests/`** — unit + sim tests for the bot and strategist.

## Why "agent" + "bot"

The project is the **agent**. The thing that submits to and runs on aiarena is a **bot** — that's the term aiarena and python-sc2 use, and we keep it for the in-game piece so the code reads naturally against those ecosystems.

## Race

Single-race specialist (Protoss) for v0. Random / multi-race is a possible later expansion.

## Status

Scaffolding + v0.1 playbook schema. Not yet runnable.
