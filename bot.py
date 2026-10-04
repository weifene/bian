"""
币安 U 本位合约量化交易机器人（实盘，双向做多做空）

策略：双均线交叉（15 分钟 K 线，5/20）
  金叉（快线上穿慢线）-> 开多 / 平空开多
  死叉（快线下穿慢线）-> 开空 / 平多开空
  始终持仓（要么多要么空），跟随趋势方向

杠杆：1x（无杠杆），风险可控

风险提示：本程序使用真实资金自动交易，合约有强平风险。
  请务必先用极小金额运行验证，确认无误后再加大金额。
"""
import os
import sys
import time
import json
import datetime

# ================= 代理配置（必须放在最前面，在导入 binance 之前生效）=================
PROXY = "socks5h://127.0.0.1:7892"  # 改成你的代理地址，不用代理就改成 None
if PROXY:
    os.environ["HTTP_PROXY"] = PROXY
    os.environ["HTTPS_PROXY"] = PROXY
    os.environ["ALL_PROXY"] = PROXY
    os.environ["NO_PROXY"] = ""  # 清空绕过列表，确保所有请求走代理

from binance.client import Client

# ================= 配置区 =================
# API 密钥从桌面密钥文件读取，不要写死在代码里
KEY_FILE = os.path.join(os.path.expanduser("~"), "Desktop", "binance_keys.txt")
if not os.path.exists(KEY_FILE):
    print(f"错误：密钥文件不存在 {KEY_FILE}")
    print("请在桌面创建 binance_keys.txt，第一行写 API Key，第二行写 API Secret")
    sys.exit(1)
with open(KEY_FILE, "r", encoding="utf-8-sig") as f:
    lines = [line.strip() for line in f.readlines() if line.strip()]
if len(lines) < 2:
    print("错误：密钥文件格式不对，需要两行：第一行 API Key，第二行 API Secret")
    sys.exit(1)
API_KEY = lines[0]
API_SECRET = lines[1]
SYMBOL = "ARBUSDT"                                  # 交易对（U 本位合约）
INTERVAL = Client.KLINE_INTERVAL_15MINUTE           # 15 分钟 K 线
FAST_PERIOD = 5                                     # 快线周期
SLOW_PERIOD = 20                                    # 慢线周期
BUY_USDT = 30                                       # 每次开仓名义金额（USDT）
LEVERAGE = 3                                        # 杠杆倍数（3x）
POLL_SECONDS = 60                                   # 每 60 秒检查一次
BASE_ASSET = SYMBOL.replace("USDT", "")  # 基础币种（ARB）
TRADE_LOG = os.path.join(os.path.dirname(__file__), "trade_log.json")  # 交易记录文件
PNL_LOG = os.path.join(os.path.dirname(__file__), "pnl_history.json")   # 分时盈亏记录文件
# ===================================================================

class ProxiedClient(Client):
    """确保 session 使用环境变量中的代理"""
    def _init_session(self):
        session = super()._init_session()
        session.trust_env = True  # 从环境变量读取代理
        return session

client = ProxiedClient(API_KEY, API_SECRET, testnet=False)


# ---------------- 合约辅助函数 ----------------

def set_leverage():
    try:
        client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
    except Exception as e:
        print(f"[警告] 设置杠杆失败（可能是仅减仓模式等）：{e}")


def ensure_one_way_mode():
    """确保合约账户为单向持仓模式（本策略从不同时双向持仓，单向即可）。
    双向模式下下单不带 positionSide 会报 -4061，故启动时自动校正。"""
    try:
        mode = client.futures_get_position_mode()
        if mode.get("dualSidePosition"):
            res = client.futures_change_position_mode(dualSidePosition=False)
            print(f"已从双向持仓模式切换为单向持仓模式：{res}")
        else:
            print("合约账户已是单向持仓模式")
    except Exception as e:
        print(f"[警告] 检查/切换持仓模式失败：{e}")


def futures_round_qty(qty, price):
    """按合约交易对允许的最小下单量取整，并保证名义价值 >= 最小下单要求"""
    step, min_notional = 0.1, 5.0
    info = client.futures_exchange_info()
    for s in info["symbols"]:
        if s["symbol"] == SYMBOL:
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    step = float(f["stepSize"])
                elif f["filterType"] == "MIN_NOTIONAL":
                    min_notional = float(f["notional"])
            break
    qty = int(qty / step) * step
    dec = len(str(step).split(".")[1]) if "." in str(step) else 0
    qty = round(qty, dec)
    # 若名义价值不足最小要求，向上补足一个 step
    while qty * price < min_notional:
        qty = round(qty + step, dec)
    return qty


