from sqlalchemy import Column, Double, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.db_v2.database import BaseV2


class ComponentV2(BaseV2):
    """A widget within a gridstack (canvas). A component's actual `data` (for
    every type, not just block_note) lives in `page_content` via
    `page_content_id` — `props` (JSONB) is structural-metadata-only: `min_w`/
    `min_h` (any component), plus `locked`/`locked_by`/`order` for components
    that are part of a Super Block Note's own tree (a component whose own
    `type` is `super_block_note`, or any descendant reached via
    `super_blocknote_id`) — never widget content.

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
