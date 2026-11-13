"""
One-off tool (with a GUI) to copy Block Note / Super Block Note content from
a tab in the OLD schema (the live Strapi CMS database — `tabs` /
`page_contents` / `tabs_parent_lnk`) into a Super Block Note component tree
on a tab in the NEW schema (`tabs` / `gridstacks` / `components` /
`page_contents`).

Scope, confirmed with the user (2026-07-08 design session):
  - Only plain BlockNote content is handled. Every node in the picked source
    subtree must have content that is a bare Block[] JSON array (or
    null/empty) — if anything looks like a schemaVersion:2 widget canvas or
    any other dict shape, the whole load is aborted with a clear error
    naming the offending node. No flattening/reconstruction heuristics.
  - The FULL original tab nesting is preserved exactly, at any depth. Every
    old tab becomes a `type="block_note"` component chained to its
    immediate parent via `super_blocknote_id` (which can itself be a
    sub-tab, not just the SBN root) — this is fully supported by the
    existing v2 backend/frontend (confirmed against the real code, not just
    prior handoff notes, which undersold this).
  - If the destination tab's canvas has no Super Block Note root component
    yet, this script creates one. If it already has one AND that root
    already has children, the whole run is refused (no partial/duplicate
    writes) — re-running against an already-migrated pair requires first
    clearing the existing children by hand.
  - Neither `locked`/`locked_by` nor `access_control` are carried over from
    the old tab rows — every migrated component starts unlocked, with
    default access control.
  - Old tab content (a BlockNote Block[] JSON array) is byte-for-byte
    compatible with the new schema's storage — no translation needed
    (confirmed: both schemas store the exact same Block[] shape, just
    wrapped/unwrapped as {"content": [...]} at the API layer).

Both DB connections are opened independently of the local FastAPI app's own
`DATABASE_URL`/`DATABASE_URL_V2` — this targets the real Strapi-hosted old
DB and the real Neon "renphil-hub-db" sandbox directly, not the local
Docker containers used elsewhere in dev. Env vars `DATABASE_URL` /
`DATABASE_URL_V2` are read as defaults for convenience, editable in the GUI.

The destination DB's `id` columns are `GENERATED ALWAYS AS IDENTITY` (real
backing sequences confirmed live on 2026-07-08 — a prior audit that found
"no auto-increment anywhere" on this same DB is stale; re-verify before
trusting that claim again if this script ever starts failing on id
handling). This script never supplies an explicit id on insert — it always
lets the DB generate one and reads it back via `RETURNING id`.

`components.link` has no unique index on the destination DB — this script
never relies on the DB to reject a duplicate; it always mints a fresh
`uuid4()` per component itself.

The source (old) connection is opened read-only for the whole run
(`SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`) as a hard
guarantee, independent of the fact that this script also simply never
issues a write statement against it.

Run:
    python scripts/migrate_sbn_content_gui.py
"""

from __future__ import annotations

import json
import os
import re
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any
from uuid import uuid4

import psycopg2
import psycopg2.extensions

MANIFEST_DIR = Path(__file__).resolve().parent / "sbn_migration_manifests"

# Default layout for an auto-created SBN root component, dropped on an empty
# canvas. Purely cosmetic — matches the rough size an existing real SBN
# root widget was found using in the destination DB, nothing enforced.
DEFAULT_ROOT_LAYOUT = {"x": 0.0, "y": 0.0, "width": 7.0, "height": 10.0}
DEFAULT_CHILD_LAYOUT = {"x": 0.0, "y": 0.0, "width": 6.0, "height": 6.0}


def normalize_dsn(url: str) -> str:
    """psycopg2 doesn't understand SQLAlchemy's `+psycopg2` dialect suffix."""
    return re.sub(r"^postgresql\+psycopg2://", "postgresql://", url.strip())


# --------------------------------------------------------------------------
# Data model for the source subtree
# --------------------------------------------------------------------------


@dataclass
class OldNode:
    id: int
    title: str
    order: int
    content: list[Any]  # always normalized to a plain Block[] list (possibly empty)
    children: list["OldNode"] = field(default_factory=list)

    def node_count(self) -> int:
        return 1 + sum(c.node_count() for c in self.children)

    def max_depth(self) -> int:
        if not self.children:
            return 0
        return 1 + max(c.max_depth() for c in self.children)


class ContentShapeError(Exception):
    """Raised when a source node's content isn't a plain BlockNote Block[]."""