def get_position():
    """返回当前持仓 (数量, 方向)。数量正=多 负=空，0=空仓"""
    pos = client.futures_position_information(symbol=SYMBOL)
    amt = float(pos[0]["positionAmt"]) if pos else 0.0
    entry = float(pos[0]["entryPrice"]) if pos and amt else 0.0
    return amt, entry


def get_wallet_balance():
    """返回 U 本位合约钱包 USDT 可用余额"""
    try:
        for b in client.futures_account_balance():
            if b["asset"] == "USDT":
                return float(b["balance"])
    except Exception:
        pass
    return 0.0


def market_open(side, qty):
    """市价开仓。side: 'LONG' 开多 / 'SHORT' 开空"""
    order_side = Client.SIDE_BUY if side == "LONG" else Client.SIDE_SELL
    order = client.futures_create_order(
        symbol=SYMBOL, side=order_side, type=Client.ORDER_TYPE_MARKET, quantity=qty
    )
    return float(order["executedQty"])


def market_close(qty, close_side):
    """市价平仓。close_side: 持多则 SELL，持空则 BUY"""
    order_side = Client.SIDE_SELL if close_side == "LONG" else Client.SIDE_BUY
    client.futures_create_order(
        symbol=SYMBOL, side=order_side, type=Client.ORDER_TYPE_MARKET, quantity=qty
    )


def get_futures_price():
    """取合约最新价（用合约 K 线，与策略数据同源）"""
    klines = client.futures_klines(symbol=SYMBOL, interval=INTERVAL, limit=1)
    return float(klines[0][4])


# ---------------- 策略逻辑 ----------------

def close_prices(period):
    """取最近 period 根 K 线的收盘价（合约 K 线）"""
    klines = client.futures_klines(symbol=SYMBOL, interval=INTERVAL, limit=period)
    return [float(k[4]) for k in klines]


def sma(prices):
    return sum(prices) / len(prices)


def check_signal():
    """返回 (快线值, 慢线值, 最新价)"""
    fast = sma(close_prices(FAST_PERIOD))
    slow = sma(close_prices(SLOW_PERIOD))
    last = close_prices(1)[0]
    return fast, slow, last


# ---------------- 交易记录与盈亏 ----------------

