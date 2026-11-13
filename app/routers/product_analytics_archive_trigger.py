"""Server-to-server trigger for one bounded Hub Analytics archive run."""

import hmac
import logging

from fastapi import APIRouter, Header, HTTPException

from app.config import get_settings
from scripts import archive_product_analytics as archive_job

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/product-analytics/archive", tags=["Internal analytics"])


@router.post("/run", include_in_schema=False)
def run_archive(x_archive_token: str | None = Header(default=None, alias="X-Analytics-Archive-Token")):
    """Run at most one 100-row batch per table and return only after it finishes.

    The current archive job also processes at most one cleanup batch per table.
    Existing per-batch PostgreSQL locks and idempotent archive registration
    protect overlapping invocations; this is not a whole-run mutex.
    """
    expected = get_settings().PRODUCT_ANALYTICS_ARCHIVE_TRIGGER_TOKEN
    if not expected or len(expected) < 32:
        raise HTTPException(status_code=503, detail="Archive trigger is not configured.")
    if not x_archive_token or not hmac.compare_digest(x_archive_token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid archive trigger token.")
    messages: list[str] = []
    progress: dict = {}
    def report(line: str) -> None:
        messages.append(line)
        logger.info("Analytics archive: %s", line)
    try:
        result = archive_job.run(
            ["--batch-size", "100", "--max-batches", "1"],
            out=report,
            progress_out=progress,
        )
    except Exception as exc:
        logger.error("Analytics archive trigger failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Archive run failed; check server logs.") from None
    if result == archive_job.EXIT_LOCKED:
        raise HTTPException(status_code=409, detail="An archive batch is already running; retry later.")
    if result != archive_job.EXIT_OK:
        raise HTTPException(status_code=503, detail="Archive run failed or is misconfigured; check server logs.")
    status = "disabled" if any(line.startswith("archive disabled:") for line in messages) else "completed"
    return {"status": status, "batch_size": 100, "max_batches_per_table": 1,
            "has_more": bool(progress.get("has_more", False)), "cleanup": progress.get("cleanup", {}),
            "archived": progress.get("archived", {})}
