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
SYMBOL = "BEAMXUSDT"                                # 交易对（U 本位合约）
INTERVAL = Client.KLINE_INTERVAL_15MINUTE           # 15 分钟 K 线
FAST_PERIOD = 5                                     # 快线周期
SLOW_PERIOD = 20                                    # 慢线周期
LEVERAGE = 10                                       # 杠杆倍数（10x：名义金额=余额×95%×10，保证金只用余额的 95%）
POSITION_RATIO = 0.95                               # 全仓开仓比例：每次用合约钱包可用余额的 95% 作为保证金（留 5% 缓冲给手续费）
TP_USDT = 30.0                                      # 止盈：浮动盈亏达到 +30 USDT 自动平仓锁利
SL_USDT = 55.0                                      # 止损：浮动盈亏达到 -55 USDT 自动平仓止损
PAUSE_FILE = r"d:\bian\manual_pause.flag"          # 手动暂停标记：检测到交易所仓位被外部改动时自动创建，恢复交易需删除此文件
LIMIT_TIMEOUT = 4                                   # 限价单等待秒数：先挂 maker 价省手续费，超时未完全成交自动转市价兜底
MAKER_FEE_RATE = 0.0002                             # 挂单成交（maker）手续费率
TAKER_FEE_RATE = 0.0005                             # 吃单成交（taker）手续费率
POLL_SECONDS = 60                                   # 每 60 秒检查一次
BASE_ASSET = SYMBOL.replace("USDT", "")  # 基础币种（ARB）
TRADE_LOG = os.path.join(os.path.dirname(__file__), "trade_log.json")  # 交易记录文件
PNL_LOG = os.path.join(os.path.dirname(__file__), "pnl_history.json")   # 分时盈亏记录文件
# ===================================================================

import requests
from requests.adapters import HTTPAdapter


class TimeoutAdapter(HTTPAdapter):
    """给所有请求强制加 15s 超时，防止代理断连后无限卡死"""
    def send(self, request, **kwargs):
        kwargs.setdefault("timeout", 15)
        return super().send(request, **kwargs)


class ProxiedClient(Client):
    """确保 session 使用环境变量中的代理，并给所有请求加 15s 超时防止卡死"""
    def _init_session(self):
        session = super()._init_session()
        session.trust_env = True  # 从环境变量读取代理
        adapter = TimeoutAdapter()
        session.mount("https://", adapter)
        session.mount("http://", adapter)
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


def calc_full_qty(price):
    """全仓模式：用余额的 POSITION_RATIO 作为保证金，按 LEVERAGE 计算名义金额。
    必须在本轮已平掉旧持仓之后调用，才能拿到平仓后的最新余额。"""
    balance = get_wallet_balance()
    notional = balance * POSITION_RATIO * LEVERAGE
    qty = futures_round_qty(notional / price, price)
    return qty, balance


def market_order(qty, side):
    """市价下单，返回 (成交数量, 平均成交价)。side: 'LONG' 买 / 'SHORT' 卖"""
    order_side = Client.SIDE_BUY if side == "LONG" else Client.SIDE_SELL
    order = client.futures_create_order(
        symbol=SYMBOL, side=order_side, type=Client.ORDER_TYPE_MARKET, quantity=qty
    )
    oid = order["orderId"]
    # 偶发：下单成功但响应缺成交字段（连接异常导致），回查订单补全
    if not order.get("avgPrice") or not order.get("executedQty"):
        order = client.futures_get_order(symbol=SYMBOL, orderId=oid)
    return float(order["executedQty"]), float(order["avgPrice"]), oid


def get_book_top(side):
    """取盘口挂单价：开多/平空挂买一，开空/平多挂卖一，被动等成交（maker 手续费）"""
    book = client.futures_order_book(symbol=SYMBOL, limit=5)
    if side == "LONG":
        return float(book["bids"][0][0])
    else:
        return float(book["asks"][0][0])


