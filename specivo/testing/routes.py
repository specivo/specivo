"""Route enumeration for tests that audit the whole HTTP surface."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts


def iter_api_routes(app: FastAPI) -> Iterator[Any]:
    """Yield every ``APIRoute`` of *app* with its effective path and dependencies.

    FastAPI keeps included routers as nested route groups instead of copying
    their routes into ``app.routes``, so iterating ``app.routes`` alone misses
    every router-mounted endpoint. Each yielded item exposes ``path``,
    ``methods`` and ``dependant`` as resolved after the router prefixes and
    router-level dependencies are applied.
    """
    for context in iter_route_contexts(app.routes):
        if isinstance(context.original_route, APIRoute):
            yield context
