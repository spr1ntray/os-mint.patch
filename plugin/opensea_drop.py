"""Official OpenSea Drops API: wait for WL, build mint calldata.

Mint goes on-chain from our key. OpenSea UI is never clicked.
Their API only supplies the ready transaction (signature lives inside calldata).
"""

from __future__ import annotations

import threading
from typing import Any

import httpx

from plugin.identity import OPENSEA_CHAIN, SEADROP, slug_search_list
from plugin.proxy import proxy_to_url
from plugin.stage import STAGE_ALLOWLIST, STAGE_ENDED, STAGE_PUBLIC, STAGE_WAIT

_NATIVE = {"", "0x0000000000000000000000000000000000000000"}

OPENSEA_API = "https://api.opensea.io"
MINT_PUBLIC_SELECTOR = "161ac21f"
_PUBLIC_TYPES = {"public_sale", "public", "publicsale"}

_key_lock = threading.Lock()
_cached_key: str | None = None


class DropRejected(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/142.0.0.0 Safari/537.36"
        ),
        "x-api-key": api_key,
    }


def _client(*, proxy: str, timeout_seconds: int) -> httpx.Client:
    return httpx.Client(
        proxy=proxy_to_url(proxy),
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=True,
    )


def _field(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return None


def instant_api_key(*, proxy: str, timeout_seconds: int) -> str:
    global _cached_key
    with _key_lock:
        if _cached_key:
            return _cached_key
        try:
            with _client(proxy=proxy, timeout_seconds=timeout_seconds) as client:
                response = client.post(f"{OPENSEA_API}/api/v2/auth/keys")
                payload = response.json()
        except Exception as exc:
            raise DropRejected("api_key") from exc
        if not isinstance(payload, dict):
            raise DropRejected("api_key")
        key = str(_field(payload, "api_key", "apiKey", "key") or "").strip()
        if not key:
            raise DropRejected("api_key")
        _cached_key = key
        return key


def get_drop(*, slug: str, proxy: str, timeout_seconds: int) -> dict[str, Any] | None:
    key = instant_api_key(proxy=proxy, timeout_seconds=timeout_seconds)
    try:
        with _client(proxy=proxy, timeout_seconds=timeout_seconds) as client:
            response = client.get(
                f"{OPENSEA_API}/api/v2/drops/{slug}",
                headers=_headers(key),
            )
    except httpx.HTTPError as exc:
        raise DropRejected("drop_request") from exc
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise DropRejected("drop_request")
    data = response.json()
    return data if isinstance(data, dict) else None


def _chain_ok(drop: dict[str, Any]) -> bool:
    chain = str(_field(drop, "chain") or "").strip().lower()
    return not chain or chain in {OPENSEA_CHAIN, "robinhood_chain", "hood"}


def find_drop(*, proxy: str, timeout_seconds: int, slugs: tuple[str, ...] = ()) -> dict[str, Any] | None:
    for slug in slug_search_list(*slugs):
        try:
            found = get_drop(slug=slug, proxy=proxy, timeout_seconds=timeout_seconds)
        except DropRejected:
            continue
        if found is not None and _chain_ok(found):
            return found
    return None


def active_mint_price_wei(drop: dict[str, Any] | None) -> int | None:
    if not drop:
        return None
    stage = _field(drop, "active_stage", "activeStage")
    if not isinstance(stage, dict):
        return None
    currency = str(_field(stage, "price_currency_address", "priceCurrencyAddress") or "").strip().lower()
    if currency not in _NATIVE:
        return None
    raw = str(_field(stage, "price") or "").strip()
    if not raw:
        return 0
    try:
        return int(raw, 0) if raw.startswith("0x") else int(raw)
    except ValueError:
        return None


def price_within_cap(price_wei: int | None, max_mint_wei: int) -> bool:
    if price_wei is None:
        return False
    return int(price_wei) <= max(0, int(max_mint_wei))


def collection_gate(drop: dict[str, Any] | None, max_mint_wei: int) -> str:
    """wait | mintable | public | ended"""
    if not drop:
        return "wait"
    stage = classify_drop(drop)
    if stage == STAGE_PUBLIC:
        return "public"
    if stage == STAGE_ENDED:
        return "ended"
    if stage != STAGE_ALLOWLIST:
        return "wait"
    if not price_within_cap(active_mint_price_wei(drop), max_mint_wei):
        return "wait"
    return "mintable"


def inspect_slugs(
    *,
    proxy: str,
    timeout_seconds: int,
    max_mint_wei: int,
    slugs: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for slug in slug_search_list(*slugs):
        try:
            drop = get_drop(slug=slug, proxy=proxy, timeout_seconds=timeout_seconds)
        except DropRejected:
            rows.append({"slug": slug, "state": "wait", "contract": "", "drop": None})
            continue
        state = collection_gate(drop, max_mint_wei)
        contract = ""
        if drop:
            contract = str(_field(drop, "contract_address", "contractAddress") or "").strip().lower()
        rows.append({"slug": slug, "state": state, "contract": contract, "drop": drop})
    return rows


def collect_mint_targets(
    *,
    proxy: str,
    timeout_seconds: int,
    max_mint_wei: int,
    slugs: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for slug in slug_search_list(*slugs):
        try:
            drop = get_drop(slug=slug, proxy=proxy, timeout_seconds=timeout_seconds)
        except DropRejected:
            continue
        if not drop or not _chain_ok(drop):
            continue
        if classify_drop(drop) != STAGE_ALLOWLIST:
            continue
        price = active_mint_price_wei(drop)
        if not price_within_cap(price, max_mint_wei):
            continue
        contract = str(_field(drop, "contract_address", "contractAddress") or "").strip().lower()
        name = str(_field(drop, "collection_slug", "collectionSlug") or slug).strip()
        if not contract:
            continue
        targets.append(
            {
                "stage": STAGE_ALLOWLIST,
                "ready": True,
                "reason": STAGE_ALLOWLIST,
                "contract": contract,
                "slug": name,
                "price_wei": int(price or 0),
                "chain": str(_field(drop, "chain") or ""),
            }
        )
    return targets


def _stage_kind(stage: dict[str, Any]) -> str:
    raw = str(_field(stage, "stage_type", "stageType") or "").strip().lower()
    label = str(_field(stage, "label") or "").strip().lower()
    if raw in _PUBLIC_TYPES or raw.startswith("public") or "public" in label:
        return "public"
    return "allowlist"


def active_stage_watch_message(drop: dict[str, Any] | None) -> str:
    if not drop:
        return ""
    active = _field(drop, "active_stage", "activeStage")
    if not isinstance(active, dict):
        return ""
    kind = _stage_kind(active)
    label = str(_field(active, "label") or "").strip() or "текущая стадия"
    if kind == "public":
        return f"Сейчас «{label}». Public не минтим"
    return ""


def classify_drop(drop: dict[str, Any] | None) -> str:
    if not drop:
        return STAGE_WAIT
    active = _field(drop, "active_stage", "activeStage")
    if isinstance(active, dict):
        kind = _stage_kind(active)
        if kind == "public":
            return STAGE_PUBLIC
        if kind == "allowlist":
            return STAGE_ALLOWLIST
        return STAGE_WAIT
    if drop.get("is_minting") is True:
        return STAGE_WAIT
    stages = _field(drop, "stages") or []
    if isinstance(stages, list) and stages:
        kinds = [_stage_kind(item) for item in stages if isinstance(item, dict)]
        if kinds and all(kind == "public" for kind in kinds):
            return STAGE_ENDED
    return STAGE_WAIT


def read_drop_snapshot(*, proxy: str, timeout_seconds: int) -> dict[str, Any]:
    drop = find_drop(proxy=proxy, timeout_seconds=timeout_seconds)
    if drop is None:
        return {
            "stage": STAGE_WAIT,
            "ready": False,
            "reason": "unpublished",
            "contract": "",
            "slug": "",
        }
    stage = classify_drop(drop)
    contract = str(_field(drop, "contract_address", "contractAddress") or "").strip().lower()
    slug = str(_field(drop, "collection_slug", "collectionSlug") or "").strip()
    return {
        "stage": stage,
        "ready": stage == STAGE_ALLOWLIST,
        "reason": stage,
        "contract": contract,
        "slug": slug,
        "chain": str(_field(drop, "chain") or ""),
    }


def build_mint_tx(
    *,
    slug: str,
    wallet: str,
    proxy: str,
    timeout_seconds: int,
    quantity: int = 1,
) -> dict[str, str]:
    key = instant_api_key(proxy=proxy, timeout_seconds=timeout_seconds)
    try:
        with _client(proxy=proxy, timeout_seconds=timeout_seconds) as client:
            response = client.post(
                f"{OPENSEA_API}/api/v2/drops/{slug}/mint",
                headers=_headers(key),
                json={"minter": wallet, "quantity": int(quantity)},
            )
    except httpx.HTTPError as exc:
        raise DropRejected("mint_request") from exc
    if response.status_code == 409:
        raise DropRejected("drop_inactive")
    if response.status_code == 422:
        raise DropRejected("not_eligible")
    if response.status_code >= 400:
        raise DropRejected("mint_request")
    data = response.json()
    if not isinstance(data, dict):
        raise DropRejected("mint_request")
    to = str(_field(data, "to", "target") or "").strip()
    raw = str(_field(data, "data", "calldata") or "").strip()
    value = str(_field(data, "value") or "0x0").strip()
    if not to or not raw:
        raise DropRejected("mint_request")
    return {"to": to, "data": raw, "value": value, "chain": str(_field(data, "chain") or "")}


def assert_safe_mint_tx(tx: dict[str, str], *, contract: str) -> None:
    target = (tx.get("to") or "").strip().lower()
    data = (tx.get("data") or "").strip().lower()
    if not data.startswith("0x") or len(data) < 10:
        raise DropRejected("bad_calldata")
    allowed = {SEADROP.lower()}
    locked = (contract or "").strip().lower()
    if locked:
        allowed.add(locked)
    if target not in allowed:
        raise DropRejected("bad_target")
    selector = data[2:10]
    if selector == MINT_PUBLIC_SELECTOR:
        raise DropRejected("public_calldata")
    chain = str(tx.get("chain") or "").strip().lower()
    if chain and chain not in {OPENSEA_CHAIN, "robinhood_chain", "hood", ""}:
        raise DropRejected("wrong_chain")


def tx_value_wei(tx: dict[str, str]) -> int:
    raw = str(tx.get("value") or "0").strip()
    if not raw:
        return 0
    return int(raw, 16) if raw.startswith("0x") else int(raw)
