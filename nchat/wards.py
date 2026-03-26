"""Ward definitions: structural constraints on the agent loop.

Wards are hard constraints, not advisory text. The entity cannot reason
its way around a ward because the ward operates outside the entity's context.

This replaces:
  - Memory nudge injection (run_agent.py:5731-5737)
  - Skill nudge injection (run_agent.py:5911-5920)
  - Budget warning injection into tool results (run_agent.py:5416-5435)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from nchat.crystals import CrystalTier


@dataclass
class MaxTurns:
    """Hard turn limit. Loop stops. No negotiation.

    When the loop reaches this many tool-calling turns, it terminates
    with status "truncated". The model doesn't need to know how many
    turns are left — it gets cut off if it exceeds the limit.
    """
    limit: int = 90


@dataclass
class RequireDone:
    """Only explicit done() terminates. Text-only responses continue the loop.

    Not currently used in the hermes fork (text-only responses terminate
    by default), but defined for future compatibility with cantrip patterns.
    """
    pass


@dataclass
class BudgetWarning:
    """Inject a single system message at a threshold percentage.

    Instead of injecting budget warnings into every tool result,
    send ONE system-level observation at the threshold. The model
    is smart enough to wrap up when it senses it's done.

    Set threshold to None to disable entirely.
    """
    threshold: float | None = 0.8  # inject at 80% of max_turns


@dataclass
class PostCastReview:
    """After the cast completes, spawn a review cantrip.

    Configured with the review crystal tier and what to review.
    This replaces mid-conversation memory/skill nudges with a
    structured post-cast review using a cheaper model.
    """
    crystal_tier: CrystalTier = CrystalTier.WORKER
    review_memory: bool = True
    review_skills: bool = True
    min_tool_calls: int = 3   # only review if the cast used 3+ tool calls
    min_turns: int = 5        # only review if the cast lasted 5+ turns


@dataclass
class Circle:
    """The environment: gates + wards.

    A simplified cantrip Circle for the current tool-calling medium.
    Gates are the tools in the registry. Wards are structural constraints.
    """
    gate_names: List[str] = field(default_factory=list)
    max_turns: MaxTurns = field(default_factory=MaxTurns)
    require_done: RequireDone | None = None
    budget_warning: BudgetWarning = field(default_factory=BudgetWarning)
    post_cast_review: PostCastReview | None = None

    def get_ward(self, ward_type: type) -> Any:
        """Get a ward by type, or None if not configured."""
        if ward_type == MaxTurns:
            return self.max_turns
        if ward_type == RequireDone:
            return self.require_done
        if ward_type == BudgetWarning:
            return self.budget_warning
        if ward_type == PostCastReview:
            return self.post_cast_review
        return None

    def has_ward(self, ward_type: type) -> bool:
        """Check if a ward is configured (not None)."""
        return self.get_ward(ward_type) is not None

    def should_warn(self, current_turn: int) -> bool:
        """Check if the budget warning should fire at this turn."""
        if not self.budget_warning or self.budget_warning.threshold is None:
            return False
        if self.max_turns.limit <= 20:
            return False  # Too short for warnings
        warn_at = int(self.max_turns.limit * self.budget_warning.threshold)
        return current_turn == warn_at

    def budget_warning_message(self, remaining: int) -> str:
        """Generate the budget warning system message."""
        return f"[Ward: {remaining} turns remaining. Consolidate.]"

    def should_review(self, turn_count: int, tool_call_count: int) -> bool:
        """Check if post-cast review should fire."""
        review = self.post_cast_review
        if not review:
            return False
        return (turn_count >= review.min_turns and
                tool_call_count >= review.min_tool_calls)


def load_circle_from_config(config: Dict[str, Any] | None = None) -> Circle:
    """Load ward configuration from cli-config.yaml.

    Config keys:
      wards.max_turns: int (default 90)
      wards.budget_warning_at: float or null (default 0.8)
      review.enabled: bool
      review.crystal: str (tier name)
      review.min_turns: int
      review.min_tool_calls: int
      review.review_memory: bool
      review.review_skills: bool
    """
    if config is None:
        try:
            from hermes_cli.config import load_config
            config = load_config()
        except Exception:
            config = {}

    # Ward defaults
    wards_config = config.get("wards", {})
    max_turns_limit = wards_config.get("max_turns", 90)
    budget_threshold = wards_config.get("budget_warning_at", 0.8)

    # Also check agent.max_turns for backward compat
    agent_config = config.get("agent", {})
    if "max_turns" in agent_config:
        max_turns_limit = agent_config["max_turns"]

    # Review config
    review_config = config.get("review", {})
    review_enabled = review_config.get("enabled", True)

    post_cast_review = None
    if review_enabled:
        tier_name = review_config.get("crystal", "worker")
        tier_map = {
            "primary": CrystalTier.PRIMARY,
            "worker": CrystalTier.WORKER,
            "utility": CrystalTier.UTILITY,
        }
        post_cast_review = PostCastReview(
            crystal_tier=tier_map.get(tier_name, CrystalTier.WORKER),
            review_memory=review_config.get("review_memory", True),
            review_skills=review_config.get("review_skills", True),
            min_tool_calls=review_config.get("min_tool_calls", 3),
            min_turns=review_config.get("min_turns", 5),
        )

    return Circle(
        max_turns=MaxTurns(limit=max_turns_limit),
        budget_warning=BudgetWarning(
            threshold=budget_threshold if budget_threshold else None,
        ),
        post_cast_review=post_cast_review,
    )
