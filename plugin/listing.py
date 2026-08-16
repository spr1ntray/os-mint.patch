from __future__ import annotations

from decimal import Decimal, InvalidOperation


def listing_price_wei(
    *,
    mode: str,
    floor_wei: int,
    mint_cost_wei: int,
    fixed_eth: str,
    profit_percent: int,
    undercut_wei: int = 1,
) -> int:
    key = (mode or "dump").strip().lower()
    if key == "fixed":
        return _eth_to_wei(fixed_eth)
    if key == "percent":
        base = max(0, int(mint_cost_wei))
        pct = max(0, int(profit_percent))
        return base + (base * pct // 100)
    floor = max(0, int(floor_wei))
    cut = max(1, int(undercut_wei))
    if floor <= cut:
        return cut
    return floor - cut


def parse_eth_wei(raw: str, *, allow_zero: bool = False) -> int:
    text = (raw or "").strip()
    if not text:
        raise ValueError("invalid_price")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("invalid_price") from exc
    if value < 0:
        raise ValueError("invalid_price")
    if value == 0:
        if allow_zero:
            return 0
        raise ValueError("invalid_price")
    return int(value * Decimal(10**18))


def _eth_to_wei(raw: str) -> int:
    return parse_eth_wei(raw, allow_zero=False)
