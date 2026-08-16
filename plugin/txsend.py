"""Sign and broadcast a prepared EVM transaction through the account proxy."""

from __future__ import annotations

from typing import Any

from eth_account import Account

from plugin.identity import CHAIN_ID
from plugin.rpc import RpcError, rpc_call


def _hex_int(value: object) -> int:
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    if not text:
        return 0
    return int(text, 16) if text.startswith("0x") else int(text)


def _to_hex(value: int) -> str:
    return hex(int(value))


def send_prepared_tx(
    *,
    private_key: str,
    to: str,
    data: str,
    value: str,
    proxy: str,
    timeout_seconds: int,
) -> str:
    account = Account.from_key(private_key)
    nonce = _hex_int(
        rpc_call(
            method="eth_getTransactionCount",
            params=[account.address, "pending"],
            proxy=proxy,
            timeout_seconds=timeout_seconds,
        )
    )
    gas_price = _hex_int(
        rpc_call(
            method="eth_gasPrice",
            params=[],
            proxy=proxy,
            timeout_seconds=timeout_seconds,
        )
    )
    estimate = _hex_int(
        rpc_call(
            method="eth_estimateGas",
            params=[
                {
                    "from": account.address,
                    "to": to,
                    "data": data,
                    "value": value if str(value).startswith("0x") else _to_hex(_hex_int(value)),
                }
            ],
            proxy=proxy,
            timeout_seconds=timeout_seconds,
        )
    )
    signed = account.sign_transaction(
        {
            "chainId": CHAIN_ID,
            "nonce": nonce,
            "to": to,
            "data": data,
            "value": _hex_int(value),
            "gas": max(estimate * 12 // 10, estimate + 21_000),
            "gasPrice": gas_price or 1,
        }
    )
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    raw_hex = raw.hex() if hasattr(raw, "hex") else str(raw)
    if not raw_hex.startswith("0x"):
        raw_hex = "0x" + raw_hex
    tx_hash = rpc_call(
        method="eth_sendRawTransaction",
        params=[raw_hex],
        proxy=proxy,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(tx_hash, str) or not tx_hash.startswith("0x"):
        raise RpcError("send_failed")
    return tx_hash


def wait_receipt(
    *,
    tx_hash: str,
    proxy: str,
    timeout_seconds: int,
    attempts: int = 20,
) -> dict[str, Any]:
    import time

    last: dict[str, Any] | None = None
    for _ in range(max(1, attempts)):
        raw = rpc_call(
            method="eth_getTransactionReceipt",
            params=[tx_hash],
            proxy=proxy,
            timeout_seconds=timeout_seconds,
        )
        if isinstance(raw, dict) and raw.get("blockNumber"):
            return raw
        last = raw if isinstance(raw, dict) else None
        time.sleep(1.5)
    if last is None:
        raise RpcError("no_receipt")
    return last


def token_id_from_receipt(receipt: dict[str, Any]) -> str:
    transfer = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    logs = receipt.get("logs")
    if not isinstance(logs, list):
        return ""
    for item in logs:
        if not isinstance(item, dict):
            continue
        topics = item.get("topics")
        if not isinstance(topics, list) or len(topics) < 4:
            continue
        if str(topics[0]).lower() != transfer:
            continue
        return str(int(str(topics[3]), 16))
    return ""
