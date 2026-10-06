"""
评分制卖出回测（快速版 · 仅 1 小时窗口）
==================================================
背景：现机器人卖出标准是固定阈值（R²<0.40 或 |斜率|<0.0002），
     等价于评分跌破约 420（R2_EXIT×1000 + SLOPE_EXIT_MIN×100000）即卖出。
目标：改为评分制——持仓期间跟踪入场以来最高评分（峰值 = R²×1000 + |斜率|×100000），
     当前评分较峰值下降 X% 时卖出。通过回测确定 X 取多少收益最优。

只回测 1 小时窗口（1m×60 根回归），每币只拉一次 1m K 线，速度远快于 backtest_grid。

用法：python backtest_score.py [TOP_N] [DAYS]
  TOP_N  回测币种数（成交量前 N），默认 30
  DAYS   回测数据天数（1m 数据），默认 3
"""
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, r"d:\bian")
import bot
from bot import client

WIN = "h1"              # 只回测 1 小时窗口
STEP, BARS = 1, 60      # 1m 聚合周期 × 60 根
TOP_N = int(sys.argv[1]) if len(sys.argv) > 1 else 30
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 3

FEE = bot.TAKER_FEE_RATE     # 每边 0.05%（现机器人市价下单 = taker）
LEV = bot.LEVERAGE            # 3x
R2_ENTER = bot.R2_ENTER       # 0.75
SL_MIN = bot.SLOPE_MIN_PCT * STEP / 15.0   # 1h 窗口进场最小斜率（按 1m 周期折算）
SL_EXIT = bot.SLOPE_EXIT_MIN * STEP / 15.0  # 1h 窗口离场最小斜率

# 评分下降阈值网格（0.05 = 评分较峰值下降 5% 即卖出）
DROP_OPTS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.70]


def retry(fn, n=5):
    """API 调用重试：代理间歇性 SSL EOF 时自动重连"""
    for i in range(n):
        try:
            return fn()
        except Exception as e:
            if i == n - 1:
                raise
            time.sleep(2 + i * 2)


def fetch_1m(symbol, limit=DAYS * 1440):
    """分页拉取 1m 收盘价，时间从新到旧合并；每批失败自动重试"""
    ks, end = [], None
    while len(ks) < limit:
        batch = retry(lambda: client.futures_klines(
            symbol=symbol, interval=bot.Client.KLINE_INTERVAL_1MINUTE, limit=1500, endTime=end))
        if not batch:
            break
        ks = batch + ks
        end = batch[0][0] - 1
        time.sleep(0.02)
    return [float(k[4]) for k in ks[-limit:]]


def rolling_reg(prices, w):
    """滑窗线性回归。返回 (slope_pct[], r2[])，长度 n；开头 w-1 个为 NaN。
    slope_pct = 斜率/均值（每根 K 线价格变动比例）。"""
    p = np.asarray(prices, dtype=float)
    n = len(p)
    slope = np.full(n, np.nan)
    r2 = np.full(n, np.nan)
    if n < w:
        return slope, r2
    xs = np.arange(w, dtype=float)
    sx = xs.sum()
    sxx = (xs * xs).sum()
    cs = np.concatenate([[0.0], np.cumsum(p)])
    cj = np.concatenate([[0.0], np.cumsum(p * np.arange(1, n + 1))])
    denom = sxx - sx * sx / w
    for i in range(w, n + 1):
        j0 = i - w
        y = p[j0:i]
        sy = cs[i] - cs[j0]
        sxy = (cj[i] - cj[j0]) - (j0 + 1) * sy
        mean_y = sy / w
        sl = (sxy - sx * sy / w) / denom
        itc = mean_y - sl * sx / w
        ss_res = float(np.sum((y - (sl * xs + itc)) ** 2))
        ss_tot = float(np.sum((y - mean_y) ** 2))
        slope[i - 1] = sl / mean_y if mean_y else 0.0
        r2[i - 1] = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, r2


def score_of(r2, slope_pct):
    """评分 = R²×1000 + |斜率|×100000（与 bot.py scan_windows / 界面一致）"""
    return r2 * 1000 + abs(slope_pct) * 100000


def simulate_score_drop(close, r2, slope, drop_pct):
    """评分制卖出：进场后跟踪最高评分，当前评分较峰值下降 drop_pct 即卖出。
    返回 (总收益, 交易数, 胜率)"""
    n = len(close)
    pos, entry, peak = 0, 0.0, 0.0
    eq = 1.0
    trades = wins = 0
    for i in range(n):
        if np.isnan(r2[i]):
            continue
        sc = score_of(r2[i], slope[i])
        if pos == 0:
            if r2[i] >= R2_ENTER and abs(slope[i]) >= SL_MIN:
                pos = 1 if slope[i] > 0 else -1
                entry = close[i]
                peak = sc
        else:
            if sc > peak:
                peak = sc
            elif peak > 0 and sc <= peak * (1 - drop_pct):
                ret = (close[i] / entry - 1) * pos * LEV - 2 * FEE
                eq *= (1 + ret)
                trades += 1
                if ret > 0:
                    wins += 1
                pos = 0
    return eq - 1, trades, (wins / trades if trades else 0.0)


