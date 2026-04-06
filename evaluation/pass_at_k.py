"""Pass@k statistical evaluation module.

Implements the Pass@k metric for measuring ARIA reliability:
  - Run each task k times (trials)
  - Pass@k = probability that at least one trial succeeds
  - Also tracks: pass_rate (c/k), stability (std dev), and consistency
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, asdict
from typing import Any, Callable


@dataclass
class TrialResult:
    trial_index: int
    passed: bool
    score: float          # 0.0–1.0
    latency_ms: float
    transcript_id: str    # links to TranscriptLogger entry
    error: str = ""
    raw_output: Any = None


@dataclass
class PassAtKResult:
    case_name: str
    k: int
    c: int                # number of passing trials
    pass_at_k: float      # P(at least 1 success in k trials)
    pass_rate: float      # c / k  (empirical success rate)
    avg_score: float
    std_score: float
    avg_latency_ms: float
    trials: list[TrialResult] = field(default_factory=list)
    category: str = ""
    difficulty: str = ""
    scorer_type: str = "hard_match"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["trials"] = [asdict(t) for t in self.trials]
        return d


def _pass_at_k_exact(n: int, c: int, k: int) -> float:
    """Exact combinatorial Pass@k from the HumanEval paper:
       Pass@k = 1 - C(n-c, k) / C(n, k)
    where n=total trials, c=correct trials, k=target k.
    Falls back to empirical estimate when n < k.
    """
    if n < k:
        return float(c > 0)
    if n - c < k:
        return 1.0
    # Use log to avoid overflow: log C(n-c,k) - log C(n,k)
    def log_comb(a: int, b: int) -> float:
        return sum(math.log(a - i) - math.log(i + 1) for i in range(b))

    log_num = log_comb(n - c, k)
    log_den = log_comb(n, k)
    return 1.0 - math.exp(log_num - log_den)


def run_pass_at_k(
    case: dict[str, Any],
    trial_fn: Callable[[dict[str, Any], int], TrialResult],
    k: int = 5,
) -> PassAtKResult:
    """Run `k` trials of a single benchmark case and compute Pass@k.

    Args:
        case: benchmark case dict (must have 'name' key)
        trial_fn: callable(case, trial_index) -> TrialResult
        k: number of trials to run

    Returns:
        PassAtKResult with all statistical metrics
    """
    trials: list[TrialResult] = []
    for i in range(k):
        result = trial_fn(case, i)
        trials.append(result)

    c = sum(1 for t in trials if t.passed)
    scores = [t.score for t in trials]
    latencies = [t.latency_ms for t in trials]

    avg_score = statistics.mean(scores) if scores else 0.0
    std_score = statistics.stdev(scores) if len(scores) > 1 else 0.0
    avg_latency = statistics.mean(latencies) if latencies else 0.0
    pass_at_k = _pass_at_k_exact(k, c, k)

    return PassAtKResult(
        case_name=case.get("name", ""),
        k=k,
        c=c,
        pass_at_k=pass_at_k,
        pass_rate=round(c / max(1, k), 4),
        avg_score=round(avg_score, 4),
        std_score=round(std_score, 4),
        avg_latency_ms=round(avg_latency, 2),
        trials=trials,
        category=case.get("category", ""),
        difficulty=case.get("difficulty", ""),
        scorer_type=case.get("scorer", "hard_match"),
    )


def aggregate_suite_results(results: list[PassAtKResult]) -> dict[str, Any]:
    """Aggregate Pass@k results across the full benchmark suite."""
    if not results:
        return {"total_cases": 0}

    total = len(results)
    avg_pass_at_k = statistics.mean(r.pass_at_k for r in results)
    avg_pass_rate = statistics.mean(r.pass_rate for r in results)
    avg_score = statistics.mean(r.avg_score for r in results)
    avg_latency = statistics.mean(r.avg_latency_ms for r in results)

    # Per-category breakdown
    by_category: dict[str, list[PassAtKResult]] = {}
    for r in results:
        by_category.setdefault(r.category or "unknown", []).append(r)

    category_stats: dict[str, dict] = {}
    for cat, cat_results in by_category.items():
        category_stats[cat] = {
            "count": len(cat_results),
            "avg_pass_at_k": round(statistics.mean(r.pass_at_k for r in cat_results), 4),
            "avg_pass_rate": round(statistics.mean(r.pass_rate for r in cat_results), 4),
        }

    # Per-difficulty breakdown
    by_difficulty: dict[str, list[PassAtKResult]] = {}
    for r in results:
        by_difficulty.setdefault(r.difficulty or "unknown", []).append(r)

    difficulty_stats: dict[str, dict] = {}
    for diff, diff_results in by_difficulty.items():
        difficulty_stats[diff] = {
            "count": len(diff_results),
            "avg_pass_at_k": round(statistics.mean(r.pass_at_k for r in diff_results), 4),
        }

    # Stability: fraction of cases where all k trials passed (perfect consistency)
    perfect_consistency = sum(1 for r in results if r.c == r.k)
    zero_pass = sum(1 for r in results if r.c == 0)

    return {
        "total_cases": total,
        "avg_pass_at_k": round(avg_pass_at_k, 4),
        "avg_pass_rate": round(avg_pass_rate, 4),
        "avg_score": round(avg_score, 4),
        "avg_latency_ms": round(avg_latency, 2),
        "perfect_consistency_cases": perfect_consistency,
        "zero_pass_cases": zero_pass,
        "by_category": category_stats,
        "by_difficulty": difficulty_stats,
    }
