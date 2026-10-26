"""View As schemas — the scope/role pickers and the response envelopes of
the read endpoints that accept ``view_as_role`` / ``view_as_scope``."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.tab import NavTabListAPIResponse, TabSummaryListAPIResponse, TabWorkspaceAPIResponse

VIEW_AS_FLAG_DESCRIPTION = (
    "True when the response was built for the role/scope given in "
    "view_as_role/view_as_scope; False for the caller's own access."
)


class ViewAsOption(BaseModel):
    id: int
    key: str = Field(description="Send this as view_as_role / view_as_scope.")
    name: str


class ViewAsScopeOption(ViewAsOption):
    is_universal: bool = Field(description="True for the universal scope, which covers every scope.")


class ViewAsScopeListAPIResponse(BaseModel):
    data: list[ViewAsScopeOption] = Field(default_factory=list)


class ViewAsRoleListAPIResponse(BaseModel):
    scope_id: int
    data: list[ViewAsOption] = Field(default_factory=list)


class NavTabListViewAsAPIResponse(NavTabListAPIResponse):
    view_as: bool = Field(default=False, description=VIEW_AS_FLAG_DESCRIPTION)


class TabSummaryListViewAsAPIResponse(TabSummaryListAPIResponse):
    view_as: bool = Field(default=False, description=VIEW_AS_FLAG_DESCRIPTION)


class TabWorkspaceViewAsAPIResponse(TabWorkspaceAPIResponse):
    view_as: bool = Field(default=False, description=VIEW_AS_FLAG_DESCRIPTION)
