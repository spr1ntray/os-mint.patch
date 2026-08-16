from __future__ import annotations

from typing import Any

import httpx

from plugin.identity import CHAIN_ID, RPC_URL
from plugin.proxy import proxy_to_url


class RpcError(Exception):
    def __init__(self, code: str = "rpc_error") -> None:
        self.code = code
        super().__init__(code)


def rpc_call(
    *,
    method: str,
    params: list[Any],
    proxy: str,
    timeout_seconds: int,
    rpc_url: str = RPC_URL,
) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        with httpx.Client(
            proxy=proxy_to_url(proxy),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
        ) as client:
            response = client.post(rpc_url, json=payload)
            data = response.json()
    except httpx.TimeoutException:
        raise RpcError("timeout") from None
    except httpx.ProxyError:
        raise RpcError("proxy_error") from None
    except httpx.HTTPError:
        raise RpcError("rpc_error") from None
    except Exception:
        raise RpcError("rpc_error") from None
    if not isinstance(data, dict):
        raise RpcError("rpc_error")
    if data.get("error"):
        raise RpcError("rpc_error")
    return data.get("result")


def assert_chain(*, proxy: str, timeout_seconds: int) -> None:
    raw = rpc_call(
        method="eth_chainId",
        params=[],
        proxy=proxy,
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(raw, str):
        raise RpcError("wrong_chain")
    value = int(raw, 16)
    if value != CHAIN_ID:
        raise RpcError("wrong_chain")
