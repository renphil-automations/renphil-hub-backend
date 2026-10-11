from sqlalchemy import Boolean, Column, DateTime, Double, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.db_v2.database import BaseV2


class ComponentV2(BaseV2):
    """A widget within a gridstack (canvas). A component's actual `data` (for
    every type, not just block_note) lives in `page_content` via
    `page_content_id` — `props` (JSONB) is structural-metadata-only: `min_w`/
    `min_h` (any component), plus `order` for components that are part of a
    Super Block Note's own tree (a component whose own `type` is
    `super_block_note`, or any descendant reached via `super_blocknote_id`)
    — never widget content. `props` ALSO still carries an SBN node's own
    `locked`/`locked_by`/`locked_at` lock (read and written by
    `super_blocknote_service.lock_sbn_node`/`unlock_sbn_node`) — until
    plan_component_locking_and_sbn_2026-09-17.md phase B moves that onto
    the four real lock columns below (phase A, which added the columns,
    left the SBN service untouched). Once B ships, any `props.locked*` keys
    still present on a live row are stale leftovers nothing reads.

    super_blocknote_id (self-referential FK) is set only for a Super Block
    Note's own nested sub-tab components, pointing at their parent SBN
    component's id (which may itself be a nested `super_block_note`, for
    arbitrary-depth nesting). NULL for every other component, including the
    SBN's own top-level widget row. No ORM `relationship()` is declared here,
    matching `GridstackV2.parent_id`'s existing pattern — traversal is always
    a plain query, never an ORM-managed collection (see the flush-per-node
    comment on delete_tab_subtree_by_document_id_v2 for why, when deleting
    self-referential rows without a relationship()).

    current_grid_id (self-referential FK to gridstacks) points a component at
    the gridstack it represents itself, rather than at a real widget — every
    gridstack gets exactly one such row, created alongside it (see
    _create_gridstack_component in gridstack_service.py). Its `type`/`props`
    mirror the gridstack's own settings.sgs (gridstack vs. super_gridstack,
    kept in sync by update_tab_content_v2). For a sub-tab gridstack — never a
    root or tab variant, whose own access_control lives on their TabV2 row
    instead — this row's `access_control` IS the sub-tab's own access
    control (see migrate_subtab_access_control_to_components.py, which
    relocated it off gridstacks.settings). A component with current_grid_id
    set is never a pickable/renderable widget — every query over "real"
    canvas components excludes both representation types
    (GRIDSTACK_REPRESENTATION_TYPES).
    """

    __tablename__ = "components"

    id = Column(Integer, primary_key=True, index=True)

    # Stable public address, separate from the raw PK — a mirror references
    # its target by this, never by `id` or by whatever transient key this
    # widget happens to use in its own canvas's layout.
    link = Column(String(255), nullable=True, unique=True, index=True)

    title = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    type = Column(String(255), nullable=False)

    x = Column(Double, nullable=True)
    y = Column(Double, nullable=True)
    width = Column(Double, nullable=True)
    height = Column(Double, nullable=True)

    props = Column(JSONB, nullable=True)
    access_control = Column(JSONB, nullable=True)

    current_grid_id = Column(Integer, ForeignKey("gridstacks.id"), nullable=True)
    gridstack_id = Column(Integer, ForeignKey("gridstacks.id"), nullable=False, index=True)
    page_content_id = Column(Integer, ForeignKey("page_contents.id"), nullable=True)

    # Set only for a Super Block Note's own nested sub-tab components — see
    # the class docstring.
    super_blocknote_id = Column(Integer, ForeignKey("components.id"), nullable=True, index=True)

    # Added by scripts/migrate_component_lock_columns.py
    # (plan_component_locking_and_sbn_2026-09-17.md §3, decision A): every
    # REAL component — `current_grid_id IS NULL`, i.e. an ordinary canvas
    # widget, a Super Block Note root, or any SBN sub-tab node — is a lock
    # node of its own, the fourth kind in `edit_lock_service`'s tree beside
    # tabs/gridstacks/nav_tabs. Same four columns, same shape, same
    # `is_lock_stale` TTL helper as those three tables (mirrored, not
    # independently designed), so the four lock flavours cannot drift onto
    # different rules. A sub-grid's representation row (`current_grid_id`
    # set) carries the columns like every other row but is NEVER a lock node
    # and is never written — it stands for its gridstack, whose own row
    # already holds the lock (`edit_lock_service.lock_node_of`).
    locked = Column(Boolean, nullable=True, default=False)
    locked_by = Column(String(255), nullable=True, default="")
    locked_at = Column(DateTime, nullable=True)
    lock_token = Column(String(64), nullable=True)
