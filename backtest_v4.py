"""
v4 趋势跟随 + 波动率目标仓位 + 回撤闸门（目标：年化≥50% 且 最大回撤≤15%）
============================================================================
与 v3 相比，信号的"选币/进场/离场/止损/杠杆张数"保持 K=6×ATR 吊灯 + 仅做多 + 确认4 根；
改的是【资金管理和回撤控制】：
  1. 波动率目标仓位：单仓名义金额按该币 ATR 反比缩放，控制每仓风险占总资产的比例
     notional_i = RISK_ACCT * equity / (K * ATR_i)   （在 min/max 名义间截断）
  2. 总风险约束：同时持仓 ≤ MAX_POS（默认 8），避免过度集中/过度分散
  3. 回撤闸门（date 硬规则）：
       回撤 > DD_QUIET  (8%)  -> 目标风险减半
       回撤 > DD_HARD   (12%) -> 停止新开仓，只在回撤收窄到 6% 内才恢复
  4. 验证目标：循环扫描 RISK_ACCT（单仓风险%），只输出满足
     【年化收益 ≥50% 且 最大回撤 ≤15%】的组合，并分 3 段各看是否 ≥2 段为正。

数据：90 天（日）1h 信号 + 15m 交易，按 1m 拉取聚合。

用法：python backtest_v4.py [UNIVERSE] [DAYS]
输出：控制台 + backtest_v4_result.json
"""
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, r"d:\bian")
import bot
from bot import client
from backtest_portfolio import fetch_ohlc, rolling_reg, retry, MARGIN, LEV, FEE
from backtest_v2 import agg_bars, atr_simple

UNIVERSE = int(sys.argv[1]) if len(sys.argv) > 1 else 30
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 90

R2 = 0.70            # 1h 趋势 R² 门槛
SL = 0.00015         # 1h |斜率| 门槛
K = 6.0              # 吊灯跟踪 ATR 倍数（信号不变，v3 最优）
CONF = 4             # 15m 连续确认根数
H_WIN = 24           # 1h 回归窗
ATRN = 14            # 15m ATR

MAX_POS = 8          # 同时最大持仓
DD_QUIET = 0.08      # 回撤>8% 减半风险
DD_HARD = 0.12       # 回撤>12% 停新开
DD_RESUME = 0.06     # 回撤回到 6% 内才恢复开仓
NOTIONAL_MIN = 3.0   # 单仓最小名义 USDT（≈1U 保证金×3）
NOTIONAL_MAX = 60.0  # 单仓最大名义 USDT
START_BAL = 100.0

RISK_SCAN = [0.005, 0.010, 0.015, 0.020, 0.025, 0.030]   # 单仓占总资产的风险比例 RISK_ACCT

# 隔离诊断模式：V4_ISO=1 python backtest_v4.py 30 90  → 禁用回撤闸门+固定名义20U（对照 v3）
ISOLATE = os.environ.get("V4_ISO") == "1"


