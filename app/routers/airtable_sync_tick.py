"""The Airtable sync's scheduler entry point (v1 plan §7, §8).

  POST /v2/airtable-sync/tick   called by the cron every few minutes

Authenticated ONLY by the `X-Sync-Token` header against AIRTABLE_SYNC_TOKEN
(unset → 503, wrong or missing → 401), like the widget cache refresh's
`routers/airtable.py:_check_cache_refresh_token`. No user: it lives outside
`routers/airtable_sync.py`, whose router requires a Hub Admin on every route.

Always fast: a due slot's run continues in the background. The reply says
what the tick did (scheduler.tick); 202 when it started a run, else 200.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db_v2.database import get_db_v2
from app.schemas.airtable_sync import SyncTickAPIResponse
from app.services.airtable_sync import scheduler
from app.services.airtable_sync.runner import SyncRunner, get_sync_runner

router = APIRouter(prefix="/v2/airtable-sync", tags=["Airtable Sync"])


def _check_sync_token(x_sync_token: str | None) -> None:
    expected = (get_settings().AIRTABLE_SYNC_TOKEN or "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The Airtable sync scheduler is not configured.",
        )
    # Bytes, not str: compare_digest raises TypeError (→ 500) on a str with
    # non-ASCII characters, and the header can hold anything.
    if not x_sync_token or not hmac.compare_digest(x_sync_token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing sync token.",
        )


@router.post(
    "/tick",
    response_model=SyncTickAPIResponse,
    summary="Scheduler tick: start the scheduled run if its slot is due",
    responses={
        202: {"description": "A scheduled run was started", "model": SyncTickAPIResponse},
        401: {"description": "Invalid or missing sync token"},
        503: {"description": "AIRTABLE_SYNC_TOKEN is not set on the backend"},
    },
)
def scheduler_tick(
    response: Response,
    x_sync_token: str | None = Header(default=None, alias="X-Sync-Token"),
    db: Session = Depends(get_db_v2),
    runner: SyncRunner = Depends(get_sync_runner),
):
    _check_sync_token(x_sync_token)
    data = scheduler.tick(db, runner)
    if data["result"] == "started":
        response.status_code = status.HTTP_202_ACCEPTED
    return {"data": data}
