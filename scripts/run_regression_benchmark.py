import json
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from aria_manager import ARIAManager

    cases_path = root / "benchmarks" / "regression_tasks.json"
    if not cases_path.is_file():
        raise SystemExit(f"missing benchmark file: {cases_path}")

    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    manager = ARIAManager()
    manager.set_api_key("")
    rows = []

    for case in cases:
        query = str(case.get("query") or "").strip()
        expected = set(case.get("expected_keywords") or [])
        plan = manager.plan_actions(query, "")
        actions = [str(a.get("type") or "") for a in (plan.get("actions") or [])]
        hit = len(expected.intersection(set(actions)))
        rows.append(
            {
                "name": case.get("name"),
                "mode": plan.get("mode"),
                "risk_level": manager.evaluate_action_risk_level(plan.get("actions") or []),
                "actions": actions,
                "expected_hit": hit,
                "expected_total": len(expected),
            }
        )

    hit_cases = sum(1 for r in rows if int(r["expected_hit"]) > 0)
    summary = {
        "total_cases": len(rows),
        "matched_cases": hit_cases,
        "match_rate": round(hit_cases / max(1, len(rows)), 4),
        "rows": rows,
    }

    out_dir = root / "data" / "benchmarks"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "latest_regression_report.json"
    out_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nSaved report to: {out_file}")


if __name__ == "__main__":
    main()
