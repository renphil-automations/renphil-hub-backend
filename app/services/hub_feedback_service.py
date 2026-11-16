"""Live-resource authorization and persistence for Hub product feedback.

Permission filtering happens in SQL before counts and pagination. Names are
resolved from current accessible resources; no historic labels or URLs persist.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db_v2.models.component import ComponentV2
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.hub_feedback import HubFeedbackV2
from app.db_v2.models.tab import TabV2
from app.schemas.hub_feedback import HubFeedbackCreate
from app.services.access_visibility_service import ViewerAccess, build_node_tree, resolve_gridstack_node


class FeedbackTargetUnavailable(Exception):
    pass


class FeedbackConflict(Exception):
    pass


COMPONENT_LABELS = {
    "block_note": "Text", "super_block_note": "Super Block Note", "markdown": "Markdown",
    "banner": "Banner", "airtable": "Airtable", "airtable_metric": "Airtable Metric",
    "links": "Links", "chart": "Chart", "button": "Button", "booking": "Booking",
    "divider": "Divider", "calendar": "Calendar", "html": "HTML", "mirror": "Mirror",
    "resource_carousel": "Resource Carousel", "detail_cards": "Detail Cards",
    "quick_actions": "Quick Actions", "thread": "Thread", "form": "Form",
}


def component_label(component: ComponentV2) -> str:
    return (component.title or "").strip() or COMPONENT_LABELS.get(component.type, "Untitled component")


@dataclass(frozen=True)
class Target:
    node: tuple[str, int]
    label: str


class FeedbackResources:
    """One current tree and resource directory per request, including admins.

    Admin bypass does not make an orphan/deleted object a valid target.
    Mirrors use the existing Hub verdict, which intersects occurrence/source.
    """
    def __init__(self, db: Session, access: ViewerAccess):
        self.access = access
        self.tree = access.visibility.tree if access.visibility is not None else build_node_tree(db)
        self.pages: dict[str, Target] = {}
        self.components: dict[str, Target] = {}
        for tab in db.scalars(select(TabV2)):
            if tab.document_id:
                self.pages[tab.document_id] = Target(("tab", tab.id), tab.title or "Untitled page")
        for grid in db.scalars(select(GridstackV2)):
            if grid.document_id and grid.document_id not in self.pages:
                node = resolve_gridstack_node(db, grid)
                if node is not None:
                    self.pages[grid.document_id] = Target(node, grid.name or "Untitled page")
        for component in db.scalars(select(ComponentV2)):
            if component.link and component.current_grid_id is None:
                self.components[component.link] = Target(("component", component.id), component_label(component))

    def allowed(self, target: Target | None, *, edit: bool = False, payload: bool = False) -> bool:
        if target is None or self.tree.is_orphan(target.node):
            return False
        if edit:
            return self.access.verdict(target.node).edit
        return self.access.is_granted(target.node) if payload else self.access.verdict(target.node).view

    def bound(self, component: Target, page: Target) -> bool:
        return page.node in self.tree.ancestors(component.node)

    def validate(self, payload: HubFeedbackCreate) -> None:
        page = self.pages.get(payload.page_id or "")
        if payload.page_id and not self.allowed(page):
            raise FeedbackTargetUnavailable()
        if payload.scope == "component":
            component = self.components.get(payload.component_link or "")
            if not self.allowed(component, payload=True) or page is None or not self.bound(component, page):
                raise FeedbackTargetUnavailable()

    def read_predicate(self):
        if self.access.full_access:
            return True
        pages = [key for key, target in self.pages.items() if self.allowed(target, edit=True)]
        components = [key for key, target in self.components.items() if self.allowed(target, edit=True)]
        return or_(
            HubFeedbackV2.scope == "hub",
            and_(HubFeedbackV2.scope == "page", HubFeedbackV2.page_id.in_(pages)),
            and_(HubFeedbackV2.scope == "component", HubFeedbackV2.component_link.in_(components)),
        )

    def serialize(self, row: HubFeedbackV2) -> dict:
        # Global diagnostic context stays out even for admins. A separate
        # explicit diagnostic endpoint will be reviewed before exposing it.
        page = self.pages.get(row.page_id or "")
        component = self.components.get(row.component_link or "")
        return {
            "id": row.public_id, "scope": row.scope, "message": row.message,
            "from_email": row.actor_email, "environment": row.environment,
            "created_at": row.created_at.isoformat(),
            "page_id": row.page_id if row.scope != "hub" and (self.access.full_access or self.allowed(page)) else None,
            "component_link": row.component_link,
            "page_name": page.label if self.allowed(page) else None,
            "component_name": component.label if self.allowed(component, payload=True) else None,
            "target_available": row.scope == "hub" or (self.allowed(page) if row.scope == "page" else self.allowed(component, payload=True)),
        }


def create_feedback(db: Session, payload: HubFeedbackCreate, *, user_id: int, email: str,
                    environment: str, resources: FeedbackResources) -> tuple[HubFeedbackV2, bool]:
    resources.validate(payload)
    key = str(payload.submission_id)
    predicate = and_(HubFeedbackV2.actor_user_id == user_id, HubFeedbackV2.submission_id == key)
    def checked_existing():
        row = db.scalar(select(HubFeedbackV2).where(predicate))
        if row is not None:
            expected = (payload.scope, payload.message, payload.page_id if payload.scope != "hub" else None,
                        payload.component_link, payload.page_id if payload.scope == "hub" else None,
                        payload.page_route if payload.scope == "hub" else None, environment)
            actual = (row.scope, row.message, row.page_id, row.component_link, row.diagnostic_page_id, row.diagnostic_route, row.environment)
            if actual != expected:
                raise FeedbackConflict()
        return row
    existing = checked_existing()
    if existing is not None:
        return existing, False
    row = HubFeedbackV2(public_id=str(uuid4()), submission_id=key, actor_user_id=user_id,
        actor_email=email, environment=environment, scope=payload.scope, message=payload.message,
        page_id=payload.page_id if payload.scope != "hub" else None,
        component_link=payload.component_link,
        diagnostic_page_id=payload.page_id if payload.scope == "hub" else None,
        diagnostic_route=payload.page_route if payload.scope == "hub" else None,
        created_at=datetime.now(timezone.utc))
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = checked_existing()
        if existing is None:
            raise
        return existing, False
    return row, True


def list_feedback(db: Session, *, resources: FeedbackResources, environment: str | None,
                  scope: str | None = None, page_id: str | None = None,
                  component_link: str | None = None, limit: int = 25, offset: int = 0,
                  public_id: str | None = None) -> tuple[list[dict], int]:
    # A component can move while its feedback keeps the original binding.
    # Do not let a reader who edits that component probe its hidden old page
    # by filtering on candidate ids, even though the serialized id is redacted.
    if page_id is not None and not resources.access.full_access and not resources.allowed(resources.pages.get(page_id)):
        return [], 0
    conditions = [resources.read_predicate()]
    if environment:
        conditions.append(HubFeedbackV2.environment == environment)
    for column, value in ((HubFeedbackV2.scope, scope), (HubFeedbackV2.page_id, page_id),
                          (HubFeedbackV2.component_link, component_link), (HubFeedbackV2.public_id, public_id)):
        if value is not None:
            conditions.append(column == value)
    total = db.scalar(select(func.count()).select_from(HubFeedbackV2).where(*conditions)) or 0
    rows = db.scalars(select(HubFeedbackV2).where(*conditions).order_by(HubFeedbackV2.created_at.desc(), HubFeedbackV2.id.desc()).offset(offset).limit(limit)).all()
    return [resources.serialize(row) for row in rows], total