class MigrationBlocked(Exception):
    """Raised when the destination guard conditions refuse the run."""


# --------------------------------------------------------------------------
# Old-schema (read-only) queries
# --------------------------------------------------------------------------


def old_connect(dsn: str) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(normalize_dsn(dsn))
    conn.set_session(readonly=True, autocommit=True)
    return conn


def search_old_tabs(conn, query: str, limit: int = 50) -> list[tuple[int, str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, document_id FROM tabs "
            "WHERE title ILIKE %s ORDER BY title, id LIMIT %s",
            (f"%{query}%", limit),
        )
        return cur.fetchall()


def _old_tab_content(conn, tab_id: int) -> Any:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pc.content FROM page_contents_tab_lnk lnk "
            "JOIN page_contents pc ON pc.id = lnk.page_content_id "
            "WHERE lnk.tab_id = %s",
            (tab_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def _old_tab_children(conn, tab_id: int) -> list[tuple[int, str, int]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.id, t.title, t.\"order\" FROM tabs_parent_lnk lnk "
            "JOIN tabs t ON t.id = lnk.tab_id "
            "WHERE lnk.inv_tab_id = %s "
            "ORDER BY t.\"order\" NULLS LAST, t.id",
            (tab_id,),
        )
        return cur.fetchall()


def _classify_content(raw: Any, node_id: int, title: str, path: str) -> list[Any]:
    """Strict validation per the confirmed scope: only a bare Block[] (or
    null/empty) is acceptable. Anything else aborts the whole load."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    raise ContentShapeError(
        f"Tab id={node_id} title={title!r} (path: {path}) has content that "
        f"isn't a plain BlockNote block list (got {type(raw).__name__} — "
        f"looks like a widget canvas or other shape). This tool only "
        f"handles plain Block Note / Super Block Note content — aborting "
        f"the whole load rather than guessing."
    )


def fetch_old_subtree(conn, root_tab_id: int, root_title: str) -> OldNode:
    """Recursively loads the full subtree rooted at root_tab_id, exactly as
    it is in the old schema — no flattening, arbitrary depth preserved."""
    visited: set[int] = set()

    def walk(tab_id: int, title: str, order: int, path: str) -> OldNode:
        if tab_id in visited:
            raise ContentShapeError(
                f"Tab id={tab_id} title={title!r} (path: {path}) appears "
                f"more than once while walking down from the root — likely "
                f"a cycle in the old schema's parent links. Aborting rather "
                f"than looping forever."
            )
        visited.add(tab_id)

        raw_content = _old_tab_content(conn, tab_id)
        content = _classify_content(raw_content, tab_id, title, path)

        node = OldNode(id=tab_id, title=title, order=order or 0, content=content)
        for child_id, child_title, child_order in _old_tab_children(conn, tab_id):
            child_path = f"{path} > {child_title}"
            node.children.append(walk(child_id, child_title, child_order, child_path))
        return node

    return walk(root_tab_id, root_title, 0, root_title)


# --------------------------------------------------------------------------
# New-schema (read-write) queries
# --------------------------------------------------------------------------


def new_connect(dsn: str) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(normalize_dsn(dsn))
    conn.autocommit = False
    return conn


def list_new_tabs(conn) -> list[tuple[int, str, str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, title, document_id FROM tabs ORDER BY title, id")
        return cur.fetchall()


@dataclass
class DestinationStatus:
    tab_id: int
    tab_title: str
    root_gridstack_id: int | None
    root_component_id: int | None
    root_page_content_id: int | None
    root_has_nonempty_content: bool
    existing_children_count: int
    blocking_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def get_destination_status(conn, tab_id: int, tab_title: str) -> DestinationStatus:
    with conn.cursor() as cur:
        cur.execute("SELECT document_id FROM tabs WHERE id = %s", (tab_id,))
        row = cur.fetchone()
        if row is None:
            raise MigrationBlocked(f"Destination tab id={tab_id} no longer exists.")
        document_id = row[0]

        cur.execute(
            "SELECT id FROM gridstacks WHERE document_id = %s AND parent_id IS NULL",
            (document_id,),
        )
        grid_row = cur.fetchone()
        if grid_row is None:
            raise MigrationBlocked(
                f"Destination tab {tab_title!r} has no root gridstack "
                f"(document_id={document_id!r}) — cannot attach any "
                f"component to it."
            )
        root_gridstack_id = grid_row[0]

        cur.execute(
            "SELECT id, page_content_id FROM components "
            "WHERE gridstack_id = %s AND type = 'super_block_note' "
            "AND super_blocknote_id IS NULL",
            (root_gridstack_id,),
        )
        sbn_rows = cur.fetchall()

    status = DestinationStatus(
        tab_id=tab_id,
        tab_title=tab_title,
        root_gridstack_id=root_gridstack_id,
        root_component_id=None,
        root_page_content_id=None,
        root_has_nonempty_content=False,
        existing_children_count=0,
    )

    if len(sbn_rows) > 1:
        status.blocking_reasons.append(
            f"Destination canvas already has {len(sbn_rows)} Super Block "
            f"Note root components — expected at most one. Resolve this "
            f"manually before migrating here."
        )
        return status

    if sbn_rows:
        root_id, page_content_id = sbn_rows[0]
        status.root_component_id = root_id
        status.root_page_content_id = page_content_id

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM components WHERE super_blocknote_id = %s",
                (root_id,),
            )
            status.existing_children_count = cur.fetchone()[0]

            if page_content_id is not None:
                cur.execute(
                    "SELECT content FROM page_contents WHERE id = %s",
                    (page_content_id,),
                )
                pc_row = cur.fetchone()
                content = pc_row[0] if pc_row else None
                status.root_has_nonempty_content = bool(content)

        if status.existing_children_count > 0:
            status.blocking_reasons.append(
                f"Destination Super Block Note already has "
                f"{status.existing_children_count} sub-tab(s) — refusing "
                f"to add more from this tool (re-run guard). Clear them "
                f"first if you want to re-migrate."
            )
        if status.root_has_nonempty_content:
            status.warnings.append(
                "Destination Super Block Note's own root already has "
                "non-empty content — running this migration will "
                "overwrite it with the source tab's own root content."
            )
    else:
        status.warnings.append(
            "Destination canvas has no Super Block Note component yet — "
            "one will be created."
        )

    return status


def _insert_page_content(conn, content: list[Any]) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO page_contents (content) VALUES (%s) RETURNING id",
            (json.dumps(content),),
        )
        return cur.fetchone()[0]


def _insert_component(
    conn,
    *,
    title: str | None,
    type_: str,
    layout: dict[str, float],
    props: dict[str, Any],
    gridstack_id: int,
    super_blocknote_id: int | None,
    page_content_id: int | None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO components
                (title, description, type, x, y, width, height, props, link,
                 access_control, super_blocknote_id, page_content_id,
                 gridstack_id, current_grid_id)
            VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, %s,
                    NULL, %s, %s, %s, NULL)
            RETURNING id
            """,
            (
                title,
                type_,
                layout["x"],
                layout["y"],
                layout["width"],
                layout["height"],
                json.dumps(props),
                str(uuid4()),
                super_blocknote_id,
                page_content_id,
                gridstack_id,
            ),
        )
        return cur.fetchone()[0]


def run_migration(
    conn,
    status: DestinationStatus,
    old_root: OldNode,
    log: "Logger",
) -> dict[str, Any]:
    """Executes the full write inside one transaction. Caller commits/rolls
    back. Re-checks the blocking condition one more time at write time
    (TOCTOU-safe against another writer between preview and run)."""

    fresh_status = get_destination_status(conn, status.tab_id, status.tab_title)
    if fresh_status.existing_children_count > 0:
        raise MigrationBlocked(
            "Destination gained sub-tabs since the preview was computed — "
            "aborting without writing anything. Re-run the preview."
        )

    manifest: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "destination_tab_id": status.tab_id,
        "destination_tab_title": status.tab_title,
        "source_root_tab_id": old_root.id,
        "source_root_title": old_root.title,
        "created_page_content_ids": [],
        "created_component_ids": [],
    }

    gridstack_id = fresh_status.root_gridstack_id

    if fresh_status.root_component_id is not None:
        root_component_id = fresh_status.root_component_id
        log.line(f"Reusing existing Super Block Note root component id={root_component_id}")
    else:
        root_component_id = _insert_component(
            conn,
            title=None,
            type_="super_block_note",
            layout=DEFAULT_ROOT_LAYOUT,
            props={},
            gridstack_id=gridstack_id,
            super_blocknote_id=None,
            page_content_id=None,
        )
        manifest["created_component_ids"].append(root_component_id)
        log.line(f"Created new Super Block Note root component id={root_component_id}")

    # Root's own content (only write it if the source root has real content —
    # avoid churning an untouched NULL page_content_id for no reason).
    if old_root.content:
        root_pc_id = _insert_page_content(conn, old_root.content)
        manifest["created_page_content_ids"].append(root_pc_id)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE components SET page_content_id = %s WHERE id = %s",
                (root_pc_id, root_component_id),
            )
        log.line(f"Wrote source root's own content into page_content id={root_pc_id}")

    counts = {"components": 0, "page_contents": len(manifest["created_page_content_ids"])}

    def migrate_children(old_node: OldNode, new_parent_component_id: int, depth: int) -> None:
        for index, child in enumerate(old_node.children):
            pc_id = _insert_page_content(conn, child.content)
            manifest["created_page_content_ids"].append(pc_id)
            counts["page_contents"] += 1

            comp_id = _insert_component(
                conn,
                title=child.title,
                type_="block_note",
                layout=DEFAULT_CHILD_LAYOUT,
                props={"locked": False, "locked_by": "", "order": index},
                gridstack_id=gridstack_id,
                super_blocknote_id=new_parent_component_id,
                page_content_id=pc_id,
            )
            manifest["created_component_ids"].append(comp_id)
            counts["components"] += 1

            log.line(
                "  " * depth
                + f"created sub-tab '{child.title}' -> component id={comp_id} "
                f"(parent component id={new_parent_component_id})"
            )
            migrate_children(child, comp_id, depth + 1)

    migrate_children(old_root, root_component_id, 1)

    manifest["root_component_id"] = root_component_id
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    manifest["counts"] = counts
    return manifest


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------


