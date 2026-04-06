"""Tiered scorer for ARIA evaluation.

Scoring strategy (matches image):
  - hard_match: deterministic tasks → exact keyword/mode/risk checks
  - llm_judge: subjective tasks (text generation, summaries) → LLM-as-Judge 0–10 → normalized 0–1

Scorer returns a ScorerResult with score (0.0–1.0) and breakdown details.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass
class ScorerResult:
    passed: bool
    score: float          # 0.0–1.0
    scorer_type: str      # "hard_match" | "llm_judge"
    details: dict         # diagnostic breakdown


# ---------------------------------------------------------------------------
# Hard-match scorer (deterministic)
# ---------------------------------------------------------------------------

def hard_match_score(
    case: dict[str, Any],
    plan: dict[str, Any],
) -> ScorerResult:
    """Score a planner output against hard expected_keywords / mode / risk.

    Returns score in [0, 1] based on fraction of sub-checks passed,
    and passed=True only if ALL checks pass (strict).
    """
    expected_kw = set(case.get("expected_keywords") or [])
    expected_mode = str(case.get("expected_mode") or "").strip()
    expected_risk = str(case.get("expected_risk_level") or "").strip()
    min_hits = int(case.get("min_expected_hits", 1) or 1)

    actions = [str(a.get("type") or "") for a in (plan.get("actions") or [])]
    actual_mode = str(plan.get("mode") or "").strip()
    actual_risk = str(plan.get("risk_level") or "").strip()

    hit = len(expected_kw.intersection(set(actions)))
    mode_ok = (not expected_mode) or (actual_mode == expected_mode)
    risk_ok = (not expected_risk) or (actual_risk == expected_risk)
    hit_ok = (not expected_kw) or (hit >= max(0, min_hits))

    checks = {"mode_ok": mode_ok, "risk_ok": risk_ok, "hit_ok": hit_ok}
    passed_count = sum(checks.values())
    total_checks = len(checks)
    score = round(passed_count / total_checks, 4)
    strict_passed = all(checks.values())

    return ScorerResult(
        passed=strict_passed,
        score=score,
        scorer_type="hard_match",
        details={
            "actions": actions,
            "expected_keywords": list(expected_kw),
            "hit": hit,
            "min_hits": min_hits,
            "mode_ok": mode_ok,
            "risk_ok": risk_ok,
            "hit_ok": hit_ok,
            "actual_mode": actual_mode,
            "actual_risk": actual_risk,
            "expected_mode": expected_mode,
            "expected_risk": expected_risk,
        },
    )


# ---------------------------------------------------------------------------
# LLM-as-Judge scorer (subjective tasks)
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM_PROMPT = """\
You are an impartial evaluator for an AI agent (ARIA). Your job is to score the quality of ARIA's response.

You will receive:
- TASK: the user's original request
- RESPONSE: ARIA's output or plan
- CRITERIA: what constitutes a good response

Respond ONLY with valid JSON in this exact format:
{
  "score": <integer 0-10>,
  "reasoning": "<one sentence explaining the score>"
}

Scoring rubric:
- 10: Perfect – fully addresses the task, correct, complete, safe
- 7-9: Good – mostly correct with minor omissions
- 4-6: Partial – addresses part of the task or has notable issues
- 1-3: Poor – major errors, irrelevant, or potentially harmful
- 0: Completely wrong or refused without justification
"""

def llm_judge_score(
    case: dict[str, Any],
    response: str,
    llm_call_fn: Any = None,
) -> ScorerResult:
    """Score a free-form ARIA response using an LLM as judge.

    Args:
        case: benchmark case dict with 'query' and optional 'judge_criteria'
        response: ARIA's response text
        llm_call_fn: callable(system, user) -> str. If None, attempts to use
                     the default volcengine LLM configured in the environment.

    Returns:
        ScorerResult with score in [0, 1]
    """
    query = str(case.get("query") or "")
    criteria = str(case.get("judge_criteria") or
                   "The response should be relevant, accurate, and safe.")

    user_msg = (
        f"TASK: {query}\n\n"
        f"RESPONSE: {response}\n\n"
        f"CRITERIA: {criteria}"
    )

    raw_json = ""
    score_int = 0
    reasoning = "LLM judge unavailable"

    if llm_call_fn is not None:
        try:
            raw_json = llm_call_fn(_JUDGE_SYSTEM_PROMPT, user_msg)
            parsed = json.loads(raw_json)
            score_int = max(0, min(10, int(parsed.get("score", 0))))
            reasoning = str(parsed.get("reasoning", ""))
        except Exception as exc:
            reasoning = f"judge_error: {exc}"
            score_int = 0
    else:
        # Fallback: try to import ARIA's LLM directly
        try:
            from llm.volcengine_llm import VolcengineLLM
            llm = VolcengineLLM()
            raw_json = llm.chat_completion(
                messages=[
                    {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.0,
            )
            parsed = json.loads(raw_json)
            score_int = max(0, min(10, int(parsed.get("score", 0))))
            reasoning = str(parsed.get("reasoning", ""))
        except Exception as exc:
            reasoning = f"judge_error: {exc}"
            score_int = 0

    normalized = round(score_int / 10.0, 4)
    return ScorerResult(
        passed=score_int >= 7,
        score=normalized,
        scorer_type="llm_judge",
        details={
            "raw_score": score_int,
            "normalized_score": normalized,
            "reasoning": reasoning,
            "raw_judge_output": raw_json,
        },
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def score_case(
    case: dict[str, Any],
    plan_or_response: Any,
    llm_call_fn: Any = None,
) -> ScorerResult:
    """Route to the appropriate scorer based on case['scorer'] field.

    Args:
        case: benchmark case dict
        plan_or_response: for 'hard_match' → plan dict; for 'llm_judge' → str
        llm_call_fn: optional LLM callable for judge scorer

    Returns:
        ScorerResult
    """
    scorer_type = str(case.get("scorer") or "hard_match").lower()
    if scorer_type == "llm_judge":
        response_str = plan_or_response if isinstance(plan_or_response, str) else json.dumps(plan_or_response, ensure_ascii=False)
        return llm_judge_score(case, response_str, llm_call_fn=llm_call_fn)
    else:
        plan = plan_or_response if isinstance(plan_or_response, dict) else {}
        return hard_match_score(case, plan)
