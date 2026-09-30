"""No-lookahead adapter for the vendored Chan.py morphology engine."""

from __future__ import annotations

from pathlib import Path
import sys
from datetime import datetime

import pandas as pd


CHAN_ROOT = Path(__file__).resolve().parents[2] / "vendor" / "chanpy"
CHAN_PROFILE = {
    "bi_strict": True,
    "trigger_step": False,
    "skip_step": 0,
    "divergence_rate": float("inf"),
    "bsp2_follow_1": False,
    "bsp3_follow_1": False,
    "min_zs_cnt": 0,
    "bs1_peak": False,
    "macd_algo": "peak",
    "bs_type": "1,2,3a,1p,2s,3b",
    "print_warning": True,
    "zs_algo": "normal",
}
CHAN_COMMIT = "429d6ed3043e27c93a003ba2b10e70a05575e1f5"


def _imports():
    root = str(CHAN_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from Chan import CChan
    from ChanConfig import CChanConfig
    from Common.CEnum import BSP_TYPE, DATA_FIELD, FX_TYPE, KL_TYPE
    from Common.CTime import CTime
    from KLine.KLine_Unit import CKLine_Unit
    return CChan, CChanConfig, BSP_TYPE, DATA_FIELD, FX_TYPE, KL_TYPE, CTime, CKLine_Unit


def analyze_chan(frame: pd.DataFrame, *, on_frame=None, render=False, code="quanti") -> dict:
    """Keep current upstream morphology separate from historical triggers."""
    if frame.empty:
        return {"points": [], "events": [], "strokes": [], "segments": [], "centers": []}
    CChan, CChanConfig, BSP_TYPE, DATA_FIELD, FX_TYPE, KL_TYPE, CTime, CKLine_Unit = _imports()
    config = CChanConfig(dict(CHAN_PROFILE))
    class LocalDataChan(CChan):
        def load(self, *args, **kwargs):
            # The API supplies its own validated bars through trigger_load.
            return iter(())

    chan = LocalDataChan(code, lv_list=[KL_TYPE.K_30M], config=config)
    emitted: set[tuple] = set()
    events: list[dict] = []
    type_names = {"1": "一", "1p": "盘整一", "2": "二", "2s": "类二", "3a": "三A", "3b": "三B"}

    def serialize(bsp):
        dt = datetime.fromtimestamp(bsp.klu.time.ts).isoformat()
        types = [t.value for t in bsp.type]
        related = bsp.relate_bsp1
        return {
            "dt": dt, "types": types,
            "price": float(bsp.klu.low if bsp.is_buy else bsp.klu.high),
            "side": "BUY" if bsp.is_buy else "SELL",
            "label": "/".join(type_names[t] + ("买" if bsp.is_buy else "卖") for t in types),
            "engine": "chan.py", "bi_index": bsp.bi.idx,
            "bi_sure": bool(bsp.bi.is_sure),
            "segment_index": bsp.bi.seg_idx,
            "segment_sure": bool(bsp.bi.parent_seg and bsp.bi.parent_seg.is_sure),
            "related_first_dt": datetime.fromtimestamp(related.klu.time.ts).isoformat() if related else None,
            "features": dict(bsp.features.items()),
        }

    def identity(point):
        return point["dt"], point["side"], tuple(point["types"])

    for row in frame.sort_values("dt").itertuples():
        dt = pd.to_datetime(row.dt)
        klu = CKLine_Unit({
            DATA_FIELD.FIELD_TIME: CTime(
                dt.year, dt.month, dt.day, dt.hour, dt.minute,
                second=dt.second, auto=False,
            ),
            DATA_FIELD.FIELD_OPEN: float(row.open),
            DATA_FIELD.FIELD_HIGH: float(row.high),
            DATA_FIELD.FIELD_LOW: float(row.low),
            DATA_FIELD.FIELD_CLOSE: float(row.close),
            DATA_FIELD.FIELD_VOLUME: float(getattr(row, "vol", 0) or 0),
        })
        chan.trigger_load({KL_TYPE.K_30M: [klu]})
        current = chan.get_latest_bsp(number=0)
        points = [serialize(bsp) for bsp in current]
        active = {identity(p) for p in points}
        for event in events:
            event["active"] = identity(event) in active
            if not event["active"]:
                event.setdefault("invalidated_at", dt.isoformat())
            else:
                event.pop("invalidated_at", None)
        if on_frame is not None:
            on_frame(dt.isoformat(), chan, points)
        # This is the confirmation gate used by Chan.py's official strategy
        # demo: a point is actionable only when it lands on the confirmed
        # second-last merged K-line, never on the still-forming last one.
        if len(chan[0]) < 2:
            continue
        confirmed_klc = chan[0][-2]
        for bsp in current:
            if not bsp.bi.is_sure:
                continue
            if bsp.klu.klc.idx != confirmed_klc.idx:
                continue
            if bsp.is_buy and confirmed_klc.fx != FX_TYPE.BOTTOM:
                continue
            if not bsp.is_buy and confirmed_klc.fx != FX_TYPE.TOP:
                continue
            point = serialize(bsp)
            key = identity(point)
            if key in emitted:
                continue
            emitted.add(key)
            events.append({
                **point,
                "confirmed_at": dt.isoformat(),
                "confirmed": True, "active": True,
            })
    event_map = {identity(e): e for e in events}
    for point in points:
        event = event_map.get(identity(point))
        point["confirmed_at"] = event["confirmed_at"] if event else None
        point["confirmed"] = event is not None
    def line(item):
        return {"start": datetime.fromtimestamp(item.get_begin_klu().time.ts).isoformat(),
                "end": datetime.fromtimestamp(item.get_end_klu().time.ts).isoformat(),
                "start_price": float(item.get_begin_val()), "end_price": float(item.get_end_val()),
                "sure": bool(item.is_sure)}
    result = {"points": sorted(points, key=lambda p: p["dt"]), "events": events,
            "strokes": [line(b) for b in chan[0].bi_list],
            "segments": [line(s) for s in chan[0].seg_list],
            "centers": [{"start": datetime.fromtimestamp(z.begin.time.ts).isoformat(),
                         "end": datetime.fromtimestamp(z.end.time.ts).isoformat(),
                         "low": float(z.low), "high": float(z.high), "sure": bool(z.is_sure)}
                        for z in chan[0].zs_list]}
    if render:
        from quanti.agent.chan_plot import render_official_chart
        result["official_image"] = render_official_chart(chan)
    return result


def confirmed_bsp_events(frame: pd.DataFrame) -> list[dict]:
    return analyze_chan(frame)["events"]


def actionable_bsp_events(frame: pd.DataFrame, *, include_all: bool = False) -> list[dict]:
    """Reduce confirmed BSPs to an executable B2 -> S2 sequence.

    Chan.py may legitimately emit several structural points while a position
    is already open. They are useful for analysis, but showing them as orders
    makes the chart suggest repeated buys. The product rule is deliberately
    narrower: only a confirmed second buy opens a flat state, a first sell is
    an on-chart warning, and a second sell closes it. No fixed percentage stop
    is invented here; the requested exit is the Chan.py second sell.
    """
    bars = frame.sort_values("dt").reset_index(drop=True).copy()
    bar_times = pd.to_datetime(bars["dt"], errors="coerce")
    candidates: list[dict] = []
    for point in sorted(
        confirmed_bsp_events(frame),
        key=lambda item: (
            pd.to_datetime(item.get("confirmed_at"), errors="coerce"),
            item["dt"],
        ),
    ):
        confirmed_at = pd.to_datetime(point.get("confirmed_at"), errors="coerce")
        if pd.isna(confirmed_at):
            continue
        # A signal is actionable only on the next completed bar. The BSP's
        # structural `dt` is deliberately retained as signal_dt for audit,
        # but must never be presented as an executable historical fill.
        next_indices = bars.index[bar_times > confirmed_at]
        if len(next_indices) == 0:
            continue
        idx = int(next_indices[0])
        bar = bars.iloc[idx]
        candidates.append({
            **point,
            "signal_dt": point["dt"],
            "dt": pd.to_datetime(bar["dt"]).isoformat(),
            "price": float(bar.open),
            "execution_price": float(bar.open),
            "_bar_index": idx,
        })

    events: list[dict] = []
    holding = False
    cycle_started = False
    for point in candidates:
        label = point.get("label")
        side = point.get("side")
        if label == "二买" and side == "BUY" and not holding:
            events.append(point)
            holding = True
            cycle_started = True
        elif label in {"一卖", "二卖"} and side == "SELL" and cycle_started:
            events.append(point)
            # 一卖 is a warning; 二卖 is the requested actual exit.
            if label == "二卖":
                holding = False
                cycle_started = False
        elif include_all and label in {"二买", "一卖", "二卖"}:
            events.append(point)

    for point in events:
        point.pop("_bar_index", None)
    return events
