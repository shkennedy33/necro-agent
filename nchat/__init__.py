"""Necronomichat v2 — cantrip-informed agent core.

The nchat package provides the fork's new subsystems:

  crystals  — Three-tier model routing (PRIMARY/WORKER/UTILITY)
  call      — Identity/system prompt builder (soul + capability presentation)
  wards     — Structural constraints (MaxTurns, BudgetWarning, PostCastReview)
  review    — Post-cast review cantrip (compressed context on WORKER crystal)
  loop      — The clean agent loop (conversation + code medium)
  dispatch  — Tool call routing through the registry
  api       — API call construction and message sanitization
  session   — Session persistence and title generation
  stream    — Streaming response handler
  loom      — Append-only turn recording (JSONL)
  medium    — Code medium: entity writes programs, gates are host functions (Phase 7a)
  sandbox   — Python subprocess sandbox with RPC gate injection (Phase 7a)
  compose   — Familiar pattern: cantrip/cast/cast_batch (Phase 7b)
  fold      — Context folding for code medium (spec §6.8)
"""

from nchat.crystals import CrystalTier, Crystal, resolve_crystal, resolve_model_id
from nchat.wards import MaxTurns, MaxTokens, BudgetWarning, PostCastReview, Circle, compose_wards
from nchat.loop import CastResult, TurnRecord, AgentBridge, ContextOverflowError, MaxRetriesError
from nchat.loom import Loom, Turn, GateCallRecord
from nchat.medium import CodeMedium, GateSpec, Observation
from nchat.sandbox import PythonSandbox, SandboxResult
from nchat.compose import ComposeEngine, CanTripConfig, CanTripHandle
from nchat.fold import fold_code_context, fold_conversation_context, should_fold

__all__ = [
    # Crystals
    "CrystalTier", "Crystal", "resolve_crystal", "resolve_model_id",
    # Wards
    "MaxTurns", "MaxTokens", "BudgetWarning", "PostCastReview", "Circle", "compose_wards",
    # Loop
    "CastResult", "TurnRecord", "AgentBridge",
    "ContextOverflowError", "MaxRetriesError",
    # Loom
    "Loom", "Turn", "GateCallRecord",
    # Medium (Phase 7a)
    "CodeMedium", "GateSpec", "Observation",
    "PythonSandbox", "SandboxResult",
    # Compose (Phase 7b)
    "ComposeEngine", "CanTripConfig", "CanTripHandle",
    # Fold (spec §6.8)
    "fold_code_context", "fold_conversation_context", "should_fold",
]
