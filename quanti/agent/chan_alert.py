"""Cloud-friendly Bark alerts for confirmed Chan.py 30-minute BSP events.

The scanner is deliberately independent from the web server and local SQLite
database so a GitHub Actions runner can execute it while the user's Mac is off.
Only upstream Chan.py events that are both confirmed and still active are sent.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
STATE_VERSION = 1
MAX_SEEN_PER_STOCK = 5000


@dataclass(frozen=True)
class StockTarget:
    code: str
    name: str

    @property
    def symbol(self) -> str:
        if self.code.startswith(("4", "8")):
            exchange = "BJ"
        elif self.code.startswith(("6", "68")):
            exchange = "SH"
        else:
            exchange = "SZ"
        return f"{self.code}.{exchange}#E"


@dataclass
class MonitorResult:
    sent: int = 0
    failed: int = 0
    bootstrapped: int = 0
    skipped: int = 0
    errors: int = 0


def event_id(code: str, event: dict) -> str:
    """Stable, non-secret identity for cross-run deduplication."""
    canonical = {
        "code": code,
        "dt": event.get("dt"),
        "confirmed_at": event.get("confirmed_at"),
        "side": event.get("side"),
        "types": sorted(str(item) for item in event.get("types", [])),
    }
    raw = json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _signal_codes(event: dict) -> list[str]:
    prefix = "B" if event.get("side") == "BUY" else "S"
    return [prefix + str(item) for item in event.get("types", [])]


def _format_time(value: str | None) -> str:
    if not value:
        return "未知"
    try:
        return datetime.fromisoformat(value).strftime("%m-%d %H:%M")
    except ValueError:
        return value


def _action_text(codes: Iterable[str]) -> str:
    values = set(codes)
    if {"S2", "S2s"} & values:
        return "二卖退出信号；请按交易纪律复核并处理持仓。"
    if {"S1", "S1p"} & values:
        return "一卖风险预警；优先复核30分钟结构，不再追高。"
    if {"B2", "B2s"} & values:
        return "二买候选；仍需通过Quanti日选与仓位纪律，不等于自动下单。"
    if {"B1", "B1p"} & values:
        return "一买观察信号；等待二买或其他规则交叉确认。"
    return "结构信号已确认；请结合当前持仓与风险规则复核。"


def bark_payload(target: StockTarget, event: dict) -> dict:
    codes = _signal_codes(event)
    code_text = "/".join(codes) or str(event.get("label", "结构信号"))
    price = event.get("price")
    price_text = f"{float(price):.2f}" if price is not None else "未知"
    body = "\n".join([
        _action_text(codes),
        f"代码 {target.code} · 结构点 {_format_time(event.get('dt'))}",
        f"确认于 {_format_time(event.get('confirmed_at'))} · 结构价 {price_text}",
        "来源 Chan.py固定版本 · 30分钟已完成K线",
    ])
    level = "timeSensitive" if any(code.startswith("S") for code in codes) else "active"
    exchange = "sh" if target.code.startswith("6") else "sz"
    return {
        "title": f"{target.name} 30分钟 {code_text}确认",
        "body": body,
        "group": "Quanti缠论",
        "level": level,
        "isArchive": "1",
        "url": f"https://quote.eastmoney.com/{exchange}{target.code}.html",
    }


def send_bark(device_key: str, target: StockTarget, event: dict) -> bool:
    import httpx

    payload = {"device_key": device_key, **bark_payload(target, event)}
    response = httpx.post("https://api.day.app/push", json=payload, timeout=15)
    if response.status_code >= 400:
        return False
    try:
        return response.json().get("code") == 200
    except ValueError:
        return False


def send_bark_status(device_key: str, title: str, body: str) -> bool:
    import httpx

    response = httpx.post(
        "https://api.day.app/push",
        json={
            "device_key": device_key,
            "title": title,
            "body": body,
            "group": "Quanti缠论",
            "level": "active",
            "isArchive": "1",
        },
        timeout=15,
    )
    if response.status_code >= 400:
        return False
    try:
        return response.json().get("code") == 200
    except ValueError:
        return False


def monitor_once(
    targets: Iterable[StockTarget],
    state: dict,
    *,
    fetch_events: Callable[[StockTarget], tuple[list[dict], str]],
    send_signal: Callable[[StockTarget, dict], bool],
    now: datetime,
) -> MonitorResult:
    """Scan one bar close and update state only after successful delivery."""
    result = MonitorResult()
    state.setdefault("version", STATE_VERSION)
    stocks = state.setdefault("stocks", {})

    for target in targets:
        try:
            events, as_of = fetch_events(target)
        except Exception as exc:  # one provider failure must not hide other stocks
            result.errors += 1
            print(f"{target.code} fetch failed: {type(exc).__name__}: {exc}")
            continue

        stock_state = stocks.setdefault(target.code, {"initialized": False, "seen": []})
        seen = list(dict.fromkeys(stock_state.get("seen", [])))
        seen_set = set(seen)
        stock_state["last_bar"] = as_of

        if not stock_state.get("initialized"):
            for event in events:
                identity = event_id(target.code, event)
                if identity not in seen_set:
                    seen.append(identity)
                    seen_set.add(identity)
            stock_state["initialized"] = True
            stock_state["seen"] = seen[-MAX_SEEN_PER_STOCK:]
            result.bootstrapped += 1
            continue

        for event in events:
            identity = event_id(target.code, event)
            if identity in seen_set:
                result.skipped += 1
                continue
            if not event.get("confirmed") or not event.get("active"):
                seen.append(identity)
                seen_set.add(identity)
                result.skipped += 1
                continue
            try:
                delivered = send_signal(target, event)
            except Exception as exc:
                delivered = False
                print(f"{target.code} push failed: {type(exc).__name__}: {exc}")
            if delivered:
                seen.append(identity)
                seen_set.add(identity)
                result.sent += 1
            else:
                result.failed += 1

        stock_state["seen"] = seen[-MAX_SEEN_PER_STOCK:]

    state["updated_at"] = now.isoformat()
    return result


def load_watchlist(path: Path) -> list[StockTarget]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("stocks", payload) if isinstance(payload, dict) else payload
    targets = [StockTarget(str(row["code"]).zfill(6), str(row["name"])) for row in rows]
    if not targets:
        raise ValueError("观察名单为空")
    return targets


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": STATE_VERSION, "stocks": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def last_completed_30m_bar(now: datetime) -> datetime:
    endpoints = (
        time(10, 0), time(10, 30), time(11, 0), time(11, 30),
        time(13, 30), time(14, 0), time(14, 30), time(15, 0),
    )
    if now.weekday() < 5:
        completed = [endpoint for endpoint in endpoints if endpoint <= now.time()]
        if completed:
            return datetime.combine(now.date(), completed[-1])
    day = now.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return datetime.combine(day, time(15, 0))


def build_fetcher(now: datetime):
    import akshare as ak
    import pandas as pd

    from quanti.agent.chan_morphology import confirmed_bsp_events

    local_now = now.astimezone(SHANGHAI).replace(tzinfo=None) if now.tzinfo else now
    cutoff = last_completed_30m_bar(local_now)

    def fetch(target: StockTarget) -> tuple[list[dict], str]:
        if target.symbol.endswith(".BJ#E"):
            raise RuntimeError("当前分钟行情接口不支持北交所")
        prefix = "sh" if target.symbol.endswith(".SH#E") else "sz"
        frame = ak.stock_zh_a_minute(
            symbol=prefix + target.code,
            period="30",
            adjust="hfq",
        ).rename(columns={"day": "dt", "volume": "vol"})
        frame["dt"] = pd.to_datetime(frame["dt"], errors="coerce")
        start = cutoff.date() - timedelta(days=400)
        frame = frame.loc[
            (frame.dt.dt.date >= start) & (frame.dt <= cutoff)
        ].sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
        if frame.empty:
            raise RuntimeError("30分钟行情为空")
        as_of = frame.iloc[-1]["dt"].to_pydatetime().replace(tzinfo=None)
        if as_of < cutoff:
            raise RuntimeError(
                f"数据尚未到齐: 最新 {as_of.isoformat()}, 应到 {cutoff.isoformat()}"
            )
        return confirmed_bsp_events(frame), as_of.isoformat()

    return fetch, cutoff


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push confirmed Chan.py signals to Bark")
    parser.add_argument("--watchlist", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    device_key = os.environ.get("BARK_DEVICE_KEY", "").strip()
    if not args.dry_run and not device_key:
        parser.error("BARK_DEVICE_KEY is required")

    now = datetime.now(SHANGHAI)
    targets = load_watchlist(args.watchlist)
    state = load_state(args.state)
    fetch_events, cutoff = build_fetcher(now)

    def sender(target: StockTarget, event: dict) -> bool:
        if args.dry_run:
            print(json.dumps(bark_payload(target, event), ensure_ascii=False))
            return True
        return send_bark(device_key, target, event)

    result = monitor_once(
        targets,
        state,
        fetch_events=fetch_events,
        send_signal=sender,
        now=now,
    )

    if (
        not args.dry_run
        and not state.get("cloud_ready_sent")
        and result.bootstrapped
        and result.errors == 0
    ):
        ready = send_bark_status(
            device_key,
            "Quanti云端监控已启用",
            f"已监控 {len(targets)} 只股票。每个30分钟收盘后检查一次；"
            "首次仅建立历史基线，不补发旧信号。",
        )
        if ready:
            state["cloud_ready_sent"] = True

    if not args.dry_run:
        save_state(args.state, state)
    print(
        f"cutoff={cutoff.isoformat()} stocks={len(targets)} "
        f"sent={result.sent} failed={result.failed} "
        f"bootstrapped={result.bootstrapped} errors={result.errors}"
    )
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
