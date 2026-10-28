"""Fixtures for importer tests that load an import bundle.

A run is built the way ``python -m specivo.cli.import_data --source bundle``
builds it: the command line is parsed with the registry's ``bundle`` source,
the adapter comes from its ``from_args`` and the pipeline from the command's
own ``build_pipeline``. Only the session factory differs.

The pipeline normally opens a session per phase and commits each one. Here it
is handed the test's session instead, which keeps every phase inside the
transaction the fixture rolls back. That costs one property — a failed phase no
longer leaves earlier phases committed — and nothing else: phase ordering,
identifier mapping and the skip-what-exists behaviour are unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from specivo.cli.import_data import build_pipeline, parse_args
from specivo.importers.core.pipeline import ImportPipeline
from specivo.importers.core.progress import NullProgressReporter
from specivo.importers.core.source import build_source_registry


class _SharedSession:
    """Hands the pipeline the test's session and refuses to close it."""

    def __init__(self, session) -> None:
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info) -> None:
        return None


def _flags(**option_kwargs) -> list[str]:
    """Translate run-option keywords (``dry_run=True``) into the command's generic flags."""
    argv: list[str] = []
    for name, value in option_kwargs.items():
        flag = "--" + name.replace("_", "-")
        if name == "resume_run_id":
            argv += ["--resume", str(value)]
        elif value is True:
            argv.append(flag)
        elif value not in (None, False):
            argv += [flag, str(value)]
    return argv


@pytest.fixture
def build_bundle_import(db_session, tmp_path, monkeypatch):
    """Return a factory building a wired pipeline that loads a bundle.

    Built the way ``--source bundle --path PATH`` builds it, against the
    test's session, with attachment storage in a temporary directory. The
    links file a bundle would get next to it goes to a temporary directory
    too (``links_path(bundle)`` names it), so a load of the fixture bundle
    never writes into the source tree.
    """
    import specivo.importers.bundle.adapter as bundle_adapter
    import specivo.services.attachment_service as attachment_service
    from specivo.importers.core.links_file import default_links_path

    storage = tmp_path / "bundle-attachments"
    storage.mkdir()
    monkeypatch.setattr(attachment_service, "_upload_dir", storage)
    links_dir = tmp_path / "bundle-links"
    links_dir.mkdir()
    monkeypatch.setattr(
        bundle_adapter,
        "default_links_path",
        lambda _directory, system, instance: default_links_path(links_dir, system, instance),
    )

    def _build(path: Path, **option_kwargs) -> ImportPipeline:
        argv = ["--source", "bundle", "--path", str(path), *_flags(**option_kwargs)]
        args, adapter_cls = parse_args(argv, build_source_registry())
        return build_pipeline(
            args,
            adapter_cls.from_args(args),
            session_factory=lambda: _SharedSession(db_session),  # type: ignore[arg-type]
            reporter=NullProgressReporter(),
        )

    def _links_path(_bundle: Path, instance: str = "tracker.example.com", system: str = "sample") -> Path:
        return default_links_path(links_dir, system, instance)

    _build.storage = storage  # type: ignore[attr-defined]
    _build.links_path = _links_path  # type: ignore[attr-defined]
    return _build
