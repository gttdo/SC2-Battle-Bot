"""CLI entry point: regenerate a per-matchup playbook from match observations.

Examples
--------
    # Show what would be sent to Claude (no API call, no API key needed)
    python -m strategist.cli TvZ --dry-run

    # Real regeneration (requires ANTHROPIC_API_KEY)
    python -m strategist.cli TvZ

    # Write to an alternate output path
    python -m strategist.cli TvZ --output /tmp/new_tvz.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from strategist.observations import load_observations, summarize

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK_DIR = PROJECT_ROOT / "playbook"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate a per-matchup playbook from match observations.",
    )
    parser.add_argument("matchup", choices=["TvT", "TvP", "TvZ"])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the request payload without calling Claude. No API key needed.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path (default: playbook/<matchup>.json).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the Anthropic model ID (default: claude-opus-4-7).",
    )
    args = parser.parse_args(argv)

    observations = load_observations(args.matchup)
    summary = summarize(observations)
    print(
        f"[strategist] {args.matchup}: loaded {summary['match_count']} obs, "
        f"{summary['wins']}w / {summary['losses']}l "
        f"(winrate={summary['winrate']:.2f})",
        file=sys.stderr,
    )

    if summary["match_count"] == 0:
        print(
            "[strategist] no observations on disk for this matchup; "
            "nothing to regenerate. Play some games first.",
            file=sys.stderr,
        )
        return 1

    # Deferred import — keeps the CLI snappy and lets the dry-run path
    # work even if anthropic isn't installed.
    from strategist.regenerate import regenerate_playbook, DEFAULT_MODEL

    kwargs: dict = {"dry_run": args.dry_run}
    if args.model:
        kwargs["model"] = args.model

    result = regenerate_playbook(args.matchup, summary, **kwargs)

    if args.dry_run:
        # Show prompt structure without firing the API call.
        print(json.dumps(result, indent=2, default=str))
        return 0

    output_path = args.output or PLAYBOOK_DIR / f"{args.matchup.lower()}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"[strategist] wrote {output_path} (model={args.model or DEFAULT_MODEL})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
