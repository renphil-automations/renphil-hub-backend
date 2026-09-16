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

    `access_control` is stored and editable, but read by nothing server-side
    — only the frontend consults it, to decide what to show. Phase 2's
    propagation engine used to cascade this value through the whole tab
    family; that engine has been removed ahead of a new access control
    algorithm, so the column is now a plain per-node value with no
    relationship to any other node's.

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

    # Added by scripts/migrate_lock_propagation_columns.py
    # (plan_lock_propagation_2026-09-08.md §1/§3.1, decision 3): a nav tab
    # is now a real lock node, not just an AC node — its edit-mode toggle
    # acquires an actual lock (Sidebar.tsx's onToggleEditMode), and locking
    # it blocks its whole subtree (every root tab and variant under it, and
    # their sub-grids). Same three-column shape as TabV2/GridstackV2's own
    # lock columns, deliberately mirrored rather than independently
    # designed, plus `lock_token` — see those models' own comments and
    # app/services/edit_lock_service.py. No pre-existing rows to worry
    # about staying consistent with (nav tabs were never lockable before
    # this column existed), so every row starts free.
    locked = Column(Boolean, nullable=True, default=False)
    locked_by = Column(String(255), nullable=True, default="")
    locked_at = Column(DateTime, nullable=True)
    lock_token = Column(String(64), nullable=True)

    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)
