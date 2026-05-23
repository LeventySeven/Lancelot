"""A minimal FastAPI app used as a fixture for the drop_in_test.py harness.

Covers the import surface documented in docs/migration/from-fastapi.md:
  - `from fastapi import FastAPI, Depends, HTTPException, Request, Response`
  - `from fastapi.responses import JSONResponse, StreamingResponse`
  - dependency injection (Depends)
  - exception handler decorator
  - Pydantic v2 request model
  - streaming response
  - background tasks
"""
from __future__ import annotations

from typing import AsyncIterator

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Request,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel


class Item(BaseModel):
    name: str
    value: int = 0


def get_token() -> str:
    return "fixture-token"


app = FastAPI()


@app.exception_handler(HTTPException)
async def http_exc_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "via": "custom-handler"},
    )


@app.get("/")
async def root() -> dict:
    return {"ok": True}


@app.post("/items")
async def create_item(
    item: Item,
    token: str = Depends(get_token),
    bg: BackgroundTasks = BackgroundTasks(),
) -> dict:
    if item.value < 0:
        raise HTTPException(status_code=422, detail="value must be non-negative")
    bg.add_task(_noop, item.name)
    return {"created": item.model_dump(), "token": token}


def _noop(_name: str) -> None:
    pass


@app.get("/stream")
async def stream() -> StreamingResponse:
    async def gen() -> AsyncIterator[bytes]:
        for i in range(5):
            yield f"chunk-{i}\n".encode()

    return StreamingResponse(gen(), media_type="text/plain")
