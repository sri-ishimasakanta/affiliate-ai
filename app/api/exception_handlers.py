"""Application 例外 → HTTP レスポンス変換。

Router 側で個別に try/except せず、ここで application-level exception handler
として一元変換する。SQLAlchemy 等の内部例外の詳細・スタックトレースは
レスポンスへ露出させない。

レスポンス形式は一貫して::

    {"error": {"code": "<machine_readable>", "message": "<human readable>"}}
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.exceptions import (
    ApplicationError,
    DuplicateEntityError,
    EntityNotFoundError,
    ExternalProviderDataError,
    ExternalProviderError,
    IncompleteSignalSetError,
    InvalidStatusTransitionError,
    ProviderNotConfiguredError,
)

# (例外型, HTTP status, 安定した machine-readable code)
_ERROR_MAP: tuple[tuple[type[ApplicationError], int, str], ...] = (
    (EntityNotFoundError, 404, "entity_not_found"),
    (DuplicateEntityError, 409, "duplicate_entity"),
    (InvalidStatusTransitionError, 409, "invalid_status_transition"),
    (IncompleteSignalSetError, 409, "incomplete_signal_set"),
    (ProviderNotConfiguredError, 503, "provider_not_configured"),
    (ExternalProviderDataError, 502, "external_provider_data_error"),
    (ExternalProviderError, 502, "external_provider_error"),
)


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


async def _handle_application_error(
    request: Request,
    exc: ApplicationError,
) -> JSONResponse:
    for exc_type, status_code, code in _ERROR_MAP:
        if isinstance(exc, exc_type):
            return _error_response(status_code, code, str(exc))
    # 想定外の ApplicationError サブクラス: 詳細は返さない。
    return _error_response(500, "application_error", "Internal application error")


async def _handle_database_error(
    request: Request,
    exc: SQLAlchemyError,
) -> JSONResponse:
    # SQLAlchemy の例外詳細は露出しない。
    return _error_response(500, "internal_error", "Internal server error")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApplicationError, _handle_application_error)
    app.add_exception_handler(SQLAlchemyError, _handle_database_error)
