"""Structured transcript logger for ARIA evaluation.

Captures chain-of-thought (CoT) and tool call traces for each trial,
enabling post-hoc attribution of failures to:
  - Prompt issues
  - Tool call errors
  - Model logic failures

Transcripts are stored in data/benchmarks/transcripts/ as JSON files.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


_TRANSCRIPT_DIR = Path(__file__).resolve().parents[1] / "data" / "benchmarks" / "transcripts"


@dataclass
class ThoughtStep:
    turn: int
    thought: str
    action_type: str
    action_params: dict = field(default_factory=dict)
    observation: str = ""
    success: bool = False
    latency_ms: float = 0.0
    error: str = ""


@dataclass
class Transcript:
    transcript_id: str
    case_name: str
    trial_index: int
    query: str
    started_at: str
    ended_at: str = ""
    total_latency_ms: float = 0.0
    steps: list[ThoughtStep] = field(default_factory=list)
    final_result: str = ""
    is_success: bool = False
    failure_attribution: str = ""   # "prompt" | "tool_call" | "model_logic" | ""
    scorer_type: str = "hard_match"
    score: float = 0.0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["steps"] = [asdict(s) for s in self.steps]
        return d


class TranscriptLogger:
    """Records structured transcripts for each benchmark trial.

    Usage:
        logger = TranscriptLogger()
        tid = logger.start(case_name, trial_index, query)
        logger.log_step(tid, ThoughtStep(...))
        logger.finish(tid, final_result, is_success, score)
        saved_path = logger.save(tid)
    """

    def __init__(self, transcript_dir: str | Path | None = None):
        self._dir = Path(transcript_dir) if transcript_dir else _TRANSCRIPT_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._active: dict[str, Transcript] = {}
        self._timers: dict[str, float] = {}

    def start(self, case_name: str, trial_index: int, query: str, scorer_type: str = "hard_match") -> str:
        """Begin a new transcript. Returns transcript_id."""
        tid = str(uuid.uuid4())
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._active[tid] = Transcript(
            transcript_id=tid,
            case_name=case_name,
            trial_index=trial_index,
            query=query,
            started_at=ts,
            scorer_type=scorer_type,
        )
        self._timers[tid] = time.monotonic()
        return tid

    def log_step(self, transcript_id: str, step: ThoughtStep) -> None:
        """Append a thought/action/observation step."""
        t = self._active.get(transcript_id)
        if t is not None:
            t.steps.append(step)

    def log_step_from_taor(self, transcript_id: str, taor_trace_item: dict[str, Any], turn: int) -> None:
        """Convenience: build a ThoughtStep from a TAOR tool_trace entry."""
        action = taor_trace_item.get("action") or {}
        obs = taor_trace_item.get("observation") or ""
        step = ThoughtStep(
            turn=turn,
            thought=str(taor_trace_item.get("thought") or ""),
            action_type=str(action.get("type") or ""),
            action_params={k: v for k, v in action.items() if k != "type"},
            observation=str(obs),
            success="error" not in str(obs).lower() and "fail" not in str(obs).lower(),
        )
        self.log_step(transcript_id, step)

    def finish(
        self,
        transcript_id: str,
        final_result: str,
        is_success: bool,
        score: float = 0.0,
        metadata: dict | None = None,
    ) -> None:
        """Mark a transcript as complete."""
        t = self._active.get(transcript_id)
        if t is None:
            return
        elapsed = (time.monotonic() - self._timers.pop(transcript_id, time.monotonic())) * 1000
        t.ended_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        t.total_latency_ms = round(elapsed, 2)
        t.final_result = final_result
        t.is_success = is_success
        t.score = score
        t.failure_attribution = self._attribute_failure(t) if not is_success else ""
        if metadata:
            t.metadata.update(metadata)

    def save(self, transcript_id: str) -> str:
        """Persist transcript to disk. Returns file path."""
        t = self._active.pop(transcript_id, None)
        if t is None:
            return ""
        filename = f"{t.case_name}_trial{t.trial_index}_{transcript_id[:8]}.json"
        path = self._dir / filename
        path.write_text(json.dumps(t.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    def save_and_get(self, transcript_id: str) -> tuple[str, Transcript | None]:
        """Save and return (path, transcript) for in-memory access after save."""
        t = self._active.get(transcript_id)
        t_copy = None
        if t:
            import copy
            t_copy = copy.deepcopy(t)
        path = self.save(transcript_id)
        return path, t_copy

    def load(self, path: str) -> dict:
        """Load a saved transcript from disk."""
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def list_transcripts(self, case_name: str | None = None) -> list[str]:
        """List all saved transcript paths, optionally filtered by case name."""
        pattern = f"{case_name}_*.json" if case_name else "*.json"
        return sorted(str(p) for p in self._dir.glob(pattern))

    def _attribute_failure(self, t: Transcript) -> str:
        """Heuristic failure attribution based on step patterns."""
        if not t.steps:
            return "prompt"  # No steps → model didn't produce valid output

        failed_tools = [s for s in t.steps if not s.success and s.action_type]
        all_failed = len(failed_tools) == len([s for s in t.steps if s.action_type])

        if all_failed and failed_tools:
            return "tool_call"  # All tool calls failed → tool/env issue

        # Check for repeated same action (stall) → model logic
        action_types = [s.action_type for s in t.steps if s.action_type]
        if len(action_types) >= 3 and len(set(action_types[-3:])) == 1:
            return "model_logic"

        if failed_tools:
            return "tool_call"

        return "model_logic"


def build_transcript_summary(paths: list[str]) -> dict[str, Any]:
    """Summarize failure attributions across multiple transcripts."""
    attributions: dict[str, int] = {"prompt": 0, "tool_call": 0, "model_logic": 0, "": 0}
    total = 0
    success = 0
    for path in paths:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            total += 1
            if data.get("is_success"):
                success += 1
            attr = data.get("failure_attribution") or ""
            attributions[attr] = attributions.get(attr, 0) + 1
        except Exception:
            pass
    return {
        "total_transcripts": total,
        "success_count": success,
        "failure_attributions": {k: v for k, v in attributions.items() if k},
    }
