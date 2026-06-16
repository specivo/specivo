"""Celery task configuration.

Settings are resolved lazily to avoid import-time get_settings() calls
that fail in CI/test environments where env vars aren't yet set.
"""

import os

from celery import Celery

# Use REDIS_URL env var directly for the broker — avoids triggering
# pydantic-settings validation (which requires SECRET_KEY etc.) at import time.
_broker = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery("specivo")
celery_app.config_from_object(
    {
        "broker_url": _broker,
        "result_backend": _broker,
        "task_serializer": "json",
        "result_serializer": "json",
        "accept_content": ["json"],
        "timezone": "UTC",
        "beat_schedule": {
            "cleanup-expired-tokens": {
                "task": "specivo.tasks.cleanup.cleanup_expired_tokens",
                "schedule": 3600,  # every hour
            },
            "ensure-audit-partitions": {
                "task": "specivo.tasks.cleanup.ensure_audit_partitions",
                "schedule": 86400,  # daily
            },
        },
    }
)

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
    webhooks,
    wiki_links,
)
