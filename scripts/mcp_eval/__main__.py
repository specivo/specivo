"""Command line: ``uv run python -m scripts.mcp_eval [--dry-run] [--mode both|lean|full] ...``. See README.md."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from scripts.mcp_eval.harness import (
    ModelConfig,
    comparison_table,
    dry_run,
    model_run,
    summary_row,
    to_json,
)
from scripts.mcp_eval.scenarios import SCENARIOS
from specivo.mcp.tool_help import FULL, LEAN, ToolListing


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m scripts.mcp_eval", description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="run the reference calls, no model")
    parser.add_argument("--mode", choices=("both", LEAN, FULL), default="both", help="which tools/list to serve")
    parser.add_argument("--toolsets", default="all", help="toolsets the connection loads (default: all)")
    parser.add_argument("--only", default="", help="comma-separated scenario ids")
    parser.add_argument("--out", type=Path, help="write every task result as JSON to this file")
    return parser.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    scenarios = [scenario for scenario in SCENARIOS if not args.only or scenario.id in args.only.split(",")]
    modes: list[ToolListing] = [LEAN, FULL] if args.mode == "both" else [args.mode]
    rows: dict[str, dict[str, Any]] = {}
    everything: list[dict[str, Any]] = []
    failed = 0
    config = None if args.dry_run else ModelConfig.from_env()
    for mode in modes:
        if config is None:
            results = await dry_run(scenarios, mode, args.toolsets)
            for result in results:
                status = "ok  " if result.error is None else "FAIL"
                print(f"{status} [{mode}] {result.scenario}: {' -> '.join(result.tools_called)}")
                if result.error:
                    print(f"     {result.error}")
            invalid = sum(result.error is not None for result in results)
            failed += invalid
            rows[mode] = {"scenarios": len(results), "valid": len(results) - invalid}
        else:
            results, size = await model_run(scenarios, mode, args.toolsets, config)
            rows[mode] = {"tools/list chars": size, **summary_row(results)}
            for result in results:
                flag = "ok  " if result.success else "MISS"
                print(f"{flag} [{mode}] {result.scenario}: {' -> '.join(result.tools_called) or '(no call)'}")
        everything += to_json(results)
    print()
    print(comparison_table(rows))
    if args.out:
        args.out.write_text(json.dumps(everything, indent=2))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.ERROR)
    return asyncio.run(_main(_arguments(argv)))


if __name__ == "__main__":
    sys.exit(main())
