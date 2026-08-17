from __future__ import annotations

import time
from typing import Any

from soft_hub.sdk import CancelledError, HubAccount, HubContext

from plugin.identity import parse_slug_blob
from plugin.listing import parse_eth_wei
from plugin.opensea_drop import (
    DropRejected,
    active_stage_id,
    active_stage_watch_message,
    assert_safe_mint_tx,
    build_mint_tx,
    inspect_slugs,
    tx_value_wei,
    watch_poll_seconds,
)
from plugin.proxy import proxy_to_url
from plugin.rpc import RpcError
from plugin.txsend import (
    InsufficientFunds,
    assert_preflight_funds,
    funds_message,
    send_prepared_tx,
    token_id_from_receipt,
    wait_receipt,
)
from plugin.wallets import normalize_wallet

MINT_KIND = "account_mint"
_MINT_KEYS = frozenset({"outcome", "stage", "minted", "token_id", "tx_hash"})


def run(context: HubContext) -> dict[str, Any]:
    if context.action_id == "mint":
        return _run_mint(context)
    raise ValueError("unsupported_action")


def _run_mint(context: HubContext) -> dict[str, Any]:
    timeout_seconds = _int_option(context.options, "timeout_seconds", 30, 5, 120)
    slugs = parse_slug_blob(str(context.options.get("collection_slugs") or ""))
    poll_seconds = watch_poll_seconds(
        _int_option(context.options, "poll_interval_seconds", 8, 2, 60),
        len(slugs),
    )
    watch_minutes = _int_option(context.options, "watch_minutes", 2880, 5, 10080)
    try:
        max_mint_wei = parse_eth_wei(
            str(context.options.get("max_mint_eth") or "0"),
            allow_zero=True,
        )
    except ValueError:
        max_mint_wei = -1

    counters = {
        "total": len(context.accounts),
        "succeeded": 0,
        "failed": 0,
        "blocked": 0,
        "cancelled": 0,
    }
    ready: list[HubAccount] = []
    context.log(
        "Старт автоминта по slug",
        data={
            "accounts": len(context.accounts),
            "watch_minutes": watch_minutes,
            "poll_interval_seconds": poll_seconds,
            "max_mint_wei": max_mint_wei,
            "slugs": list(slugs),
        },
    )
    if not slugs or max_mint_wei < 0:
        for account in context.accounts:
            _finish_mint(
                context,
                account,
                status="blocked",
                stage="preflight",
                message="Нет ни одного slug или потолок цены кривой",
                result_status="blocked",
                data=_empty_mint("invalid_price", "preflight"),
            )
        return {
            "total": len(context.accounts),
            "succeeded": 0,
            "failed": 0,
            "blocked": len(context.accounts),
            "cancelled": 0,
        }

    for account in context.accounts:
        context.check_cancelled()
        context.account_state(
            account.id,
            status="running",
            stage="preflight",
            progress=0.05,
            message="Проверяем кошелёк, proxy и баланс",
        )
        try:
            wallet = normalize_wallet(account.evm_address)
            proxy = account.secret("proxy")
            proxy_to_url(proxy)
            account.secret("evm_private_key")
            assert_preflight_funds(
                wallet=wallet,
                proxy=proxy,
                timeout_seconds=timeout_seconds,
            )
        except (KeyError, ValueError):
            _finish_mint(
                context,
                account,
                status="blocked",
                stage="preflight",
                message="Нет приватника или proxy",
                result_status="blocked",
                data=_empty_mint("blocked", "preflight"),
            )
            counters["blocked"] += 1
            continue
        except InsufficientFunds as err:
            _finish_mint(
                context,
                account,
                status="blocked",
                stage="preflight",
                message=funds_message(err),
                result_status="blocked",
                data=_empty_mint("insufficient_funds", "preflight"),
            )
            counters["blocked"] += 1
            continue
        except RpcError:
            _finish_mint(
                context,
                account,
                status="blocked",
                stage="preflight",
                message="Не удалось проверить баланс",
                result_status="blocked",
                data=_empty_mint("balance_check", "preflight"),
            )
            counters["blocked"] += 1
            continue
        ready.append(account)

    if not ready:
        return counters

    def _watching(*, extra: str = "") -> None:
        shown = ", ".join(slugs[:4])
        if len(slugs) > 4:
            shown += f" и ещё {len(slugs) - 4}"
        cap = str(context.options.get("max_mint_eth") or "0")
        if max_mint_wei == 0:
            message = f"Ждём бесплатный WL: {shown}"
        else:
            message = f"Ждём WL до {cap} ETH: {shown}"
        if extra:
            message = extra
        for account in ready:
            context.account_state(
                account.id,
                status="running",
                stage="watch",
                progress=0.18,
                message=message,
            )

    proxies = [account.secret("proxy") for account in ready]
    deadline = time.monotonic() + watch_minutes * 60
    closed_slugs: set[str] = set()
    best: dict[str, str] = {}
    skip_stage: dict[tuple[str, str], str] = {}
    poll_tick = 0
    _watching()
    try:
        while True:
            context.check_cancelled()
            if all(best.get(account.id) == "succeeded" for account in ready):
                break
            rows = inspect_slugs(
                proxy=proxies[poll_tick % len(proxies)],
                timeout_seconds=timeout_seconds,
                max_mint_wei=max_mint_wei,
                slugs=slugs,
            )
            poll_tick += 1
            for row in rows:
                slug = str(row["slug"])
                if slug in closed_slugs:
                    continue
                state = str(row["state"])
                if state in {"public", "ended"}:
                    closed_slugs.add(slug)
                    continue
                if state != "mintable":
                    continue
                stage_key = active_stage_id(row.get("drop") if isinstance(row.get("drop"), dict) else None)
                pending = [
                    account
                    for account in ready
                    if best.get(account.id) != "succeeded"
                    and (not stage_key or skip_stage.get((account.id, slug)) != stage_key)
                ]
                if not pending:
                    continue
                _watching(extra=f"Минтим {slug}. Одна NFT, public не трогаем")
                outcomes = context.map_accounts(
                    lambda account, row=row: _mint_one(
                        context,
                        account,
                        slug=str(row["slug"]),
                        contract=str(row["contract"]),
                        timeout_seconds=timeout_seconds,
                        max_mint_wei=max_mint_wei,
                    ),
                    accounts=tuple(pending),
                )
                for account, status in zip(pending, outcomes, strict=True):
                    if status == "waiting":
                        if stage_key:
                            skip_stage[(account.id, slug)] = stage_key
                        continue
                    if status == "sold_out":
                        closed_slugs.add(slug)
                        continue
                    if best.get(account.id) != "succeeded":
                        best[account.id] = status
            if all(best.get(account.id) == "succeeded" for account in ready):
                break
            if closed_slugs.issuperset(slugs):
                for account in ready:
                    if best.get(account.id) == "succeeded":
                        continue
                    _finish_mint(
                        context,
                        account,
                        status="failed",
                        stage="completed",
                        message="Клеймить было нечего",
                        result_status="failed",
                        data=_empty_mint("nothing_to_claim", "public"),
                    )
                    best[account.id] = "failed"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            notes = [
                active_stage_watch_message(row.get("drop"))
                for row in rows
                if row.get("drop")
            ]
            _watching(extra=next((note for note in notes if note), ""))
            _interruptible_sleep(context, min(float(poll_seconds), remaining))
    except CancelledError:
        for account in ready:
            if account.id in best:
                continue
            context.account_state(
                account.id,
                status="cancelled",
                stage="cancelled",
                message="Ожидание минта остановлено",
            )
            counters["cancelled"] += 1
        for status in best.values():
            counters[status] = counters.get(status, 0) + 1
        return counters

    for account in ready:
        status = best.get(account.id)
        if status:
            counters[status] = counters.get(status, 0) + 1
            continue
        _finish_mint(
            context,
            account,
            status="failed",
            stage="failed",
            message="Подходящий WL по цене не открылся",
            result_status="failed",
            data=_empty_mint("timeout", "wait"),
        )
        counters["failed"] += 1
    return counters


