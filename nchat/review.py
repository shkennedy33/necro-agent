"""Post-cast review cantrip.

Replaces _spawn_background_review with a focused, cheap entity that:
  1. Gets a COMPRESSED version of the conversation (not full history)
  2. Uses a WORKER crystal (Sonnet, not Opus)
  3. Has access to memory and skill_manage gates only
  4. Max 4 turns

Token savings: 10-20x cost reduction for the review step.
  Before: 30 turns of full Opus input tokens re-processed
  After:  ~2-4K tokens of compressed context on Sonnet
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional
from uuid import uuid4

from nchat.crystals import CrystalTier, resolve_crystal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Review prompts — identical to hermes's originals, just extracted here.
# ---------------------------------------------------------------------------

MEMORY_REVIEW_PROMPT = (
    "Review the conversation summary below. If anything is worth saving to "
    "persistent memory (user preferences, environment facts, stable conventions, "
    "corrections), save it now using the memory tool. Focus on facts that "
    "reduce future user steering. Do NOT save task progress or session outcomes."
)

SKILL_REVIEW_PROMPT = (
    "Review the conversation summary below. If the user completed a complex "
    "task (5+ steps), fixed a tricky error, or discovered a non-trivial workflow, "
    "save the approach as a skill using skill_manage. If an existing skill was "
    "used and found outdated, patch it."
)

COMBINED_REVIEW_PROMPT = (
    "Review the conversation summary below. Two tasks:\n\n"
    "1. MEMORY: If anything is worth saving to persistent memory (user preferences, "
    "environment facts, corrections), save it using the memory tool.\n\n"
    "2. SKILLS: If the user completed a complex task, fixed a tricky error, or "
    "discovered a non-trivial workflow, save it as a skill using skill_manage.\n\n"
    "Focus on durable facts and reusable procedures. Do NOT save task progress."
)


def _compress_for_review(messages: List[Dict], max_chars: int = 8000) -> str:
    """Extract salient content from a conversation for review.

    Not a full transcript. A compressed summary:
    - User messages (full text)
    - Assistant text responses (full text)
    - Tool calls (name + abbreviated args only)
    - Tool results (first 200 chars only)
    """
    parts = []
    total_chars = 0

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "user":
            text = f"User: {content}"
        elif role == "assistant":
            text_parts = []
            if content:
                text_parts.append(f"Assistant: {content}")
            # Summarize tool calls
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                name = fn.get("name", "?")
                args_str = fn.get("arguments", "")
                if len(args_str) > 100:
                    args_str = args_str[:100] + "..."
                text_parts.append(f"  → {name}({args_str})")
            text = "\n".join(text_parts)
        elif role == "tool":
            # Abbreviate tool results
            tool_id = msg.get("tool_call_id", "?")
            if isinstance(content, str) and len(content) > 200:
                content = content[:200] + "..."
            text = f"  ← {content}"
        elif role == "system":
            continue  # Skip system messages in review
        else:
            continue

        if not text.strip():
            continue

        total_chars += len(text)
        if total_chars > max_chars:
            parts.append("[... earlier conversation truncated for review ...]")
            break
        parts.append(text)

    return "\n".join(parts)


def spawn_review_cantrip(
    messages: List[Dict],
    review_memory: bool = True,
    review_skills: bool = True,
    crystal_tier: CrystalTier = CrystalTier.WORKER,
    parent_agent: Any = None,
    max_iterations: int = 4,
) -> None:
    """Spawn a review entity using a worker crystal.

    Runs in a background thread. The review entity gets compressed
    context and has access to memory and skill_manage gates only.

    Args:
        messages: The conversation history to review.
        review_memory: Whether to review for memory saves.
        review_skills: Whether to review for skill creation.
        crystal_tier: Which crystal to use (default: WORKER).
        parent_agent: The parent AIAgent (for memory store access).
        max_iterations: Max tool-calling turns for the review (default: 4).
    """
    if not review_memory and not review_skills:
        return

    # Pick the right prompt
    if review_memory and review_skills:
        prompt = COMBINED_REVIEW_PROMPT
    elif review_memory:
        prompt = MEMORY_REVIEW_PROMPT
    else:
        prompt = SKILL_REVIEW_PROMPT

    # Compress the conversation
    review_context = _compress_for_review(messages)
    full_prompt = f"{prompt}\n\nConversation summary:\n{review_context}"

    def _run():
        try:
            import contextlib
            import os as _os

            # Resolve the worker crystal
            crystal = resolve_crystal(crystal_tier, parent_agent=parent_agent)

            with open(_os.devnull, "w") as _devnull, \
                 contextlib.redirect_stdout(_devnull), \
                 contextlib.redirect_stderr(_devnull):

                from run_agent import AIAgent

                review_agent = AIAgent(
                    model=crystal.model,
                    base_url=crystal.base_url or None,
                    api_key=crystal.api_key or None,
                    provider=crystal.provider or None,
                    api_mode=crystal.api_mode,
                    max_iterations=max_iterations,
                    quiet_mode=True,
                    platform=getattr(parent_agent, "platform", None),
                    enabled_toolsets=["memory", "skills"],
                    skip_context_files=True,
                    skip_memory=False,
                )

                # Share the memory store with parent
                if parent_agent:
                    review_agent._memory_store = getattr(
                        parent_agent, "_memory_store", None
                    )
                    review_agent._memory_enabled = getattr(
                        parent_agent, "_memory_enabled", True
                    )
                    review_agent._user_profile_enabled = getattr(
                        parent_agent, "_user_profile_enabled", True
                    )

                # Disable nudges in the review agent
                review_agent._memory_nudge_interval = 0
                review_agent._skill_nudge_interval = 0

                # Run with compressed context only (no parent history)
                review_agent.run_conversation(
                    user_message=full_prompt,
                    conversation_history=[],
                )

            # Surface actions to user (same pattern as hermes)
            actions = []
            for msg in getattr(review_agent, "_session_messages", []):
                if not isinstance(msg, dict) or msg.get("role") != "tool":
                    continue
                try:
                    data = json.loads(msg.get("content", "{}"))
                except (json.JSONDecodeError, TypeError):
                    continue
                if not data.get("success"):
                    continue
                message = data.get("message", "")
                if any(kw in message.lower() for kw in
                       ["created", "updated", "added", "removed", "replaced"]):
                    actions.append(message)

            if actions and parent_agent:
                summary = " · ".join(dict.fromkeys(actions))
                safe_print = getattr(parent_agent, "_safe_print", None)
                if safe_print:
                    safe_print(f"  💾 {summary}")

        except Exception as e:
            logger.debug("Review cantrip failed: %s", e)

    t = threading.Thread(target=_run, daemon=True, name="nchat-review")
    t.start()
