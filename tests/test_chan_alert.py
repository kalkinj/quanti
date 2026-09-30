from __future__ import annotations

from datetime import datetime

from quanti.agent.chan_alert import (
    StockTarget,
    bark_payload,
    event_id,
    monitor_once,
)


def _event(**overrides):
    event = {
        "dt": "2026-09-30T10:00:00",
        "confirmed_at": "2026-09-30T10:30:00",
        "price": 42.18,
        "side": "BUY",
        "types": ["2"],
        "label": "二买",
        "confirmed": True,
        "active": True,
    }
    event.update(overrides)
    return event


def test_event_id_is_stable_and_stock_specific():
    event = _event(types=["2s", "2"])
    assert event_id("300604", event) == event_id(
        "300604", {**event, "types": ["2", "2s"]}
    )
    assert event_id("300604", event) != event_id("300790", event)


def test_first_scan_bootstraps_history_without_sending_old_signals():
    target = StockTarget("300604", "长川科技")
    state = {}
    sent = []

    result = monitor_once(
        [target], state,
        fetch_events=lambda _: ([_event()], "2026-09-30T10:30:00"),
        send_signal=lambda target, event: sent.append((target, event)) or True,
        now=datetime(2026, 9, 30, 10, 35),
    )

    assert result.bootstrapped == 1
    assert result.sent == 0
    assert sent == []
    assert state["stocks"]["300604"]["seen"] == [event_id("300604", _event())]


def test_only_new_active_confirmed_signal_is_sent_once():
    target = StockTarget("300604", "长川科技")
    old = _event()
    new = _event(
        dt="2026-09-30T11:00:00",
        confirmed_at="2026-09-30T11:30:00",
        side="SELL",
        types=["2"],
        label="二卖",
        price=41.30,
    )
    state = {
        "version": 1,
        "stocks": {
            "300604": {
                "initialized": True,
                "seen": [event_id("300604", old)],
            }
        },
    }
    sent = []

    for _ in range(2):
        result = monitor_once(
            [target], state,
            fetch_events=lambda _: ([old, new], "2026-09-30T11:30:00"),
            send_signal=lambda stock, event: sent.append((stock.code, event["label"])) or True,
            now=datetime(2026, 9, 30, 11, 35),
        )

    assert result.sent == 0
    assert sent == [("300604", "二卖")]


def test_inactive_or_unconfirmed_points_are_recorded_but_not_sent():
    target = StockTarget("300604", "长川科技")
    state = {"version": 1, "stocks": {"300604": {"initialized": True, "seen": []}}}
    events = [
        _event(active=False),
        _event(dt="2026-09-30T11:00:00", confirmed=False),
    ]
    sent = []

    result = monitor_once(
        [target], state,
        fetch_events=lambda _: (events, "2026-09-30T11:30:00"),
        send_signal=lambda stock, event: sent.append(event) or True,
        now=datetime(2026, 9, 30, 11, 35),
    )

    assert result.sent == 0
    assert sent == []
    assert len(state["stocks"]["300604"]["seen"]) == 2


def test_failed_push_is_not_marked_seen_so_next_run_retries():
    target = StockTarget("300604", "长川科技")
    state = {"version": 1, "stocks": {"300604": {"initialized": True, "seen": []}}}
    signal = _event()

    failed = monitor_once(
        [target], state,
        fetch_events=lambda _: ([signal], "2026-09-30T10:30:00"),
        send_signal=lambda *_: False,
        now=datetime(2026, 9, 30, 10, 35),
    )
    assert failed.failed == 1
    assert state["stocks"]["300604"]["seen"] == []

    retried = monitor_once(
        [target], state,
        fetch_events=lambda _: ([signal], "2026-09-30T10:30:00"),
        send_signal=lambda *_: True,
        now=datetime(2026, 9, 30, 10, 36),
    )
    assert retried.sent == 1
    assert state["stocks"]["300604"]["seen"] == [event_id("300604", signal)]


def test_bark_payload_explains_signal_instead_of_pretending_to_trade():
    target = StockTarget("300604", "长川科技")
    payload = bark_payload(target, _event(side="SELL", types=["2"], label="二卖"))

    assert payload["title"] == "长川科技 30分钟 S2确认"
    assert "退出信号" in payload["body"]
    assert "结构点 09-30 10:00" in payload["body"]
    assert "确认于 09-30 10:30" in payload["body"]
    assert payload["level"] == "timeSensitive"
    assert "device_key" not in payload
