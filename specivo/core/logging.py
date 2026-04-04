"""Structured JSON logging configuration.

In production (debug=False) each log line is a JSON object with the fields:
  timestamp, level, logger, message, request_id

In debug mode a human-readable format is used instead for easier local dev.

File logging: when LOG_FILE is set (or the default /app/data/logs/specivo.log
exists), logs are also written to a rotating file. Sensitive values (passwords,
tokens, secrets, emails, usernames) are masked automatically.
"""

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

# Patterns that should be masked in file logs.
_SENSITIVE_PATTERNS = re.compile(
    r"("
    # key=value patterns for sensitive keys
    r"(?:password|passwd|secret|token|authorization|cookie|api[_-]?key|credential|dsn)"
    r"\s*[:=]\s*"
    r")"
    r"(\S+)",
    re.IGNORECASE,
)

# Email addresses: user part → masked
_EMAIL_PATTERN = re.compile(r"([a-zA-Z0-9_.+-]+)@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)")

# login="username" or user="username" patterns (but not user_id, user %d, etc.)
_USERNAME_PATTERN = re.compile(
    r"(?:login|username|user_name|display_name)"
    r'(\s*[:=]\s*["\']?)'
    r"([^\"',\s}]+)",
    re.IGNORECASE,
)


def _mask(value: str) -> str:
    """Mask a sensitive value, keeping first and last char if long enough."""
    if len(value) <= 4:
        return "***"
    return value[0] + "***" + value[-1]


def _sanitize(message: str) -> str:
    """Remove sensitive data from a log message."""
    # Mask key=value secrets
    message = _SENSITIVE_PATTERNS.sub(lambda m: m.group(1) + "***", message)
    # Mask email user parts
    message = _EMAIL_PATTERN.sub(lambda m: _mask(m.group(1)) + "@" + m.group(2), message)
    # Mask usernames in login=/username= patterns
    message = _USERNAME_PATTERN.sub(lambda m: m.group(0).replace(m.group(2), _mask(m.group(2))), message)
    return message


class _SanitizingFilter(logging.Filter):
    """Strip sensitive data from log records before they reach file handlers."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _sanitize(str(record.msg))
        if record.exc_text:
            record.exc_text = _sanitize(record.exc_text)
        return True


class _JSONFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


class _SanitizedJSONFormatter(_JSONFormatter):
    """JSON formatter that sanitizes sensitive data before writing."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _sanitize(record.getMessage()),
            "request_id": getattr(record, "request_id", None),
        }

        if record.exc_info:
            payload["exc_info"] = _sanitize(self.formatException(record.exc_info))

        return json.dumps(payload, ensure_ascii=False)


# Default log file path (inside the Docker data volume).
_DEFAULT_LOG_FILE = "/app/data/logs/specivo.log"
_LOG_MAX_BYTES = 1024 * 1024 * 1024  # 1 GB per file
_LOG_BACKUP_COUNT = 5  # keep 5 rotated files (5 GB total)


def setup_logging(debug: bool = False) -> None:
    """Configure root logger.

    Args:
        debug: When *True* use a human-readable format; when *False* emit
               structured JSON suitable for log aggregation pipelines.
    """
    level = logging.DEBUG if debug else logging.INFO

    handler = logging.StreamHandler(sys.stdout)

    if debug:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    else:
        handler.setFormatter(_JSONFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    # Remove any handlers added by basicConfig or uvicorn before we run.
    root.handlers.clear()
    root.addHandler(handler)

    # File handler — persistent, sanitized, rotated.
    log_file = os.environ.get("LOG_FILE", _DEFAULT_LOG_FILE)
    if log_file:
        log_dir = os.path.dirname(log_file)
        try:
            os.makedirs(log_dir, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=_LOG_MAX_BYTES,
                backupCount=_LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(_SanitizedJSONFormatter())
            root.addHandler(file_handler)
        except OSError:
            # Data volume not mounted or read-only — skip file logging silently.
            pass

    # Quiet noisy libraries
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
