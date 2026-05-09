# SC2 Agent

An Opus-powered StarCraft II agent for the [aiarena.net](https://aiarena.net) ladder.

The "agent" is the system as a whole — a long-lived offline brain that learns from ladder results, plus a fast in-game executor that plays the actual matches. The split is forced by aiarena's no-network-during-match rule: the LLM runs *between* games, not during them.

## Architecture

- **`bot/`** — in-game executor. Built on [ares-sc2](https://github.com/AresSC2/ares-sc2) (which sits on [python-sc2](https://github.com/BurnySc2/python-sc2)). Reads a compiled `*_builds.yml` and runs ares's BuildRunner. No LLM calls, no network. This is what gets zipped and uploaded as an aiarena "bot."
- **`strategist/`** — offline brain. Pulls match results and replays via the [aiarena Data API](https://aiarena.net/wiki/data-api/), parses replays with `sc2reader`, and uses Claude Opus to regenerate the playbook between submissions. Dev-only, never ships.
- **`playbook/`** — JSON schema and per-matchup playbooks (`tvz.json`, etc.). The strategist's output format — richer than ares's YAML so the LLM has more to reason about (reactions, composition, conditional steps). [`compile.py`](playbook/compile.py) translates JSON → ares YAML. Schema enforces shape; the bot enforces vocabulary.
- **`tests/`** — unit + sim tests for the bot and strategist.

## Build/test/run workflow

```powershell
# 1. Install Python deps
pip install -r requirements.txt

# 2. Clone ares-sc2 alongside this repo (NOT pip-installable; run.py
#    adds its src directories to sys.path at runtime)
cd ..
git clone https://github.com/AresSC2/ares-sc2.git
cd SC2-Battle-Bot

# 3. Validate everything
python -m pytest tests

# 4. Compile playbook JSON -> ares-sc2 YAML
python -m playbook.compile playbook/tvz.json -o bot/terran_builds.yml --race Terran

# 5. Run a local test game (requires StarCraft II + maps installed)
python run.py
```

Aiarena ladder play uses the same `run.py`; LadderManager invokes it with
`--LadderServer <host> --GamePort <p> --StartPort <p> --OpponentId <id>`
and `bot/ladder.py` joins the running game.

## Adaptation, in four tiers

The agent learns at four different timescales. Each catches things the others miss.

1. **Within a match** (milliseconds): scripted reactions in the playbook fire when scouting detects known enemy patterns (proxy gate, mass roach, etc.). No LLM — too slow for real-time.
2. **Across matches against the same opponent** (game-to-game): the bot writes a per-opponent file to aiarena's persistent `./data` directory after every match. At game start, if we've played this opponent before, we load priors that bias the playbook execution — earlier defense vs known rushers, tech-target shifts vs known compositions, pre-popped reactions for highly predictable openings. Schema in [`playbook/opponent_schema.json`](playbook/opponent_schema.json), worked example in [`playbook/opponents.example.json`](playbook/opponents.example.json).
3. **Between submissions** (hours to days): the offline `strategist/` runs a two-stage Opus pipeline — replay parsing produces structured postmortems, then a second pass rewrites the matchup playbook from those postmortems. Faster and more sample-efficient than dumping raw replays at an LLM.
4. **Across playbook variants** (long-term): the strategist generates multiple variants per matchup; the bot picks one per match via Thompson sampling on recent winrate. Forces exploration, prevents local-maximum lock-in.

## Why "agent" + "bot"

The project is the **agent**. The thing that submits to and runs on aiarena is a **bot** — that's the term aiarena and python-sc2 use, and we keep it for the in-game piece so the code reads naturally against those ecosystems.

## Race

Single-race specialist (Terran) for v0. Random / multi-race is a possible later expansion.

## Status

v0.2 schema + working JSON → ares YAML compiler + bot scaffold + tier-1/tier-2 adaptation modules. Reference TvZ playbook compiles cleanly. The bot subclasses `AresBot`, dispatches build by `enemy_race` at `on_start`, runs the reactions registry each `on_step`, and writes per-(opponent, matchup) priors to `./data/opponents/` at `on_end`.

What's verified: 50 unit tests passing across the schema, compiler, opponent priors module, reactions registry + `ling_flood` detector, and match observation recorder. What's NOT yet verified end-to-end: the actual `bot/main.py` requires ares-sc2 + a real SC2 install to run a game; the wall-off response handler for `ling_flood` is a logged stub pending ares mediator integration. Next iterations: real placement responder, additional reaction detectors, and the offline strategist.
