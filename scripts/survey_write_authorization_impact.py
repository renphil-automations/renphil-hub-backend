"""
READ-ONLY survey of live access-control data — the pre-flight for
`AI Docs/plan_write_authorization_tabs_v2_sbn_v2_2026-08-03.md` §8.1.

Answers one question: *if every mutating endpoint in tabs_v2.py and
super_blocknote_v2.py started enforcing `access_control_service.can_edit`
tomorrow, who could still edit what, and what would become uneditable by
anyone except a Hub Admin?*

Emits **no writes of any kind** — SELECTs only, no commit, no flush, session
opened and closed read-only. Safe against the live DB.

Run:
    PYTHONPATH=. ./.venv/Scripts/python.exe scripts/survey_write_authorization_impact.py
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db_v2.database import SessionLocalV2  # noqa: E402
from app.db_v2.models.component import ComponentV2  # noqa: E402
from app.db_v2.models.gridstack import GridstackV2  # noqa: E402
from app.db_v2.models.hub import HubV2  # noqa: E402
from app.db_v2.models.nav_tab import NavTabV2  # noqa: E402
from app.db_v2.models.tab import TabV2  # noqa: E402
from app.services.tab_service import _SCOPED_ROLE_SCOPES  # noqa: E402

GRIDSTACK_REPRESENTATION_TYPES = ("gridstack", "super_gridstack")
SBN_ROOT_TYPE = "super_block_note"


def _principals(group: dict[str, Any] | None) -> list[str]:
    """Render one access_control group as flat, comparable labels."""
    group = group or {}
    out: list[str] = []
    for u in group.get("users") or []:
        if isinstance(u, dict):
            out.append(f"user:{(u.get('email') or '').strip().lower()}")
    for r in group.get("roles") or []:
        if not isinstance(r, dict):
            continue
        name = r.get("name", "")
        scope = (r.get("scope") or "").strip()
        detail = r.get("program") or r.get("function") or ""
        label = f"role:{name}"
        if scope:
            label += f" [{scope}{'/' + detail if detail else ''}]"
        if scope in _SCOPED_ROLE_SCOPES:
            label += "  <<< SKIPPED by backend can_edit"
        out.append(label)
    return out


def _admins_signature(ac: dict[str, Any] | None) -> str:
    if ac is None:
        return "<NULL access_control>"
    admins = _principals(ac.get("admins"))
    if not admins:
        return "<empty admins — Hub Admin role only>"
    return ", ".join(sorted(admins))


def _report(header: str, signatures: Counter[str], total: int) -> None:
    print(f"\n### {header}  (n={total})")
    if total == 0:
        print("  (none)")
        return
    for sig, count in signatures.most_common():
        print(f"  {count:>4} x  {sig}")


def main() -> None:
    db = SessionLocalV2()
    try:
        print("=" * 78)
        print("WRITE-AUTHORIZATION IMPACT SURVEY (read-only)")
        print("=" * 78)

        # -- hub -------------------------------------------------------
        hub = db.query(HubV2).order_by(HubV2.id).first()
        print("\n### Hub")
        if hub is None:
            print("  !! NO HUB ROW — require_hub_editor denies everyone but Hub Admins")
        else:
            print(f"  admins:  {_admins_signature(hub.access_control)}")
            print(f"  viewers: {', '.join(_principals((hub.access_control or {}).get('viewers')))
                                 or '<empty = universal>'}")

        # -- nav tabs --------------------------------------------------
        nav_tabs = db.query(NavTabV2).all()
        _report(
            "Nav tabs (already gated today)",
            Counter(_admins_signature(n.access_control) for n in nav_tabs),
            len(nav_tabs),
        )

        # -- root tabs / variants (NodeRef 'tab') ----------------------
        tabs = db.query(TabV2).all()
        roots = [t for t in tabs if t.parent_tab_id is None]
        variants = [t for t in tabs if t.parent_tab_id is not None]
        _report(
            "Root tabs — NEWLY gated by require_tab_editor",
            Counter(_admins_signature(t.access_control) for t in roots),
            len(roots),
        )
        _report(
            "Tab variants — NEWLY gated by require_tab_editor",
            Counter(_admins_signature(t.access_control) for t in variants),
            len(variants),
        )
        orphan_roots = [t for t in roots if t.nav_tab_id is None]
        if orphan_roots:
            print(
                f"\n  !! {len(orphan_roots)} root tab(s) have nav_tab_id IS NULL — "
                "_parent_of() returns None for these, so an upward AC resolution "
                "falls back to the hub, not to a nav tab."
            )
            for t in orphan_roots[:10]:
                print(f"       id={t.id} document_id={t.document_id!r} title={t.title!r}")

        # -- sub-tab gridstacks (NodeRef 'gridstack') ------------------
        subtabs = db.query(GridstackV2).filter(GridstackV2.parent_id.isnot(None)).all()
        rep_by_grid = {
            c.current_grid_id: c
            for c in db.query(ComponentV2).filter(ComponentV2.current_grid_id.isnot(None)).all()
        }
        sub_sigs: Counter[str] = Counter()
        missing_rep: list[GridstackV2] = []
        for g in subtabs:
            rep = rep_by_grid.get(g.id)
            if rep is None:
                missing_rep.append(g)
                sub_sigs["<NO representation component — read_ac returns None>"] += 1
            else:
                sub_sigs[_admins_signature(rep.access_control)] += 1
        _report(
            "Sub-tab gridstacks — NEWLY gated by require_tab_editor",
            sub_sigs,
            len(subtabs),
        )
        if missing_rep:
            print(
                f"\n  !! {len(missing_rep)} sub-tab gridstack(s) have no representation "
                "component (current_grid_id never backfilled). read_ac -> None."
            )
            for g in missing_rep[:10]:
                print(f"       id={g.id} document_id={g.document_id!r} name={g.name!r}")

        # -- SBN nodes -------------------------------------------------
        sbn_nodes = (
            db.query(ComponentV2)
            .filter(
                (ComponentV2.super_blocknote_id.isnot(None))
                | (ComponentV2.type == SBN_ROOT_TYPE)
            )
            .all()
        )
        _report(
            "SBN nodes (roots + descendants) — NEWLY gated by require_sbn_editor",
            Counter(_admins_signature(c.access_control) for c in sbn_nodes),
            len(sbn_nodes),
        )
        sbn_no_grid = [c for c in sbn_nodes if c.gridstack_id is None]
        if sbn_no_grid:
            print(
                f"\n  !! {len(sbn_no_grid)} SBN node(s) have gridstack_id IS NULL — "
                "resolved_parent_ac() falls straight through to the hub's AC."
            )

        # -- canvas widgets (subset-constrained, not gated directly) ---
        widgets = (
            db.query(ComponentV2)
            .filter(
                ComponentV2.super_blocknote_id.is_(None),
                ComponentV2.current_grid_id.is_(None),
                ComponentV2.type.notin_(GRIDSTACK_REPRESENTATION_TYPES),
                ComponentV2.type != SBN_ROOT_TYPE,
            )
            .all()
        )
        explicit = [w for w in widgets if w.access_control]
        print(
            f"\n### Canvas widgets  (n={len(widgets)}; {len(explicit)} carry an explicit "
            "access_control, the rest inherit)"
        )
        print("  Not gated per-widget — they ride PUT /{id}/content, gated at the tab.")

        # -- who can still edit anything -------------------------------
        print("\n" + "=" * 78)
        print("PRINCIPALS APPEARING IN ANY 'admins' GROUP (backend-evaluable only)")
        print("=" * 78)
        reach: dict[str, set[str]] = defaultdict(set)

        def collect(kind: str, ac: dict[str, Any] | None) -> None:
            for p in _principals((ac or {}).get("admins")):
                if "<<<" in p:
                    continue
                reach[p].add(kind)

        if hub is not None:
            collect("hub", hub.access_control)
        for n in nav_tabs:
            collect("nav_tab", n.access_control)
        for t in roots:
            collect("root_tab", t.access_control)
        for t in variants:
            collect("variant", t.access_control)
        for g in subtabs:
            rep = rep_by_grid.get(g.id)
            if rep is not None:
                collect("sub_tab", rep.access_control)
        for c in sbn_nodes:
            collect("sbn_node", c.access_control)

        if not reach:
            print("  (none — ONLY the 'Hub Admin' JWT role can edit anything)")
        for principal, kinds in sorted(reach.items()):
            print(f"  {principal}\n      -> {', '.join(sorted(kinds))}")

        # -- scoped-role grants that the backend will silently ignore --
        print("\n" + "=" * 78)
        print("SCOPED (Program/Function) ROLE GRANTS — GRANTED IN UI, DENIED BY BACKEND")
        print("=" * 78)
        found = False
        buckets: list[tuple[str, Any, dict[str, Any] | None]] = []
        if hub is not None:
            buckets.append(("hub", hub.document_id, hub.access_control))
        buckets += [("nav_tab", n.document_id, n.access_control) for n in nav_tabs]
        buckets += [("root_tab", t.document_id, t.access_control) for t in roots]
        buckets += [("variant", t.document_id, t.access_control) for t in variants]
        buckets += [
            ("sub_tab", g.document_id, rep_by_grid[g.id].access_control)
            for g in subtabs
            if g.id in rep_by_grid
        ]
        buckets += [("sbn_node", c.link, c.access_control) for c in sbn_nodes]
        for kind, doc_id, ac in buckets:
            hits = [p for p in _principals((ac or {}).get("admins")) if "<<<" in p]
            if hits:
                found = True
                print(f"  {kind} {doc_id!r}: {', '.join(hits)}")
        if not found:
            print("  (none — the scoped-role divergence has zero live blast radius)")

        print("\nDone. No writes were issued.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