def _mint_one(
    context: HubContext,
    account: HubAccount,
    *,
    slug: str,
    contract: str,
    timeout_seconds: int,
    max_mint_wei: int,
) -> str:
    context.check_cancelled()
    wallet = normalize_wallet(account.evm_address)
    proxy = account.secret("proxy")
    context.account_state(
        account.id,
        status="running",
        stage="prepare",
        progress=0.40,
        message="Берём calldata WL-минта. Сайт OpenSea не открываем",
    )
    try:
        try:
            prepared = build_mint_tx(
                slug=slug,
                wallet=wallet,
                proxy=proxy,
                timeout_seconds=timeout_seconds,
            )
            assert_safe_mint_tx(prepared, contract=contract)
            if tx_value_wei(prepared) > max_mint_wei:
                raise DropRejected("mint_too_expensive")
        except DropRejected as err:
            code = str(err)
            if code in {"not_eligible", "drop_inactive", "rate_limited"}:
                context.account_state(
                    account.id,
                    status="running",
                    stage="watch",
                    progress=0.18,
                    message=(
                        "OpenSea просит подождать"
                        if code == "rate_limited"
                        else "Эта стадия не для нас. Ждём свой WL"
                    ),
                )
                return "waiting"
            if code == "sold_out":
                return "sold_out"
            _finish_mint(
                context,
                account,
                status="failed",
                stage="failed",
                message=_reject_message(code),
                result_status="failed",
                data=_empty_mint(code, "allowlist"),
            )
            return "failed"

        context.check_cancelled()
        context.account_state(
            account.id,
            status="running",
            stage="mint",
            progress=0.70,
            message="Подписываем и шлём минт в сеть",
        )
        tx_hash = send_prepared_tx(
            private_key=account.secret("evm_private_key"),
            to=prepared["to"],
            data=prepared["data"],
            value=prepared["value"],
            proxy=proxy,
            timeout_seconds=timeout_seconds,
        )
        receipt = wait_receipt(
            tx_hash=tx_hash,
            proxy=proxy,
            timeout_seconds=timeout_seconds,
        )
        if str(receipt.get("status") or "").lower() in {"0x0", "0"}:
            _finish_mint(
                context,
                account,
                status="failed",
                stage="failed",
                message="Минт ревертнулся в сети",
                result_status="failed",
                data={**_empty_mint("reverted", "mint"), "tx_hash": tx_hash},
            )
            return "failed"
        token_id = token_id_from_receipt(receipt)
        _finish_mint(
            context,
            account,
            status="succeeded",
            stage="completed",
            message="Сминтили ончейн",
            result_status="succeeded",
            data={
                "outcome": "minted",
                "stage": "mint",
                "minted": True,
                "token_id": token_id,
                "tx_hash": tx_hash,
            },
            progress=1.0,
        )
        return "succeeded"
    except InsufficientFunds as err:
        _finish_mint(
            context,
            account,
            status="failed",
            stage="failed",
            message=funds_message(err),
            result_status="failed",
            data=_empty_mint("insufficient_funds", "mint"),
        )
        return "failed"
    except RpcError:
        _finish_mint(
            context,
            account,
            status="failed",
            stage="failed",
            message="Сеть не приняла транзакцию",
            result_status="failed",
            data=_empty_mint("rpc_error", "mint"),
        )
        return "failed"
    except CancelledError:
        context.account_state(
            account.id,
            status="cancelled",
            stage="cancelled",
            message="Минт остановлен. Проверьте explorer перед повтором",
        )
        return "cancelled"
    except Exception:
        _finish_mint(
            context,
            account,
            status="failed",
            stage="failed",
            message="Минт не выполнен. Перед повтором проверьте explorer",
            result_status="failed",
            data=_empty_mint("failed", "failed"),
        )
        return "failed"


