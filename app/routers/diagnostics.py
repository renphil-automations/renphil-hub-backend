from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.services.relation_service import (
    get_relation_diagnostics,
    repair_missing_content
)

router = APIRouter(prefix="/diagnostics", tags=["Diagnostics"], dependencies=[Depends(get_current_user)])


@router.get(
    "/relations",
    summary="Check relationship integrity",
    description="""
Checks database relationship integrity.

Reports:
- tabs without page content
- page contents without tabs
- broken tab/page-content links
- broken parent-child links
""",
    response_description="Relationship diagnostics report"
)
def relation_diagnostics(db: Session = Depends(get_db)):
    return get_relation_diagnostics(db)

@router.post(
    "/repair-missing-content",
    summary="Repair missing page content",
    description="""
Creates missing page content records
for tabs that do not have one.
""",
    response_description="Repair result"
)
def repair_content(
    db: Session = Depends(get_db)
):
    return repair_missing_content(db)