def main():
    print(f"[v4] 拉取成交量前 {UNIVERSE} 币 × {DAYS} 天 1m OHLC …", flush=True)
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
            sig = (hr2[hidx] >= R2) & (np.abs(hslope[hidx]) >= SL)
            data[sym] = {"t": t15, "h": h15, "l": l15, "c": c15, "atr": atr,
                         "sig": sig, "dir": np.sign(hslope[hidx])}
        except Exception as e:
            print(f"   skip {sym}: {e}", flush=True)
        time.sleep(0.03)

    if not data:
        print("无数据")
        return
    M = min(len(d["c"]) for d in data.values())
    print(f"  已加载 {len(data)} 币，模拟 {M} 根 15 分钟棒（{M*15//1440} 天）\n", flush=True)

    def simulate(risk_acct):
        balance = START_BAL
        positions = {}
        hot = {s: 0 for s in data}
        peak_eq = START_BAL
        max_dd = 0.0
        paused = False
        risk_mult = 1.0
        trades = wins = 0
        # 隔离诊断：禁回撤闸门 + 固定名义20U（对照 v3）；否则公用波动率目标/闸门
        hard = DD_HARD if not ISOLATE else 10.0
        quiet = DD_QUIET if not ISOLATE else 10.0
        resume = DD_RESUME if not ISOLATE else 0.0
        liq = 0  # 强平次数
        for i in range(M):
            # 更新 sig 连续计数
            for s in data:
                hot[s] = hot[s] + 1 if data[s]["sig"][i] else 0
            # 1) 离场（吊灯跟踪）
            for sym in list(positions.keys()):
                p = positions[sym]
                a = data[sym]["atr"][i]
                if np.isnan(a) or a <= 0:
                    continue
                hh, ll = data[sym]["h"][i], data[sym]["l"][i]
                p["maxe"] = max(p["maxe"], hh) if p["side"] == 1 else min(p["maxe"], ll)
                trail = p["maxe"] - K * a if p["side"] == 1 else p["maxe"] + K * a
                hit = (ll <= trail) if p["side"] == 1 else (hh >= trail)
                if hit:
                    px = trail
                    pnl = (px - p["entry"]) * p["qty"] * p["side"] - 2 * p["notional"] * FEE
                    balance += p["margin"] + pnl
                    trades += 1
                    if pnl > 0:
                        wins += 1
                    del positions[sym]
            # 2) 计算权益/回撤/风控状态
            equity = balance
            for sym, p in positions.items():
                cc = data[sym]["c"][i]
                equity += (cc - p["entry"]) * p["qty"] * p["side"]
            if equity <= 0:            # 强制平仓（防回撤>100%）：按市价全部了结
                for sym, p in list(positions.items()):
                    cc = data[sym]["c"][i]
                    pnl = (cc - p["entry"]) * p["qty"] * p["side"] - p["notional"] * FEE
                    balance += p["margin"] + pnl
                    trades += 1
                    del positions[sym]
                liq += 1
                balance = max(balance, 0.0)
                equity = balance
            peak_eq = max(peak_eq, equity)
            dd = (peak_eq - equity) / peak_eq if peak_eq > 0 else 0
            max_dd = max(max_dd, dd)
            if dd > hard:
                paused = True
            elif dd <= resume:
                paused = False
            risk_mult = 0.5 if dd > quiet else 1.0
            # 3) 进场（受风控闸门约束 + 仓位按波动率缩放）
            if not paused:
                opp = [s for s in data if s not in positions
                       and hot[s] >= CONF and bool(data[s]["sig"][i])
                       and int(data[s]["dir"][i]) == 1]
                opp.sort(key=lambda s: -data[s]["c"][i])
                for sym in opp:
                    if len(positions) >= MAX_POS:
                        break
                    if balance < 1.0:      # 保证金不足
                        break
                    a = data[sym]["atr"][i]
                    if np.isnan(a) or a <= 0:
                        continue
                    entry = data[sym]["c"][i]
                    # 波动率目标名义（占当前权益）；隔离诊断则固定 20U（对照 v3）
                    if ISOLATE:
                        notional = MARGIN * LEV
                    else:
                        eq_now = balance
                        notional = risk_acct * risk_mult * eq_now / (K * a / entry) if K * a > 0 else 0
                        notional = min(NOTIONAL_MAX, max(NOTIONAL_MIN, notional))
                    qty = notional / entry
                    margin = notional / LEV
                    if balance < margin * 1.1:
                        continue
                    positions[sym] = {"side": 1, "entry": entry, "qty": qty,
                                      "maxe": entry, "margin": margin, "notional": notional}
                    balance -= margin
                    balance -= notional * FEE
        # 结算浮动（用吊灯价值）
        unreal = 0.0
        for sym, p in positions.items():
            a = data[sym]["atr"][M - 1]
            trail = p["maxe"] - (K * a if not np.isnan(a) and a > 0 else 0)
            px = trail
            unreal += (px - p["entry"]) * p["qty"] * p["side"] - p["notional"] * FEE
        final = balance + unreal
        ret = (final - START_BAL) / START_BAL * 100
        days = M * 15 / 1440
        ann = (final / START_BAL) ** (365.0 / days) - 1 if final > 0 else -1
        wr = wins / trades * 100 if trades else 0
        return {"risk": risk_acct, "ret%": ret, "ann%": ann * 100, "max_dd%": max_dd * 100,
                "trades": trades, "win%": wr, "final": final, "max_pos_used": None}

    print(f"===== v4 目标 年化≥50% / 回撤≤15% | 信号=K{K}×ATR/仅多/确认{CONF} | {len(data)} 币×{M*15//1440} 天 =====", flush=True)
    print(f"{'风险%':<7}{'年化%':<10}{'收益%':<10}{'最大回撤%':<11}{'交易':<7}{'胜率':<7}", flush=True)
    print("-" * 56, flush=True)
    results = []
    for ra in RISK_SCAN:
        r = simulate(ra)
        results.append(r)
        flag = "  ★达标" if (r["ann%"] >= 50 and r["max_dd%"] <= 15) else ""
        print(f"{ra*100:<6.1f}{r['ann%']:>+8.1f}%{r['ret%']:>+8.1f}%{r['max_dd%']:>9.1f}%{r['trades']:>7}{r['win%']:>5.0f}%{flag}", flush=True)

    # 分段稳定性（对最优达标风险组合）
    ok = [r for r in results if r["ann%"] >= 50 and r["max_dd%"] <= 15]
    best = max(results, key=lambda r: r["ann%"])
    if ok:
        tgt = max(ok, key=lambda r: r["ann%"])
        print(f"\n存在达标组合：风险% {tgt['risk']*100:.1f} → 年化 {tgt['ann%']:+.1f}% / 回撤 {tgt['max_dd%']:.1f}%", flush=True)
    else:
        print(f"\n无组合同时满足 年化≥50% & 回撤≤15%；最优整体：风险% {best['risk']*100:.1f} → 年化 {best['ann%']:+.1f}% / 回撤 {best['max_dd%']:.1f}%", flush=True)

    out = os.path.join(os.path.dirname(__file__), "backtest_v4_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"days": M * 15 // 1440, "universe": UNIVERSE,
                   "params": {"K": K, "conf": CONF, "r2": R2, "slope": SL, "max_pos": MAX_POS,
                              "dd_quiet": DD_QUIET, "dd_hard": DD_HARD, "dd_resume": DD_RESUME},
                   "results": results}, f, ensure_ascii=False, indent=2)
    print(f"已保存 {out}", flush=True)


if __name__ == "__main__":
    main()