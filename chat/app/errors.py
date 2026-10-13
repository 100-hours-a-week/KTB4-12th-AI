import logging
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


ERROR_CODES = {
    400: "INVALID_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN",
    404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED", 409: "CONFLICT", 413: "PAYLOAD_TOO_LARGE",
    429: "TOO_MANY_REQUESTS", 500: "INTERNAL_SERVER_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


class APIError(Exception):
    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None):
        self.status = status
        self.message = message
        self.headers = headers or {}


def error_body(message: str, status: int) -> dict:
    trace_id = str(uuid4())
    code = ERROR_CODES.get(status, "HTTP_ERROR")
    logging.warning("API failure status=%s code=%s traceId=%s", status, code, trace_id)
    return {"message": message, "error": {"code": code, "traceId": trace_id}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def handle_api_error(request: Request, exc: APIError):
        return JSONResponse(error_body(exc.message, exc.status), status_code=exc.status, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(error_body("요청 형식이나 값이 올바르지 않습니다.", 400), status_code=400)

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException):
        return JSONResponse(error_body(str(exc.detail), exc.status_code), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception):
        logging.exception("Unexpected API error", exc_info=exc)
        return JSONResponse(error_body("요청 처리 중 오류가 발생했습니다.", 500), status_code=500)
