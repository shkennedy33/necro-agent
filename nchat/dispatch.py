"""Tool call dispatch — routes gate calls to handlers.

Extracted from run_agent.py's tool dispatch logic. Provides a clean
interface between the loop and the tool registry.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def dispatch_tool_call(
    tool_name: str,
    tool_args: Dict[str, Any],
    task_id: str | None = None,
    parent_agent: Any = None,
) -> str:
    """Dispatch a single tool call through the registry.

    Args:
        tool_name: The tool/gate name to call.
        tool_args: Arguments for the tool.
        task_id: Task ID for terminal session isolation.
        parent_agent: Parent agent context (some tools need this).

    Returns:
        JSON string result from the tool.
    """
    from tools.registry import registry

    kwargs = {}
    if task_id:
        kwargs["task_id"] = task_id
    if parent_agent:
        kwargs["parent_agent"] = parent_agent

    try:
        result = registry.dispatch(tool_name, tool_args, **kwargs)
        return result
    except Exception as e:
        logger.exception("Tool dispatch error for %s: %s", tool_name, e)
        return json.dumps({"error": f"Tool execution failed: {type(e).__name__}: {e}"})


def parse_tool_arguments(arguments_str: str) -> Dict[str, Any]:
    """Parse tool call arguments from JSON string.

    Handles common edge cases: empty string, malformed JSON,
    and non-dict results.
    """
    if not arguments_str or not arguments_str.strip():
        return {}
    try:
        parsed = json.loads(arguments_str)
        if isinstance(parsed, dict):
            return parsed
        return {"value": parsed}
    except json.JSONDecodeError:
        return {"raw": arguments_str}
