"""Import bundles for tests.

``full/`` is a hand-written bundle that uses every file and most properties of
format version 1; it is also the example the format's documentation points to.
:func:`write_bundle` builds small ones in a test's temporary directory, from
records given as dicts, for checking one rule at a time.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

FULL = Path(__file__).parent / "full"

# The smallest set of lookups an issue needs.
LOOKUPS: dict[str, Any] = {
    "trackers": [{"source_ref": "bug", "name": "Bug", "default_status_ref": "open"}],
    "statuses": [{"source_ref": "open", "name": "Open", "category": "backlog"}],
    "priorities": [{"source_ref": "normal", "name": "Normal", "is_default": True}],
    "activities": [{"source_ref": "dev", "name": "Development", "is_default": True}],
}


def manifest(**overrides: Any) -> dict[str, Any]:
    """Return a valid manifest, with *overrides* applied."""
    data: dict[str, Any] = {
        "format": "specivo-import-bundle",
        "format_version": "1.0",
        "source": {"system": "sample", "instance": "tracker.example.com"},
        "generator": {"name": "pytest", "version": "1"},
        "exported_at": "2024-03-01T09:30:00Z",
    }
    data.update(overrides)
    return data


def user(ref: str = "u1", **fields: Any) -> dict[str, Any]:
    return {"source_ref": ref, "login": f"login_{ref}", "display_name": f"User {ref}", **fields}


def project(ref: str = "p1", **fields: Any) -> dict[str, Any]:
    return {"source_ref": ref, "identifier": f"proj-{ref}", "name": f"Project {ref}", **fields}


def issue(ref: str = "i1", project_ref: str = "p1", **fields: Any) -> dict[str, Any]:
    return {
        "source_ref": ref,
        "project_ref": project_ref,
        "tracker_ref": "bug",
        "status_ref": "open",
        "priority_ref": "normal",
        "subject": f"Issue {ref}",
        **fields,
    }


def write_bundle(
    root: Path,
    *,
    manifest_data: dict[str, Any] | None = None,
    lookups: dict[str, Any] | None = None,
    files: dict[str, bytes] | None = None,
    **records: list[dict[str, Any]] | str,
) -> Path:
    """Write a bundle directory at *root* and return it.

    Each keyword names a record file without ``.jsonl`` and gives its records;
    a string is written verbatim instead, for lines that are not valid JSON.
    *files* maps a name under ``files/`` to its bytes; use :func:`stored` to
    name a file by its hash.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(manifest_data or manifest()))
    (root / "lookups.json").write_text(json.dumps(LOOKUPS if lookups is None else lookups))
    for stem, content in records.items():
        if isinstance(content, str):
            text = content
        else:
            text = "".join(json.dumps(record) + "\n" for record in content)
        (root / f"{stem}.jsonl").write_text(text)
    if files:
        (root / "files").mkdir()
        for name, data in files.items():
            (root / "files" / name).write_bytes(data)
    return root


def stored(data: bytes) -> str:
    """Return the name a file with *data* has under ``files/``."""
    return hashlib.sha256(data).hexdigest()


def copy_full(target: Path) -> Path:
    """Copy the full example bundle to *target*, for a test that changes it."""
    shutil.copytree(FULL, target)
    return target
