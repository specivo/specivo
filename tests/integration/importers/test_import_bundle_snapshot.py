"""One bundle, loaded twice, gives the same database.

The full example bundle is imported into two freshly migrated databases, once
from its directory and once from a ``.zip`` of it, each committing phase by
phase as the command does. The two results are compared row by row, by natural
key rather than id (see :mod:`tests.fixtures.import_compare`), with the moment
of import masked where the bundle gave a record no date, so a loader that
depends on how the bundle was packed, or on anything but its records, shows up
as a named difference.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fixtures.import_bundle import FULL
from tests.fixtures.import_compare import import_into_fresh_database, mask_import_time, raw_differences

pytestmark = [
    pytest.mark.asyncio(loop_scope="function"),
    pytest.mark.integration,
    pytest.mark.serial,
    pytest.mark.slow,
]

# Custom-field key -> the kind of record its values name, as the bundle declares it.
REFERENCE_FIELDS = {"reviewer": "user"}


def _argv(path: Path, links: Path) -> list[str]:
    return ["--source", "bundle", "--path", str(path), "--links-file", str(links), "--merge-duplicate-statuses"]


async def test_a_zip_and_a_directory_import_the_same_database(tmp_path):
    started = datetime.now(UTC)
    zipped = Path(shutil.make_archive(str(tmp_path / "bundle"), "zip", FULL))

    _, from_directory = await import_into_fresh_database(
        _argv(FULL, tmp_path / "directory-links.sqlite"), REFERENCE_FIELDS, tmp_path / "directory-files"
    )
    _, from_zip = await import_into_fresh_database(
        _argv(zipped, tmp_path / "zip-links.sqlite"), REFERENCE_FIELDS, tmp_path / "zip-files"
    )

    assert from_directory["counts"]["Issue"] > 0
    differences = raw_differences(mask_import_time(from_directory, started), mask_import_time(from_zip, started))
    assert differences == {}, "\n".join(f"{k}:\n  directory {a!r}\n  zip {b!r}" for k, (a, b) in differences.items())
