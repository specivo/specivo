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
    }
)
