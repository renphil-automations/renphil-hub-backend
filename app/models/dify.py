"""Pydantic schemas for Dify.ai chat interactions."""

from pydantic import BaseModel, Field


class DifyQueryRequest(BaseModel):
    """Incoming chat query from the frontend."""
    query: str = Field(..., min_length=1, max_length=4000, description="The user's question.")
    conversation_id: str | None = Field(
        default=None,
        description="Optional conversation ID to continue an existing chat.",
    )
    user: str = Field(
        default="renphil-user",
        description="Unique user identifier sent to Dify.",
    )


class DifyQueryResponse(BaseModel):
    """Simplified response returned to the frontend."""
    answer: str
    conversation_id: str | None = None
    message_id: str | None = None
