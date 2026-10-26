#!/usr/bin/env python3
"""Container health check for the Specivo image (Dockerfile HEALTHCHECK).

In a container that serves the web app (uvicorn is PID 1) it requests the
``/health/`` endpoint on the port uvicorn listens on, behind STEALTH_PREFIX
when one is set. The endpoint answers 200 while the app is up and reports the
database and Redis status in its body.

The Celery containers run the same image without a web server; for them the
check passes, since there is nothing to probe over HTTP.
"""

import os
import sys
import urllib.request


def _pid1_args() -> list[str]:
    try:
        with open("/proc/1/cmdline", "rb") as fh:
            return [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
    except OSError:
        return []


def main() -> int:
    args = _pid1_args()
    if not any(os.path.basename(a) == "uvicorn" for a in args):
        return 0
    port = "8000"
    for i, arg in enumerate(args):
        if arg == "--port" and i + 1 < len(args):
            port = args[i + 1]
        elif arg.startswith("--port="):
            port = arg.split("=", 1)[1]
    prefix = os.environ.get("STEALTH_PREFIX", "").rstrip("/")
    url = f"http://127.0.0.1:{port}{prefix}/health/"
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:  # noqa: S310 - fixed local URL
            return 0 if resp.status == 200 else 1
    except Exception as exc:  # noqa: BLE001 - any failure means unhealthy
        print(f"health check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
