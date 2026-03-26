"""Necronomichat v2 — cantrip-informed agent core.

The nchat package provides the fork's new subsystems:

  crystals  — Three-tier model routing (PRIMARY/WORKER/UTILITY)
  call      — Identity/system prompt builder (soul + capability presentation)
  wards     — Structural constraints (MaxTurns, BudgetWarning, PostCastReview)
  review    — Post-cast review cantrip (compressed context on WORKER crystal)
  loop      — The clean agent loop (target architecture)
  dispatch  — Tool call routing through the registry
  api       — API call construction and message sanitization
  session   — Session persistence and title generation
  stream    — Streaming response handler
  loom      — Append-only turn recording (JSONL)
"""

from nchat.crystals import CrystalTier, Crystal, resolve_crystal
from nchat.wards import MaxTurns, BudgetWarning, PostCastReview, Circle
from nchat.loop import CastResult, TurnRecord, AgentBridge, ContextOverflowError, MaxRetriesError
from nchat.loom import Loom, Turn, GateCallRecord

__all__ = [
    "CrystalTier", "Crystal", "resolve_crystal",
    "MaxTurns", "BudgetWarning", "PostCastReview", "Circle",
    "CastResult", "TurnRecord", "AgentBridge",
    "ContextOverflowError", "MaxRetriesError",
    "Loom", "Turn", "GateCallRecord",
]
