"""
v3 简单趋势策略回测 - 让赢家跑
==================================================
核心假设：v2 亏损主因是把赢家砍太早（35% 胜率、止损/时间止损小赚大亏）。
v3 去掉过紧硬止损，只保留【宽吊灯跟踪 + 顺势单向】，让大趋势跑出盈亏比。

规则（尽量简单，15m 步进，共享余额 2U×3x）：
  信号：1h×24 回归，R²≥R2 且 |斜率|≥SL 视为趋势成立，方向=斜率符号
  入场：趋势成立且连续 conf 根 15m（确认过滤噪声）→ 以现价顺势进
  出场：吊灯跟踪 = 持仓期最有利价 ∓ K×ATR15（只砍在最坏方向，不设过紧硬止损）
    （可选：long_only=True 只做多，回避逆势空单与正资金费率）
  杠杆/费：3x，taker 双边 0.05%

用法：python backtest_v3.py [UNIVERSE] [DAYS]
  输出：控制台 + backtest_v3_result.json（扫描 K×ATR × 确认根数 × 多空开关）
"""
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, r"d:\bian")
import bot
from bot import client
from backtest_portfolio import fetch_ohlc, rolling_reg, score_of, retry, START_BAL, MARGIN, LEV, FEE
from backtest_v2 import agg_bars, atr_simple

UNIVERSE = int(sys.argv[1]) if len(sys.argv) > 1 else 30
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 7

R2 = 0.70          # 1h 趋势 R² 门槛（放宽一点，让行情进来）
SL = 0.00015       # 1h |斜率| 门槛
KS = [3, 4, 6]     # 吊灯 ATR 倍数
CONFS = [1, 4]     # 趋势连续确认根数（15m）
LONG_ONLY = [True, False]