def wait_fill_or_fallback(order, total_qty, side):
    """混合下单核心：轮询等待限价单成交；超时未完全成交则取消，用市价补足差额。
    返回 (实际成交数量, 加权平均价, 成交类型, 手续费估算)：
      maker=限价全成交 / taker=全靠市价 / mixed=限价部分+市价补足"""
    oid = order["orderId"]
    filled, cost = 0.0, 0.0
    limit_filled, limit_avg = 0.0, 0.0
    for _ in range(int(LIMIT_TIMEOUT)):
        st = client.futures_get_order(symbol=SYMBOL, orderId=oid)
        status = st["status"]
        if status == "FILLED":
            limit_filled = float(st.get("executedQty", 0) or 0)
            limit_avg = float(st.get("avgPrice", 0) or 0)
            filled, cost = limit_filled, limit_filled * limit_avg
            break
        if status in ("CANCELED", "EXPIRED"):
            break
        time.sleep(1)
    else:
        # 超时未成交：取消限价单，避免后续与信号冲突
        try:
            client.futures_cancel_order(symbol=SYMBOL, orderId=oid)
        except Exception:
            pass
        st = client.futures_get_order(symbol=SYMBOL, orderId=oid)
        limit_filled = float(st.get("executedQty", 0) or 0)
        limit_avg = float(st.get("avgPrice", 0) or 0)
        filled, cost = limit_filled, limit_filled * limit_avg
    # 差额用市价补足
    remain = total_qty - filled
    market_filled, market_cost = 0.0, 0.0
    if remain > 0:
        q2, p2, _ = market_order(remain, side)
        market_filled, market_cost = q2, p2 * q2
        filled += q2
        cost += market_cost
    avg = cost / filled if filled else 0.0
    # 手续费估算：限价部分按 maker 费率，市价补足部分按 taker 费率
    fee = limit_filled * limit_avg * MAKER_FEE_RATE + market_cost * TAKER_FEE_RATE
    if limit_filled <= 0:
        otype = "taker"
    elif remain > 0:
        otype = "mixed"
    else:
        otype = "maker"
    return filled, avg, otype, fee


def smart_open(side, qty):
    """混合开仓：先挂限价（maker 手续费），超时未成交转市价。返回 (成交数量, 均价, 成交类型)"""
    px = get_book_top(side)
    qty = futures_round_qty(qty, px)
    order_side = Client.SIDE_BUY if side == "LONG" else Client.SIDE_SELL
    order = client.futures_create_order(
        symbol=SYMBOL, side=order_side, type=Client.ORDER_TYPE_LIMIT,
        quantity=qty, price=px, timeInForce=Client.TIME_IN_FORCE_GTC,
    )
    return wait_fill_or_fallback(order, qty, side)


def smart_close(qty, position_side):
    """混合平仓：先挂限价（maker 手续费），超时未成交转市价。
    position_side: 被平仓位方向（持多平多传 'LONG'，持空平空传 'SHORT'）。
    返回 (成交数量, 均价, 成交类型)"""
    close_side = "SHORT" if position_side == "LONG" else "LONG"  # 平多卖、平空买
    px = get_book_top(close_side)
    qty = futures_round_qty(qty, px)
    order_side = Client.SIDE_SELL if close_side == "SHORT" else Client.SIDE_BUY
    order = client.futures_create_order(
        symbol=SYMBOL, side=order_side, type=Client.ORDER_TYPE_LIMIT,
        quantity=qty, price=px, timeInForce=Client.TIME_IN_FORCE_GTC,
    )
    return wait_fill_or_fallback(order, qty, close_side)


def get_futures_price():
    """取合约最新价（用合约 K 线，与策略数据同源）"""
    klines = client.futures_klines(symbol=SYMBOL, interval=INTERVAL, limit=1)
    return float(klines[0][4])


def check_tp_sl(amt, entry, price):
    """止盈止损检查：按当前浮动盈亏判断是否触发。
    返回 'TP'（止盈）/ 'SL'（止损）/ None（未触发）"""
    if not amt or not entry:
        return None
    side = 1 if amt > 0 else -1
    floating = (price - entry) * abs(amt) * side
    if floating >= TP_USDT:
        return "TP"
    if floating <= -SL_USDT:
        return "SL"
    return None


def is_paused():
    """是否处于手动暂停状态（检测到交易所仓位被外部改动后自动暂停）"""
    return os.path.exists(PAUSE_FILE)


