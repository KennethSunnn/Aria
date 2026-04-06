from runtime.timing_breakdown import compute_timing_breakdown


def test_compute_timing_breakdown_basic():
    d = compute_timing_breakdown(elapsed_ms=100, llm_ms=60, local_action_ms=30)
    assert d == {"elapsed_ms": 100, "llm_ms": 60, "local_action_ms": 30, "other_ms": 10}


def test_compute_timing_breakdown_other_non_negative():
    d = compute_timing_breakdown(elapsed_ms=50, llm_ms=80, local_action_ms=40)
    assert d["other_ms"] == 0
    assert d["elapsed_ms"] == 50
