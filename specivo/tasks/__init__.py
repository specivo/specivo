"""Celery task configuration.

Settings are resolved lazily to avoid import-time get_settings() calls
that fail in CI/test environments where env vars aren't yet set.
"""

import os

from celery import Celery
from celery.signals import worker_init  # type: ignore[import-untyped]

from specivo.tasks.beat import default_schedule_file

# Use REDIS_URL env var directly for the broker — avoids triggering
# pydantic-settings validation (which requires SECRET_KEY etc.) at import time.
_broker = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Beat cadence for the recurring-task poller. Mirrors
# Settings.recurring_tasks_beat_interval_seconds (default 3600); kept as a
# literal here to avoid importing pydantic Settings at module import time
# (which would force SECRET_KEY etc. to be set just to load the worker).
_RECURRING_BEAT_INTERVAL_SECONDS = int(os.environ.get("RECURRING_TASKS_BEAT_INTERVAL_SECONDS", "3600"))

# Queue for the tasks that run the embedding model. Each worker process that
# embeds loads its own copy of the model (several hundred MB), so these tasks go
# to a queue of their own, consumed by the single-process ``celery-embeddings``
# service, while ``celery-worker`` keeps the default queue. Set
# EMBEDDING_QUEUE=celery to run them in the general worker instead.
EMBEDDING_QUEUE = os.environ.get("EMBEDDING_QUEUE", "embeddings")

celery_app = Celery("specivo")
celery_app.config_from_object(
    {
        "broker_url": _broker,
        "result_backend": _broker,
        "task_serializer": "json",
        "result_serializer": "json",
        "accept_content": ["json"],
        "timezone": "UTC",
        "task_routes": {
            "specivo.tasks.embeddings.embed_search_sources": {"queue": EMBEDDING_QUEUE},
            "specivo.tasks.embeddings.backfill_model_embeddings": {"queue": EMBEDDING_QUEUE},
        },
        # Beat's schedule (a pickle) lives in a private 0700 directory, never in
        # the working directory or a shared temp dir; see specivo/tasks/beat.py.
        "beat_scheduler": "specivo.tasks.beat:PrivateStateScheduler",
        "beat_schedule_filename": default_schedule_file(),
        "beat_schedule": {
            "cleanup-expired-tokens": {
                "task": "specivo.tasks.cleanup.cleanup_expired_tokens",
                "schedule": 3600,  # every hour
            },
            "ensure-audit-partitions": {
                "task": "specivo.tasks.cleanup.ensure_audit_partitions",
                "schedule": 86400,  # daily
            },
            "generate-recurring-tasks": {
                "task": "specivo.tasks.recurring.generate_recurring_tasks",
                "schedule": _RECURRING_BEAT_INTERVAL_SECONDS,
            },
        },
    }
)


@worker_init.connect
def _bootstrap_worker(**_kwargs) -> None:
    """Load plugins in the worker the same way ``create_app()`` does.

    Runs once in the worker's main process, before the pool forks, so every
    child inherits the plugins and the password hasher registry. Kept out of
    module import for the same reason the settings are: importing this package
    must not require a configured environment.
    """
    from specivo.core.plugin_manager import bootstrap

    bootstrap()


# Eagerly import every task module so that @celery_app.task decorators
# run at worker startup. The worker entry point is
# ``celery -A specivo.tasks worker`` which only triggers import of this
# package's __init__; submodules must be imported here, or the worker
# starts with zero tasks registered and rejects every dispatched task
# with "Received unregistered task".
#
# Explicit imports preferred over autodiscover_tasks(...) — clearer and
# avoids autodiscover edge cases. partition_management is a pure helper
# (no @celery_app.task) and is imported on demand by cleanup, so it
# does not need a top-level import here.
from specivo.tasks import (  # noqa: E402, F401
    cleanup,
    embeddings,
    notifications,
    recurring,
    search,
    webhooks,
    wiki_links,
)