def pause_bot(reason):
    """写入暂停标记文件，机器人停止自动交易直到用户恢复"""
    with open(PAUSE_FILE, "w", encoding="utf-8") as f:
        f.write(f"{reason} @ {datetime.datetime.now()}\n")


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
    print(f"机器人启动 | {SYMBOL} 双向做多做空 | 杠杆 {LEVERAGE}x | 全仓模式（保证金用余额的 {POSITION_RATIO*100:.0f}%）| 止盈/止损 {TP_USDT:.0f}/{SL_USDT:.0f}U | 下单：限价优先（maker），超时 {LIMIT_TIMEOUT}s 转市价")
    print(f"合约钱包 USDT 余额: {get_wallet_balance():.2f}")
    print(f"当前持仓: {'记录有持仓' if open_position else '记录空仓'} | 历史已平仓 {len(trades)} 笔")
    if is_paused():
        print(f"[警告] 存在暂停标记（{os.path.basename(PAUSE_FILE)}），机器人启动后保持暂停状态，删除该文件后恢复自动交易")
    risk_exit_side = None  # 止盈/止损离场方向：同方向不立即重进，等信号翻向对面再开仓
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

            # 手动暂停保护：检测交易所仓位被外部改动（手动平仓/手动开仓/手动翻向），自动暂停不自动交易
            if is_paused():
                print(f"[{now}] [暂停中] 检测到手动操作已暂停交易，等待恢复（删除 {os.path.basename(PAUSE_FILE)} 后恢复自动交易）", flush=True)
                time.sleep(POLL_SECONDS)
                continue
            manual = None
            if open_position and amt == 0:
                manual = "手动平仓"
            elif open_position and ((open_position["side"] == "LONG" and amt < 0) or (open_position["side"] == "SHORT" and amt > 0)):
                manual = "手动反向开仓"
            elif not open_position and amt != 0:
                manual = "手动开仓"
            if manual:
                print(f"[{now}] [警告] 检测到{manual}（本地记录 {'多' if open_position and open_position['side'] == 'LONG' else '空'}，实际仓位 {amt}），自动暂停交易保护，不会动你的仓位", flush=True)
                open_position = None
                save_trades(trades, open_position)
                pause_bot(manual)
                time.sleep(POLL_SECONDS)
                continue

            # 止盈/止损检查：触发则平仓锁利/止损，并标记离场方向（同方向暂不重进）
            hit = check_tp_sl(amt, entry, last)
            if hit:
                pos_side = "LONG" if amt > 0 else "SHORT"
                fq, fp, ot, fee = smart_close(abs(amt), pos_side)
                pnl = (fp - entry) * fq * (1 if pos_side == "LONG" else -1)
                trades.append({
                    "buy_time": rec_pos["time"] if rec_pos else now,
                    "buy_price": rec_pos["price"] if rec_pos else 0,
                    "buy_qty": abs(amt),
                    "buy_cost": rec_pos["cost"] if rec_pos else fq * fp,
                    "sell_time": now,
                    "sell_price": fp,
                    "sell_qty": fq,
                    "sell_revenue": fq * fp,
                    "pnl": pnl,
                    "side": pos_side,
                    "entry_type": rec_pos.get("order_type", "—") if rec_pos else "—",
                    "exit_type": ot,
                    "entry_fee": rec_pos.get("entry_fee", 0) if rec_pos else 0,
                    "exit_fee": fee,
                    "reason": "止盈" if hit == "TP" else "止损",
                })
                print(f"[{now}] >>> {('止盈' if hit == 'TP' else '止损')} {fq} {BASE_ASSET} @ {fp:.4f}（{ot}），盈亏 {pnl:+.2f} USDT（手续费 {fee:.4f}U）", flush=True)
                open_position = None
                save_trades(trades, open_position)
                risk_exit_side = pos_side
                amt = 0

            # 信号=多：应持多
            if signal == "多":
                if amt < 0:  # 当前持空 -> 平空
                    fill_qty, fill_px, otype, exit_fee = smart_close(abs(amt), "SHORT")
                    pnl = (fill_px - entry) * fill_qty * -1 if entry else 0.0
                    trades.append({
                        "buy_time": rec_pos["time"] if rec_pos else now,
                        "buy_price": rec_pos["price"] if rec_pos else 0,
                        "buy_qty": abs(amt),
                        "buy_cost": fill_qty * fill_px,
                        "sell_time": now,
                        "sell_price": fill_px,
                        "sell_qty": fill_qty,
                        "sell_revenue": rec_pos["cost"] if rec_pos else fill_qty * fill_px,
                        "pnl": pnl,
                        "side": "SHORT",
                        "entry_type": rec_pos.get("order_type", "—") if rec_pos else "—",
                        "exit_type": otype,
                        "entry_fee": rec_pos.get("entry_fee", 0) if rec_pos else 0,
                        "exit_fee": exit_fee,
                    })
                    print(f"[{now}] >>> 平空 @ {fill_px:.4f}（{otype}），盈亏 {pnl:+.2f} USDT（手续费 {exit_fee:.4f}U）", flush=True)
                    open_position = None
                    save_trades(trades, open_position)
                if amt <= 0 and risk_exit_side != "LONG":  # 空仓或刚平空 -> 开多（止盈/止损离场后同方向不立即重进）
                    qty_to_trade, balance = calc_full_qty(last)
                    if qty_to_trade <= 0 or qty_to_trade * last / LEVERAGE > balance:
                        print(f"[{now}] [警告] 余额不足无法开多（可用 {balance:.2f} USDT），跳过本轮")
                        time.sleep(POLL_SECONDS)
                        continue
                    fill_qty, fill_px, otype, entry_fee = smart_open("LONG", qty_to_trade)
                    risk_exit_side = None
                    open_position = {"time": now, "side": "LONG", "qty": fill_qty, "price": fill_px, "cost": fill_qty * fill_px, "order_type": otype, "entry_fee": entry_fee}
                    save_trades(trades, open_position)
                    print(f"[{now}] >>> 金叉开多 {fill_qty} {BASE_ASSET} @ {fill_px:.4f}，名义 {fill_qty*fill_px:.2f} USDT（全仓·{otype}）", flush=True)
            # 信号=空：应持空
            else:
                if amt > 0:  # 当前持多 -> 平多
                    fill_qty, fill_px, otype, exit_fee = smart_close(amt, "LONG")
                    pnl = (fill_px - entry) * fill_qty if entry else 0.0
                    trades.append({
                        "buy_time": rec_pos["time"] if rec_pos else now,
                        "buy_price": rec_pos["price"] if rec_pos else 0,
                        "buy_qty": amt,
                        "buy_cost": rec_pos["cost"] if rec_pos else 0,
                        "sell_time": now,
                        "sell_price": fill_px,
                        "sell_qty": fill_qty,
                        "sell_revenue": fill_qty * fill_px,
                        "pnl": pnl,
                        "side": "LONG",
                        "entry_type": rec_pos.get("order_type", "—") if rec_pos else "—",
                        "exit_type": otype,
                        "entry_fee": rec_pos.get("entry_fee", 0) if rec_pos else 0,
                        "exit_fee": exit_fee,
                    })
                    print(f"[{now}] >>> 平多 @ {fill_px:.4f}（{otype}），盈亏 {pnl:+.2f} USDT（手续费 {exit_fee:.4f}U）", flush=True)
                    open_position = None
                    save_trades(trades, open_position)
                if amt >= 0 and risk_exit_side != "SHORT":  # 空仓或刚平多 -> 开空（止盈/止损离场后同方向不立即重进）
                    qty_to_trade, balance = calc_full_qty(last)
                    if qty_to_trade <= 0 or qty_to_trade * last / LEVERAGE > balance:
                        print(f"[{now}] [警告] 余额不足无法开空（可用 {balance:.2f} USDT），跳过本轮")
                        time.sleep(POLL_SECONDS)
                        continue
                    fill_qty, fill_px, otype, entry_fee = smart_open("SHORT", qty_to_trade)
                    risk_exit_side = None
                    open_position = {"time": now, "side": "SHORT", "qty": fill_qty, "price": fill_px, "cost": fill_qty * fill_px, "order_type": otype, "entry_fee": entry_fee}
                    save_trades(trades, open_position)
                    print(f"[{now}] >>> 死叉开空 {fill_qty} {BASE_ASSET} @ {fill_px:.4f}，名义 {fill_qty*fill_px:.2f} USDT（全仓·{otype}）", flush=True)
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
