"""Embedding tasks run on their own queue and worker; the tooling works from a release bundle.

Each worker process that embeds loads its own copy of the model, so the
embedding tasks are routed to a queue that one single-process worker consumes.
The make targets that run in the api container must work with only the
production compose file, which is all a release bundle has, and the bundle must
contain what its install guide tells people to run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from specivo.tasks import EMBEDDING_QUEUE, celery_app
from specivo.tasks.embeddings import _consumes_embedding_queue

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
EMBEDDING_TASKS = (
    "specivo.tasks.embeddings.embed_search_sources",
    "specivo.tasks.embeddings.backfill_model_embeddings",
)


# ---------------------------------------------------------------------------
# Celery routing
# ---------------------------------------------------------------------------


def test_default_embedding_queue_is_embeddings():
    assert EMBEDDING_QUEUE == "embeddings"


@pytest.mark.parametrize("task_name", EMBEDDING_TASKS)
def test_embedding_tasks_are_routed_to_the_embedding_queue(task_name):
    route = celery_app.amqp.router.route({}, task_name, args=(), kwargs={})
    assert route["queue"].name == EMBEDDING_QUEUE


@pytest.mark.parametrize(
    "task_name",
    ["specivo.tasks.notifications.send_notification_email", "specivo.tasks.wiki_links.rebuild_wiki_page_links"],
)
def test_other_tasks_stay_on_the_default_queue(task_name):
    route = celery_app.amqp.router.route({}, task_name, args=(), kwargs={})
    assert route["queue"].name == celery_app.conf.task_default_queue


def _consumer(*names: str) -> SimpleNamespace:
    return SimpleNamespace(task_consumer=SimpleNamespace(queues=[SimpleNamespace(name=n) for n in names]))


def test_only_the_embedding_worker_logs_the_model_status():
    assert _consumes_embedding_queue(_consumer(EMBEDDING_QUEUE)) is True
    assert _consumes_embedding_queue(_consumer("celery")) is False
    # Unknown shape: log rather than stay silent.
    assert _consumes_embedding_queue(object()) is True


# ---------------------------------------------------------------------------
# Compose files
# ---------------------------------------------------------------------------


def _services(*files: str) -> dict:
    services: dict = {}
    for name in files:
        for service, spec in yaml.safe_load((ROOT / name).read_text())["services"].items():
            services.setdefault(service, {}).update(spec)
    return services


def test_production_compose_runs_one_single_process_embedding_worker():
    services = _services("docker-compose.yml")
    worker = services["celery-embeddings"]
    command = worker["command"]
    assert "--queues=${EMBEDDING_QUEUE:-embeddings}" in command
    assert "--concurrency=${EMBEDDING_WORKER_CONCURRENCY:-1}" in command
    # It reads the model from the same data directory as the api.
    assert worker["volumes"] == services["api"]["volumes"]
    assert worker["image"] == services["celery-worker"]["image"]

    # The general worker does not listen on the embedding queue.
    assert "--queues" not in services["celery-worker"]["command"]
    assert "-Q" not in services["celery-worker"]["command"].split()


def test_dev_overlay_builds_the_embedding_worker_from_source():
    services = _services("docker-compose.yml", "docker-compose.dev.yml")
    worker = services["celery-embeddings"]
    assert worker["image"] == "specivo/specivo:dev"
    assert "./specivo:/app/specivo" in worker["volumes"]
    assert worker["environment"] == services["celery-worker"]["environment"]


# ---------------------------------------------------------------------------
# Makefile targets on a bundle install
# ---------------------------------------------------------------------------

_make = shutil.which("make")


def _dry_run(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("PROD", "ARGS", "MAKEFLAGS")}
    result = subprocess.run([_make, "-n", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env)
    return result.stdout


@pytest.fixture
def bundle_dir(tmp_path: Path) -> Path:
    """A directory shaped like an extracted release bundle: no dev overlay."""
    shutil.copy(ROOT / "Makefile", tmp_path / "Makefile")
    shutil.copy(ROOT / "docker-compose.yml", tmp_path / "docker-compose.yml")
    return tmp_path


@pytest.mark.skipif(_make is None, reason="make is not installed")
@pytest.mark.parametrize(
    ("target", "command"),
    [
        ("download-model", "python -m specivo.cli.download_model"),
        ("backfill-embeddings", "python -m specivo.cli.backfill_embeddings"),
        ("reindex-fts", "python -m specivo.cli.reindex_fts"),
        ("import-data", "python -m specivo.cli.import_data --list-sources"),
    ],
)
def test_container_targets_use_the_production_compose_file_in_a_bundle(bundle_dir, target, command):
    out = _dry_run(bundle_dir, target)
    assert f"docker compose exec api {command}" in out
    assert "docker-compose.dev.yml" not in out


@pytest.mark.skipif(_make is None, reason="make is not installed")
def test_container_targets_use_the_dev_stack_in_a_checkout_unless_prod(bundle_dir):
    (bundle_dir / "docker-compose.dev.yml").write_text("services: {}\n")
    assert "-f docker-compose.dev.yml exec api" in _dry_run(bundle_dir, "backfill-embeddings")
    prod = _dry_run(bundle_dir, "backfill-embeddings", "PROD=1", "ARGS=--reembed")
    assert "docker compose exec api python -m specivo.cli.backfill_embeddings --reembed" in prod


@pytest.mark.skipif(_make is None, reason="make is not installed")
def test_make_help_lists_the_container_targets(bundle_dir):
    result = subprocess.run([_make, "help"], cwd=bundle_dir, capture_output=True, text=True, check=True)
    for target in ("download-model", "backfill-embeddings", "reindex-fts", "import-data"):
        assert re.search(rf"^\s+{target}\s+\S", result.stdout, re.M), target
    assert "PROD=1" in result.stdout


# ---------------------------------------------------------------------------
# Release bundle contents
# ---------------------------------------------------------------------------


def _bundle_paths(script: str, target: str) -> set[str]:
    """Paths inside the bundle that the ``cp`` lines of *script* produce."""
    files = set()
    for src, dest in re.findall(rf"^\s*cp (?:-r )?(\S+) {target}/(\S*?);?(?:\s|$)", script, re.M):
        files.add(f"{dest}{Path(src).name}" if dest.endswith("/") or not dest else dest)
    return files


def _release_bundle_files() -> set[str]:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    steps = workflow["jobs"]["create-release"]["steps"]
    script = next(s["run"] for s in steps if s.get("name") == "Create offline deployment bundle")
    return _bundle_paths(script, "bundle")


def _make_bundle_files() -> set[str]:
    makefile = (ROOT / "Makefile").read_text()
    recipe = makefile[makefile.index("\nbundle:") : makefile.index("# Embedding model")]
    return _bundle_paths(recipe.replace("\t", ""), "_bundle")


BUNDLE_ESSENTIALS = {
    "Makefile",
    "docker-compose.yml",
    ".env.example",
    "scripts/configure.py",
    "scripts/download-model.sh",
    "docs/INSTALL.md",
}


def test_release_workflow_and_make_bundle_ship_the_same_layout():
    assert BUNDLE_ESSENTIALS <= _release_bundle_files()
    assert BUNDLE_ESSENTIALS <= _make_bundle_files()


def test_install_guide_only_tells_bundle_users_to_run_what_the_bundle_has():
    guide = (ROOT / "docs" / "INSTALL.md").read_text()
    bundle = _release_bundle_files()
    assert "python3 scripts/configure.py" in guide and "scripts/configure.py" in bundle
    assert "python3 configure.py" not in guide
    assert "docker compose exec api python -m specivo.cli.download_model" in guide
    for script in re.findall(r"\b(scripts/[\w.-]+)", guide):
        assert script in bundle, script
    assert "specivo-models.tar.gz" not in guide
