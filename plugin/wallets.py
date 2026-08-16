from __future__ import annotations

import re

WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def normalize_wallet(raw: str) -> str:
    value = (raw or "").strip()
    if value and not value.startswith("0x") and not value.startswith("0X"):
        value = "0x" + value
    if not WALLET_RE.match(value):
        raise ValueError("invalid_wallet")
    return "0x" + value[2:]