def load_trades():
    if not os.path.exists(TRADE_LOG):
        return [], None
    try:
        with open(TRADE_LOG, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("trades", []), data.get("open_position", None)
    except Exception:
        return [], None


def save_trades(trades, open_position):
    with open(TRADE_LOG, "w", encoding="utf-8") as f:
        json.dump({"trades": trades, "open_position": open_position}, f, ensure_ascii=False, indent=2)


def calc_pnl(trades, open_position, current_price):
    """盈亏：已实现 + 浮动"""
    realized = sum(t.get("pnl", 0) for t in trades)
    floating = 0.0
    if open_position:
        qty = open_position["qty"]
        entry = open_position["price"]
        side = 1 if open_position["side"] == "LONG" else -1
        floating = (current_price - entry) * qty * side
    return realized, floating, realized + floating


def append_pnl_history(now, price, realized, floating, total, signal, position):
    """每轮轮询追加一条分时盈亏快照到 pnl_history.json（最多保留 10000 条）"""
    rec = {
        "time": now,
        "price": round(price, 6),
        "realized": round(realized, 4),
        "floating": round(floating, 4),
        "total": round(total, 4),
        "signal": signal,
        "position": position,
    }
    data = []
    if os.path.exists(PNL_LOG):
        try:
            with open(PNL_LOG, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = []
    data.append(rec)
    if len(data) > 10000:
        data = data[-10000:]
    with open(PNL_LOG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------- 主循环 ----------------

def main():
    trades, open_position = load_trades()
    set_leverage()
    ensure_one_way_mode()
    print(f"机器人启动 | {SYMBOL} 双向做多做空 | 杠杆 {LEVERAGE}x | 每次开仓 {BUY_USDT} USDT 名义")
    print(f"合约钱包 USDT 余额: {get_wallet_balance():.2f}")
    print(f"当前持仓: {'记录有持仓' if open_position else '记录空仓'} | 历史已平仓 {len(trades)} 笔")
    while True:
        try:
            fast, slow, last = check_signal()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            signal = "多" if fast > slow else "空"
            realized, floating, total = calc_pnl(trades, open_position, last)
            pos_str = f"{open_position['side']}" if open_position else "空仓"
            print(f"[{now}] {SYMBOL} {last:.4f} | 快线{FAST_PERIOD} {fast:.4f} | 慢线{SLOW_PERIOD} {slow:.4f} | 信号{signal} | 持仓{pos_str} | 已实现 {realized:.2f}U | 浮动 {floating:+.2f}U | 总盈亏 {total:+.2f}U",
                  flush=True)

            append_pnl_history(now, last, realized, floating, total, signal, pos_str)

            amt, entry = get_position()  # 实际交易所持仓
            rec_pos = open_position       # 本地记录持仓
            qty_to_trade = futures_round_qty(BUY_USDT / last, last)  # 开仓数量

            if qty_to_trade <= 0:
                print(f"[{now}] [警告] 计算开仓数量为 0（5 USDT 可能低于最小下单量），跳过本轮")
                time.sleep(POLL_SECONDS)
                continue

            # 信号=多：应持多
            if signal == "多":
                if amt < 0:  # 当前持空 -> 平空
                    market_close(abs(amt), "SHORT")
                    pnl = (last - entry) * abs(amt) * -1 if entry else 0.0
                    trades.append({
                        "buy_time": rec_pos["time"] if rec_pos else now,
                        "buy_price": rec_pos["price"] if rec_pos else 0,
                        "buy_qty": abs(amt),
                        "buy_cost": 0,
                        "sell_time": now,
                        "sell_price": last,
                        "sell_qty": abs(amt),
                        "sell_revenue": 0,
                        "pnl": pnl,
                        "side": "SHORT",
                    })
                    print(f"[{now}] >>> 平空 @ {last:.4f}，盈亏 {pnl:+.2f} USDT", flush=True)
                    open_position = None
                if amt <= 0:  # 空仓或刚平空 -> 开多
                    market_open("LONG", qty_to_trade)
                    open_position = {"time": now, "side": "LONG", "qty": qty_to_trade, "price": last, "cost": qty_to_trade * last}
                    save_trades(trades, open_position)
                    print(f"[{now}] >>> 金叉开多 {qty_to_trade} {BASE_ASSET} @ {last:.4f}，名义 {qty_to_trade*last:.2f} USDT", flush=True)
            # 信号=空：应持空
            else:
                if amt > 0:  # 当前持多 -> 平多
                    market_close(amt, "LONG")
                    pnl = (last - entry) * amt if entry else 0.0
                    trades.append({
                        "buy_time": rec_pos["time"] if rec_pos else now,
                        "buy_price": rec_pos["price"] if rec_pos else 0,
                        "buy_qty": amt,
                        "buy_cost": rec_pos["cost"] if rec_pos else 0,
                        "sell_time": now,
                        "sell_price": last,
                        "sell_qty": amt,
                        "sell_revenue": amt * last,
                        "pnl": pnl,
                        "side": "LONG",
                    })
                    print(f"[{now}] >>> 平多 @ {last:.4f}，盈亏 {pnl:+.2f} USDT", flush=True)
                    open_position = None
                    save_trades(trades, open_position)
                if amt >= 0:  # 空仓或刚平多 -> 开空
                    market_open("SHORT", qty_to_trade)
                    open_position = {"time": now, "side": "SHORT", "qty": qty_to_trade, "price": last, "cost": qty_to_trade * last}
                    save_trades(trades, open_position)
                    print(f"[{now}] >>> 死叉开空 {qty_to_trade} {BASE_ASSET} @ {last:.4f}，名义 {qty_to_trade*last:.2f} USDT", flush=True)
        except Exception as e:
            print(f"[{now}] 出错: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if "--check" in sys.argv:
        fast, slow, last = check_signal()
        print(f"{SYMBOL} 最新价 {last:.4f} | 快线({FAST_PERIOD}) {fast:.4f} | 慢线({SLOW_PERIOD}) {slow:.4f}")
        print("当前信号:", "金叉（应开多）" if fast > slow else "死叉（应开空）")
        print(f"合约钱包 USDT 余额: {get_wallet_balance():.2f}")
    else:
        main()
