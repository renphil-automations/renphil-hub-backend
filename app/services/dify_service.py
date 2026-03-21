"""
Dify.ai service.

Sends user queries to a Dify chat-completion endpoint and returns
the assistant's response.  Uses the shared async HTTP client so
connections are reused.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

import httpx

from app.config import Settings
from app.helpers.exceptions import DifyError
from app.helpers.http_client import get_http_client
from app.models.dify import DifyQueryRequest, DifyQueryResponse

logger = logging.getLogger(__name__)


class DifyService:
    """Handles communication with the Dify.ai API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ── helpers ─────────────────────────────────────────────────────────
    def _build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.DIFY_API_KEY}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _build_payload(
        request: DifyQueryRequest, response_mode: str = "blocking"
    ) -> dict:
        payload: dict = {
            "inputs": {},
            "query": request.query,
            "response_mode": response_mode,
            "user": request.user,
        }
        if request.conversation_id:
            payload["conversation_id"] = request.conversation_id
        return payload

    # ── blocking chat ──────────────────────────────────────────────────
    async def chat(self, request: DifyQueryRequest) -> DifyQueryResponse:
        """
        Send a blocking-mode chat message to Dify and return the reply.

        Dify doc reference: POST /chat-messages
        """
        client = get_http_client()
        url = f"{self._settings.DIFY_API_BASE_URL}/chat-messages"

        try:
            response = await client.post(
                url,
                json=self._build_payload(request, "blocking"),
                headers=self._build_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Dify API returned %s: %s",
                exc.response.status_code,
                exc.response.text,
            )
            raise DifyError(
                f"Dify API error ({exc.response.status_code}): {exc.response.text}"
            ) from exc
        except httpx.RequestError as exc:
            logger.error("Dify request failed: %s", exc)
            raise DifyError("Could not reach Dify.ai.") from exc

        data = response.json()

        return DifyQueryResponse(
            answer=data.get("answer", ""),
            conversation_id=data.get("conversation_id"),
            message_id=data.get("message_id"),
        )

    # ── streaming chat ─────────────────────────────────────────────────
    async def chat_stream(
        self, request: DifyQueryRequest
    ) -> AsyncGenerator[bytes, None]:
        """
        Send a streaming-mode chat message to Dify and yield raw SSE
        chunks as they arrive.

        Each chunk is forwarded verbatim so the client receives standard
        Server-Sent Events (``data: {...}\\n\\n``).
        """
        client = get_http_client()
        url = f"{self._settings.DIFY_API_BASE_URL}/chat-messages"

        try:
            async with client.stream(
                "POST",
                url,
                json=self._build_payload(request, "streaming"),
                headers=self._build_headers(),
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    logger.error(
                        "Dify streaming API returned %s: %s",
                        response.status_code,
                        body.decode(),
                    )
                    raise DifyError(
                        f"Dify API error ({response.status_code}): {body.decode()}"
                    )

                async for chunk in response.aiter_bytes():
                    yield chunk

        except httpx.RequestError as exc:
            logger.error("Dify streaming request failed: %s", exc)
            raise DifyError("Could not reach Dify.ai.") from exc
