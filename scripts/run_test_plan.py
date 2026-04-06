"""Test plan runner for ARIA.

Reads a test plan YAML from data/test_plans/, executes suites in priority order,
and produces a structured JSON report.

Usage:
    python scripts/run_test_plan.py --plan main_plan.yaml --tier ci
    python scripts/run_test_plan.py --plan main_plan.yaml --tier smoke --fail-fast
    python scripts/run_test_plan.py --plan main_plan.yaml --tier full

Tiers:
    smoke   → only suites with tier=smoke
    ci      → smoke + ci suites
    full    → all suites

Exit codes:
    0   all suites passed
    1   one or more suites failed
    2   plan file not found or invalid
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TIER_ORDER = {"smoke": 0, "ci": 1, "full": 2}
_REPORT_PATH = ROOT / "data" / "benchmarks" / "test_plan_report.json"


# ---------------------------------------------------------------------------
# Plan loading
# ---------------------------------------------------------------------------

def _load_plan(plan_name: str) -> dict[str, Any]:
    plan_dir = ROOT / "data" / "test_plans"
    path = plan_dir / plan_name
    if not path.is_file():
        raise SystemExit(f"[run_test_plan] plan file not found: {path}")
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        return yaml.safe_load(text)
    # Minimal fallback: only works for very simple YAML without anchors/flow
    raise SystemExit(
        "[run_test_plan] PyYAML not installed. Run: pip install pyyaml"
    )


def _filter_suites(suites: list[dict], tier: str) -> list[dict]:
    """Return suites whose tier is within the requested tier level."""
    target_level = _TIER_ORDER.get(tier, 99)
    filtered = [s for s in suites if _TIER_ORDER.get(s.get("tier", "full"), 2) <= target_level]
    return sorted(filtered, key=lambda s: int(s.get("priority", 99)))


# ---------------------------------------------------------------------------
# Suite executors
# ---------------------------------------------------------------------------

def _run_pytest_suite(suite: dict[str, Any]) -> dict[str, Any]:
    """Run a pytest suite. Returns result dict."""
    paths = suite.get("paths") or []
    marks = suite.get("marks") or ""
    timeout = int(suite.get("timeout_seconds") or 120)

    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=short"]
    if marks:
        cmd += ["-m", marks]
    cmd += paths

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(ROOT),
        )
        elapsed = round((time.monotonic() - t0) * 1000)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""

        # Parse pytest summary line: "N passed, M failed, K error"
        passed = failed = errors = skipped = 0
        for line in (stdout + stderr).splitlines():
            line_l = line.lower()
            if " passed" in line_l or " failed" in line_l or " error" in line_l:
                import re
                for m in re.finditer(r"(\d+)\s+(passed|failed|error|skipped)", line_l):
                    n, kind = int(m.group(1)), m.group(2)
                    if kind == "passed":
                        passed += n
                    elif kind in ("failed", "error"):
                        failed += n
                    elif kind == "skipped":
                        skipped += n

        suite_passed = proc.returncode == 0
        return {
            "passed": suite_passed,
            "returncode": proc.returncode,
            "elapsed_ms": elapsed,
            "test_passed": passed,
            "test_failed": failed,
            "test_skipped": skipped,
            "stdout_tail": stdout[-2000:] if len(stdout) > 2000 else stdout,
            "stderr_tail": stderr[-500:] if len(stderr) > 500 else stderr,
        }
    except subprocess.TimeoutExpired:
        return {
            "passed": False,
            "returncode": -1,
            "elapsed_ms": timeout * 1000,
            "error": f"timeout after {timeout}s",
        }
    except Exception as exc:
        return {"passed": False, "returncode": -1, "error": str(exc)}


def _run_script_suite(suite: dict[str, Any]) -> dict[str, Any]:
    """Run an arbitrary Python script suite."""
    script = suite.get("script") or ""
    args = suite.get("args") or []
    timeout = int(suite.get("timeout_seconds") or 300)

    if not script:
        return {"passed": False, "returncode": -1, "error": "missing 'script' field"}

    cmd = [sys.executable, str(ROOT / script)] + [str(a) for a in args]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(ROOT),
        )
        elapsed = round((time.monotonic() - t0) * 1000)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        return {
            "passed": proc.returncode == 0,
            "returncode": proc.returncode,
            "elapsed_ms": elapsed,
            "stdout_tail": stdout[-3000:] if len(stdout) > 3000 else stdout,
            "stderr_tail": stderr[-500:] if len(stderr) > 500 else stderr,
        }
    except subprocess.TimeoutExpired:
        return {
            "passed": False,
            "returncode": -1,
            "elapsed_ms": timeout * 1000,
            "error": f"timeout after {timeout}s",
        }
    except Exception as exc:
        return {"passed": False, "returncode": -1, "error": str(exc)}


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_plan(plan: dict[str, Any], tier: str, fail_fast: bool) -> dict[str, Any]:
    suites = _filter_suites(plan.get("suites") or [], tier)
    if not suites:
        print(f"[run_test_plan] No suites found for tier '{tier}'.")
        return {"passed": True, "suite_results": [], "total_suites": 0}

    suite_results: list[dict[str, Any]] = []
    overall_passed = True
    total_elapsed = 0

    print(f"\n{'='*64}")
    print(f"  Plan : {plan.get('plan_id', '?')}  |  Tier: {tier}  |  Suites: {len(suites)}")
    print(f"{'='*64}\n")

    for suite in suites:
        sid = suite.get("id", "?")
        desc = suite.get("description", "")
        stype = suite.get("type", "pytest")
        suite_fail_fast = bool(suite.get("fail_fast")) or fail_fast

        print(f"  [{sid}] {desc}")
        t0 = time.monotonic()

        if stype == "pytest":
            result = _run_pytest_suite(suite)
        elif stype == "script":
            result = _run_script_suite(suite)
        else:
            result = {"passed": False, "returncode": -1, "error": f"unknown suite type: {stype}"}

        elapsed = result.get("elapsed_ms") or round((time.monotonic() - t0) * 1000)
        total_elapsed += elapsed
        status = "PASS" if result["passed"] else "FAIL"
        color_tag = "" if result["passed"] else " ← FAILED"

        if stype == "pytest":
            p = result.get("test_passed", "?")
            f = result.get("test_failed", "?")
            print(f"    → {status}{color_tag}  ({p} passed, {f} failed)  {elapsed}ms")
        else:
            rc = result.get("returncode", "?")
            print(f"    → {status}{color_tag}  rc={rc}  {elapsed}ms")

        if not result["passed"] and result.get("stdout_tail"):
            # Print last few lines of output on failure for quick diagnosis
            tail = result["stdout_tail"].strip().splitlines()[-15:]
            for line in tail:
                print(f"      | {line}")

        suite_results.append({"suite_id": sid, "tier": suite.get("tier"), **result})

        if not result["passed"]:
            overall_passed = False
            if suite_fail_fast:
                print(f"\n  [fail-fast] stopping after '{sid}' failure.\n")
                break

    print(f"\n{'='*64}")
    print(f"  Result: {'ALL PASSED' if overall_passed else 'FAILED'}  |  Total: {total_elapsed}ms")
    print(f"{'='*64}\n")

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "plan_id": plan.get("plan_id"),
        "tier": tier,
        "passed": overall_passed,
        "total_suites": len(suites),
        "passed_suites": sum(1 for r in suite_results if r.get("passed")),
        "failed_suites": sum(1 for r in suite_results if not r.get("passed")),
        "total_elapsed_ms": total_elapsed,
        "suite_results": suite_results,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Run an ARIA test plan.")
    parser.add_argument("--plan", default="main_plan.yaml", help="Plan YAML filename in data/test_plans/")
    parser.add_argument(
        "--tier",
        default="ci",
        choices=["smoke", "ci", "full"],
        help="Tier to run (smoke < ci < full)",
    )
    parser.add_argument("--fail-fast", action="store_true", help="Stop on first suite failure")
    parser.add_argument("--output", default=str(_REPORT_PATH), help="Output report path")
    args = parser.parse_args()

    plan = _load_plan(args.plan)
    report = run_plan(plan, tier=args.tier, fail_fast=args.fail_fast)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report saved to: {out}")

    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
