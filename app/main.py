"""港口危险品配载预审 API。"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.reviews import store
from app.rules import assess, removal_impact
from app.validation import (
    ApiError,
    validate_payload,
    validate_review_command_payload,
    validate_review_create_payload,
)

app = FastAPI(title="Dangerous Goods Stowage Pre-review", version="1.0.0")


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


async def _load_json(request: Request) -> object:
    try:
        return json.loads(await request.body())
    except (UnicodeDecodeError, ValueError):
        raise ApiError("INVALID_JSON", "Request body must be valid JSON.")


def _stored_json_response(status_code: int, body: bytes) -> Response:
    """直接回放存储层渲染好的字节，重试与首次响应字节级一致。"""
    return Response(status_code=status_code, content=body, media_type="application/json")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/stowage/assess")
async def assess_stowage(request: Request) -> JSONResponse:
    payload = await _load_json(request)
    hold, items = validate_payload(payload)
    result = assess(items)
    return JSONResponse(
        status_code=200,
        content={
            "hold": hold,
            "conclusion": result["conclusion"],
            "evidence": result["evidence"],
        },
    )


@app.post("/api/v1/stowage/removal-impact")
async def removal_impact_stowage(request: Request) -> JSONResponse:
    payload = await _load_json(request)
    hold, items = validate_payload(payload)
    analysis = removal_impact(items)
    return JSONResponse(
        status_code=200,
        content={
            "hold": hold,
            "original": analysis["original"],
            "removals": analysis["removals"],
            "recommendations": analysis["recommendations"],
        },
    )


@app.post("/api/v1/stowage/reviews", status_code=201)
async def create_review(request: Request) -> Response:
    payload = await _load_json(request)
    hold, items, command_id = validate_review_create_payload(payload)
    status_code, body = store.create_review(hold, items, command_id)
    return _stored_json_response(status_code, body)


@app.post("/api/v1/stowage/reviews/{review_id}/commands")
async def review_command(review_id: str, request: Request) -> Response:
    payload = await _load_json(request)
    command_id, action, expected_revision, items = validate_review_command_payload(
        payload
    )
    status_code, body = store.apply_command(
        review_id, command_id, action, expected_revision, items
    )
    return _stored_json_response(status_code, body)
