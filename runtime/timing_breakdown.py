"""Wall-clock timing breakdown for API responses (LLM vs local actions vs other)."""


def compute_timing_breakdown(
    *,
    elapsed_ms: int,
    llm_ms: int = 0,
    local_action_ms: int = 0,
) -> dict[str, int]:
    e = max(0, int(elapsed_ms or 0))
    l = max(0, int(llm_ms or 0))
    a = max(0, int(local_action_ms or 0))
    o = max(0, e - l - a)
    return {
        "elapsed_ms": e,
        "llm_ms": l,
        "local_action_ms": a,
        "other_ms": o,
    }
