"""Shared OpenSea / Robinhood mint identity. Slugs come from the run form."""

from __future__ import annotations

import re

CHAIN_ID = 4663
RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
SEADROP = "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5"
OPENSEA_CHAIN = "robinhood"
COLLECTION_SLUG: str | None = None
CONTRACT: str | None = None

_SPLIT = re.compile(r"[\s,;]+")


def is_configured() -> bool:
    return False


def locked_contract() -> str | None:
    return None


def locked_slug() -> str | None:
    return None


def normalize_collection_slug(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    lower = text.lower()
    marker = "/collection/"
    if marker in lower:
        text = text[lower.index(marker) + len(marker) :]
    text = text.split("?", 1)[0].split("#", 1)[0].split("/", 1)[0].strip().lower()
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-_")
    if not text or any(char not in allowed for char in text):
        return ""
    return text


def parse_slug_blob(raw: str) -> tuple[str, ...]:
    found: list[str] = []
    for part in _SPLIT.split(raw or ""):
        slug = normalize_collection_slug(part)
        if slug and slug not in found:
            found.append(slug)
    return tuple(found)


def slug_search_list(*extra: str) -> tuple[str, ...]:
    found: list[str] = []
    for item in extra:
        for slug in parse_slug_blob(item):
            if slug not in found:
                found.append(slug)
    return tuple(found)
