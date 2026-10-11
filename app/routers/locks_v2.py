"""Lock-state poll router — see `app/services/lock_state_service.py` for why
this exists (the frontend's lock badges went stale until a hard refresh).

One read-only route. POST, not GET, only because the body is a list of ids
that would not fit comfortably in a query string; it writes nothing, takes
no edit session, and is safe to call as often as the client polls (every
15 s while the browser tab is visible).
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db_v2.database import get_db_v2
from app.dependencies import get_current_user, get_lock_view, get_viewer_access
from app.schemas.tab import LockStatesAPIResponse, LockStatesRequest
from app.services import edit_lock_service
from app.services.access_visibility_service import ViewerAccess
from app.services.lock_state_service import get_lock_states

router = APIRouter(prefix="/v2/locks", tags=["Locks V2"], dependencies=[Depends(get_current_user)])


@router.post(
    "/state",
    response_model=LockStatesAPIResponse,
    summary="Current lock state for a batch of nav tabs, tabs and SBN nodes (v2)",
    description="Returns only the lock fields each list endpoint already "
    "carries, for the ids named in the body. Ids that do not exist or that "
    "the caller cannot view are omitted.",
)
def post_lock_states(
    body: LockStatesRequest,
    db: Session = Depends(get_db_v2),
    access: ViewerAccess = Depends(get_viewer_access),
    lock_view: edit_lock_service.LockView = Depends(get_lock_view),
):
    return {
        "data": get_lock_states(
            db,
            nav_tabs=body.nav_tabs,
            tabs=body.tabs,
            sbn=body.sbn,
            access=access,
            lock_view=lock_view,
        )
    }
