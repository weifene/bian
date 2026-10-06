"""
斜率趋势策略参数网格回测：为 4 个趋势窗口（1h/3h/6h/12h）分别搜索盈利最高的
"趋势干净度"标准（进场 R² 阈值 / 进场斜率基准阈值 / 离场 R² 阈值）。

与 bot.py 逻辑对齐：
- 进场：R² >= r2_enter 且 |斜率| >= slope_base * (窗口聚合周期分钟/15)
- 离场：R² < r2_exit 或 |斜率| < 0.0002（走平）或动态回撤 >= 10%
- 全仓：3x 杠杆 × 95% 保证金；手续费每边 0.02%（maker）
- 数据：每币拉 4320 根 1m（≈3 天，3 次分页），本地聚合出各窗口 K 线序列

用法：python backtest_grid.py
输出：每窗口 Top 组合 + 汇总 JSON（backtest_grid_result.json）
"""
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, r"d:\bian")
import bot
from bot import client

WINS = [("h1", 1, 60), ("h3", 3, 60), ("h6", 5, 72), ("h12", 15, 48)]   # 窗口名, 聚合周期(分钟), 窗口根数
TOP_N = 30            # 回测币种数（成交量前 N）
DAYS = 3              # 回测数据天数（1m 根数 = DAYS*1440）
FEE = bot.MAKER_FEE_RATE
LEV = bot.LEVERAGE
POS_RATIO = bot.POSITION_RATIO
TRAIL = bot.TRAIL_PCT
SLOPE_EXIT = bot.SLOPE_EXIT_MIN

# 参数网格
R2_ENTER_OPTS = [0.5, 0.6, 0.7, 0.8, 0.9]          # 进场 R² 阈值
SLOPE_BASE_OPTS = [0.0002, 0.0004, 0.0006, 0.0008, 0.0012]  # 基准斜率阈值（15m 口径，按周期折算）
R2_EXIT_OPTS = [0.3, 0.45]                          # 离场 R² 阈值


def retry(fn, n=5):
    """API 调用重试：代理间歇性 SSL EOF 时自动重连（备用域名间歇性拒绝）"""
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
    cs = np.concatenate([[0.0], np.cumsum(p)])              # 前缀和
    cj = np.concatenate([[0.0], np.cumsum(p * np.arange(1, n + 1))])  # 前缀和(j*y_j)，j 从 1 起
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


def simulate(close, slope, r2, r2_enter, slope_base, m, r2_exit):
    """单窗口单参数组合回测。返回 (总收益, 交易数, 胜率, 平均每笔%)"""
    sl_min = slope_base * m / 15.0
    n = len(close)
    pos, entry, best = 0, 0.0, 0.0
    eq = 1.0
    trades, wins = 0, 0
    for i in range(n):
        if np.isnan(r2[i]):
            continue
        if pos == 0:
            if r2[i] >= r2_enter and abs(slope[i]) >= sl_min:
                pos = 1 if slope[i] > 0 else -1
                entry = close[i]
                best = close[i]
        else:
            # 动态回撤跟踪
            if pos == 1:
                best = max(best, close[i])
                trail_hit = close[i] <= best * (1 - TRAIL)
            else:
                best = min(best, close[i])
                trail_hit = close[i] >= best * (1 + TRAIL)
            if r2[i] < r2_exit or abs(slope[i]) < SLOPE_EXIT or trail_hit:
                ret = (close[i] / entry - 1) * pos * LEV * POS_RATIO - 2 * FEE
                eq *= (1 + ret)
                trades += 1
                if ret > 0:
                    wins += 1
                pos = 0
    return eq - 1, trades, (wins / trades if trades else 0.0), (eq - 1) / trades if trades else 0.0


def agg_prices(closes_1m, step, bars):
    """1m 序列聚合为 step 分钟收盘序列（每 step 根取最后一根）。
    返回全部聚合序列（3 天数据：h1≈4320 根、h3≈1440、h6≈864、h12≈288），
    滑窗回归在完整序列上滑动以产生足够多的信号点。"""
    src = closes_1m
    agg = [src[i] for i in range(step - 1, len(src), step)]
    return np.asarray(agg, dtype=float)


def main():
    print(f"[backtest_grid] 拉取成交量前 {TOP_N} 币的 {DAYS} 天 1m 数据，聚合 4 窗口做参数网格回测…", flush=True)
    info = retry(client.futures_exchange_info)
    perps = [s["symbol"] for s in info["symbols"]
             if s["status"] == "TRADING" and s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL"]
    ticks = retry(client.futures_ticker)
    vol = {t["symbol"]: float(t["quoteVolume"]) for t in ticks if t["symbol"] in perps}
    cand = sorted(vol, key=vol.get, reverse=True)[:TOP_N]

    # 数据缓存：每个币的 4 窗口价格序列
    data = {}  # sym -> {win: close_array}
    for sym in cand:
        try:
            c1m = fetch_1m(sym)
            if len(c1m) < DAYS * 1440 * 0.8:
                print(f"  跳过 {sym}（数据不足 {len(c1m)} 根）", flush=True)
                continue
            data[sym] = {win: agg_prices(c1m, step, bars) for win, step, bars in WINS}
            print(f"  已加载 {sym}（{len(c1m)} 根 1m）", flush=True)
        except Exception as e:
            print(f"  跳过 {sym}：{e}", flush=True)
        time.sleep(0.03)

    if not data:
        print("没有可用数据")
        return

    combos = [(re_, sb, rx) for re_ in R2_ENTER_OPTS for sb in SLOPE_BASE_OPTS for rx in R2_EXIT_OPTS]
    print(f"\n参数组合数：{len(combos)} × {len(WINS)} 窗口 × {len(data)} 币，开始网格回测…", flush=True)

    result = {}
    for win, step, bars in WINS:
        agg_ret = []
        for re_, sb, rx in combos:
            rets, trs = [], []
            for sym, win_data in data.items():
                arr = win_data[win]
                slope, r2 = rolling_reg(arr, bars)
                if np.sum(~np.isnan(r2)) < bars:
                    continue
                ret, tr, wr, apr = simulate(arr, slope, r2, re_, sb, step, rx)
                rets.append(ret)
                trs.append(tr)
            if rets:
                agg_ret.append({
                    "r2_enter": re_, "slope_base": sb, "r2_exit": rx,
                    "avg_ret%": float(np.mean(rets) * 100),
                    "med_ret%": float(np.median(rets) * 100),
                    "win_coins%": float(np.mean([1 if r > 0 else 0 for r in rets]) * 100),
                    "avg_trades": float(np.mean(trs)),
                })
        agg_ret.sort(key=lambda x: -x["avg_ret%"])
        result[win] = agg_ret
        print(f"\n===== 窗口 {win}（{step}min×{bars}）Top5 组合 =====", flush=True)
        for c in agg_ret[:5]:
            print(f"  R2进{c['r2_enter']:.2f} 斜基{c['slope_base']:.4f} R2出{c['r2_exit']:.2f} → "
                  f"平均{c['avg_ret%']:.2f}% 中位{c['med_ret%']:.2f}% 盈利币种{c['win_coins%']:.0f}% 均交易{c['avg_trades']:.0f}", flush=True)

    out = os.path.join(os.path.dirname(__file__), "backtest_grid_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n汇总已保存：{out}", flush=True)


if __name__ == "__main__":
    main()
