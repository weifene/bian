"""
v4 趋势跟随参数扫描网格回测（快速版）
============================================================================
优化：直接拉取 15m K线（8640根 vs 1m 的 129600根），数据量减少 15 倍。
回测精度：15m 级别足够（策略就是 15m 交易粒度）。

用法：python backtest_v4_grid_fast.py [UNIVERSE=30] [DAYS=90]
"""
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, r"d:\bian")
from bot import client
from backtest_portfolio import fetch_ohlc, rolling_reg, retry, MARGIN, LEV, FEE
from backtest_v2 import agg_bars, atr_simple

UNIVERSE = int(sys.argv[1]) if len(sys.argv) > 1 else 30
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 90

GRID_R2   = [0.65, 0.70, 0.75, 0.80, 0.85]
GRID_SL   = [0.0001, 0.00015, 0.0002, 0.0003]
GRID_K    = [3.0, 4.0, 5.0, 6.0, 7.0]
GRID_CONF = [2, 3, 4, 5, 6]

H_WIN = 24
ATRN = 14
MAX_POS = 8
START_BAL = 100.0
TARGET_ANN = 50.0
TARGET_DD = 20.0


def fetch_15m_ohlc(symbol, days):
    """直接拉 15m K线，返回 (hi, lo, cl, times) 从旧到新"""
    limit = days * 96  # 15m 每天 96 根
    ks, end = [], None
    while len(ks) < limit:
        batch = retry(lambda: client.futures_klines(
            symbol=symbol, interval=client.KLINE_INTERVAL_15MINUTE, limit=1500, endTime=end))
        if not batch:
            break
        ks = batch + ks
        end = batch[0][0] - 1
        time.sleep(0.02)
    ks = ks[-limit:]
    times = np.asarray([k[0] for k in ks], dtype=np.int64)
    op = np.asarray([float(k[1]) for k in ks], dtype=float)
    hi = np.asarray([float(k[2]) for k in ks], dtype=float)
    lo = np.asarray([float(k[3]) for k in ks], dtype=float)
    cl = np.asarray([float(k[4]) for k in ks], dtype=float)
    return times, op, hi, lo, cl


def agg_1h_from_15m(t15, c15):
    """从 15m 收盘价聚合出 1h 收盘价（每4根取最后）"""
    n = len(c15) // 4
    c1h = np.array([c15[i * 4 + 3] for i in range(n)])
    t1h = np.array([t15[i * 4 + 3] for i in range(n)])
    return t1h, c1h


