"""
Dify router — chat completion proxy.

Endpoints:
  POST /chat        → sends a query to Dify.ai and returns the response (blocking)
  POST /chat/stream → sends a query to Dify.ai and returns an SSE stream
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.dependencies import get_current_user, get_dify_service
from app.models.auth import UserInfo
from app.models.dify import DifyQueryRequest, DifyQueryResponse
from app.services.dify_service import DifyService

router = APIRouter(prefix="/dify", tags=["Dify.ai"])


@router.post("/chat", response_model=DifyQueryResponse, summary="Chat with Dify.ai")
async def chat(
    body: DifyQueryRequest,
    user: UserInfo = Depends(get_current_user),
    dify_service: DifyService = Depends(get_dify_service),
):
    """
    Forward the user's query to Dify.ai and return the assistant's
    response.  Requires authentication.

    The caller may include an optional ``conversation_id`` to continue
    an existing conversation thread.
    """
    # Override the user field with the authenticated email for traceability
    body.user = user.email
    return await dify_service.chat(body)


@router.post("/chat/stream", summary="Chat with Dify.ai (streaming SSE)")
async def chat_stream(
    body: DifyQueryRequest,
    user: UserInfo = Depends(get_current_user),
    dify_service: DifyService = Depends(get_dify_service),
):
    """
    Forward the user's query to Dify.ai and stream back the response
    as **Server-Sent Events**.  Requires authentication.

    The caller may include an optional ``conversation_id`` to continue
    an existing conversation thread.
    """
    body.user = user.email
    return StreamingResponse(
        dify_service.chat_stream(body),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",       # disable nginx buffering
        },
    )
