"""Dependency-free execution range shared by CLI, state and the step registry."""

MONITOR_STEP = 20
PIPELINE_STEP_NUMBERS = tuple(range(MONITOR_STEP + 1))


def require_step(number: int) -> int:
    if not isinstance(number, int) or isinstance(number, bool) or number not in PIPELINE_STEP_NUMBERS:
        raise ValueError(f"步骤号必须是 0–{MONITOR_STEP} 的整数")
    return number
