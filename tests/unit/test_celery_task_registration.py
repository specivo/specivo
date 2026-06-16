"""Unit tests for Celery task registration.

Verifies that importing ``specivo.tasks`` eagerly registers every
``@celery_app.task``-decorated function across all task modules.

A worker started with ``celery -A specivo.tasks worker`` only loads
``specivo/tasks/__init__.py``. If that module does not import each
task submodule, the corresponding ``@celery_app.task`` decorators
never run and the worker has zero tasks registered. Beat-dispatched
work then fails with ``KeyError`` / ``Received unregistered task``.

These tests guard against regressing back to that state.
"""

from __future__ import annotations

import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


# Modules under specivo/tasks/ that don't define any @celery_app.task —
# helpers, not Celery task modules. Skip them in registration scans.
_NON_TASK_MODULES = {"partition_management"}


def _discover_task_modules() -> list[str]:
    """Enumerate non-helper modules under specivo.tasks/."""
    pkg_path = Path(__file__).resolve().parents[2] / "specivo" / "tasks"
    names: list[str] = []
    for info in pkgutil.iter_modules([str(pkg_path)]):
        if info.ispkg:
            continue
        if info.name.startswith("_"):
            continue
        if info.name in _NON_TASK_MODULES:
            continue
        names.append(info.name)
    return sorted(names)


def _registered_task_names() -> set[str]:
    """Snapshot of currently registered task names on ``celery_app``.

    Importing ``specivo.tasks`` is sufficient — if ``__init__`` wires up
    submodule imports, decorators run and tasks appear in ``celery_app.tasks``.
    """
    from specivo.tasks import celery_app

    return set(celery_app.tasks.keys())


class TestBeatScheduledTasksRegistered:
    """Every task referenced by beat_schedule must be registered."""

    def test_all_beat_scheduled_tasks_are_registered(self):
        from specivo.tasks import celery_app

        registered = _registered_task_names()
        beat = celery_app.conf.beat_schedule

        missing = []
        for entry_name, entry in beat.items():
            task_path = entry["task"]
            if task_path not in registered:
                missing.append((entry_name, task_path))

        assert not missing, (
            "Beat-scheduled tasks are missing from celery_app.tasks "
            f"(worker would reject them): {missing}. "
            "Make sure specivo/tasks/__init__.py imports the modules that "
            "define these tasks so their @celery_app.task decorators run."
        )


class TestPerModuleTaskRegistration:
    """Each task module must contribute at least one registered task.

    This is the strongest guard against the original bug: if a module
    is omitted from the eager imports in ``specivo/tasks/__init__.py``,
    none of its tasks will be registered, and this test fails for that
    module specifically.
    """

    @pytest.mark.parametrize("module_name", _discover_task_modules())
    def test_module_has_registered_tasks(self, module_name: str):
        registered = _registered_task_names()

        # Tasks register themselves under their fully-qualified name
        # ("specivo.tasks.<module>.<func>"). Any task whose name starts
        # with "specivo.tasks.<module>." counts as a contribution.
        prefix = f"specivo.tasks.{module_name}."
        contributed = [name for name in registered if name.startswith(prefix)]

        assert contributed, (
            f"Task module 'specivo.tasks.{module_name}' has no registered tasks. "
            "Either it defines no @celery_app.task functions (then add it to "
            "_NON_TASK_MODULES in this test) or specivo/tasks/__init__.py is "
            "missing an explicit import for it (worker will reject any task "
            "dispatched to this module)."
        )


class TestEagerSubmoduleImports:
    """Importing ``specivo.tasks`` alone must be enough.

    The worker entry point is ``celery -A specivo.tasks worker``. That
    only triggers import of the package's ``__init__``. If a submodule
    isn't transitively imported from there, the worker won't see it.
    """

    def test_all_task_submodules_imported_after_package_import(self):
        # Run in a subprocess so we observe a clean import — independent
        # of any module caching introduced by other tests in this session,
        # and without polluting sys.modules for tests that follow.
        modules = _discover_task_modules()
        script = (
            "import sys, importlib, json\n"
            "importlib.import_module('specivo.tasks')\n"
            f"mods = {modules!r}\n"
            "missing = [f'specivo.tasks.{m}' for m in mods "
            "if f'specivo.tasks.{m}' not in sys.modules]\n"
            "print(json.dumps(missing))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
        )
        import json

        not_loaded = json.loads(result.stdout.strip().splitlines()[-1])

        assert not not_loaded, (
            "These task modules are not transitively imported by "
            "specivo/tasks/__init__.py and therefore won't register their "
            f"tasks on the worker: {not_loaded}"
        )