def main():
    print(f"[fast-grid] 拉取成交量前 {UNIVERSE} 币 × {DAYS} 天 15m OHLC …", flush=True)
    info = retry(client.futures_exchange_info)
    perps = [s["symbol"] for s in info["symbols"]
             if s["status"] == "TRADING" and s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL"]
    ticks = retry(client.futures_ticker)
    vol = {t["symbol"]: float(t["quoteVolume"]) for t in ticks if t["symbol"] in perps}
    uni = sorted(vol, key=vol.get, reverse=True)[:UNIVERSE]
    print(f"  选币池：{uni}\n", flush=True)

    data = {}
    for i, sym in enumerate(uni):
        try:
            times, op, hi, lo, cl = fetch_15m_ohlc(sym, DAYS)
            if len(cl) < DAYS * 96 * 0.7:
                print(f"   skip {sym}: 数据不足 ({len(cl)}根)", flush=True)
                continue
            t1h, c1h = agg_1h_from_15m(times, cl)
            hslope, hr2 = rolling_reg(c1h, H_WIN)
            atr = atr_simple(hi, lo, cl, ATRN)
            # 对齐：15m 每根对应最近的 1h 索引
            hidx = np.clip(np.searchsorted(t1h, times, side="right") - 1, 0, len(t1h) - 1)
            data[sym] = {
                "h": hi, "l": lo, "c": cl, "atr": atr,
                "hslope": hslope, "hr2": hr2, "hidx": hidx,
            }
        except Exception as e:
            print(f"   skip {sym}: {e}", flush=True)
        if (i + 1) % 5 == 0:
            print(f"  已加载 {i+1}/{len(uni)} …", flush=True)

    if not data:
        print("无数据")
        sys.exit(1)
    M = min(len(d["c"]) for d in data.values())
    print(f"\n  共 {len(data)} 币，模拟 {M} 根 15 分钟棒（{M*15//1440} 天）\n", flush=True)

    total = len(GRID_R2) * len(GRID_SL) * len(GRID_K) * len(GRID_CONF)
    print(f"===== 参数网格扫描：{len(GRID_R2)}×{len(GRID_SL)}×{len(GRID_K)}×{len(GRID_CONF)} = {total} 组合 =====", flush=True)
    print(f"仓位：固定 {MARGIN}U×{LEV}x={MARGIN*LEV}U 名义，最多 {MAX_POS} 仓 | 目标：年化≥{TARGET_ANN}% 且 回撤≤{TARGET_DD}%\n", flush=True)

    header = f"{'R²':<6}{'斜率':<10}{'K':<4}{'确认':<5}{'收益%':<8}{'年化%':<8}{'回撤%':<8}{'交易':<6}{'胜率%':<7}"
    print(header, flush=True)
    print("-" * 70, flush=True)

    results = []
    done = 0
    for r2 in GRID_R2:
        for sl in GRID_SL:
            for k in GRID_K:
                for conf in GRID_CONF:
                    r = simulate(data, M, r2, sl, k, conf)
                    results.append(r)
                    done += 1
                    flag = " ★" if (r["ann%"] >= TARGET_ANN and r["max_dd%"] <= TARGET_DD) else ""
                    print(f"{r2:<6}{sl:<10}{k:<4}{conf:<5}"
                          f"{r['ret%']:>+6.1f}%{r['ann%']:>+7.1f}%{r['max_dd%']:>7.1f}%"
                          f"{r['trades']:>6}{r['win%']:>6.1f}%{flag}", flush=True)

    # 汇总排序
    ok = sorted([r for r in results if r["ann%"] >= TARGET_ANN and r["max_dd%"] <= TARGET_DD], key=lambda r: -r["ann%"])
    results.sort(key=lambda r: -r["ann%"])

    print(f"\n===== 扫描完成，共 {total} 组合 =====", flush=True)
    if ok:
        print(f"✅ 达标组合 {len(ok)} 个，Top 10：\n")
        print(header, flush=True)
        print("-" * 70, flush=True)
        for r in ok[:10]:
            print(f"{r['r2']:<6}{r['sl']:<10}{r['k']:<4}{r['conf']:<5}"
                  f"{r['ret%']:>+6.1f}%{r['ann%']:>+7.1f}%{r['max_dd%']:>7.1f}%"
                  f"{r['trades']:>6}{r['win%']:>6.1f}%  ★", flush=True)
    else:
        print(f"❌ 无组合达标。Top 10 最优：\n")
        print(header, flush=True)
        print("-" * 70, flush=True)
        for r in results[:10]:
            print(f"{r['r2']:<6}{r['sl']:<10}{r['k']:<4}{r['conf']:<5}"
                  f"{r['ret%']:>+6.1f}%{r['ann%']:>+7.1f}%{r['max_dd%']:>7.1f}%"
                  f"{r['trades']:>6}{r['win%']:>6.1f}%", flush=True)

    # 最优组合分币盈亏
    best = results[0]
    print(f"\n===== 最优组合分币盈亏（R²={best['r2']} 斜率={best['sl']} K={best['k']} 确认={best['conf']}）=====")
    pb, nb = best["pnl_by"], best["n_by"]
    rank = sorted(pb.items(), key=lambda x: -x[1])
    n_pos = sum(1 for v in pb.values() if v > 0)
    n_neg = sum(1 for v in pb.values() if v < 0)
    print(f"{'币':<14}{'净盈亏U':>10}{'笔数':>6}   盈利 {n_pos} 币 / 亏损 {n_neg} 币 / 持平 {len(pb)-n_pos-n_neg} 币", flush=True)
    print("-" * 45, flush=True)
    for s, v in rank:
        tag = "✅" if v > 0 else ("  " if v == 0 else "❌")
        print(f"{s:<14}{v:>+10.3f}{nb.get(s, 0):>6}  {tag}", flush=True)

    # 保存
    out = os.path.join(os.path.dirname(__file__), "backtest_v4_grid_result.json")
    save = []
    for r in results:
        rr = {kk: vv for kk, vv in r.items() if kk not in ("pnl_by", "n_by")}
        save.append(rr)
    save_best = dict(best)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"days": M * 15 // 1440, "universe": UNIVERSE,
                   "grid": {"r2": GRID_R2, "sl": GRID_SL, "k": GRID_K, "conf": GRID_CONF},
                   "best": save_best,
                   "results": save}, f, ensure_ascii=False, indent=2)
    print(f"\n已保存 {out}", flush=True)


