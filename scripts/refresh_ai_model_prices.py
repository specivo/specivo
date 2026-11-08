#!/usr/bin/env python3
"""Refresh ``specivo/data/ai_model_prices.json`` from LiteLLM's model price file.

The source is ``model_prices_and_context_window.json`` in the LiteLLM
repository (https://github.com/BerriAI/litellm, MIT, Copyright (c) 2023 Berri
AI). Only the text models of the first-party providers in
``specivo.core.ai_pricing_seed.LITELLM_PROVIDERS`` are kept, with their
prices per one million tokens (see ``litellm_entry`` for the field mapping).

``as_of`` is the commit time of the source file: LiteLLM holds current prices
only, so a snapshot describes that instant, and the seed loader opens new
price periods from it. Run once per release; see ``specivo/data/README.md``.

Usage::

    uv run python scripts/refresh_ai_model_prices.py             # download the newest file
    uv run python scripts/refresh_ai_model_prices.py --ref <sha>  # a given commit
    uv run python scripts/refresh_ai_model_prices.py --source prices.json \\
        --as-of 2026-11-06T06:29:17Z --commit <sha>              # a local copy, offline
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from specivo.core.ai_pricing_seed import (
    SNAPSHOT_PATH,
    format_instant,
    litellm_to_snapshot,
    parse_instant,
    parse_snapshot,
)

REPOSITORY = "BerriAI/litellm"
FILE = "model_prices_and_context_window.json"
RAW_URL = "https://raw.githubusercontent.com/{repo}/{ref}/{file}"
COMMITS_URL = "https://api.github.com/repos/{repo}/commits?path={file}&sha={ref}&per_page=1"
LICENSE_NAME = "MIT"
COPYRIGHT = "Copyright (c) 2023 Berri AI"

Fetch = Callable[[str], bytes]


def http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "specivo-price-refresh"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed https URLs
        return bytes(response.read())


def latest_commit(fetch: Fetch, ref: str) -> tuple[str, datetime]:
    """The newest commit touching the price file at *ref*: its SHA and commit time."""
    data = json.loads(fetch(COMMITS_URL.format(repo=REPOSITORY, file=FILE, ref=ref)))
    if not data:
        raise SystemExit(f"No commit of {FILE} found at {ref}")
    commit = data[0]
    return commit["sha"], parse_instant(commit["commit"]["committer"]["date"], where="commit date")


def source_info(commit: str | None) -> dict[str, str]:
    ref = commit or "main"
    return {
        "name": "LiteLLM model_prices_and_context_window.json",
        "url": f"https://github.com/{REPOSITORY}/blob/{ref}/{FILE}",
        "commit": commit or "",
        "license": LICENSE_NAME,
        "copyright": COPYRIGHT,
    }


def render(document: Mapping[str, Any]) -> str:
    """The snapshot as JSON with one model per line, so refresh diffs stay readable."""
    header = {k: v for k, v in document.items() if k != "models"}
    lines = ["{"]
    for key, value in header.items():
        lines.append(f"  {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)},")
    lines.append('  "models": [')
    models = document["models"]
    for i, item in enumerate(models):
        comma = "," if i < len(models) - 1 else ""
        lines.append(f"    {json.dumps(item, ensure_ascii=False)}{comma}")
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def build(raw: Mapping[str, Any], as_of: datetime, commit: str | None) -> tuple[str, dict[str, Any], Any]:
    document, report = litellm_to_snapshot(raw, as_of=as_of, source=source_info(commit))
    parse_snapshot(document)  # the loader must accept what we write
    return render(document), document, report


def main(argv: list[str] | None = None, fetch: Fetch = http_get) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", help="read a local copy of the LiteLLM file instead of downloading it")
    parser.add_argument("--ref", default="main", help="LiteLLM branch, tag or commit to download (default: main)")
    parser.add_argument("--as-of", help="the instant the prices describe (required with --source)")
    parser.add_argument("--commit", help="the LiteLLM commit the local copy comes from")
    parser.add_argument("--output", default=str(SNAPSHOT_PATH), help="where to write the snapshot")
    args = parser.parse_args(argv)

    if args.source:
        if not args.as_of:
            parser.error("--as-of is required with --source")
        raw_bytes = Path(args.source).read_bytes()
        as_of = parse_instant(args.as_of, where="--as-of")
        commit = args.commit
    else:
        commit, as_of = latest_commit(fetch, args.ref)
        raw_bytes = fetch(RAW_URL.format(repo=REPOSITORY, ref=commit, file=FILE))
    raw = json.loads(raw_bytes)
    if not isinstance(raw, dict):
        raise SystemExit("The LiteLLM file is not a JSON object")

    text, document, report = build(raw, as_of, commit)
    Path(args.output).write_text(text, encoding="utf-8")
    print(f"Wrote {len(document['models'])} models as of {format_instant(as_of)} to {args.output}")
    if report.skipped_duplicates:
        print(f"Duplicate keys left out: {', '.join(report.skipped_duplicates)}")
    if report.rounded:
        print(f"Prices rounded to 6 decimal places per 1M tokens: {len(report.rounded)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
