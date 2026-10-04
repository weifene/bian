# -*- coding: utf-8 -*-
"""
策略矩阵回测：针对 SOLUSDT，测试多种策略变体，找出震荡市下最优改进。
数据：15m K线，拉取最近 4000 根（约 42 天），避免偶然性。
"""
import os
import math
import requests

os.environ["HTTP_PROXY"] = "socks5h://127.0.0.1:7892"
os.environ["HTTPS_PROXY"] = "socks5h://127.0.0.1:7892"
os.environ["ALL_PROXY"] = "socks5h://127.0.0.1:7892"

SYMBOL = "SOLUSDT"
FEE = 0.001  # 单边手续费 0.1%


def get_klines(symbol, interval="15m", total=4000):
    out = []
    limit = 1000
    end = None
    for _ in range(total // limit):
        params = {"symbol": symbol, "interval": interval, "limit": limit}
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
    out = []
    s = 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= period:
            s -= closes[i - period]
        out.append(s / period if i >= period - 1 else None)
    return out


def rsi_series(closes, period=14):
    out = [None] * period
    gains, losses = [], []
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0))
        losses.append(max(-chg, 0))
        if i >= period:
            ag = sum(gains[i - period:i]) / period
            al = sum(losses[i - period:i]) / period
            out.append(100 - 100 / (1 + (ag / al if al else 1e9)))
    return out


def run(closes, fast=5, slow=20, use_trend=False, trend_period=200,
        use_rsi=False, rsi_max=70, tp=None, sl=None):
    """返回 (交易次数, 累计收益率%)，全仓，止盈止损按持仓价计算"""
    fast_s = sma_series(closes, fast)
    slow_s = sma_series(closes, slow)
    trend_s = sma_series(closes, trend_period) if use_trend else None
    rsi = rsi_series(closes) if use_rsi else None
    equity, holding, trades = 1.0, False, 0
    entry_price = None
    for i in range(slow, len(closes)):
        if fast_s[i] is None or slow_s[i] is None:
            continue
        price = closes[i]
        if trend_s is not None and trend_s[i] is None:
            continue
        # 止盈止损检查（持仓中）
        if holding and (tp or sl):
            chg = (price - entry_price) / entry_price
            if (tp and chg >= tp) or (sl and chg <= -sl):
                equity *= price / closes[i - 1] * (1 - FEE)
                holding = False
                trades += 1
                continue
        trend_ok = (trend_s is None) or (price > trend_s[i])
        rsi_ok = (rsi is None) or (rsi[i] is not None and rsi[i] < rsi_max)
        if fast_s[i] > slow_s[i] and not holding and trend_ok and rsi_ok:
            equity *= (1 - FEE)
            holding = True
            trades += 1
            entry_price = price
        elif fast_s[i] < slow_s[i] and holding:
            equity *= price / closes[i - 1] * (1 - FEE)
            holding = False
            trades += 1
    if holding:
        equity *= closes[-1] / closes[-2] * (1 - FEE)
    return trades, (equity - 1) * 100


klines = get_klines(SYMBOL)
closes = [float(k[4]) for k in klines]
print(f"{SYMBOL} 数据：{len(closes)} 根 15m K 线（约 {len(closes)*15/1440:.1f} 天）\n")

variants = [
    ("A 原版双均线 5/20",       dict()),
    ("B 双均线+200SMA趋势过滤", dict(use_trend=True, trend_period=200)),
    ("C 双均线+RSI<70过滤",     dict(use_rsi=True, rsi_max=70)),
    ("D 双均线+趋势+RSI",       dict(use_trend=True, trend_period=200, use_rsi=True, rsi_max=70)),
    ("E 均线 10/30",            dict(fast=10, slow=30)),
    ("F 双均线+2%止盈/1%止损",  dict(tp=0.02, sl=0.01)),
    ("G 双均线+趋势+止盈止损",  dict(use_trend=True, trend_period=200, tp=0.02, sl=0.01)),
]

print(f"{'策略':<26}{'交易次数':<10}{'收益率%':<12}")
print("-" * 50)
for name, kw in variants:
    trades, ret = run(closes, **kw)
    print(f"{name:<26}{trades:<10}{ret:<12.2f}")