def simulate_fixed_line(close, r2, slope):
    """现标准卖出（对照组）：R² < R2_EXIT 或 |斜率| < SL_EXIT 即卖出"""
    n = len(close)
    pos, entry = 0, 0.0
    eq = 1.0
    trades = wins = 0
    for i in range(n):
        if np.isnan(r2[i]):
            continue
        if pos == 0:
            if r2[i] >= R2_ENTER and abs(slope[i]) >= SL_MIN:
                pos = 1 if slope[i] > 0 else -1
                entry = close[i]
        else:
            if r2[i] < bot.R2_EXIT or abs(slope[i]) < SL_EXIT:
                ret = (close[i] / entry - 1) * pos * LEV - 2 * FEE
                eq *= (1 + ret)
                trades += 1
                if ret > 0:
                    wins += 1
                pos = 0
    return eq - 1, trades, (wins / trades if trades else 0.0)


def main():
    print(f"[backtest_score] 拉取成交量前 {TOP_N} 币的 {DAYS} 天 1m 数据（仅 1h 窗口 {STEP}min×{BARS} 根）…", flush=True)
    info = retry(client.futures_exchange_info)
    perps = [s["symbol"] for s in info["symbols"]
             if s["status"] == "TRADING" and s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL"]
    ticks = retry(client.futures_ticker)
    vol = {t["symbol"]: float(t["quoteVolume"]) for t in ticks if t["symbol"] in perps}
    cand = sorted(vol, key=vol.get, reverse=True)[:TOP_N]

    close_data = {}
    for sym in cand:
        try:
            c1m = fetch_1m(sym)
            if len(c1m) < DAYS * 1440 * 0.8:
                print(f"  跳过 {sym}（数据不足 {len(c1m)} 根）", flush=True)
                continue
            close_data[sym] = np.asarray(c1m, dtype=float)
            print(f"  已加载 {sym}（{len(c1m)} 根 1m）", flush=True)
        except Exception as e:
            print(f"  跳过 {sym}：{e}", flush=True)
        time.sleep(0.03)

    if not close_data:
        print("没有可用数据")
        return

    # 基线：现固定阈值卖出
    base_rets = []
    for sym, arr in close_data.items():
        slope, r2 = rolling_reg(arr, BARS)
        if np.sum(~np.isnan(r2)) < BARS:
            continue
        ret, _, _ = simulate_fixed_line(arr, r2, slope)
        base_rets.append(ret)
    base_avg = float(np.mean(base_rets)) * 100

    print(f"\n===== 1h 窗口评分制卖出回测 | {len(close_data)} 币 × {DAYS} 天 | 进场 R²≥{R2_ENTER} 斜率≥{SL_MIN:.6f} =====", flush=True)
    print(f"对照组（现标准：R²<{bot.R2_EXIT} 或 |斜率|<{SL_EXIT:.6f} 卖出）→ 平均收益 {base_avg:+.2f}%", flush=True)
    print(f"\n{'下降阈值':<8}{'平均收益%':<12}{'中位收益%':<12}{'盈利币种%':<10}{'总交易':<8}{'胜率':<8}", flush=True)
    print("-" * 58, flush=True)

    result = {}
    rows = []
    for drop in DROP_OPTS:
        rets, trs, wrs = [], [], []
        for sym, arr in close_data.items():
            slope, r2 = rolling_reg(arr, BARS)
            if np.sum(~np.isnan(r2)) < BARS:
                continue
            ret, tr, wr = simulate_score_drop(arr, r2, slope, drop)
            rets.append(ret)
            trs.append(tr)
            wrs.append(wr)
        avg = float(np.mean(rets)) * 100
        med = float(np.median(rets)) * 100
        winc = float(np.mean([1 if r > 0 else 0 for r in rets])) * 100
        tot_tr = int(np.sum(trs))
        wr = float(np.mean(wrs)) * 100
        rows.append((drop, avg, med, winc, tot_tr, wr))
        result[str(drop)] = {"avg_ret%": avg, "med_ret%": med, "win_coins%": winc,
                             "total_trades": tot_tr, "win_rate%": wr}
        print(f"{drop*100:<6.0f}%{avg:>+10.2f}%{med:>+10.2f}%{winc:>9.0f}%{tot_tr:>8}{wr:>7.0f}%", flush=True)

    rows.sort(key=lambda r: -r[1])
    best = rows[0]
    print(f"\n最优下降阈值：{best[0]*100:.0f}%（平均收益 {best[1]:+.2f}%，中位 {best[2]:+.2f}%，"
          f"盈利币种 {best[3]:.0f}%，总交易 {best[4]} 笔）", flush=True)

    out = os.path.join(os.path.dirname(__file__), "backtest_score_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"window": WIN, "bars": BARS, "days": DAYS, "coins": len(close_data),
                   "baseline_fixed_line_avg%": base_avg, "results": result,
                   "best": {"drop_pct": best[0], "avg_ret%": best[1]}}, f, ensure_ascii=False, indent=2)
    print(f"\n汇总已保存：{out}", flush=True)


if __name__ == "__main__":
    main()