def main():
    print(f"[v3] 拉取成交量前 {UNIVERSE} 币 × {DAYS} 天 1m OHLC …", flush=True)
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
            hslope, hr2 = rolling_reg(c1, 24)
            t15, o15, h15, l15, c15, g15 = agg_bars(op, hi, lo, cl, times, 15)
            atr = atr_simple(h15, l15, c15, 14)
            hidx = np.clip(np.searchsorted(t1 + g1, t15, side="right") - 1, 0, len(t1) - 1)
            # 每根 15m 的"顺势强度"：为真表示趋势仍顺（1h 信号 + R²及斜率达标）
            sig = (hr2[hidx] >= R2) & (np.abs(hslope[hidx]) >= SL)
            data[sym] = {"t": t15, "h": h15, "l": l15, "c": c15, "atr": atr,
                         "sig": sig, "dir": np.sign(hslope[hidx])}
        except Exception as e:
            print(f"  skip {sym}: {e}", flush=True)
        time.sleep(0.03)

    if not data:
        print("无数据")
        return
    M = min(len(d["c"]) for d in data.values())
    print(f"  已加载 {len(data)} 币，模拟 {M} 根 15 分钟棒（{M*15//1440} 天）\n", flush=True)

    def simulate(k, conf, long_only, seg=None):
        """seg=(i0,i1) 限定模拟区间（分段稳健性验证）；None=全程。"""
        i0, i1 = seg if seg else (0, M)
        balance = START_BAL
        positions = {}
        trades = wins = max_pos = long_n = 0
        hot = {s: 0 for s in data}   # 连续满足趋势的根数（到 i0 为止的历史累计）
        for i in range(i0, min(i1, M)):
            # 更新 sig 连续计数
            for s in data:
                hot[s] = hot[s] + 1 if data[s]["sig"][i] else 0
            # 一、离场（吊灯跟踪，无硬止损；只比 maxe 方向）
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
                    if pnl > 0:
                        wins += 1
                    if p["side"] == -1:
                        long_n -= 1
                    del positions[sym]
            # 二、入场
            for sym in data:
                if sym in positions or balance < MARGIN * 1.1:
                    continue
                d = data[sym]
                if hot[sym] < conf or not bool(d["sig"][i]):
                    continue
                side = int(d["dir"][i])
                if side == 0 or (long_only and side == -1):
                    continue
                entry = d["c"][i]
                notional = MARGIN * LEV
                positions[sym] = {"side": side, "entry": entry, "qty": notional / entry,
                                  "maxe": entry, "margin": MARGIN, "notional": notional}
                if side == -1:
                    long_n += 1
                balance -= MARGIN
                balance -= notional * FEE
            max_pos = max(max_pos, len(positions))

        unreal = 0.0
        for sym, p in positions.items():
            cc = data[sym]["c"][M - 1]
            trail = p["maxe"] - (k * data[sym]["atr"][M - 1]) if p["side"] == 1 else p["maxe"] + (k * data[sym]["atr"][M - 1])
            px = trail
            unreal += (px - p["entry"]) * p["qty"] * p["side"] - p["notional"] * FEE
        final_bal = balance + unreal
        ret_pct = (final_bal - START_BAL) / START_BAL * 100
        wr = wins / trades * 100 if trades else 0
        return {"K": k, "conf": conf, "long_only": long_only, "ret%": ret_pct, "trades": trades,
                "win%": wr, "max_pos": max_pos, "open": len(positions)}

    print(f"===== v3 简单趋势 | 让赢家跑 | {len(data)} 币 × {M*15//1440} 天 | 2U×{LEV}x | taker 双边费 =====", flush=True)
    print(f"{'K×ATR':<7}{'确认':<6}{'仅多':<6}{'收益%':<9}{'交易':<6}{'胜率':<6}{'峰仓':<5}{'期末':<5}", flush=True)
    print("-" * 56, flush=True)
    results = []
    for k in KS:
        for conf in CONFS:
            for lo in LONG_ONLY:
                r = simulate(k, conf, lo)
                results.append(r)
                print(f"{k:<7}{conf:<6}{str(lo):<6}{r['ret%']:>+7.2f}%{r['trades']:>6}{r['win%']:>5.0f}%{r['max_pos']:>5}{r['open']:>5}", flush=True)

    best = max(results, key=lambda r: r["ret%"])
    print(f"\n最优：K={best['K']}×ATR 确认{best['conf']} 仅多={best['long_only']} → {best['ret%']:+.2f}%"
          f"，{best['trades']} 笔，胜率 {best['win%']:.0f}%", flush=True)

    # ============ 分段稳健性验证（最优参数，30 天切 3 段各 10 天） ============
    print(f"\n===== 分段稳健性 | 最优参数参与 K={best['K']} 确认{best['conf']} 仅多={best['long_only']} | 三参宽 3 段 =====", flush=True)
    print(f"{'段':<6}{'区间':<14}{'收益%':<9}{'交易':<6}{'胜率':<6}{'期末':<5}", flush=True)
    print("-" * 48, flush=True)
    n_seg = 3
    seg_len = M // n_seg
    seg_rows = []
    for s in range(n_seg):
        i0, i1 = s * seg_len, (s + 1) * seg_len
        r = simulate(best["K"], best["conf"], best["long_only"], (i0, i1))
        seg_rows.append(r)
        seg_days = (i1 - i0) * 15 // 1440
        print(f"第{s+1}段 | {seg_days}天  |{r['ret%']:>+7.2f}%{r['trades']:>6}{r['win%']:>5.0f}%{r['open']:>5}", flush=True)
    pos_ct = sum(1 for r in seg_rows if r["ret%"] > 0)
    seg_strs = ["%+.2f%%" % r["ret%"] for r in seg_rows]
    print(f"\n3 段中盈利 {pos_ct}/3 段 | 段内收益 {seg_strs}", flush=True)

    out = os.path.join(os.path.dirname(__file__), "backtest_v3_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"days": M * 15 // 1440, "universe": len(data), "r2": R2, "slope": SL,
                   "results": results, "best": best, "segments": seg_rows}, f, ensure_ascii=False, indent=2)
    print(f"已保存 {out}", flush=True)


if __name__ == "__main__":
    main()