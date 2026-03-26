"""API call construction — system prompt assembly, message sanitization.

Extracted from run_agent.py. Handles the translation between the
internal message format and what the model API expects.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


def assemble_system_prompt(
    call: str,
    capability_presentation: str | None = None,
) -> str:
    """Combine identity (call) and capability presentation into system prompt.

    In compact mode, call already contains the full system prompt from
    nchat.call.build_system_prompt_compact(). In that case, capability_presentation
    is None and we just return the call.
    """
    if capability_presentation:
        return f"{call}\n\n{capability_presentation}"
    return call


def sanitize_messages(messages: List[Dict]) -> List[Dict]:
    """Fix orphaned tool_call / tool_result pairs before API calls.

    Ensures every tool_call has a matching tool result and vice versa.
    This is critical after context compression which may remove messages
    from the middle of the conversation.
    """
    # Collect all tool_call IDs from assistant messages
    call_ids: set = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                cid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
                if cid:
                    call_ids.add(cid)

    # Collect all result IDs from tool messages
    result_ids: set = set()
    for msg in messages:
        if msg.get("role") == "tool":
            cid = msg.get("tool_call_id")
            if cid:
                result_ids.add(cid)

    # Drop orphaned results
    orphaned_results = result_ids - call_ids
    if orphaned_results:
        messages = [
            m for m in messages
            if not (m.get("role") == "tool" and m.get("tool_call_id") in orphaned_results)
        ]

    # Inject stubs for missing results
    missing_results = call_ids - result_ids
    if missing_results:
        patched = []
        for msg in messages:
            patched.append(msg)
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    cid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
                    if cid in missing_results:
                        patched.append({
                            "role": "tool",
                            "tool_call_id": cid,
                            "content": "[result removed during context compression]",
                        })
                        missing_results.discard(cid)
        messages = patched

    return messages


def build_tool_schemas(
    gate_names: Set[str],
    compact: bool = False,
) -> List[Dict]:
    """Build OpenAI-format tool schemas from the registry.

    Args:
        gate_names: Set of tool names to include.
        compact: Use compact descriptions when available.

    Returns:
        List of tool schema dicts.
    """
    from tools.registry import registry
    return registry.get_definitions(gate_names, quiet=True, compact=compact)
