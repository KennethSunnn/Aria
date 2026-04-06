import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch


def _run_planner_regression(manager: Any, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        query = str(case.get("query") or "").strip()
        expected = set(case.get("expected_keywords") or [])
        expected_mode = str(case.get("expected_mode") or "").strip()
        expected_risk = str(case.get("expected_risk_level") or "").strip()
        min_expected_hits = int(case.get("min_expected_hits", 1) or 1)
        plan = manager.plan_actions(query, "")
        actions = [str(a.get("type") or "") for a in (plan.get("actions") or [])]
        hit = len(expected.intersection(set(actions)))
        risk_level = manager.evaluate_action_risk_level(plan.get("actions") or [])
        mode_ok = (not expected_mode) or (str(plan.get("mode") or "") == expected_mode)
        risk_ok = (not expected_risk) or (risk_level == expected_risk)
        hit_ok = hit >= max(0, min_expected_hits)
        strict_ok = bool(mode_ok and risk_ok and hit_ok)
        rows.append(
            {
                "name": case.get("name"),
                "mode": plan.get("mode"),
                "risk_level": risk_level,
                "actions": actions,
                "expected_hit": hit,
                "expected_total": len(expected),
                "mode_ok": mode_ok,
                "risk_ok": risk_ok,
                "hit_ok": hit_ok,
                "strict_ok": strict_ok,
            }
        )
    return rows


def _run_autonomy_loop_regression(manager: Any) -> list[dict[str, Any]]:
    conversation_id = "benchmark-conv"
    request_id = "benchmark-req"

    class _NoopMethodManager:
        pass

    class _NoopConversationManager:
        pass

    method_mgr = _NoopMethodManager()
    conv_mgr = _NoopConversationManager()

    def _simulate_case(
        *,
        name: str,
        action: dict[str, Any],
        outputs: list[dict[str, Any]],
        expect_state: str,
        expect_manual_takeover: bool = False,
        cancel_after_first: bool = False,
    ) -> dict[str, Any]:
        seq = list(outputs)

        def _handler(_action: dict[str, Any], _cid: str, _mm: Any, _cm: Any) -> dict[str, Any]:
            if seq:
                return seq.pop(0)
            return outputs[-1]

        original = manager.action_registry.get(action.get("type"))
        manager.action_registry[action.get("type")] = _handler
        manager.max_action_retries = 1
        cancel_state = {"calls": 0}

        def _is_cancelled(_rid: str) -> bool:
            cancel_state["calls"] += 1
            return bool(cancel_after_first and cancel_state["calls"] > 1)

        try:
            if cancel_after_first:
                with patch.object(manager, "is_cancelled", side_effect=_is_cancelled):
                    payload = manager.execute_actions([action], conversation_id, request_id, method_mgr, conv_mgr)
            else:
                payload = manager.execute_actions([action], conversation_id, request_id, method_mgr, conv_mgr)
        finally:
            if original is None:
                manager.action_registry.pop(action.get("type"), None)
            else:
                manager.action_registry[action.get("type")] = original

        report = payload.get("report") or []
        last = report[-1] if report else {}
        got_state = str(last.get("outcome_state") or "")
        got_manual = bool(payload.get("manual_takeover_required")) or bool(last.get("needs_manual_takeover"))
        strict_ok = got_state == expect_state and got_manual is expect_manual_takeover
        return {
            "name": name,
            "attempts": len(report),
            "final_state": got_state,
            "manual_takeover_required": got_manual,
            "expected_state": expect_state,
            "expected_manual_takeover": expect_manual_takeover,
            "strict_ok": strict_ok,
        }

    return [
        _simulate_case(
            name="recoverable_error_then_success",
            action={"type": "browser_click", "target": "#submit", "params": {}, "filters": {}, "risk": "low"},
            outputs=[
                {"success": False, "error_code": "timeout", "retryable": True, "stderr": "timeout"},
                {"success": True, "stdout": "clicked"},
            ],
            expect_state="success",
        ),
        _simulate_case(
            name="browser_open_js_error_then_success",
            action={
                "type": "browser_open",
                "target": "https://example.com",
                "params": {"url": "https://example.com"},
                "filters": {},
                "risk": "low",
            },
            outputs=[
                {"success": False, "error_code": "js_error", "retryable": True, "stderr": "js_error"},
                {"success": True, "stdout": "opened"},
            ],
            expect_state="success",
        ),
        _simulate_case(
            name="desktop_type_uia_busy_then_success",
            action={
                "type": "desktop_type",
                "target": "",
                "params": {"text": "hello"},
                "filters": {},
                "risk": "low",
            },
            outputs=[
                {"success": False, "error_code": "uia_busy", "retryable": True, "stderr": "busy"},
                {"success": True, "stdout": "typed"},
            ],
            expect_state="success",
        ),
        _simulate_case(
            name="verify_failed_then_manual_takeover",
            action={
                "type": "file_write",
                "target": "tmp/runtime-loop-check.txt",
                "params": {"path": "tmp/runtime-loop-check.txt", "content": "x"},
                "filters": {},
                "risk": "low",
            },
            outputs=[
                {"success": True, "stdout": "write_ok"},
                {"success": True, "stdout": "write_ok"},
            ],
            expect_state="verify_failed",
            expect_manual_takeover=True,
        ),
        _simulate_case(
            name="retry_exhausted_uia_busy_then_failed",
            action={"type": "desktop_hotkey", "target": "ctrl+s", "params": {}, "filters": {}, "risk": "low"},
            outputs=[
                {"success": False, "error_code": "uia_busy", "retryable": True, "stderr": "busy"},
                {"success": False, "error_code": "uia_busy", "retryable": True, "stderr": "busy"},
            ],
            # max_action_retries=1：第二次仍失败则末行 outcome_state 为 failed（非 recoverable_error）
            expect_state="failed",
            expect_manual_takeover=False,
        ),
        _simulate_case(
            name="cancelled_inflight",
            action={"type": "browser_wait", "target": "", "params": {}, "filters": {}, "risk": "low"},
            outputs=[{"success": False, "error_code": "timeout", "retryable": True}],
            expect_state="cancelled",
            cancel_after_first=True,
        ),
    ]


def _run_computer_use_regression() -> list[dict[str, Any]]:
    """纯逻辑：坐标与白名单策略（不移动鼠标）。"""
    from automation import computer_use

    rows: list[dict[str, Any]] = []
    m = {"left": 0, "top": 0, "width": 1000, "height": 1000}
    p = computer_use.resolve_screen_point({"x": 0, "y": 0, "coord_space": "normalized_1000"}, metrics=m)
    rows.append(
        {
            "name": "computer_use_norm_origin",
            "strict_ok": p == (0, 0),
        }
    )
    os.environ["ARIA_COMPUTER_USE_ALLOW_REGIONS"] = "[[0,0,50,50]]"
    os.environ["ARIA_COMPUTER_USE"] = "1"
    ok_in, _ = computer_use.ensure_mutation_allowed(10, 10)
    ok_out, _ = computer_use.ensure_mutation_allowed(100, 100)
    rows.append({"name": "computer_use_allow_region_enforced", "strict_ok": ok_in and not ok_out})
    del os.environ["ARIA_COMPUTER_USE_ALLOW_REGIONS"]
    return rows



def main() -> None:
    parser = argparse.ArgumentParser(description="Run ARIA planner regression benchmark.")
    parser.add_argument("--min-match-rate", type=float, default=0.0, help="Fail if match_rate is below this threshold.")
    parser.add_argument("--min-strict-pass-rate", type=float, default=0.0, help="Fail if strict_pass_rate is below this threshold.")
    parser.add_argument(
        "--min-runtime-loop-pass-rate",
        type=float,
        default=0.0,
        help="Fail if runtime_loop_pass_rate is below this threshold.",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from aria_manager import ARIAManager

    cases_path = root / "data" / "benchmarks" / "regression_tasks.json"
    if not cases_path.is_file():
        raise SystemExit(f"missing benchmark file: {cases_path}")

    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    manager = ARIAManager()
    manager.set_api_key("")
    rows = _run_planner_regression(manager, cases)
    runtime_rows = _run_autonomy_loop_regression(manager)
    cu_rows = _run_computer_use_regression()
    cu_ok = sum(1 for r in cu_rows if bool(r.get("strict_ok")))
    cu_total = len(cu_rows)

    hit_cases = sum(1 for r in rows if int(r["expected_hit"]) > 0)
    strict_cases = sum(1 for r in rows if bool(r.get("strict_ok")))
    runtime_strict_cases = sum(1 for r in runtime_rows if bool(r.get("strict_ok")))
    runtime_total = len(runtime_rows)
    total = len(rows)
    runtime_loop_pass_rate = round(runtime_strict_cases / max(1, runtime_total), 4)
    summary: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "orchestration_runtime": "runtime_scheduler",
        "max_parallel_agents": int(getattr(getattr(manager, "agent_scheduler", None), "max_parallel_agents", 1) or 1),
        "total_cases": total,
        "matched_cases": hit_cases,
        "strict_passed_cases": strict_cases,
        "match_rate": round(hit_cases / max(1, total), 4),
        "strict_pass_rate": round(strict_cases / max(1, total), 4),
        "runtime_loop_total_cases": runtime_total,
        "runtime_loop_passed_cases": runtime_strict_cases,
        "runtime_loop_pass_rate": runtime_loop_pass_rate,
        "runtime_loop_ok": runtime_loop_pass_rate >= float(args.min_runtime_loop_pass_rate or 0.0),
        "computer_use_total_cases": cu_total,
        "computer_use_passed_cases": cu_ok,
        "computer_use_pass_rate": round(cu_ok / max(1, cu_total), 4),
        "computer_use_ok": (cu_ok >= cu_total),
        "strict_ok": (
            round(strict_cases / max(1, total), 4) >= float(args.min_strict_pass_rate or 0.0)
            and runtime_loop_pass_rate >= float(args.min_runtime_loop_pass_rate or 0.0)
            and (cu_ok >= cu_total)
        ),
        "rows": rows,
        "runtime_loop_rows": runtime_rows,
        "computer_use_rows": cu_rows,
    }

    out_dir = root / "data" / "benchmarks"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "latest_regression_report.json"
    out_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nSaved report to: {out_file}")
    if float(summary["match_rate"] or 0.0) < float(args.min_match_rate or 0.0):
        raise SystemExit(
            f"benchmark failed: match_rate={summary['match_rate']:.4f} < min_match_rate={float(args.min_match_rate):.4f}"
        )
    if float(summary["strict_pass_rate"] or 0.0) < float(args.min_strict_pass_rate or 0.0):
        raise SystemExit(
            "benchmark failed: strict_pass_rate="
            f"{summary['strict_pass_rate']:.4f} < min_strict_pass_rate={float(args.min_strict_pass_rate):.4f}"
        )
    if float(summary["runtime_loop_pass_rate"] or 0.0) < float(args.min_runtime_loop_pass_rate or 0.0):
        raise SystemExit(
            "benchmark failed: runtime_loop_pass_rate="
            f"{summary['runtime_loop_pass_rate']:.4f} < "
            f"min_runtime_loop_pass_rate={float(args.min_runtime_loop_pass_rate):.4f}"
        )


if __name__ == "__main__":
    main()
