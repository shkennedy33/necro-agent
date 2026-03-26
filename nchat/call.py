"""Identity / call builder: SOUL.md → clean system prompt.

Two layers, cleanly separated per cantrip CIRCLE-11:

  Layer 1: The Call (identity). SOUL.md content. Nothing else.
  Layer 2: Capability presentation. Auto-generated from the circle's
           active gate set, memory snapshot, skills index, etc.

The soul is the soul. The circle presents its own capabilities.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default identity — used when no SOUL.md exists.
# Two sentences. The model already knows how to do things.
# ---------------------------------------------------------------------------

DEFAULT_CALL = (
    "You are Necronomichat, an AI entity created by your operator. "
    "You are direct, perceptive, and genuine."
)


# ---------------------------------------------------------------------------
# Platform hints — one line per platform. Not behavioral guidance,
# just the bare minimum the model needs to format correctly.
# ---------------------------------------------------------------------------

PLATFORM_HINTS_COMPACT = {
    "whatsapp": "You are on WhatsApp. Markdown does not render. Use MEDIA:/path for file attachments.",
    "telegram": "You are on Telegram. Markdown does not render. Use MEDIA:/path for file attachments.",
    "discord": "You are in Discord. Use MEDIA:/path for file attachments.",
    "slack": "You are in Slack. Use MEDIA:/path for file attachments.",
    "signal": "You are on Signal. Markdown does not render. Use MEDIA:/path for file attachments.",
}


def build_call(soul_path: Path | None = None) -> str:
    """Build the identity layer. Pure soul, nothing else.

    If SOUL.md exists, return its content (with YAML frontmatter stripped
    and security scanning applied). Otherwise, return the minimal default.
    """
    if soul_path is None:
        # Default location
        from hermes_constants import get_hermes_home
        soul_path = get_hermes_home() / "SOUL.md"

    if soul_path and soul_path.exists():
        try:
            content = soul_path.read_text(encoding="utf-8").strip()
            content = _strip_yaml_frontmatter(content)
            # Reuse hermes's security scanning
            from agent.prompt_builder import _scan_context_content
            content = _scan_context_content(content, "SOUL.md")
            if content:
                return content
        except Exception as e:
            logger.warning("Failed to load SOUL.md: %s", e)

    return DEFAULT_CALL


def _strip_yaml_frontmatter(content: str) -> str:
    """Remove YAML frontmatter (--- ... ---) from the start of content."""
    if content.startswith("---"):
        lines = content.split("\n")
        in_frontmatter = True
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                return "\n".join(lines[i + 1:]).strip()
        # No closing --- found, return as-is
    return content


def build_capability_presentation(
    gate_names: List[str],
    gate_descriptions: Dict[str, str],
    *,
    memory_snapshot: str | None = None,
    user_profile: str | None = None,
    skills_index: str | None = None,
    session_id: str | None = None,
    model: str | None = None,
    platform: str | None = None,
    honcho_block: str | None = None,
) -> str:
    """Build the circle's capability presentation.

    Auto-generated from the active gate set. This is the only place
    tool/gate information enters the system prompt. No behavioral guidance.
    No "do NOT" instructions. Just what exists.

    Args:
        gate_names: Names of active tools/gates.
        gate_descriptions: Map of gate name → one-line description.
        memory_snapshot: Formatted memory block (if entries exist).
        user_profile: Formatted user profile block (if entries exist).
        skills_index: Skills listing (if skills exist).
        session_id: Current session identifier.
        model: Model identifier string.
        platform: Platform name (cli, telegram, discord, etc.).
        honcho_block: Honcho integration block (if active).
    """
    from hermes_time import now as _hermes_now
    now = _hermes_now()

    sections = []

    # Environment metadata
    env_lines = [f"Conversation started: {now.strftime('%A, %B %d, %Y %I:%M %p')}"]
    if session_id:
        env_lines.append(f"Session: {session_id}")
    if model:
        env_lines.append(f"Model: {model}")
    sections.append("## Environment\n\n" + "\n".join(env_lines))

    # Active gates — just names and one-liners
    if gate_names:
        gate_lines = ["You have access to the following tools. Use them as needed.\n"]
        for name in sorted(gate_names):
            desc = gate_descriptions.get(name, "")
            gate_lines.append(f"- {name}: {desc}" if desc else f"- {name}")
        sections.append("## Active Gates\n\n" + "\n".join(gate_lines))

    # Memory (only if entries exist)
    if memory_snapshot:
        sections.append(f"## Memory\n\n{memory_snapshot}")

    # User profile (only if entries exist)
    if user_profile:
        sections.append(f"## User Profile\n\n{user_profile}")

    # Skills (only if skills exist)
    if skills_index:
        sections.append(f"## Skills\n\n{skills_index}")

    # Honcho (if active)
    if honcho_block:
        sections.append(honcho_block)

    # Platform hint — one line
    platform_key = (platform or "").lower().strip()
    if platform_key in PLATFORM_HINTS_COMPACT:
        sections.append(PLATFORM_HINTS_COMPACT[platform_key])

    return "\n\n".join(sections)


def build_system_prompt_compact(
    soul_path: Path | None = None,
    gate_names: List[str] | None = None,
    gate_descriptions: Dict[str, str] | None = None,
    *,
    memory_snapshot: str | None = None,
    user_profile: str | None = None,
    skills_index: str | None = None,
    session_id: str | None = None,
    model: str | None = None,
    platform: str | None = None,
    system_message: str | None = None,
    honcho_block: str | None = None,
    context_files_prompt: str | None = None,
) -> str:
    """Assemble the full compact system prompt.

    Identity (call) + capability presentation (circle-derived).
    No behavioral guidance. No "do NOT" instructions.
    """
    parts = [build_call(soul_path)]

    if system_message:
        parts.append(system_message)

    cap = build_capability_presentation(
        gate_names=gate_names or [],
        gate_descriptions=gate_descriptions or {},
        memory_snapshot=memory_snapshot,
        user_profile=user_profile,
        skills_index=skills_index,
        session_id=session_id,
        model=model,
        platform=platform,
        honcho_block=honcho_block,
    )
    if cap:
        parts.append(cap)

    # Context files (AGENTS.md etc.) — still included if present
    if context_files_prompt:
        parts.append(context_files_prompt)

    return "\n\n".join(parts)