class Logger:
    def __init__(self, widget: ScrolledText):
        self.widget = widget

    def line(self, text: str) -> None:
        self.widget.insert(tk.END, text + "\n")
        self.widget.see(tk.END)
        self.widget.update_idletasks()


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("SBN Content Migration — old schema -> new schema")
        self.geometry("1100x800")

        self.old_conn: psycopg2.extensions.connection | None = None
        self.new_conn: psycopg2.extensions.connection | None = None
        self.old_tree_root: OldNode | None = None
        self.new_tabs: list[tuple[int, str, str]] = []
        self.dest_status: DestinationStatus | None = None

        self._build_connection_bar()
        self._build_main_panes()
        self._build_log_pane()

    # -- connection bar ----------------------------------------------------

    def _build_connection_bar(self) -> None:
        frame = ttk.LabelFrame(self, text="Database connections")
        frame.pack(fill=tk.X, padx=8, pady=6)

        ttk.Label(frame, text="Source (old / Strapi) DATABASE_URL:").grid(row=0, column=0, sticky="w")
        self.old_dsn_var = tk.StringVar(value=os.environ.get("DATABASE_URL", ""))
        ttk.Entry(frame, textvariable=self.old_dsn_var, width=90, show="*").grid(row=0, column=1, padx=4)

        ttk.Label(frame, text="Destination (new) DATABASE_URL_V2:").grid(row=1, column=0, sticky="w")
        self.new_dsn_var = tk.StringVar(value=os.environ.get("DATABASE_URL_V2", ""))
        ttk.Entry(frame, textvariable=self.new_dsn_var, width=90, show="*").grid(row=1, column=1, padx=4)

        ttk.Button(frame, text="Connect", command=self.on_connect).grid(row=0, column=2, rowspan=2, padx=8)

        self.conn_status_var = tk.StringVar(value="Not connected.")
        ttk.Label(frame, textvariable=self.conn_status_var, foreground="gray").grid(
            row=2, column=0, columnspan=3, sticky="w"
        )

    def on_connect(self) -> None:
        try:
            self.old_conn = old_connect(self.old_dsn_var.get())
            self.new_conn = new_connect(self.new_dsn_var.get())
        except Exception as exc:  # noqa: BLE001 - surfaced to the user directly
            messagebox.showerror("Connection failed", str(exc))
            return

        try:
            self.new_tabs = list_new_tabs(self.new_conn)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Failed to list destination tabs", str(exc))
            return

        self.dest_combo["values"] = [f"{t[1]}  ({t[2]})" for t in self.new_tabs]
        self.conn_status_var.set(
            "Connected. Source is read-only for this whole session; "
            "destination is read-write, but every write happens inside one "
            "transaction you must explicitly confirm."
        )

    # -- main panes ----------------------------------------------------------

    def _build_main_panes(self) -> None:
        panes = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)

        # Left: source picker + tree preview
        left = ttk.LabelFrame(panes, text="Source tab (old schema)")
        panes.add(left, weight=1)

        search_row = ttk.Frame(left)
        search_row.pack(fill=tk.X, padx=4, pady=4)
        self.old_search_var = tk.StringVar()
        ttk.Entry(search_row, textvariable=self.old_search_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(search_row, text="Search", command=self.on_search_old).pack(side=tk.LEFT, padx=4)

        self.old_results = tk.Listbox(left, height=6)
        self.old_results.pack(fill=tk.X, padx=4, pady=4)
        self.old_results.bind("<<ListboxSelect>>", self.on_pick_old_tab)
        self._old_search_matches: list[tuple[int, str, str]] = []

        ttk.Label(left, text="Loaded subtree preview:").pack(anchor="w", padx=4)
        self.old_tree_view = ttk.Treeview(left, columns=("info",), show="tree headings")
        self.old_tree_view.heading("#0", text="Tab")
        self.old_tree_view.heading("info", text="Info")
        self.old_tree_view.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        self.old_summary_var = tk.StringVar(value="No source tab loaded yet.")
        ttk.Label(left, textvariable=self.old_summary_var, wraplength=480, foreground="gray").pack(
            anchor="w", padx=4, pady=(0, 4)
        )

        # Right: destination picker + status
        right = ttk.LabelFrame(panes, text="Destination tab (new schema)")
        panes.add(right, weight=1)

        ttk.Label(right, text="Pick destination tab:").pack(anchor="w", padx=4, pady=(4, 0))
        self.dest_combo = ttk.Combobox(right, state="readonly")
        self.dest_combo.pack(fill=tk.X, padx=4, pady=4)
        self.dest_combo.bind("<<ComboboxSelected>>", self.on_pick_dest_tab)

        self.dest_status_text = ScrolledText(right, height=14, state="disabled")
        self.dest_status_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        button_row = ttk.Frame(right)
        button_row.pack(fill=tk.X, padx=4, pady=4)
        ttk.Button(button_row, text="Refresh destination status", command=self.on_pick_dest_tab).pack(
            side=tk.LEFT
        )
        self.migrate_button = ttk.Button(
            button_row, text="Run migration", command=self.on_run_migration, state="disabled"
        )
        self.migrate_button.pack(side=tk.RIGHT)

    def _build_log_pane(self) -> None:
        frame = ttk.LabelFrame(self, text="Log")
        frame.pack(fill=tk.BOTH, expand=False, padx=8, pady=(0, 8))
        self.log_widget = ScrolledText(frame, height=10)
        self.log_widget.pack(fill=tk.BOTH, expand=True)
        self.log = Logger(self.log_widget)

    # -- source side ---------------------------------------------------------

    def on_search_old(self) -> None:
        if self.old_conn is None:
            messagebox.showwarning("Not connected", "Connect to both databases first.")
            return
        query = self.old_search_var.get().strip()
        if not query:
            return
        try:
            self._old_search_matches = search_old_tabs(self.old_conn, query)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Search failed", str(exc))
            return

        self.old_results.delete(0, tk.END)
        for tab_id, title, document_id in self._old_search_matches:
            self.old_results.insert(tk.END, f"[{tab_id}] {title}")

    def on_pick_old_tab(self, _event=None) -> None:
        selection = self.old_results.curselection()
        if not selection:
            return
        tab_id, title, document_id = self._old_search_matches[selection[0]]

        try:
            self.old_tree_root = fetch_old_subtree(self.old_conn, tab_id, title)
        except ContentShapeError as exc:
            self.old_tree_root = None
            messagebox.showerror("Unsupported content shape", str(exc))
            self.old_summary_var.set("Load failed — see error dialog.")
            return
        except Exception as exc:  # noqa: BLE001
            self.old_tree_root = None
            messagebox.showerror("Failed to load source subtree", str(exc))
            return

        self._render_old_tree()
        node_count = self.old_tree_root.node_count()
        depth = self.old_tree_root.max_depth()
        self.old_summary_var.set(
            f"Loaded '{title}' — {node_count} node(s) total, max depth {depth}. "
            f"Content validated: every node is a plain BlockNote block list."
        )
        self.log.line(f"Loaded source subtree '{title}' (id={tab_id}): {node_count} nodes, depth {depth}.")
        self._update_migrate_button_state()

    def _render_old_tree(self) -> None:
        self.old_tree_view.delete(*self.old_tree_view.get_children())
        if self.old_tree_root is None:
            return

        def insert(node: OldNode, parent: str) -> None:
            label = f"{node.title}"
            info = f"id={node.id}, blocks={len(node.content)}"
            item = self.old_tree_view.insert(parent, tk.END, text=label, values=(info,))
            for child in node.children:
                insert(child, item)

        insert(self.old_tree_root, "")

    # -- destination side ------------------------------------------------------

    def on_pick_dest_tab(self, _event=None) -> None:
        if self.new_conn is None:
            messagebox.showwarning("Not connected", "Connect to both databases first.")
            return
        index = self.dest_combo.current()
        if index < 0:
            return
        tab_id, title, document_id = self.new_tabs[index]

        try:
            self.dest_status = get_destination_status(self.new_conn, tab_id, title)
        except MigrationBlocked as exc:
            self.dest_status = None
            self._set_dest_status_text(str(exc))
            self._update_migrate_button_state()
            return
        except Exception as exc:  # noqa: BLE001
            self.dest_status = None
            messagebox.showerror("Failed to inspect destination", str(exc))
            return

        lines = [f"Destination tab: {title}  (document_id={document_id})"]
        if self.dest_status.root_gridstack_id is not None:
            lines.append(f"Root gridstack id: {self.dest_status.root_gridstack_id}")
        if self.dest_status.root_component_id is not None:
            lines.append(f"Existing Super Block Note root component id: {self.dest_status.root_component_id}")
            lines.append(f"Existing sub-tabs under it: {self.dest_status.existing_children_count}")
            lines.append(f"Root already has its own content: {self.dest_status.root_has_nonempty_content}")
        else:
            lines.append("No Super Block Note component on this canvas yet.")

        if self.dest_status.warnings:
            lines.append("")
            lines.append("Warnings:")
            lines.extend(f"  - {w}" for w in self.dest_status.warnings)

        if self.dest_status.blocking_reasons:
            lines.append("")
            lines.append("BLOCKED:")
            lines.extend(f"  - {b}" for b in self.dest_status.blocking_reasons)

        self._set_dest_status_text("\n".join(lines))
        self._update_migrate_button_state()

    def _set_dest_status_text(self, text: str) -> None:
        self.dest_status_text.configure(state="normal")
        self.dest_status_text.delete("1.0", tk.END)
        self.dest_status_text.insert(tk.END, text)
        self.dest_status_text.configure(state="disabled")

    def _update_migrate_button_state(self) -> None:
        ready = (
            self.old_tree_root is not None
            and self.dest_status is not None
            and not self.dest_status.blocking_reasons
        )
        self.migrate_button.configure(state="normal" if ready else "disabled")

    # -- run migration -----------------------------------------------------

    def on_run_migration(self) -> None:
        if self.old_tree_root is None or self.dest_status is None or self.new_conn is None:
            return

        node_count = self.old_tree_root.node_count()
        depth = self.old_tree_root.max_depth()
        confirmed = messagebox.askyesno(
            "Confirm migration",
            f"This will write to the live destination database.\n\n"
            f"Source: '{self.old_tree_root.title}' ({node_count} node(s), depth {depth})\n"
            f"Destination: '{self.dest_status.tab_title}'\n\n"
            f"Proceed?",
        )
        if not confirmed:
            return

        try:
            manifest = run_migration(self.new_conn, self.dest_status, self.old_tree_root, self.log)
            self.new_conn.commit()
        except MigrationBlocked as exc:
            self.new_conn.rollback()
            messagebox.showerror("Migration blocked", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.new_conn.rollback()
            self.log.line(f"ERROR — rolled back everything: {exc}")
            messagebox.showerror("Migration failed (rolled back)", str(exc))
            return

        manifest_path = self._save_manifest(manifest)
        self.log.line(
            f"Migration committed. components={manifest['counts']['components']} "
            f"page_contents={manifest['counts']['page_contents']}. "
            f"Manifest saved to {manifest_path}"
        )
        messagebox.showinfo(
            "Migration complete",
            f"Created {manifest['counts']['components']} component(s) and "
            f"{manifest['counts']['page_contents']} page_content row(s).\n\n"
            f"Manifest (for manual rollback reference) saved to:\n{manifest_path}",
        )

        # Refresh destination status so the guard reflects the new state.
        self.on_pick_dest_tab()

    def _save_manifest(self, manifest: dict[str, Any]) -> Path:
        MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_title = re.sub(r"[^A-Za-z0-9._-]+", "_", self.dest_status.tab_title)
        path = MANIFEST_DIR / f"{stamp}_{safe_title}.json"
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return path


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
