"""Authenticated proxy for asynchronous Hub -> Agent API index updates."""

from __future__ import annotations

from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.dependencies import get_current_user
from app.helpers.http_client import get_http_client
from app.models.auth import UserInfo

router = APIRouter(prefix="/knowledge", tags=["Knowledge"])


class KnowledgeOperation(BaseModel):
    component_id: int = Field(gt=0)
    action: Literal["upsert", "delete"]


class KnowledgeJobRequest(BaseModel):
    operations: list[KnowledgeOperation] = Field(min_length=1, max_length=500)
    session_id: str | None = Field(default=None, max_length=200)


def _agent_config() -> tuple[str, dict[str, str]]:
    settings = get_settings()
    base_url = (settings.AGENT_API_URL or "").strip().rstrip("/")
    sync_token = (settings.AGENT_SYNC_TOKEN or "").strip()
    if not base_url or not sync_token:
        raise HTTPException(
            status_code=503,
            detail="RenPhil Knowledge update service is not configured.",
        )
    return base_url, {"X-Sync-Token": sync_token}


async def _forward(response_awaitable):
    try:
        response = await response_awaitable
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        detail = "RenPhil Knowledge update service rejected the request."
        try:
            payload = exc.response.json()
            detail = str(payload.get("detail") or detail)
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=detail) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="RenPhil Knowledge update service is temporarily unavailable.",
        ) from exc


@router.post("/jobs", status_code=202)
async def create_job(
    request: KnowledgeJobRequest,
    user: UserInfo = Depends(get_current_user),
):
    base_url, service_headers = _agent_config()
    # A map makes the last operation authoritative and removes duplicate
    # component requests generated during a single UI save.
    deduplicated = {
        operation.component_id: operation.action for operation in request.operations
    }
    payload = {
        "operations": [
            {"component_id": component_id, "action": action}
            for component_id, action in deduplicated.items()
        ],
        "actor_email": user.email,
        "session_id": request.session_id,
    }
    return await _forward(
        get_http_client().post(
            f"{base_url}/page_content/jobs",
            headers=service_headers,
            json=payload,
        )
    )


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    _user: UserInfo = Depends(get_current_user),
):
    base_url, service_headers = _agent_config()
    return await _forward(
        get_http_client().get(
            f"{base_url}/page_content/jobs/{job_id}",
            headers=service_headers,
        )
    )
