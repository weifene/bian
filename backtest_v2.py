"""
v2 策略组合回测（针对现有策略的 4 大痛点改版）
==================================================
痛点对照 → v2 对策：
  1. 频率太高、费损吃利润            → 抬升周期：1h 趋势过滤（24 根），交易数降一个量级
  2. 30m 窗口“趋势”多为噪声          → 用 24h 小时级回归定趋势方向 + 力度
  3. 追峰值（评分高≈局部顶）入场       → 趋势成立后等 15m 回调到 EMA20 附近再进（maker 价）
  4. 只有评分回撤、无价格止损         → 硬止损 1.5×ATR + 吊灯跟踪 3×ATR + 时间止损

开平仓（15m 步进，共享余额 2U×3x，持仓不限，商品池=宇宙全体）：
  信号：1h×(24 根) 回归  score = R²×1000 + |斜率|×100000，要求 R²≥R2_H 且 score>SCORE_H，方向=斜率符号
  入场：趋势方向成立且 15m 回调贴 EMA20（多: close≤EMA20）→ 以现价进（近似 maker）
  止损：入场 ∓1.5×ATR15（固定，atr 取入场时）       → 硬止损
  移动：吊灯 = 持仓期最高(多)/最低(空) ∓ 3×ATR15     → 跟踪止损
  时间：持仓 ≥ TIME_15（根 15m）且未顺势创新高/低    → 时间止损
  费率：市价 taker 双边 0.05%（保守，未给 maker 折扣）

用法：python backtest_v2.py [UNIVERSE] [DAYS]
  UNIVERSE  宇宙币数（按成交量取前 N，默认 40）
  DAYS      回测天数（1m 数据拉取，默认 7）

输出：控制台 + backtest_v2_result.json（横向扫描：跟踪ATR倍数 × 时间止损根数）
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

UNIVERSE = int(sys.argv[1]) if len(sys.argv) > 1 else 40
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 7

# ---- v2 参数 ----
H_WIN = 24                # 1h 回归窗（24 根 = 1 天趋势）
R2_H = 0.80               # 1h 趋势干净 R² 门槛
SCORE_H = 1100            # 1h 评分门槛（与现行一致）
EMA_N = 20                # 15m 回调均线
ATR_N = 14                # 15m ATR 周期
TRAIL_MULTS = [2.0, 3.0, 4.0]   # 吊灯跟踪 ATR 倍数（扫描）
TIME_OPTS = [8, 12, 16]         # 时间止损：多少根 15m 未创新高/低离场（扫描）


def agg_bars(op, hi, lo, cl, times, step):
    """把 1m 数组按 step 根合并为更高级别 K 线（丢弃尾部不成组部分）。"""
    m = len(cl) // step
    o = op[::step][:m]
    h = np.array([hi[k * step:(k + 1) * step].max() for k in range(m)])
    l = np.array([lo[k * step:(k + 1) * step].min() for k in range(m)])
    c = cl[step - 1::step][:m]                 # 每组的最后一根收盘
    t = times[step - 1::step][:m]
    gv = np.full(m, step * 3600 * 1000)         # 每根 K 线的毫秒跨度
    return t, o, h, l, c, gv


def ema(x, n):
    a = 2.0 / (n + 1)
    out = np.empty_like(x, dtype=float)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
    return out


def atr_simple(hi, lo, cl, n):
    """简单均值 ATR（不是 Wilder，足够回测用）。"""
    tr = np.empty_like(hi, dtype=float)
    tr[0] = hi[0] - lo[0]
    h = hi[1:]
    lp = cl[:-1]
    tr[1:] = np.maximum(h - lo[1:], np.maximum(np.abs(h - lp), np.abs(lo[1:] - lp)))
    out = np.full_like(hi, np.nan)
    c = np.concatenate([[0.0], np.cumsum(tr)])
    for i in range(n, len(tr) + 1):
        out[i - 1] = (c[i] - c[i - n]) / n
    return out


def main():
    print(f"[v2] 拉取成交量前 {UNIVERSE} 币 × {DAYS} 天 1m OHLC …", flush=True)
    info = retry(client.futures_exchange_info)
    perps = [s["symbol"] for s in info["symbols"]
             if s["status"] == "TRADING" and s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL"]
    ticks = retry(client.futures_ticker)
    vol = {t["symbol"]: float(t["quoteVolume"]) for t in ticks if t["symbol"] in perps}
    uni = sorted(vol, key=vol.get, reverse=True)[:UNIVERSE]

    data = {}                       # sym -> dict of 15m arrays + 广播的 1h 信号
    for sym in uni:
        try:
            times, op, hi, lo, cl = fetch_ohlc(sym, limit=DAYS * 1440)
            if len(cl) < DAYS * 1440 * 0.8:
                continue
            # ---- 1h 信号 ----
            t1, o1, h1, l1, c1, g1 = agg_bars(op, hi, lo, cl, times, 60)
            hslope, hr2 = rolling_reg(c1, H_WIN)
            hscore = score_of(hr2, hslope)
            # ---- 15m 交易 ----
            t15, o15, h15, l15, c15, g15 = agg_bars(op, hi, lo, cl, times, 15)
            e20 = ema(c15, EMA_N)
            atr = atr_simple(h15, l15, c15, ATR_N)
            # 每个 15m 时刻用"最新已收盘的一根 1h"信号（收盘时间 = 开盘+1h，避免前视）
            hidx = np.searchsorted(t1 + g1, t15, side="right") - 1
            hidx = np.clip(hidx, 0, m1 := len(t1) - 1)
            data[sym] = {
                "t": t15, "o": o15, "h": h15, "l": l15, "c": c15, "atr": atr, "ema": e20,
                "h_score": hscore[hidx], "h_dir": np.sign(hslope[hidx]), "h_r2": hr2[hidx],
            }
        except Exception as e:
            print(f"  skip {sym}: {e}", flush=True)
        time.sleep(0.03)

    if not data:
        print("无数据")
        return
    M = min(len(d["c"]) for d in data.values())
    print(f"  已加载 {len(data)} 币，模拟 {M} 根 15 分钟棒（{M*15//1440} 天）", flush=True)

    def simulate(trail_mult, time_n):
        balance = START_BAL
        positions = {}      # sym -> {side, entry, qty, stop, maxe, bars}
        trades = wins = max_pos = 0
        for i in range(M):
            # ---- 一、管理持仓 ----
            for sym in list(positions.keys()):
                p = positions[sym]
                a14 = data[sym]["atr"][i]
                if np.isnan(a14) or a14 <= 0:
                    continue
                hh, ll, cc = data[sym]["h"][i], data[sym]["l"][i], data[sym]["c"][i]
                side = p["side"]
                exit_price = None
                # 硬止损（用入场时 ATR）
                stop = p["stop"]
                if side == 1 and ll <= stop:
                    exit_price = stop
                elif side == -1 and hh >= stop:
                    exit_price = stop
                # 吊灯跟踪
                if exit_price is None:
                    trail = (p["maxe"] - trail_mult * a14) if side == 1 else (p["maxe"] + trail_mult * a14)
                    if side == 1 and ll <= trail:
                        exit_price = trail
                    elif side == -1 and hh >= trail:
                        exit_price = trail
                # 时间止损：≥time_n 未顺势创新高/低
                if exit_price is None and p["bars"] >= time_n:
                    made_new = (cc > p["maxe"]) if side == 1 else (cc < p["maxe"])
                    if not made_new:
                        exit_price = cc
                if exit_price is not None:
                    pnl = (exit_price - p["entry"]) * p["qty"] * side - 2 * p["notional"] * FEE
                    balance += p["margin"] + pnl
                    trades += 1
                    if pnl > 0:
                        wins += 1
                    del positions[sym]
            # ---- 更新 maxe / bars（对还在仓的）----
            for sym, p in positions.items():
                hh, ll = data[sym]["h"][i], data[sym]["l"][i]
                p["maxe"] = max(p["maxe"], hh) if p["side"] == 1 else min(p["maxe"], ll)
                p["bars"] += 1

            # ---- 二、开仓（1h 趋势成立 + 15m 回调贴 EMA）----
            for sym in list(data.keys()):
                if sym in positions:
                    continue
                if balance < MARGIN * 1.1:
                    continue
                hsc = data[sym]["h_score"][i]
                hr2 = data[sym]["h_r2"][i]
                hd = data[sym]["h_dir"][i]
                e20 = data[sym]["ema"][i]
                cc = data[sym]["c"][i]
                a14 = data[sym]["atr"][i]
                if np.isnan(hr2) or hr2 < R2_H or hsc <= SCORE_H or hd == 0:
                    continue
                if np.isnan(e20) or np.isnan(a14) or a14 <= 0:
                    continue
                # 回调至 EMA 附近才进（多：现价 ≤ EMA；空：现价 ≥ EMA）
                if hd == 1 and cc <= e20 * 1.005:
                    side = 1
                    entry = cc
                elif hd == -1 and cc >= e20 * 0.995:
                    side = -1
                    entry = cc
                else:
                    continue
                notional = MARGIN * LEV
                positions[sym] = {
                    "side": side, "entry": entry, "qty": notional / entry,
                    "stop": entry - 1.5 * a14 if side == 1 else entry + 1.5 * a14,
                    "maxe": entry, "bars": 0, "margin": MARGIN, "notional": notional,
                }
                balance -= MARGIN
                balance -= notional * FEE
            max_pos = max(max_pos, len(positions))

        unreal = 0.0
        for sym, p in positions.items():
            cc = data[sym]["c"][M - 1]
            unreal += (cc - p["entry"]) * p["qty"] * p["side"] - p["notional"] * FEE
        final_bal = balance + unreal
        ret_pct = (final_bal - START_BAL) / START_BAL * 100
        wr = wins / trades * 100 if trades else 0
        return {"trail": trail_mult, "time": time_n, "ret%": ret_pct, "trades": trades,
                "win_rate%": wr, "max_pos": max_pos, "final": final_bal, "open": len(positions)}

    print(f"\n===== v2 组合回测 | {len(data)} 币 × {M*15//1440} 天 | 2U×{LEV}x | 市价 taker 双边费 =====", flush=True)
    print(f"{'跟踪ATR':<8}{'时间ns':<7}{'收益%':<10}{'交易数':<7}{'胜率':<7}{'峰仓':<6}{'期末仓':<6}", flush=True)
    print("-" * 56, flush=True)
    results = []
    for tm in TRAIL_MULTS:
        for tn in TIME_OPTS:
            r = simulate(tm, tn)
            results.append(r)
            print(f"{tm:<8.1f}{tn:<7}{r['ret%']:>+8.2f}%{r['trades']:>7}{r['win_rate%']:>6.0f}%{r['max_pos']:>6}{r['open']:>6}", flush=True)

    best = max(results, key=lambda r: r["ret%"])
    print(f"\n最优参数：跟踪 {best['trail']}×ATR + 时间 {best['time']} 根 → 收益 {best['ret%']:+.2f}%"
          f"，交易 {best['trades']}，胜率 {best['win_rate%']:.0f}%，期末持仓 {best['open']}", flush=True)

    out = os.path.join(os.path.dirname(__file__), "backtest_v2_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"days": M * 15 // 1440, "universe": len(data), "margin": MARGIN, "lev": LEV,
                   "fee": FEE, "params": {"h_win": H_WIN, "r2_h": R2_H, "score_h": SCORE_H,
                                          "ema": EMA_N, "atr": ATR_N, "time_15": TIME_OPTS, "trail": TRAIL_MULTS,
                                          "hard_stop_atr": 1.5},
                   "results": results, "best": {k: v for k, v in best.items()}}, f, ensure_ascii=False, indent=2)
    print(f"汇总已保存：{out}", flush=True)


if __name__ == "__main__":
    main()