def _empty_mint(outcome: str, stage: str) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "stage": stage,
        "minted": False,
        "token_id": "",
        "tx_hash": "",
    }


def _finish_mint(
    context: HubContext,
    account: HubAccount,
    *,
    status: str,
    stage: str,
    message: str,
    result_status: str,
    data: dict[str, Any],
    progress: float | None = None,
) -> None:
    _finish(
        context,
        account,
        status=status,
        stage=stage,
        message=message,
        result_status=result_status,
        data=data,
        progress=progress,
    )


def _reject_message(code: str) -> str:
    mapping = {
        "timeout": "Истёк таймаут",
        "proxy_error": "Proxy не отвечает",
        "request_failed": "Сеть не ответила",
        "public_stage": "Открыт public. WL-минт не отправляем",
        "stage_ended": "Окно минта уже закрыто",
        "api_key": "OpenSea не выдала ключ API",
        "drop_request": "Не удалось прочитать drop на OpenSea",
        "mint_request": "OpenSea не собрала транзакцию минта",
        "drop_inactive": "Drop ещё не активен",
        "sold_out": "Коллекция уже распродана",
        "rate_limited": "OpenSea временно режет запросы",
        "not_eligible": "Кошелёк не в allowlist этой стадии",
        "bad_target": "OpenSea вернула чужой контракт — не шлём",
        "bad_calldata": "OpenSea вернула пустой calldata",
        "public_calldata": "Это public-минт. Не отправляем",
        "wrong_chain": "Это не Robinhood Chain",
        "no_slug": "Нет slug коллекции",
        "reverted": "Минт ревертнулся в сети",
        "rpc_error": "RPC не принял транзакцию",
        "mint_too_expensive": "Цена WL выше потолка",
        "insufficient_funds": "Не хватает ETH на минт или газ",
        "balance_check": "Не удалось проверить баланс",
        "invalid_price": "Некорректный потолок цены минта",
        "nothing_to_claim": "Клеймить было нечего",
    }
    return mapping.get(code, "Минт не принят")


def _finish(
    context: HubContext,
    account: HubAccount,
    *,
    status: str,
    stage: str,
    message: str,
    result_status: str,
    data: dict[str, Any],
    progress: float | None = None,
) -> None:
    safe_data = {key: data[key] for key in _MINT_KEYS if key in data}
    context.result(
        f"{account.label}: {message}",
        kind=MINT_KIND,
        status=result_status,
        account_id=account.id,
        data=safe_data,
    )
    kwargs: dict[str, Any] = {"status": status, "stage": stage, "message": message}
    if progress is not None:
        kwargs["progress"] = progress
    context.account_state(account.id, **kwargs)


def _int_option(options: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = options.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid_{name}")
    if not low <= value <= high:
        raise ValueError(f"invalid_{name}")
    return value


def _interruptible_sleep(context: HubContext, seconds: float) -> None:
    if seconds <= 0:
        return
    end = time.monotonic() + seconds
    while True:
        context.check_cancelled()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))
