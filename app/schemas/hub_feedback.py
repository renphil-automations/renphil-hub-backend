from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class HubFeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: UUID
    scope: Literal["hub", "page", "component"]
    message: str = Field(min_length=1, max_length=2000)
    page_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    component_link: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    page_route: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("page_route")
    @classmethod
    def private_route_template(cls, value):
        if value is None:
            return None
        from app.services.product_analytics_service import route_template
        safe = route_template(value)
        if safe is None:
            raise ValueError("Feedback page context must be an internal route")
        return safe

    @field_validator("message")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("Feedback must contain text without null characters")
        return value

    @model_validator(mode="after")
    def valid_scope(self):
        if self.scope != "hub" and not self.page_id:
            raise ValueError("Page feedback requires a page")
        if self.scope == "component" and not self.component_link:
            raise ValueError("Component feedback requires a component")
        if self.scope != "component" and self.component_link is not None:
            raise ValueError("Only component feedback accepts a component")
        return self
