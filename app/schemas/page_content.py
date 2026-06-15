from typing import Any

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr


class PageContentWorkspaceResponse(BaseModel):
    # Page content object returned inside the tab workspace response.
    # The frontend should use documentId instead of the internal numeric database id.
    documentId: StrictStr | None = None

    # Main editor/body content.
    # It can be a JSON object, list of blocks, or null depending on the stored data.
    content: dict[str, Any] | list[Any] | None = None


class PageContentAPIResponse(BaseModel):
    # Used by:
    # GET /tabs/{documentId}/content
    # PUT /tabs/{documentId}/content
    data: PageContentWorkspaceResponse


class PageContentResponse(BaseModel):
    # Kept for backward compatibility with older service/router code.
    # Later we can remove it if no old endpoint still depends on it.
    id: StrictInt
    document_id: StrictStr | None = None
    content: dict[str, Any] | list[Any] | None = None

    model_config = ConfigDict(from_attributes=True)