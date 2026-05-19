"""
Gemini (Google Generative AI) service.

Thin wrapper around the ``google-genai`` SDK. Loads prompt templates
from the ``prompts/`` directory and exposes high-level helpers that
return parsed JSON payloads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from app.config import Settings

logger = logging.getLogger(__name__)

# Repo-root /prompts directory. ``app/`` is one level below the repo root.
_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_TICKET_PROMPT_FILE = "ticket_data_parsing.yaml"
_SLACK_MESSAGE_PLACEHOLDER = "{{SLACK_MESSAGE}}"

# Match an optional ```json ... ``` (or ``` ... ```) fenced block so we
# can recover JSON even when the model ignores the "no markdown" rule.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


class GeminiService:
    """Handles communication with the Gemini API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None  # lazily initialised on first use
        self._ticket_prompt: str | None = None

    # ── client ────────────────────────────────────────────────────────
    def _get_client(self):
        if self._client is None:
            if not self._settings.GEMINI_API_KEY:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="GEMINI_API_KEY is not configured.",
                )
            try:
                # Imported lazily so the app starts even if the package
                # is not installed in the local environment.
                from google import genai  # type: ignore
            except ImportError as exc:
                logger.exception("google-genai package is not installed.")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="google-genai package is not installed.",
                ) from exc
            self._client = genai.Client(api_key=self._settings.GEMINI_API_KEY)
        return self._client

    # ── prompt loading ────────────────────────────────────────────────
    def _load_ticket_prompt(self) -> str:
        if self._ticket_prompt is None:
            path = _PROMPTS_DIR / _TICKET_PROMPT_FILE
            try:
                template = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.exception("Failed to load ticket prompt at %s", path)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Ticket parsing prompt not found at {path}.",
                ) from exc
            if _SLACK_MESSAGE_PLACEHOLDER not in template:
                # Append the placeholder if the prompt file does not
                # already include it so the message is always passed.
                logger.warning(
                    "Ticket prompt %s missing '%s' placeholder; appending it.",
                    path,
                    _SLACK_MESSAGE_PLACEHOLDER,
                )
                template = (
                    template.rstrip()
                    + "\n\nSlack message:\n"
                    + _SLACK_MESSAGE_PLACEHOLDER
                    + "\n"
                )
            self._ticket_prompt = template
        return self._ticket_prompt

    # ── helpers ───────────────────────────────────────────────────────
    @staticmethod
    def _extract_json(raw: str) -> dict[str, Any]:
        """Parse the model's response, tolerating accidental code fences."""
        if not raw:
            raise ValueError("Empty response from Gemini.")
        text = raw.strip()
        fence_match = _FENCE_RE.match(text)
        if fence_match:
            text = fence_match.group(1).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.error("Gemini returned non-JSON output: %r", raw)
            raise ValueError(
                f"Gemini did not return valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"Gemini returned JSON of unexpected type: {type(parsed).__name__}"
            )
        return parsed

    def _generate_json_sync(self, prompt: str) -> dict[str, Any]:
        """Synchronous call into the Gemini SDK; off-loaded to a thread."""
        client = self._get_client()
        model = self._settings.GEMINI_MODEL
        logger.info("Calling Gemini model=%s", model)
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
        except Exception as exc:
            logger.exception("Gemini generate_content call failed.")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Gemini API error: {exc}",
            ) from exc

        raw_text = getattr(response, "text", None)
        if not raw_text:
            logger.error("Gemini response did not include text output.")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Gemini response did not include text output.",
            )
        logger.debug("Gemini raw response: %s", raw_text)
        try:
            return self._extract_json(raw_text)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc

    # ── public API ────────────────────────────────────────────────────
    async def extract_ticket_fields(self, slack_message: str) -> dict[str, Any]:
        """Run the ticket-extraction prompt against the given Slack message.

        Returns a dict with the keys defined in the prompt:
        ``title``, ``description``, ``assignee_email``, ``assignee_full_name``,
        ``due_date``. Missing values are ``None``.
        """
        template = self._load_ticket_prompt()
        prompt = template.replace(_SLACK_MESSAGE_PLACEHOLDER, slack_message or "")
        logger.info(
            "Gemini ticket extraction: dispatching prompt (message_len=%d).",
            len(slack_message or ""),
        )
        parsed = await asyncio.to_thread(self._generate_json_sync, prompt)
        # Normalise: ensure all expected keys are present, default to None.
        for key in (
            "title",
            "description",
            "assignee_email",
            "assignee_full_name",
            "due_date",
        ):
            parsed.setdefault(key, None)
        return parsed
