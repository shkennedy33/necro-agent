"""Session persistence — extracted from run_agent.py.

Handles saving/loading conversation state to the session DB (SQLite)
and session log files. Wraps hermes_state.py without replacing it.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def save_session(
    session_db: Any,
    session_id: str,
    messages: List[Dict],
    result: Any,
    platform: str | None = None,
) -> None:
    """Save session state to the session database.

    Args:
        session_db: The hermes_state session database instance.
        session_id: Session identifier.
        messages: Full conversation messages.
        result: CastResult or dict with response metadata.
        platform: Platform name for metadata.
    """
    if not session_db:
        return

    try:
        # Extract the response text
        if hasattr(result, "response"):
            response_text = result.response
        elif isinstance(result, dict):
            response_text = result.get("final_response", "")
        else:
            response_text = str(result)

        session_db.save_messages(session_id, messages)
        logger.debug("Session %s saved (%d messages)", session_id, len(messages))
    except Exception as e:
        logger.warning("Session save failed: %s", e)


def generate_title_async(
    session_db: Any,
    session_id: str,
    user_message: str,
    response: str,
) -> None:
    """Generate a title for the session in a background thread.

    Uses the UTILITY crystal for cheap title generation.
    """
    import threading

    def _generate():
        try:
            from nchat.crystals import resolve_crystal, CrystalTier
            crystal = resolve_crystal(CrystalTier.UTILITY)

            if not crystal.client or not crystal.model:
                return

            completion = crystal.client.chat.completions.create(
                model=crystal.model,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Generate a short title (3-6 words) for this conversation.\n\n"
                        f"User: {user_message[:200]}\n"
                        f"Assistant: {response[:200]}\n\n"
                        f"Title:"
                    ),
                }],
                max_tokens=30,
            )

            title = completion.choices[0].message.content.strip()
            title = title.strip('"\'')
            if title and session_db:
                session_db.update_title(session_id, title)
                logger.debug("Session %s titled: %s", session_id, title)

        except Exception as e:
            logger.debug("Title generation failed: %s", e)

    t = threading.Thread(target=_generate, daemon=True, name="nchat-title")
    t.start()
