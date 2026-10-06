"""
v4 趋势跟随参数扫描网格回测
============================================================================
基于 backtest_v4.py 的核心逻辑，扩展为参数网格扫描：
- 一次性下载 30 币 × 90 天 1m K 线并聚合为 15m/1h，缓存到内存
- 扫描参数网格：R2_ENTRY / SL_H / K(ATR倍数) / CONF(确认根数)
- 每个组合输出：90天收益%、年化%、最大回撤%、交易笔数、胜率、分币盈亏
- 仓位口径：固定 2U 保证金 × 3x = 6U 名义，最多 8 仓（对齐实盘 bot.py）
- 回撤闸门：禁用（实盘已移除，与回测对齐）

用法：python backtest_v4_grid.py [UNIVERSE=30] [DAYS=90]
输出：控制台表格 + backtest_v4_grid_result.json
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

# ==================== 网格参数 ====================
UNIVERSE = int(sys.argv[1]) if len(sys.argv) > 1 else 30
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 90

# 待扫描的参数网格
GRID_R2     = [0.65, 0.70, 0.75, 0.80, 0.85]        # 1h 趋势 R²
GRID_SL     = [0.0001, 0.00015, 0.0002, 0.0003]     # 1h |斜率| 门槛
GRID_K      = [3.0, 4.0, 5.0, 6.0, 7.0]             # 吊灯 ATR 倍数
GRID_CONF   = [2, 3, 4, 5, 6]                       # 连续确认根数

# 实盘固定参数（对齐 bot.py）
H_WIN = 24           # 1h 回归窗
ATRN = 14            # 15m ATR
MAX_POS = 8
START_BAL = 100.0

# 目标：年化 ≥50%，最大回撤 ≤20%
TARGET_ANN = 50.0
TARGET_DD = 20.0


def load_data():
    """下载并聚合 30 币 × 90 天数据，返回 data dict"""
    print(f"[grid] 拉取成交量前 {UNIVERSE} 币 × {DAYS} 天 1m OHLC …", flush=True)
    info = retry(client.futures_exchange_info)
    perps = [s["symbol"] for s in info["symbols"]
             if s["status"] == "TRADING" and s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL"]
    ticks = retry(client.futures_ticker)
    vol = {t["symbol"]: float(t["quoteVolume"]) for t in ticks if t["symbol"] in perps}
    uni = sorted(vol, key=vol.get, reverse=True)[:UNIVERSE]

    data = {}
    for sym in uni:
        try:
            times, op, hi, lo, cl = fetch_ohlc(sym, limit=DAYS * 1440)
            if len(cl) < DAYS * 1440 * 0.8:
                continue
            t1, o1, h1, l1, c1, g1 = agg_bars(op, hi, lo, cl, times, 60)
            hslope, hr2 = rolling_reg(c1, H_WIN)
            t15, o15, h15, l15, c15, g15 = agg_bars(op, hi, lo, cl, times, 15)
            atr = atr_simple(h15, l15, c15, ATRN)
            hidx = np.clip(np.searchsorted(t1 + g1, t15, side="right") - 1, 0, len(t1) - 1)
            data[sym] = {
                "t": t15, "h": h15, "l": l15, "c": c15, "atr": atr,
                "hslope": hslope, "hr2": hr2, "hidx": hidx,
            }
        except Exception as e:
            print(f"   skip {sym}: {e}", flush=True)
        time.sleep(0.03)

    if not data:
        print("无数据")
        sys.exit(1)
    M = min(len(d["c"]) for d in data.values())
    print(f"  已加载 {len(data)} 币，模拟 {M} 根 15 分钟棒（{M*15//1440} 天）\n", flush=True)
    return data, M


def simulate(data, M, r2, sl, k, conf):
    """单次模拟：返回汇总结果 + 分币盈亏"""
    balance = START_BAL
    positions = {}
    hot = {s: 0 for s in data}
    peak_eq = START_BAL
    max_dd = 0.0
    trades = wins = 0
    pnl_by = {s: 0.0 for s in data}
    n_by = {s: 0 for s in data}
    liq = 0

    # 预计算每币的 sig / dir（不随参数变化的部分可复用）
    # sig = (hr2[hidx] >= R2) & (|hslope[hidx]| >= SL)
    for sym, d in data.items():
        d["_sig"] = (d["hr2"][d["hidx"]] >= r2) & (np.abs(d["hslope"][d["hidx"]]) >= sl)
        d["_dir"] = np.sign(d["hslope"][d["hidx"]])

    for i in range(M):
        # 更新 sig 连续计数
        for s in data:
            hot[s] = hot[s] + 1 if data[s]["_sig"][i] else 0

        # 1) 离场（吊灯跟踪）
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

        # 2) 权益/回撤
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

        # 3) 进场（实盘口径：固定 2U×3x=6U 名义，最多 8 仓）
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
            notional = MARGIN * LEV  # 固定 6U 名义
            qty = notional / entry
            margin = notional / LEV
            if balance < margin * 1.1:
                continue
            positions[sym] = {"side": 1, "entry": entry, "qty": qty,
                              "maxe": entry, "margin": margin, "notional": notional}
            balance -= margin
            balance -= notional * FEE

    # 结算浮动
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


def main():
    data, M = load_data()

    total_combos = len(GRID_R2) * len(GRID_SL) * len(GRID_K) * len(GRID_CONF)
    print(f"===== 参数网格扫描：R²×斜率×K×确认 = {len(GRID_R2)}×{len(GRID_SL)}×{len(GRID_K)}×{len(GRID_CONF)} = {total_combos} 组合 =====", flush=True)
    print(f"仓位口径：固定 {MARGIN}U×{LEV}x={MARGIN*LEV}U 名义，最多 {MAX_POS} 仓，无回撤闸门")
    print(f"目标：年化≥{TARGET_ANN}% 且 回撤≤{TARGET_DD}%\n", flush=True)

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

    # ---- 汇总排序 ----
    ok = [r for r in results if r["ann%"] >= TARGET_ANN and r["max_dd%"] <= TARGET_DD]
    ok.sort(key=lambda r: -r["ann%"])
    results.sort(key=lambda r: -r["ann%"])

    print(f"\n===== 扫描完成，共 {total_combos} 组合 =====", flush=True)
    if ok:
        print(f"✅ 达标组合 {len(ok)} 个（年化≥{TARGET_ANN}% 且 回撤≤{TARGET_DD}%），Top 10：\n")
        print(header, flush=True)
        print("-" * 70, flush=True)
        for r in ok[:10]:
            print(f"{r['r2']:<6}{r['sl']:<10}{r['k']:<4}{r['conf']:<5}"
                  f"{r['ret%']:>+6.1f}%{r['ann%']:>+7.1f}%{r['max_dd%']:>7.1f}%"
                  f"{r['trades']:>6}{r['win%']:>6.1f}%  ★", flush=True)
    else:
        print(f"❌ 无组合达标（年化≥{TARGET_ANN}% 且 回撤≤{TARGET_DD}%）。Top 10 最优：\n")
        print(header, flush=True)
        print("-" * 70, flush=True)
        for r in results[:10]:
            print(f"{r['r2']:<6}{r['sl']:<10}{r['k']:<4}{r['conf']:<5}"
                  f"{r['ret%']:>+6.1f}%{r['ann%']:>+7.1f}%{r['max_dd%']:>7.1f}%"
                  f"{r['trades']:>6}{r['win%']:>6.1f}%", flush=True)

    # ---- 最优组合的分币盈亏 ----
    best = results[0]
    print(f"\n===== 最优组合分币盈亏（R²={best['r2']} 斜率={best['sl']} K={best['k']} 确认={best['conf']}）=====")
    pb, nb = best["pnl_by"], best["n_by"]
    rank = sorted(pb.items(), key=lambda x: -x[1])
    n_pos = sum(1 for v in pb.values() if v > 0)
    print(f"{'币':<14}{'净盈亏U':>10}{'笔数':>6}   {'盈利':>2}/{len(pb)}", flush=True)
    print("-" * 40, flush=True)
    for s, v in rank:
        tag = "✅" if v > 0 else ("  " if v == 0 else "❌")
        print(f"{s:<14}{v:>+10.3f}{nb.get(s, 0):>6}  {tag}", flush=True)

    # ---- 保存 ----
    out = os.path.join(os.path.dirname(__file__), "backtest_v4_grid_result.json")
    save = []
    for r in results:
        rr = {kk: vv for kk, vv in r.items() if kk not in ("pnl_by", "n_by")}
        save.append(rr)
    # 分币盈亏只保存最优组合
    save_best = dict(best)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"days": M * 15 // 1440, "universe": UNIVERSE,
                   "grid": {"r2": GRID_R2, "sl": GRID_SL, "k": GRID_K, "conf": GRID_CONF},
                   "best": save_best,
                   "results": save}, f, ensure_ascii=False, indent=2)
    print(f"\n已保存 {out}", flush=True)


if __name__ == "__main__":
    main()
