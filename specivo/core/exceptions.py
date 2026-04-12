"""Structured error handling with machine-readable error codes."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base application error with structured error code."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        field: str | None = None,
        details: dict | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)  # makes str(exc) return the message
        self.code = code
        self.message = message
        self.status_code = status_code
        self.field = field
        self.details = details
        self.headers = headers


class NotFoundError(AppError):
    def __init__(self, message: str = "Not found", **kwargs):
        super().__init__(code="not_found", message=message, status_code=404, **kwargs)


class PermissionDeniedError(AppError):
    def __init__(self, message: str = "Permission denied", **kwargs):
        super().__init__(code="permission_denied", message=message, status_code=403, **kwargs)


class ConflictError(AppError):
    def __init__(self, message: str = "Conflict", **kwargs):
        super().__init__(code="conflict_lock_version", message=message, status_code=409, **kwargs)


class UnauthorizedError(AppError):
    def __init__(self, message: str = "Unauthorized", **kwargs):
        super().__init__(code="unauthorized", message=message, status_code=401, **kwargs)


class ValidationError(AppError):
    def __init__(self, message: str = "Validation error", **kwargs):
        super().__init__(code="validation_error", message=message, status_code=422, **kwargs)


# Map HTTP status codes to semantic error codes
_HTTP_STATUS_CODES: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "permission_denied",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_410_GONE: "gone",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "validation_error",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limit_exceeded",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "internal_error",
    status.HTTP_502_BAD_GATEWAY: "bad_gateway",
    status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
}

# Default human-readable titles for common HTTP error codes
_ERROR_TITLES: dict[int, str] = {
    400: "Bad request",
    401: "Unauthorized",
    403: "Access denied",
    404: "Page not found",
    405: "Method not allowed",
    409: "Conflict",
    410: "Gone",
    422: "Validation error",
    429: "Too many requests",
    500: "Something went wrong",
    502: "Bad gateway",
    503: "Service unavailable",
}

# Default user-facing messages for common HTTP error codes
_ERROR_MESSAGES: dict[int, str] = {
    403: (
        "You don't have permission to view this resource. "
        "If you believe this is a mistake, contact your project administrator."
    ),
    404: (
        "The page you're looking for doesn't exist or has been moved. "
        "Check the URL or navigate back to familiar ground."
    ),
    500: (
        "An unexpected error occurred. The issue has been logged "
        "and we'll look into it. Try refreshing the page or come back later."
    ),
}


def _error_body(errors: list[dict]) -> dict:
    return {"errors": errors}


def _wants_html(request: Request) -> bool:
    """Return True if the request prefers an HTML response (browser)."""
    accept = request.headers.get("accept", "")
    # Quick heuristic: browsers send text/html before application/json.
    # API clients (fetch with json, curl, httpie) send application/json
    # or omit text/html entirely.
    if "text/html" not in accept:
        return False
    # If both are present, check which appears first
    html_pos = accept.find("text/html")
    json_pos = accept.find("application/json")
    if json_pos == -1:
        return True
    return html_pos < json_pos


def _render_error_html(
    request: Request,
    status_code: int,
    title: str | None = None,
    message: str | None = None,
    details: dict[str, str] | None = None,
) -> HTMLResponse:
    """Render a styled HTML error page.

    Uses the authenticated layout (pages/error.html with sidebar + header)
    when possible, falling back to the standalone layout (_shared/errors/error.html)
    if the template or user context is unavailable.
    """
    title = title or _ERROR_TITLES.get(status_code, "Error")
    message = message or _ERROR_MESSAGES.get(status_code, str(status_code))

    # Always include the request path in details for context
    if details is None:
        details = {}
    if "Path" not in details:
        details["Path"] = str(request.url.path)

    context = {
        "request": request,
        "status_code": status_code,
        "title": title,
        "message": message,
        "details": details if details else None,
    }

    try:
        from specivo.web.deps import get_templates

        templates = get_templates()

        # Try the full layout first (with sidebar/header)
        try:
            return templates.TemplateResponse(
                request,
                "pages/error.html",
                context,
                status_code=status_code,
            )
        except Exception:
            pass

        # Fall back to standalone error template
        return templates.TemplateResponse(
            request,
            "errors/error.html",
            context,
            status_code=status_code,
        )
    except Exception:
        # Last resort: if template rendering itself fails, return minimal HTML
        logger.exception("Failed to render error template for %s", status_code)
        return HTMLResponse(
            content=f"<h1>{status_code} — {title}</h1><p>{message}</p>",
            status_code=status_code,
        )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse | HTMLResponse:
    if _wants_html(request) and exc.status_code in (403, 404, 500):
        return _render_error_html(
            request,
            status_code=exc.status_code,
            message=exc.message if exc.message != exc.code else None,
            details=exc.details,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body([{"code": exc.code, "message": exc.message, "field": exc.field, "details": exc.details}]),
        headers=exc.headers,
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse | HTMLResponse:
    # Skip HTML rendering for redirect responses (302, 307, etc.)
    if 300 <= exc.status_code < 400:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body([{"code": "redirect", "message": str(exc.detail), "field": None, "details": None}]),
            headers=exc.headers,
        )

    if _wants_html(request) and exc.status_code in (403, 404, 500):
        detail_str = str(exc.detail) if exc.detail else None
        # Use the detail as message only if it is meaningful (not just "Not Found")
        message = None
        if detail_str and detail_str not in ("Not Found", "Forbidden", "Internal Server Error"):
            message = detail_str
        return _render_error_html(
            request,
            status_code=exc.status_code,
            message=message,
        )

    code = _HTTP_STATUS_CODES.get(exc.status_code, "http_error")
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body([{"code": code, "message": str(exc.detail), "field": None, "details": None}]),
        headers=exc.headers,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse | HTMLResponse:
    """Catch-all for unhandled exceptions — renders a 500 page for browsers."""
    logger.exception("Unhandled exception: %s", exc)

    if _wants_html(request):
        return _render_error_html(request, status_code=500)

    return JSONResponse(
        status_code=500,
        content=_error_body(
            [{"code": "internal_error", "message": "Internal server error", "field": None, "details": None}]
        ),
    )


async def request_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Convert Pydantic V2 RequestValidationError into the standard error envelope."""
    errors = []
    for error in exc.errors():
        loc = error.get("loc", ())
        # Skip the first element if it's "body" / "query" / "path" — keep field path only
        field_parts = [str(p) for p in loc if p not in ("body", "query", "path", "header")]
        field = ".".join(field_parts) if field_parts else None
        raw_input = error.get("input")
        if isinstance(raw_input, bytes):
            raw_input = raw_input.decode("utf-8", errors="replace")
        errors.append(
            {
                "code": "validation_error",
                "message": error.get("msg", "Validation error"),
                "field": field,
                "details": {"type": error.get("type"), "input": raw_input},
            }
        )
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content=_error_body(errors))
