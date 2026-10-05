"""
双均线策略选币回测（与 bot.py 同参数）
- 扫描币安 U 本位合约成交量前 100 的币
- 用最近 2 天（192 根 15 分钟 K 线）回测
- 策略：金叉开多 / 死叉开空，始终持仓（永不空仓），与 bot.py 完全一致
- 手续费按每边 0.02%（maker）估算，与 bot.py 的 MAKER_FEE_RATE 一致

用法：python backtest.py
"""
import os
import sys
import time

sys.path.insert(0, r"d:\bian")
import bot
from bot import client

FAST = bot.FAST_PERIOD          # 5
SLOW = bot.SLOW_PERIOD          # 20
FEE = bot.MAKER_FEE_RATE        # 每边 0.02%（maker）
BARS = 1500                     # 拉取 K 线数量（1m×1500≈25 小时，3 次分页可扩到 3 天）
PERIOD = 1440                   # 回测窗口：最近 24 小时（1440 根 1 分钟 K 线）


def backtest(closes, fast=FAST, slow=SLOW, fee=FEE):
    """始终持仓的双均线策略回测。返回 (策略总收益%, 交易数, 胜场数, 盈亏比)"""
    n = len(closes)
    pos, entry, trades = 0, 0.0, []
    for i in range(slow - 1, n):
        f = sum(closes[i - fast + 1:i + 1]) / fast
        s = sum(closes[i - slow + 1:i + 1]) / slow
        sig = 1 if f > s else -1
        if pos == 0:
            pos, entry = sig, closes[i]
        elif sig != pos:
            # 换仓：平旧仓(1边手续费) + 开新仓(1边手续费)
            trades.append((pos, entry, closes[i], (closes[i] / entry - 1) * pos - 2 * fee))
            pos, entry = sig, closes[i]
    if pos:
        # 最后一笔未平仓，按最新价结算，只收平仓 1 边手续费
        trades.append((pos, entry, closes[-1], (closes[-1] / entry - 1) * pos - fee))
    total = 1.0
    for _, _, _, r in trades:
        total *= (1 + r)
    wins = sum(1 for t in trades if t[3] > 0)
    gains = sum(t[3] for t in trades if t[3] > 0)
    losses = -sum(t[3] for t in trades if t[3] <= 0)
    pf = gains / losses if losses > 0 else float("inf")
    return total - 1, len(trades), wins, pf


info = client.futures_exchange_info()
perps = [s["symbol"] for s in info["symbols"]
         if s["status"] == "TRADING" and s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL"]
ticks = client.futures_ticker()  # GET /fapi/v1/ticker/24hr，含 24h 成交量
vol = {t["symbol"]: float(t["quoteVolume"]) for t in ticks if t["symbol"] in perps}
cand = sorted(vol, key=vol.get, reverse=True)[:100]

results = []
for sym in cand:
    try:
        ks = client.futures_klines(symbol=sym, interval=bot.INTERVAL, limit=BARS)
        full = [float(k[4]) for k in ks]
        closes = full[SLOW - 1:][-PERIOD:]  # 预热 19 根后，取最近 2 天
        if len(closes) < PERIOD:
            continue
        ret, n_tr, wins, pf = backtest(closes)
        bh = closes[-1] / closes[0] - 1  # 买入持有对照
        results.append((sym, ret * 100, bh * 100, n_tr, wins, pf))
    except Exception:
        pass
    time.sleep(0.05)

results.sort(key=lambda r: r[1], reverse=True)

print(f"\n=== 双均线策略({bot.INTERVAL} MA{FAST}/{SLOW}) 最近24小时回测 | 成交量前100币 | 手续费每边{FEE*100:.2f}% ===")
print(f"{'排名':<4}{'交易对':<10}{'策略收益%':<11}{'买入持有%':<11}{'交易数':<6}{'胜率':<8}{'盈亏比':<8}")
for i, (sym, ret, bh, n, wins, pf) in enumerate(results[:20], 1):
    mark = " ← 当前" if sym == bot.SYMBOL else ""
    pf_s = "∞" if pf >= 99 else f"{pf:.2f}"
    print(f"{i:<4}{sym:<10}{ret:>8.2f}%  {bh:>8.2f}%  {n:<6}{wins / n * 100:>5.0f}%  {pf_s:>7}{mark}")
print(f"\n共扫描 {len(results)} 个币（其余因数据不足跳过）")
