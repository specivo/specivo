"""Structured error handling with machine-readable error codes."""

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


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


def _error_body(errors: list[dict]) -> dict:
    return {"errors": errors}


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body([{"code": exc.code, "message": exc.message, "field": exc.field, "details": exc.details}]),
        headers=exc.headers,
    )


async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    code = _HTTP_STATUS_CODES.get(exc.status_code, "http_error")
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body([{"code": code, "message": str(exc.detail), "field": None, "details": None}]),
        headers=exc.headers,
    )


async def request_validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Convert Pydantic V2 RequestValidationError into the standard error envelope."""
    errors = []
    for error in exc.errors():
        loc = error.get("loc", ())
        # Skip the first element if it's "body" / "query" / "path" — keep field path only
        field_parts = [str(p) for p in loc if p not in ("body", "query", "path", "header")]
        field = ".".join(field_parts) if field_parts else None
        errors.append(
            {
                "code": "validation_error",
                "message": error.get("msg", "Validation error"),
                "field": field,
                "details": {"type": error.get("type"), "input": error.get("input")},
            }
        )
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content=_error_body(errors))
