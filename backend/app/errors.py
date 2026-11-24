"""Typed domain exceptions and the single JSON error envelope: {error:{code,message,request_id}}."""

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger()


class AppError(Exception):
    status_code = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.__class__.__doc__ or self.code
        super().__init__(self.message)


class UnauthenticatedError(AppError):
    """Authentication required."""

    status_code, code = 401, "UNAUTHENTICATED"


class InvalidCredentialsError(AppError):
    """Invalid email or password."""

    status_code, code = 401, "INVALID_CREDENTIALS"


class ForbiddenError(AppError):
    """You do not have permission to do that."""

    status_code, code = 403, "FORBIDDEN"


class CsrfOriginRejectedError(AppError):
    """Request origin is not allowed."""

    status_code, code = 403, "CSRF_ORIGIN_REJECTED"


class SeedDocumentProtectedError(AppError):
    """Seed documents cannot be deleted."""

    status_code, code = 403, "SEED_DOCUMENT_PROTECTED"


class NotFoundError(AppError):
    """Resource not found."""

    status_code, code = 404, "NOT_FOUND"


class EmailTakenError(AppError):
    """An account with this email already exists."""

    status_code, code = 409, "EMAIL_TAKEN"


class DocumentLimitError(AppError):
    """Per-user document limit reached."""

    status_code, code = 409, "DOCUMENT_LIMIT"


class DuplicateDocumentError(AppError):
    """You already uploaded this file."""

    status_code, code = 409, "DUPLICATE_DOCUMENT"


class FileTooLargeError(AppError):
    """File exceeds the upload size limit."""

    status_code, code = 413, "FILE_TOO_LARGE"


class InvalidPdfError(AppError):
    """File is not a readable, unencrypted PDF."""

    status_code, code = 422, "INVALID_PDF"


class TooManyPagesError(AppError):
    """PDF exceeds the page limit."""

    status_code, code = 422, "TOO_MANY_PAGES"


class ScannedPdfUnsupportedError(AppError):
    """No extractable text found (scanned PDFs are not supported)."""

    status_code, code = 422, "SCANNED_PDF_UNSUPPORTED"


class DailyQuotaExceededError(AppError):
    """Your daily query quota is used up."""

    status_code, code = 429, "DAILY_QUOTA_EXCEEDED"


class GlobalQuotaExceededError(AppError):
    """The demo's global daily query budget is used up."""

    status_code, code = 429, "GLOBAL_QUOTA_EXCEEDED"


class LLMUnavailableError(AppError):
    """The language model is temporarily unavailable."""

    status_code, code = 503, "LLM_UNAVAILABLE"


class RequestTimeoutError(AppError):
    """The request exceeded its deadline."""

    status_code, code = 504, "REQUEST_TIMEOUT"


class SecurityViolationError(AppError):
    """Generated SQL was blocked by policy. Terminal: never retried, never shown to the LLM."""

    status_code, code = 400, "SQL_BLOCKED"


class SQLSyntaxOrSchemaError(AppError):
    """Generated SQL has a syntax or schema problem. The only retryable SQL failure."""

    status_code, code = 400, "SQL_INVALID"


class SQLTimeoutError(AppError):
    """The query exceeded its time limit."""

    status_code, code = 504, "QUERY_TIMEOUT"


def error_body(code: str, message: str, request_id: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "req_unknown"))


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        body = error_body(exc.code, exc.message, _request_id(request))
        return JSONResponse(body, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = f"{where}: {first.get('msg', 'invalid request')}" if where else "invalid request"
        return JSONResponse(
            error_body("VALIDATION_ERROR", message, _request_id(request)), status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else f"HTTP_{exc.status_code}"
        body = error_body(code, str(exc.detail), _request_id(request))
        return JSONResponse(body, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_exception", error=repr(exc), exc_info=exc)
        body = error_body("INTERNAL_ERROR", "Internal server error", _request_id(request))
        return JSONResponse(body, status_code=500)
