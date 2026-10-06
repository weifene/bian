"""
完全复刻实盘逻辑的组合回测
==================================================
与 bot.py 当前逻辑一一对应：
  · 选池：每 4 小时（0/4/8/12/16/20 点，按 K 线真实时间戳对齐）按日内振幅(24h 高-低/现价)
          从「宇宙」里取前 SCAN_TOP_N 只；已持仓币始终并入池、参与评分。
  · 扫描/评分：每次重算 30 分钟窗口（1min×30 根）score = R²×1000 + |斜率|×100000。
          候选需要 R²≥R2_ENTER 且 |斜率|≥SL_MIN 且 score>800。
  · 补仓触发：候选队列 score>800 较上次变化 ≥QUEUE_CHANGE_PCT 或空仓时补仓。
          按评分降序、未持仓、市价开 2U×3x=6U 名义，余额 <2U×1.1 停止，不限制持仓个数。
  · 平仓：持仓期间跟踪峰值评分，当前评分 ≤ 峰值×(1-下降阈值) 即市价平，reason=评分回撤。
          手续费 taker 双边 0.05%。
逐 1 分钟步进，共享余额逐仓占用/释放，完全对齐实盘的组合行为。

用法：python backtest_portfolio.py [UNIVERSE] [DAYS] [DROP...]
  UNIVERSE  宇宙币数（按成交量取前 N，默认 40，用于在池内滚动 top10）
  DAYS      回测天数（1m 数据，默认 3）
  DROP...   可选的下降阈值列表，默认 [0.20,0.25,0.30,0.40,0.50,0.60,0.70]
"""
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, r"d:\bian")
import bot
from bot import client

BARS = 30                 # 评分窗口：1min × 30 根（与 bot.scan_windows 一致）
UNIVERSE = int(sys.argv[1]) if len(sys.argv) > 1 else 40
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 3
_DROP_ARG = sys.argv[3:]
DROP_OPTS = [float(x) for x in _DROP_ARG] if _DROP_ARG else \
    [0.20, 0.25, 0.30, 0.40, 0.50, 0.60, 0.70]

# 引用实盘参数
R2_ENTER = bot.R2_ENTER                     # 0.75
ENTRY_SCORE = bot.ENTRY_SCORE               # 800
QUEUE_CHANGE_PCT = bot.QUEUE_CHANGE_PCT     # 0.10
LEV = bot.LEVERAGE                          # 3
MARGIN = bot.POS_MARGIN_USDT                # 2U
FEE = bot.TAKER_FEE_RATE                    # 0.05% 每边
SL_MIN = bot.SLOPE_MIN_PCT * bot.interval_minutes() / 15.0   # 1min 窗口进场最小斜率
POOL_N = min(bot.SCAN_TOP_N, UNIVERSE)      # top10（不超过宇宙）
START_BAL = 100.0                           # 初始余额（回测用，方便折算 %）
SEG_MS = 4 * 3600 * 1000                    # 4 小时段（选池周期）


def retry(fn, n=5):
    for i in range(n):
        try:
            return fn()
        except Exception:
            if i == n - 1:
                raise
            time.sleep(2 + i * 2)


def fetch_ohlc(symbol, limit=DAYS * 1440):
    """分页拉 1m OHLC，返回 (times_ms[], open[], close[], high[], low[]) 从旧到新。"""
    ks, end = [], None
    while len(ks) < limit:
        batch = retry(lambda: client.futures_klines(
            symbol=symbol, interval=bot.Client.KLINE_INTERVAL_1MINUTE, limit=1500, endTime=end))
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


def rolling_reg(prices, w):
    """滑窗线性回归 -> (slope_pct[], r2[])，开头 w-1 个为 NaN。"""
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


def score_of(r2, slope):
    return r2 * 1000 + abs(slope) * 100000


def queue_change_pct(a, b):
    """队列同并集变化率，与 bot.queue_change_pct 一致。"""
    u = set(a) | set(b)
    if not u:
        return 0.0
    inter = set(a) & set(b)
    return (len(u) - len(inter)) / len(u)


def amplitude_at(hi, lo, cl, t, win=1440):
    """24h 振幅%：(窗口中最高-最低)/现价。"""
    j0 = max(0, t - win + 1)
    h = float(np.max(hi[j0:t + 1]))
    l = float(np.min(lo[j0:t + 1]))
    px = cl[t]
    return (h - l) / px * 100 if px > 0 else 0.0


