"""
Inventory every Airtable widget and the token it holds — READ ONLY.

Why this exists: the PAT is now write-only (the server never sends it back),
which is correct but removes the only way anyone could answer "which widgets
use the token I am about to revoke?". This script answers it from the
database, printing each token's non-secret `pat<id>` prefix — the same
identifier Airtable's own token-management UI shows — never the token.

Use it to drive the post-deploy rotation: any widget listed here had its
token stored BEFORE the read-side strip landed, so that token was delivered
to every viewer who ever loaded the page and should be rotated in Airtable
and re-entered in the Property Panel.

**Runs no writes.** Issues SELECTs only, against DATABASE_URL_V2.

    cd renphil-hub-backend
    PYTHONPATH=. ./.venv/Scripts/python.exe scripts/list_airtable_widget_tokens.py
"""
from __future__ import annotations

import sys
from collections import defaultdict

from app.db_v2.database import SessionLocalV2
from app.db_v2.models.tab import TabV2  # noqa: F401  (FK resolution)
from app.db_v2.models.gridstack import GridstackV2
from app.db_v2.models.component import ComponentV2
from app.db_v2.models.page_content import PageContentV2  # noqa: F401

from app.services.gridstack_service import (
    AIRTABLE_WIDGET_TYPE,
    _pat_hint,
    _raw_component_data,
)


def main() -> int:
    db = SessionLocalV2()
    try:
        components = (
            db.query(ComponentV2)
            .filter(ComponentV2.type == AIRTABLE_WIDGET_TYPE)
            .order_by(ComponentV2.id.asc())
            .all()
        )

        if not components:
            print("No Airtable widgets found.")
            return 0

        by_token: dict[str, list[str]] = defaultdict(list)
        rows: list[tuple[str, ...]] = []

        for c in components:
            data = _raw_component_data(db, c)
            pat = data.get("pat")
            hint = _pat_hint(pat) or "-"
            has = "yes" if isinstance(pat, str) and pat.strip() else "NO"

            gridstack = (
                db.query(GridstackV2).filter(GridstackV2.id == c.gridstack_id).first()
            )
            location = gridstack.name if gridstack else "?"

            rows.append((
                str(c.id),
                c.link or "-",
                (c.title or "-")[:28],
                (location or "-")[:24],
                has,
                hint,
                str(data.get("patUpdatedAt") or "-")[:10],
                "yes" if data.get("personalizeEnabled") else "no",
                str(data.get("personalizeColumn") or "-")[:20],
            ))
            if has == "yes":
                by_token[hint].append(c.link or str(c.id))

        headers = ("id", "link", "title", "tab", "pat", "token",
                   "set", "pers", "column")
        widths = [max(len(str(r[i])) for r in (*rows, headers)) for i in range(len(headers))]
        line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
        print(f"\n{line}\n{'-' * len(line)}")
        for r in rows:
            print("  ".join(str(v).ljust(w) for v, w in zip(r, widths)))

        print(f"\n{len(components)} Airtable widget(s); "
              f"{sum(1 for r in rows if r[4] == 'yes')} with a token stored.")

        shared = {t: links for t, links in by_token.items() if len(links) > 1}
        if shared:
            # ASCII only in printed output: this runs in the Windows console,
            # where the default code page mangles non-ASCII punctuation.
            print("\nTOKENS REUSED ACROSS WIDGETS - rotating one of these affects "
                  "every widget listed with it:")
            for token, links in shared.items():
                print(f"  {token}: {', '.join(links)}")
        else:
            print("\nNo token is shared between widgets.")

        missing = [r for r in rows if r[4] == "NO"]
        if missing:
            print(f"\n{len(missing)} widget(s) have NO token and will not render:")
            for r in missing:
                print(f"  id={r[0]} link={r[1]} title={r[2]}")

        print("\nRotation: for each token above, create a replacement in Airtable "
              "(scope data.records:read), paste it into the widget's Property "
              "Panel, save, re-run this script to confirm the prefix changed, "
              "then revoke the old token.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
