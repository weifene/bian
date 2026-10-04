# -*- coding: utf-8 -*-
"""
合约双向策略回测：fast>slow 持多，fast<slow 持空，始终在场。
用现货 15m K线数据（合约K线与现货K线同源）。
手续费：合约 taker 单边 0.05%（往返 0.1%）。
"""
import os
import math
import requests

os.environ["HTTP_PROXY"] = "socks5h://127.0.0.1:7892"
os.environ["HTTPS_PROXY"] = "socks5h://127.0.0.1:7892"
os.environ["ALL_PROXY"] = "socks5h://127.0.0.1:7892"

CANDIDATES = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "PEPEUSDT",
              "SUIUSDT", "XRPUSDT", "AVAXUSDT", "LINKUSDT", "WIFUSDT",
              "ADAUSDT", "LTCUSDT", "FILUSDT", "ARBUSDT"]
FEE = 0.001  # 往返 0.1%


def get_klines(symbol, total=4000):
    out, limit, end = [], 1000, None
    for _ in range(total // limit):
        params = {"symbol": symbol, "interval": "15m", "limit": limit}
        if end:
            params["endTime"] = end
        r = requests.get("https://api1.binance.com/api/v3/klines", params=params, timeout=20)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out = batch + out
        end = int(batch[0][0]) - 1
    return out


def sma_series(closes, period):
    out, s = [], 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= period:
            s -= closes[i - period]
        out.append(s / period if i >= period - 1 else None)
    return out


def backtest_dual(closes, fast=5, slow=20):
    """双向始终在场：fast>slow 持多，fast<slow 持空"""
    fast_s = sma_series(closes, fast)
    slow_s = sma_series(closes, slow)
    equity, pos = 1.0, 0  # pos: 0空仓 1多 -1空
    trades = 0
    for i in range(slow, len(closes)):
        if fast_s[i] is None or slow_s[i] is None:
            continue
        ret = closes[i] / closes[i - 1] - 1
        target = 1 if fast_s[i] > slow_s[i] else -1
        if target != pos:
            if pos != 0:
                equity *= (1 - FEE)  # 平仓手续费
            pos = target
            equity *= (1 - FEE)  # 开仓手续费
            trades += 1
        equity *= (1 + ret * pos)  # 持仓收益
    return trades, (equity - 1) * 100


print(f"{'币种':<10}{'交易次数':<10}{'双向策略收益%':<14}")
print("-" * 44)
results = []
for s in CANDIDATES:
    try:
        closes = [float(k[4]) for k in get_klines(s)]
        trades, ret = backtest_dual(closes)
        results.append((s, trades, ret))
        print(f"{s:<10}{trades:<10}{ret:<14.2f}")
    except Exception as e:
        print(f"{s:<10} 出错: {e}")

print("\n=== 按收益排序 ===")
for s, t, r in sorted(results, key=lambda x: x[2], reverse=True):
    print(f"{s:<10} 收益 {r:+.2f}% | 交易 {t} 次")