def main():
    print(f"[portfolio] 拉取成交量前 {UNIVERSE} 币 × {DAYS} 天 1m OHLC …", flush=True)
    info = retry(client.futures_exchange_info)
    perps = [s["symbol"] for s in info["symbols"]
             if s["status"] == "TRADING" and s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL"]
    ticks = retry(client.futures_ticker)
    vol = {t["symbol"]: float(t["quoteVolume"]) for t in ticks if t["symbol"] in perps}
    uni = sorted(vol, key=vol.get, reverse=True)[:UNIVERSE]

    data = {}
    n = None
    for sym in uni:
        try:
            times, op, hi, lo, cl = fetch_ohlc(sym)
            if len(cl) < DAYS * 1440 * 0.8:
                continue
            slope, r2 = rolling_reg(cl, BARS)
            data[sym] = {"times": times, "open": op, "hi": hi, "lo": lo, "close": cl,
                         "slope": slope, "r2": r2,
                         "score": score_of(r2, slope)}
            n = len(cl) if n is None else min(n, len(cl))
        except Exception as e:
            print(f"  skip {sym}: {e}", flush=True)
        time.sleep(0.03)

    if not data:
        print("无数据")
        return
    print(f"  已加载 {len(data)} 币，模拟 {n} 个 1 分钟棒", flush=True)

    # ============ 组合模拟（参数化进场 / 平仓阈值） ============
    def simulate(drop, r2_enter, slope_min, entry_score):
        balance = START_BAL
        positions = {}      # sym -> {side, entry, qty, peak}
        prev_queue = set()
        pool = None
        last_seg = None
        trades = wins = max_pos = 0
        for t in range(n):
            # ---- 选池轮换（4 小时段，按真实时间戳对齐）----
            seg = int(data[list(data.keys())[0]]["times"][t] // SEG_MS)
            if seg != last_seg:
                last_seg = seg
                amps = {s: amplitude_at(data[s]["hi"], data[s]["lo"], data[s]["close"], t)
                        for s in data}
                pool = sorted(data, key=lambda s: -amps[s])[:POOL_N]

            # ---- 一、管理持仓：评分回撤平仓 ----
            for sym in list(positions.keys()):
                sc = data[sym]["score"][t]
                pos = positions[sym]
                if sc > pos["peak"]:
                    pos["peak"] = sc
                elif pos["peak"] > 0 and sc <= pos["peak"] * (1 - drop):
                    close = data[sym]["close"][t]
                    pnl = (close - pos["entry"]) * pos["qty"] * (1 if pos["side"] == "LONG" else -1) \
                          - 2 * pos["notional"] * FEE
                    balance += pos["margin"] + pnl
                    trades += 1
                    if pnl > 0:
                        wins += 1
                    del positions[sym]

            # ---- 二、扫描当前池 + 已持仓 ----
            scanned = set(pool) | set(positions.keys())
            cur_queue = set()
            for sym in scanned:
                r2v = data[sym]["r2"][t]
                slv = data[sym]["slope"][t]
                if np.isnan(r2v):
                    continue
                if r2v >= r2_enter and abs(slv) >= slope_min and data[sym]["score"][t] > entry_score:
                    cur_queue.add(sym)
            chg = queue_change_pct(prev_queue, cur_queue)
            if cur_queue:
                prev_queue = cur_queue

            # ---- 三、队列变化≥阈值 或 空仓 -> 补仓 ----
            if chg >= QUEUE_CHANGE_PCT or len(positions) == 0:
                cands = sorted(cur_queue - set(positions.keys()),
                               key=lambda s: -data[s]["score"][t])
                for sym in cands:
                    if balance < MARGIN * 1.1:
                        break
                    sc = data[sym]["score"][t]
                    direction = 1 if data[sym]["slope"][t] > 0 else -1
                    entry = data[sym]["close"][t]
                    notional = MARGIN * LEV
                    positions[sym] = {
                        "side": "LONG" if direction > 0 else "SHORT",
                        "entry": entry,
                        "qty": notional / entry,
                        "peak": sc,
                        "margin": MARGIN,
                        "notional": notional,
                    }
                    balance -= MARGIN          # 占用保证金
                    balance -= notional * FEE  # 开仓手续费
            max_pos = max(max_pos, len(positions))

        unreal = 0.0
        for sym, pos in positions.items():
            close = data[sym]["close"][n - 1]
            side = 1 if pos["side"] == "LONG" else -1
            unreal += (close - pos["entry"]) * pos["qty"] * side - pos["notional"] * FEE
        final_bal = balance + unreal
        ret_pct = (final_bal - START_BAL) / START_BAL * 100
        wr = wins / trades * 100 if trades else 0
        return {"drop": drop, "r2": r2_enter, "slope": slope_min, "es": entry_score,
                "ret%": ret_pct, "trades": trades, "win_rate%": wr,
                "max_pos": max_pos, "final": final_bal}

    # ---- A. 逐下降阈值（基线进场参数）----
    print(f"\n[A] 平仓阈值扫描 | 池 top{POOL_N}/4h | 2U×{LEV}x | 队列变更≥{QUEUE_CHANGE_PCT*100:.0f}%", flush=True)
    print(f"{'下降%':<7}{'收益%':<10}{'交易数':<8}{'胜率':<7}{'峰仓':<6}", flush=True)
    print("-" * 44, flush=True)
    res_drop = [simulate(d, R2_ENTER, SL_MIN, ENTRY_SCORE) for d in DROP_OPTS]
    for r in res_drop:
        print(f"{r['drop']*100:<6.0f}%{r['ret%']:>+8.2f}%{r['trades']:>8}{r['win_rate%']:>6.0f}%{r['max_pos']:>5}", flush=True)

    # ---- B. 收紧进场参数（固定用 A 中最优平仓阈值）----
    best_drop = max(res_drop, key=lambda r: r["ret%"])["drop"]
    R2S = [0.75, 0.80, 0.88, 0.95]
    SL_MULT = [1, 2, 4, 8]                      # SL_MIN 的倍数（收紧斜率）
    ESS = [800, 950, 1100]
    print(f"\n[B] 收紧进场参数 | 固定平仓阈值 {best_drop*100:.0f}%（A 中最优）", flush=True)
    print(f"{'R2':<6}{'斜率x':<7}{'评分':<6}{'收益%':<10}{'交易数':<8}{'胜率':<7}{'峰仓':<6}", flush=True)
    print("-" * 52, flush=True)
    res_entry = []
    for r2 in R2S:
        for sm in SL_MULT:
            for es in ESS:
                r = simulate(best_drop, r2, SL_MIN * sm, es)
                res_entry.append((r2, sm, es, r))
                print(f"{r2:<6.2f}{sm:<7}{es:<6}{r['ret%']:>+8.2f}%{r['trades']:>8}{r['win_rate%']:>6.0f}%{r['max_pos']:>5}", flush=True)

    rows = sorted(res_drop, key=lambda r: -r["ret%"])
    print(f"\n[A] 最优平仓阈值：{rows[0]['drop']*100:.0f}%（收益 {rows[0]['ret%']:+.2f}%，交易 {rows[0]['trades']}）", flush=True)
    ebest = sorted(res_entry, key=lambda x: -x[3]['ret%'])[0]
    print(f"[B] 最优进场参数：R²={ebest[0]} 斜率×{ebest[1]} 评分>{ebest[2]} → 收益 {ebest[3]['ret%']:+.2f}%"
          f"，交易 {ebest[3]['trades']}，胜率 {ebest[3]['win_rate%']:.0f}%，峰仓 {ebest[3]['max_pos']}", flush=True)

    out = os.path.join(os.path.dirname(__file__), "backtest_portfolio_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "days": DAYS, "universe": len(data), "pool_n": POOL_N, "margin": MARGIN,
            "start_balance": START_BAL,
            "drop_scan": [dict(r) for r in res_drop],
            "entry_scan": [dict(r) for (_r2, _sm, _es, r) in res_entry],
            "best_drop": {"drop%": rows[0]["drop"]*100, "ret%": rows[0]["ret%"]},
            "best_entry": {"r2": ebest[0], "slope_x": ebest[1], "entry_score": ebest[2], "ret%": ebest[3]["ret%"]},
        }, f, ensure_ascii=False, indent=2)
    print(f"汇总已保存：{out}", flush=True)


if __name__ == "__main__":
    main()