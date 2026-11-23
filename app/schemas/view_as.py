"""View As schemas — the scope/role pickers and the response envelopes of
the read endpoints that accept ``view_as=role_key:scope_key``."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.tab import NavTabListAPIResponse, TabSummaryListAPIResponse, TabWorkspaceAPIResponse

VIEW_AS_FROM_DESCRIPTION = (
    "The `view_as_from` audit email, echoed back under View As; null when none was sent or "
    "without `view_as` pairs."
)

VIEW_AS_FLAG_DESCRIPTION = (
    "True when the response was built from the `view_as` pairs; False for the caller's own access."
)


class ViewAsOption(BaseModel):
    id: int
    key: str = Field(description="Use in `view_as=role_key:scope_key`.")
    name: str


class ViewAsScopeOption(ViewAsOption):
    is_universal: bool = Field(description="True for the universal scope, which covers every scope.")


class ViewAsScopeListAPIResponse(BaseModel):
    data: list[ViewAsScopeOption] = Field(default_factory=list)


class ViewAsUserAssignment(BaseModel):
    role_key: str
    scope_key: str


class ViewAsUserOption(BaseModel):
    id: int
    name: str
    email: str = Field(description="Send this as `view_as_from`, alongside the user's assignments as `view_as` pairs.")
    assignments: list[ViewAsUserAssignment] = Field(
        default_factory=list,
        description="The user's role/scope assignments, as keys. Empty when they hold none.",
    )


class ViewAsUserListAPIResponse(BaseModel):
    data: list[ViewAsUserOption] = Field(default_factory=list)


class ViewAsRoleListAPIResponse(BaseModel):
    scope_id: int
    data: list[ViewAsOption] = Field(default_factory=list)


class NavTabListViewAsAPIResponse(NavTabListAPIResponse):
    view_as: bool = Field(default=False, description=VIEW_AS_FLAG_DESCRIPTION)
    view_as_from: str | None = Field(default=None, description=VIEW_AS_FROM_DESCRIPTION)


class TabSummaryListViewAsAPIResponse(TabSummaryListAPIResponse):
    view_as: bool = Field(default=False, description=VIEW_AS_FLAG_DESCRIPTION)
    view_as_from: str | None = Field(default=None, description=VIEW_AS_FROM_DESCRIPTION)


class TabWorkspaceViewAsAPIResponse(TabWorkspaceAPIResponse):
    view_as: bool = Field(default=False, description=VIEW_AS_FLAG_DESCRIPTION)
    view_as_from: str | None = Field(default=None, description=VIEW_AS_FROM_DESCRIPTION)
