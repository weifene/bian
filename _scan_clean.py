# -*- coding: utf-8 -*-
"""扫描 24 小时窗口内"干净趋势"（少噪音、方向持续）的币，适配双均线策略（1m 周期）"""
import sys
sys.path.insert(0, r"d:\bian")
from bot import client

FAST, SLOW, FEE = 5, 20, 0.0002
info = client.futures_exchange_info()
tickers = {t["symbol"]: float(t.get("quoteVolume", 0) or 0) for t in client.futures_ticker()}
pairs = []
for s in info["symbols"]:
    if s["contractType"] != "PERPETUAL" or s["status"] != "TRADING" or s["quoteAsset"] != "USDT":
        continue
    qv = tickers.get(s["symbol"], 0)
    if qv < 2000000:  # 24h 成交量低于 200 万 USDT 跳过（防流动性差的空气币）
        continue
    pairs.append((s["symbol"], qv))

print(f"扫描 {len(pairs)} 个活跃合约（24h量>200万U）")
results = []
for sym, qv in pairs:
    try:
        ks = client.futures_klines(symbol=sym, interval="1m", limit=1500)
        if len(ks) < 60:
            continue
        cs = [float(k[4]) for k in ks]
        c = cs[19:][-1440:]  # 预热 19 根后取最近 24 小时
        t = []
        pos, ei = 0, 0
        for i in range(SLOW - 1, len(c)):
            f = sum(c[i - FAST + 1:i + 1]) / FAST
            s = sum(c[i - SLOW + 1:i + 1]) / SLOW
            sig = 1 if f > s else -1
            if pos == 0:
                pos, ei = sig, i
            elif sig != pos:
                t.append((pos, (c[i] / c[ei] - 1) * pos - 2 * FEE))
                pos, ei = sig, i
        if pos:
            t.append((pos, (c[-1] / c[ei] - 1) * pos - FEE))
        if len(t) < 2:
            continue
        tot = 1.0
        for _, r in t:
            tot *= (1 + r)
        w = sum(1 for _, r in t if r > 0)
        g = sum(r for _, r in t if r > 0)
        l = -sum(r for _, r in t if r <= 0)
        pf = g / l if l > 0 else 99
        sig = "多" if sum(cs[-5:]) / 5 > sum(cs[-20:]) / 20 else "空"
        results.append((sym, tot - 1, len(t), w / len(t), pf, qv, sig, c[-1]))
    except Exception:
        continue

results.sort(key=lambda x: -x[1])
hdr = f"{'币种':<12}{'24h收益':>8}{'交易':>5}{'胜率':>6}{'盈亏比':>6}{'24h量':>9}{'信号':>4}{'现价':>12}"
print(hdr)
print("-" * len(hdr))
for r in results[:20]:
    print(f"{r[0]:<12}{r[1]*100:>7.1f}%{r[2]:>5}{r[3]*100:>5.0f}%{r[4]:>6.1f}{r[5]/1000000:>8.1f}M{r[6]:>4}{r[7]:>12.6f}")
