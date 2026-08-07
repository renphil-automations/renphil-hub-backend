from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.db_v2.database import BaseV2


class NavTabV2(BaseV2):
    """The parent layer above root tabs (see plan_nav_tabs_2026-07-28.md).
    A nav tab owns a set of root TabV2 rows (TabV2.nav_tab_id) and renders as
    its own dashboard — same tab bar, same canvas, same edit/lock flow.

    `slug` is stored, not derived from title on the fly the way root-tab
    slugs are (slugifyTitle) — it anchors the top-level `/<nav-slug>/...`
    URL, which must not silently change on rename. Generated from the title
    on create, regenerated on rename except when `protected`. See
    nav_tab_service._resolve_nav_slug for the uniqueness/reserved-word gate
    that keeps this namespace sound.

    `protected` is true for exactly the Dashboard row (the pre-existing
    unassigned-roots case, promoted to a real nav tab by the migration).
    Blocks rename and delete server-side, which is what pins its slug to
    `dashboard` and keeps every pre-existing `/dashboard/...` URL stable.
    Phase 2 flips this to False with a one-row UPDATE and needs no routing
    change at all.

    `access_control` is stored and editable in phase 1 but read by nothing —
    present now so phase 2 (access-control propagation, the `hub` object)
    needs no second migration.

    `icon` is a lucide-react icon name (kebab-case, e.g. "layout-grid"),
    looked up client-side via `lucide-react/dynamic`'s `DynamicIcon` — never
    validated against the icon library itself, just a lookup key. NULL means
    "no icon chosen", which renders the existing default (`LayoutGrid`). No
    backfill, no uniqueness constraint.

    No relationship() here, matching every other model in db_v2/ — traversal
    is always a plain query."""

    __tablename__ = "nav_tabs"

    id = Column(Integer, primary_key=True, index=True)

    document_id = Column(String(255), nullable=True, index=True)

    slug = Column(String(255), nullable=False, unique=True, index=True)
    title = Column(String(255), nullable=True)
    order = Column(Integer, nullable=True)

    access_control = Column(JSONB, nullable=True)

    protected = Column(Boolean, nullable=False, default=False)

    icon = Column(String(64), nullable=True)

    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)
