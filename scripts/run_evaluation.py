"""ARIA Evaluation Runner — integrates Pass@k, tiered scoring, and transcript logging.

Usage:
    python scripts/run_evaluation.py [options]

Options:
    --k INT                  Trials per case (default: 5)
    --cases PATH             Path to benchmark cases JSON (default: data/benchmarks/regression_tasks.json)
    --output PATH            Output report path (default: data/benchmarks/latest_eval_report.json)
    --categories CAT,...     Run only specified categories (comma-separated)
    --min-avg-pass-at-k N    Fail if avg Pass@k < N (0.0–1.0)
    --no-transcripts         Skip saving transcripts to disk
    --dry-run                Validate config only, don't run trials

The runner produces:
    data/benchmarks/latest_eval_report.json   — full structured report
    data/benchmarks/transcripts/              — per-trial CoT transcripts
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation.pass_at_k import run_pass_at_k, aggregate_suite_results, TrialResult, PassAtKResult
from evaluation.scorer import score_case
from evaluation.transcript_logger import TranscriptLogger, ThoughtStep, build_transcript_summary


# ---------------------------------------------------------------------------
# Trial execution: planner-level (no real browser/desktop side-effects)
# ---------------------------------------------------------------------------

def _make_planner_trial_fn(manager: Any, logger: TranscriptLogger, save_transcripts: bool):
    """Factory: returns a trial_fn(case, trial_index) -> TrialResult for planner eval."""

    def trial_fn(case: dict[str, Any], trial_index: int) -> TrialResult:
        query = str(case.get("query") or "")
        scorer_type = str(case.get("scorer") or "hard_match")
        tid = logger.start(case["name"], trial_index, query, scorer_type=scorer_type)

        t0 = time.monotonic()
        error_msg = ""
        plan: dict[str, Any] = {}
        try:
            plan = manager.plan_actions(query, "")
            # Enrich plan with evaluated risk level
            risk = manager.evaluate_action_risk_level(plan.get("actions") or [])
            plan["risk_level"] = risk
        except Exception as exc:
            error_msg = str(exc)

        latency_ms = (time.monotonic() - t0) * 1000

        scorer_result = score_case(case, plan)

        # Log a synthetic step for the planner action
        logger.log_step(tid, ThoughtStep(
            turn=0,
            thought=f"Plan for: {query}",
            action_type="plan_actions",
            action_params={"query": query},
            observation=json.dumps({
                "mode": plan.get("mode"),
                "actions": [a.get("type") for a in (plan.get("actions") or [])],
                "risk_level": plan.get("risk_level"),
            }),
            success=scorer_result.passed,
            latency_ms=round(latency_ms, 2),
            error=error_msg,
        ))

        logger.finish(
            tid,
            final_result=json.dumps(plan, ensure_ascii=False),
            is_success=scorer_result.passed,
            score=scorer_result.score,
            metadata={"scorer_details": scorer_result.details},
        )

        transcript_id = tid
        if save_transcripts:
            path, _ = logger.save_and_get(tid)
            transcript_id = path
        else:
            logger._active.pop(tid, None)

        return TrialResult(
            trial_index=trial_index,
            passed=scorer_result.passed,
            score=scorer_result.score,
            latency_ms=round(latency_ms, 2),
            transcript_id=transcript_id,
            error=error_msg,
            raw_output=plan,
        )

    return trial_fn


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_evaluation(
    manager: Any,
    cases: list[dict[str, Any]],
    k: int = 5,
    categories: list[str] | None = None,
    save_transcripts: bool = True,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Run Pass@k evaluation over all cases and return the full report dict."""
    logger = TranscriptLogger()

    if categories:
        cases = [c for c in cases if c.get("category") in categories]

    trial_fn = _make_planner_trial_fn(manager, logger, save_transcripts)

    results: list[PassAtKResult] = []
    for case in cases:
        print(f"  [{case.get('name')}] running {k} trials...", flush=True)
        result = run_pass_at_k(case, trial_fn, k=k)
        results.append(result)
        status = "PASS" if result.pass_at_k >= 0.8 else ("WARN" if result.pass_at_k >= 0.5 else "FAIL")
        print(
            f"    → Pass@{k}={result.pass_at_k:.3f}  pass_rate={result.pass_rate:.3f}"
            f"  avg_score={result.avg_score:.3f}  [{status}]",
            flush=True,
        )

    suite = aggregate_suite_results(results)
    transcript_paths = logger.list_transcripts() if save_transcripts else []
    transcript_summary = build_transcript_summary(transcript_paths) if transcript_paths else {}

    report: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "k": k,
        "categories_filter": categories or [],
        "suite": suite,
        "transcript_summary": transcript_summary,
        "cases": [r.to_dict() for r in results],
    }

    out = output_path or (ROOT / "data" / "benchmarks" / "latest_eval_report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nReport saved to: {out}", flush=True)
    return report


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Run ARIA Pass@k evaluation benchmark.")
    parser.add_argument("--k", type=int, default=5, help="Trials per case (default: 5)")
    parser.add_argument(
        "--cases",
        default=str(ROOT / "data" / "benchmarks" / "regression_tasks.json"),
        help="Path to benchmark cases JSON",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "data" / "benchmarks" / "latest_eval_report.json"),
        help="Output report path",
    )
    parser.add_argument("--categories", default="", help="Comma-separated category filter")
    parser.add_argument(
        "--min-avg-pass-at-k",
        type=float,
        default=0.0,
        help="Fail if avg Pass@k is below this threshold",
    )
    parser.add_argument("--no-transcripts", action="store_true", help="Skip saving transcripts")
    parser.add_argument("--dry-run", action="store_true", help="Validate config only")
    args = parser.parse_args()

    cases_path = Path(args.cases)
    if not cases_path.is_file():
        raise SystemExit(f"benchmark file not found: {cases_path}")

    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    print(f"Loaded {len(cases)} cases from {cases_path}")

    categories = [c.strip() for c in args.categories.split(",") if c.strip()] or None

    if args.dry_run:
        print("Dry-run: config valid. Exiting.")
        return

    from aria_manager import ARIAManager
    manager = ARIAManager()
    manager.set_api_key("")

    print(f"\nStarting evaluation: k={args.k}, categories={categories or 'all'}\n")
    report = run_evaluation(
        manager=manager,
        cases=cases,
        k=args.k,
        categories=categories,
        save_transcripts=not args.no_transcripts,
        output_path=Path(args.output),
    )

    suite = report.get("suite") or {}
    avg_pak = float(suite.get("avg_pass_at_k") or 0.0)
    print(f"\n{'='*60}")
    print(f"  avg Pass@{args.k}         : {avg_pak:.4f}")
    print(f"  avg pass_rate       : {suite.get('avg_pass_rate', 0.0):.4f}")
    print(f"  avg score           : {suite.get('avg_score', 0.0):.4f}")
    print(f"  avg latency         : {suite.get('avg_latency_ms', 0.0):.0f} ms")
    print(f"  perfect consistency : {suite.get('perfect_consistency_cases', 0)}/{suite.get('total_cases', 0)}")
    print(f"  zero-pass cases     : {suite.get('zero_pass_cases', 0)}")

    ts = report.get("transcript_summary") or {}
    if ts:
        print(f"\n  Failure attributions: {ts.get('failure_attributions', {})}")

    print(f"{'='*60}\n")

    if avg_pak < args.min_avg_pass_at_k:
        raise SystemExit(
            f"evaluation failed: avg_pass_at_k={avg_pak:.4f} < min={args.min_avg_pass_at_k:.4f}"
        )


if __name__ == "__main__":
    main()
