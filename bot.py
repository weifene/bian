"""
最简单的币安现货量化交易机器人（实盘）

策略：双均线交叉（15 分钟 K 线）
  金叉（快线上穿慢线）-> 买入
  死叉（快线下穿慢线）-> 卖出

风险提示：本程序使用真实资金自动交易，默认每次买入 5 SDT。
  请务必先在测试网验证策略，或用极小金额运行，确认无误后再加大金额。
"""
import os
import sys
import time
import datetime

# ================= 代理配置（必须放在最前面，在导入 binance 之前生效）=================
# 国内直连币安不稳定，请配置本地代理。常见端口：Clash HTTP=7890，Clash SOCKS5=7891
PROXY = "socks5h://127.0.0.1:7892"  # 改成你的代理地址，不用代理就改成 None
if PROXY:
    os.environ["HTTP_PROXY"] = PROXY
    os.environ["HTTPS_PROXY"] = PROXY
    os.environ["ALL_PROXY"] = PROXY
    os.environ["NO_PROXY"] = ""  # 清空绕过列表，确保所有请求走代理

from binance.client import Client

# ================= 配置区：请填写你的实盘 API Key =================
API_KEY = "jfDhV6VSdXXr9MNwyPBtZu3zTR94TSLf6X56mRuzsDRyswvRqpLPjyvKZEyL7eKp" # 换成你的实盘 API Key
API_SECRET = "7bImFpPiFRyQfp3QsuiaOBLKBSJpxdZ36Fj6F3BuxZA7TUX6WHfTN7lLyZ9I4sbv" # 换成你的实盘 API Secret
SYMBOL = "BTCUSDT"                                  # 交易对
INTERVAL = Client.KLINE_INTERVAL_15MINUTE           # 15 分钟 K 线
FAST_PERIOD = 5                                     # 快线周期
SLOW_PERIOD = 20                                    # 慢线周期
BUY_USDT = 5                                        # 每次买入金额（USDT）
POLL_SECONDS = 60                                   # 每 60 秒检查一次
# ===================================================================

class ProxiedClient(Client):
    """确保 session 使用环境变量中的代理"""
    def _init_session(self):
        session = super()._init_session()
        session.trust_env = True  # 从环境变量读取代理
        return session

client = ProxiedClient(API_KEY, API_SECRET, testnet=False)


def close_prices(period):
    """取最近 period 根 K 线的收盘价"""
    klines = client.get_klines(symbol=SYMBOL, interval=INTERVAL, limit=period)
    return [float(k[4]) for k in klines]


def sma(prices):
    return sum(prices) / len(prices)


def check_signal():
    """返回 (快线值, 慢线值, 最新价)"""
    fast = sma(close_prices(FAST_PERIOD))
    slow = sma(close_prices(SLOW_PERIOD))
    last = close_prices(1)[0]
    return fast, slow, last


def round_qty(qty):
    """按交易对允许的最小下单量取整"""
    info = client.get_symbol_info(SYMBOL)
    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step = float(f["stepSize"])
            return int(qty / step) * step
    return qty


def has_position():
    return float(client.get_asset_balance(asset="BTC")["free"]) > 0


def main():
    position = has_position()
    print(f"机器人启动，当前持仓: {'是' if position else '否'}")
    while True:
        try:
            fast, slow, last = check_signal()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now}] {SYMBOL} 最新价 {last:.2f} | 快线{FAST_PERIOD} {fast:.2f} | 慢线{SLOW_PERIOD} {slow:.2f} | 持仓 {'是' if position else '否'}",
                  flush=True)
            if fast > slow and not position:
                client.order_market_buy(symbol=SYMBOL, quoteOrderQty=BUY_USDT)
                position = True
                print(f"[{now}] >>> 金叉，已买入 {BUY_USDT} USDT")
            elif fast < slow and position:
                qty = round_qty(float(client.get_asset_balance(asset="BTC")["free"]))
                if qty > 0:
                    client.order_market_sell(symbol=SYMBOL, quantity=qty)
                position = False
                print(f"[{now}] >>> 死叉，已卖出")
        except Exception as e:
            print("出错:", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if "--check" in sys.argv:
        fast, slow, last = check_signal()
        print(f"{SYMBOL} 最新价 {last:.2f} | 快线({FAST_PERIOD}) {fast:.2f} | 慢线({SLOW_PERIOD}) {slow:.2f}")
        print("当前信号:", "金叉（可买入）" if fast > slow else "死叉（可卖出）")
    else:
        main()
