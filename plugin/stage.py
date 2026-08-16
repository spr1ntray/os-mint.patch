from __future__ import annotations

STAGE_WAIT = "wait"
STAGE_ALLOWLIST = "allowlist"
STAGE_PUBLIC = "public"
STAGE_ENDED = "ended"


def classify_stage(
    *,
    now: int,
    allow_start: int,
    allow_end: int,
    public_start: int,
    public_end: int,
) -> str:
    """WL window wins if it overlaps public. Public is never treated as mintable."""
    if _inside(now, allow_start, allow_end):
        return STAGE_ALLOWLIST
    if _inside(now, public_start, public_end):
        return STAGE_PUBLIC
    if _has_window(allow_start, allow_end) and now < allow_start:
        return STAGE_WAIT
    if _has_window(public_start, public_end) and now < public_start:
        return STAGE_WAIT
    if _has_window(allow_start, allow_end) or _has_window(public_start, public_end):
        return STAGE_ENDED
    return STAGE_WAIT


def _has_window(start: int, end: int) -> bool:
    return start > 0 and end > start


def _inside(now: int, start: int, end: int) -> bool:
    return _has_window(start, end) and start <= now < end
