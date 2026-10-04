# -*- coding: utf-8 -*-
"""
候选币种分析：针对双均线（5/20，15分钟K线）策略，用币安真实历史数据回测对比。
指标：
  - 波动率：15分钟收益的标准差（年化前）
  - 平均振幅：每根K线 (high-low)/close 均值
  - 双均线策略回测收益：忽略滑点，扣除 0.2% 往返手续费，全仓滚动
  - 成交额：近1000根K线平均成交额（USDT），衡量流动性
"""
import os
import math
import requests

PROXY = "socks5h://127.0.0.1:7892"
os.environ["HTTP_PROXY"] = PROXY
os.environ["HTTPS_PROXY"] = PROXY
os.environ["ALL_PROXY"] = PROXY

CANDIDATES = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "PEPEUSDT",
    "WIFUSDT", "BONKUSDT", "SHIBUSDT", "SUIUSDT", "XRPUSDT",
    "LTCUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT",
]
FAST, SLOW = 5, 20
FEE = 0.002  # 往返手续费 0.2%

def get_klines(symbol, limit=1000):
    url = f"https://api1.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": "15m", "limit": limit}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def sma(vals):
    return sum(vals) / len(vals)

def backtest(closes):
    """双均线全仓滚动回测，返回累计收益率"""
    if len(closes) <= SLOW:
        return 0.0
    equity = 1.0
    holding = False
    for i in range(SLOW, len(closes)):
        fast = sma(closes[i - FAST + 1:i + 1])
        slow = sma(closes[i - SLOW + 1:i + 1])
        price = closes[i]
        if fast > slow and not holding:
            equity *= (1 - FEE / 2)  # 买入手续费
            holding = True
        elif fast < slow and holding:
            equity *= price / closes[i - 1] * (1 - FEE / 2)  # 本K线收益+卖出手续费
            holding = False
    if holding:  # 期末仍持仓，按最后收盘价结算
        pass
    return (equity - 1) * 100

def analyze(symbol):
    klines = get_klines(symbol)
    closes = [float(k[4]) for k in klines]
    rets = []
    amps = []
    vols = []
    for k in klines:
        h, l, c = float(k[2]), float(k[3]), float(k[4])
        amps.append((h - l) / c if c else 0)
        vols.append(float(k[5]) * c)  # 成交额 ≈ 成交量*收盘价
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            rets.append(closes[i] / closes[i - 1] - 1)
    if not rets:
        return None
    vol = math.sqrt(sum(r * r for r in rets) / len(rets)) * 100  # 单根K线波动率%
    amp = sum(amps) / len(amps) * 100
    ret = backtest(closes)
    avg_vol = sum(vols) / len(vols)
    return symbol, vol, amp, ret, avg_vol

print(f"{'币种':<10}{'单K波动%':<10}{'平均振幅%':<10}{'策略收益%':<12}{'平均成交额(USDT)'}")
print("-" * 60)
results = []
for s in CANDIDATES:
    try:
        r = analyze(s)
        if r:
            results.append(r)
            print(f"{r[0]:<10}{r[1]:<10.3f}{r[2]:<10.3f}{r[3]:<12.2f}{r[4]:,.0f}")
    except Exception as e:
        print(f"{s:<10} 出错: {e}")

print("\n=== 按策略收益排序 ===")
for r in sorted(results, key=lambda x: x[3], reverse=True):
    print(f"{r[0]:<10} 策略收益 {r[3]:+.2f}% | 单K波动 {r[1]:.3f}% | 振幅 {r[2]:.3f}% | 成交额 {r[4]:,.0f} USDT")