def simulate(data, M, r2, sl, k, conf):
    """模拟单次回测"""
    balance = START_BAL
    positions = {}
    hot = {s: 0 for s in data}
    peak_eq = START_BAL
    max_dd = 0.0
    trades = wins = 0
    pnl_by = {s: 0.0 for s in data}
    n_by = {s: 0 for s in data}
    liq = 0

    for sym, d in data.items():
        d["_sig"] = (d["hr2"][d["hidx"]] >= r2) & (np.abs(d["hslope"][d["hidx"]]) >= sl)
        d["_dir"] = np.sign(d["hslope"][d["hidx"]])

    for i in range(M):
        for s in data:
            hot[s] = hot[s] + 1 if data[s]["_sig"][i] else 0

        # 离场
        for sym in list(positions.keys()):
            p = positions[sym]
            a = data[sym]["atr"][i]
            if np.isnan(a) or a <= 0:
                continue
            hh, ll = data[sym]["h"][i], data[sym]["l"][i]
            p["maxe"] = max(p["maxe"], hh) if p["side"] == 1 else min(p["maxe"], ll)
            trail = p["maxe"] - k * a if p["side"] == 1 else p["maxe"] + k * a
            hit = (ll <= trail) if p["side"] == 1 else (hh >= trail)
            if hit:
                px = trail
                pnl = (px - p["entry"]) * p["qty"] * p["side"] - 2 * p["notional"] * FEE
                balance += p["margin"] + pnl
                trades += 1
                pnl_by[sym] += pnl
                n_by[sym] += 1
                if pnl > 0:
                    wins += 1
                del positions[sym]

        # 权益/回撤
        equity = balance
        for sym, p in positions.items():
            cc = data[sym]["c"][i]
            equity += (cc - p["entry"]) * p["qty"] * p["side"]
        if equity <= 0:
            for sym, p in list(positions.items()):
                cc = data[sym]["c"][i]
                pnl = (cc - p["entry"]) * p["qty"] * p["side"] - p["notional"] * FEE
                balance += p["margin"] + pnl
                trades += 1
                pnl_by[sym] += pnl
                n_by[sym] += 1
                del positions[sym]
            liq += 1
            balance = max(balance, 0.0)
            equity = balance
        peak_eq = max(peak_eq, equity)
        dd = (peak_eq - equity) / peak_eq if peak_eq > 0 else 0
        max_dd = max(max_dd, dd)

        # 进场
        opp = [s for s in data if s not in positions
               and hot[s] >= conf and bool(data[s]["_sig"][i])
               and int(data[s]["_dir"][i]) == 1]
        opp.sort(key=lambda s: -data[s]["c"][i])
        for sym in opp:
            if len(positions) >= MAX_POS:
                break
            if balance < 1.0:
                break
            a = data[sym]["atr"][i]
            if np.isnan(a) or a <= 0:
                continue
            entry = data[sym]["c"][i]
            notional = MARGIN * LEV
            qty = notional / entry
            margin = notional / LEV
            if balance < margin * 1.1:
                continue
            positions[sym] = {"side": 1, "entry": entry, "qty": qty,
                              "maxe": entry, "margin": margin, "notional": notional}
            balance -= margin
            balance -= notional * FEE

    # 结算
    unreal = 0.0
    for sym, p in positions.items():
        a = data[sym]["atr"][M - 1]
        trail = p["maxe"] - (k * a if not np.isnan(a) and a > 0 else 0)
        px = trail
        up = (px - p["entry"]) * p["qty"] * p["side"] - p["notional"] * FEE
        unreal += up
        pnl_by[sym] += up

    final = balance + unreal
    ret = (final - START_BAL) / START_BAL * 100
    days = M * 15 / 1440
    ann = (final / START_BAL) ** (365.0 / days) - 1 if final > 0 else -1
    wr = wins / trades * 100 if trades else 0

    return {
        "r2": r2, "sl": sl, "k": k, "conf": conf,
        "ret%": round(ret, 2), "ann%": round(ann * 100, 1), "max_dd%": round(max_dd * 100, 1),
        "trades": trades, "win%": round(wr, 1), "final": round(final, 2),
        "liq": liq,
        "pnl_by": pnl_by, "n_by": n_by,
    }


if __name__ == "__main__":
    main()
