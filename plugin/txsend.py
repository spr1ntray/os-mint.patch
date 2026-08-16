"""Sign and broadcast a prepared EVM transaction through the account proxy."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from eth_account import Account

from plugin.identity import CHAIN_ID
from plugin.rpc import RpcError, get_gas_price, get_native_balance, rpc_call

PREFLIGHT_GAS_UNITS = 250_000


class InsufficientFunds(Exception):
    def __init__(
        self,
        *,
        balance: int,
        need: int,
        value: int = 0,
        gas_cost: int = 0,
    ) -> None:
        self.balance = int(balance)
        self.need = int(need)
        self.value = int(value)
        self.gas_cost = int(gas_cost)
        super().__init__("insufficient_funds")


def _hex_int(value: object) -> int:
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    if not text:
        return 0
    return int(text, 16) if text.startswith("0x") else int(text)


def tx_gas_limit(estimate: int) -> int:
    estimate = max(0, int(estimate))
    return max(estimate * 12 // 10, estimate + 21_000)


def tx_need_wei(value: int, gas: int, gas_price: int) -> int:
    return max(0, int(value)) + max(0, int(gas)) * max(1, int(gas_price))


def preflight_gas_reserve(gas_price: int) -> int:
    return tx_need_wei(0, PREFLIGHT_GAS_UNITS, gas_price)


def wei_to_eth(wei: int) -> str:
    text = format(Decimal(int(wei)) / Decimal(10**18), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def funds_message(err: InsufficientFunds) -> str:
    if err.balance <= 0:
        return "На кошельке нет ETH"
    have = wei_to_eth(err.balance)
    need = wei_to_eth(err.need)
    if err.value <= 0:
        return f"Не хватает ETH на газ: нужно {need}, на кошельке {have}"
    return f"Не хватает ETH на минт и газ: нужно {need}, на кошельке {have}"


def assert_preflight_funds(*, wallet: str, proxy: str, timeout_seconds: int) -> int:
    balance = get_native_balance(
        wallet=wallet,
        proxy=proxy,
        timeout_seconds=timeout_seconds,
    )
    gas_price = get_gas_price(proxy=proxy, timeout_seconds=timeout_seconds)
    need = preflight_gas_reserve(gas_price)
    if balance < need:
        raise InsufficientFunds(balance=balance, need=need, value=0, gas_cost=need)
    return balance


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
    value_wei = _hex_int(value)
    value_hex = value if str(value).startswith("0x") else _to_hex(value_wei)
    balance = get_native_balance(
        wallet=account.address,
        proxy=proxy,
        timeout_seconds=timeout_seconds,
    )
    if balance < value_wei:
        raise InsufficientFunds(
            balance=balance,
            need=value_wei,
            value=value_wei,
            gas_cost=0,
        )
    nonce = _hex_int(
        rpc_call(
            method="eth_getTransactionCount",
            params=[account.address, "pending"],
            proxy=proxy,
            timeout_seconds=timeout_seconds,
        )
    )
    gas_price = get_gas_price(proxy=proxy, timeout_seconds=timeout_seconds)
    estimate = _hex_int(
        rpc_call(
            method="eth_estimateGas",
            params=[
                {
                    "from": account.address,
                    "to": to,
                    "data": data,
                    "value": value_hex,
                }
            ],
            proxy=proxy,
            timeout_seconds=timeout_seconds,
        )
    )
    gas = tx_gas_limit(estimate)
    gas_cost = gas * gas_price
    need = tx_need_wei(value_wei, gas, gas_price)
    if balance < need:
        raise InsufficientFunds(
            balance=balance,
            need=need,
            value=value_wei,
            gas_cost=gas_cost,
        )
    signed = account.sign_transaction(
        {
            "chainId": CHAIN_ID,
            "nonce": nonce,
            "to": to,
            "data": data,
            "value": value_wei,
            "gas": gas,
            "gasPrice": gas_price,
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
