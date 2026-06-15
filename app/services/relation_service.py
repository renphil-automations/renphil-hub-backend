from sqlalchemy.orm import Session

from app.models.tab import Tab
from app.models.page_content import PageContent
from app.models.links import PageContentTabLink, TabParentLink



def get_relation_diagnostics(db: Session):
    tabs = db.query(Tab).all()
    page_contents = db.query(PageContent).all()
    tab_content_links = db.query(PageContentTabLink).all()
    parent_links = db.query(TabParentLink).all()

    tab_ids = {tab.id for tab in tabs}
    page_content_ids = {content.id for content in page_contents}

    linked_tab_ids = {
        link.tab_id for link in tab_content_links if link.tab_id is not None
    }

    linked_page_content_ids = {
        link.page_content_id
        for link in tab_content_links
        if link.page_content_id is not None
    }

    tabs_without_content = [
        tab.id for tab in tabs if tab.id not in linked_tab_ids
    ]

    page_contents_without_tab = [
        content.id
        for content in page_contents
        if content.id not in linked_page_content_ids
    ]

    broken_tab_content_links = []

    for link in tab_content_links:
        if link.tab_id not in tab_ids or link.page_content_id not in page_content_ids:
            broken_tab_content_links.append(
                {
                    "link_id": link.id,
                    "tab_id": link.tab_id,
                    "page_content_id": link.page_content_id,
                }
            )

    broken_parent_links = []

    for link in parent_links:
        if link.tab_id not in tab_ids or link.inv_tab_id not in tab_ids:
            broken_parent_links.append(
                {
                    "link_id": link.id,
                    "tab_id": link.tab_id,
                    "parent_id": link.inv_tab_id,
                }
            )

    return {
        "tabs_without_content": tabs_without_content,
        "page_contents_without_tab": page_contents_without_tab,
        "broken_tab_content_links": broken_tab_content_links,
        "broken_parent_links": broken_parent_links,
    }

def repair_missing_content(db: Session):
    repaired_tabs = []

    tabs = db.query(Tab).all()

    linked_tab_ids = {
        link.tab_id
        for link in db.query(PageContentTabLink).all()
    }

    try:
        for tab in tabs:
            if tab.id not in linked_tab_ids:

                new_content = PageContent(
                    content={}
                )

                db.add(new_content)
                db.flush()

                new_link = PageContentTabLink(
                    tab_id=tab.id,
                    page_content_id=new_content.id
                )

                db.add(new_link)

                repaired_tabs.append(tab.id)

        db.commit()

        return {
            "message": "Repair completed",
            "repaired_tabs": repaired_tabs
        }

    except Exception:
        db.rollback()
        raise