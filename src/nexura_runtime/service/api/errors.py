# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Safe error handlers for the service API: never echo request values back to the client."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return 422 with loc/type/msg only; the offending input (maybe a secret) is dropped."""
    detail = [
        {
            "loc": list(err.get("loc", ())),
            "type": str(err.get("type", "value_error")),
            "msg": str(err.get("msg", "Invalid value")),
        }
        for err in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


def register_validation_handler(app: FastAPI) -> None:
    """Install the input-free RequestValidationError handler on ``app``."""
    app.add_exception_handler(RequestValidationError, _validation_handler)  # type: ignore[arg-type]
