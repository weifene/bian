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
SYMBOL = "CARVUSDT"                                # 交易对（U 本位合约）
INTERVAL = Client.KLINE_INTERVAL_15MINUTE           # 15 分钟 K 线（趋势窗口由 WINDOW_HOURS 决定：1 小时 = 4 根）
FAST_PERIOD = 5                                     # 快线周期
SLOW_PERIOD = 20                                    # 慢线周期
WINDOW_OPTIONS = (1, 3, 6, 12)                     # 可选趋势窗口（小时）：界面可选择最近 1/3/6/12 小时
WINDOW_HOURS = 12                                   # 当前趋势窗口（小时）：扫描、开仓复核、持仓信号统一使用，默认最近 12 小时
SCAN_WINDOW_FILE = r"d:\bian\scan_window.json"     # 可视化界面选择的窗口写入此文件，机器人每轮读取并生效
R2_ENTER = 0.75                                     # 进场 R² 阈值：趋势干净度达标才进场（0~1，越高越严）
R2_EXIT = 0.40                                      # 离场 R² 阈值：跌破则认为趋势破坏 -> 空仓
SLOPE_MIN_PCT = 0.0008                              # 进场最小斜率（每根K线价格变动比例）：低于此不进场
SLOPE_EXIT_MIN = 0.0002                             # 离场最小斜率：斜率低于此视为走平 -> 空仓
MIN_VOLUME = 2000000                                 # 空仓选币扫描：24h 成交量下限（USDT），过滤空气币
SCAN_INTERVAL = 120                                   # 空仓时全市场扫描间隔（秒）：2 分钟扫一次
SCAN_TOP_N = 10                                       # 扫描保留的前 N 个干净趋势目标（写入界面显示）
SCAN_RESULTS_FILE = r"d:\bian\scan_results.json"     # 扫描结果（Top N）写入此文件，供可视化界面读取展示
OPEN_REQUEST_FILE = r"d:\bian\open_request.json"     # 可视化界面选定的开仓币种写入这里，机器人读取后开仓（开仓后自动删除）
LAST_OPEN_NOTE = {"text": "", "time": ""}             # 最近一次界面开仓请求的处理结果（拒绝原因/成功信息），写入状态快照供界面显示
STOP_FILE = r"d:\bian\stop_request.flag"             # 可视化界面"停止程序"按钮：存在此文件机器人优雅退出
STATUS_FILE = r"d:\bian\bot_status.json"             # 每轮轮询写入运行状态快照（界面状态显示与心跳检测）
LEVERAGE = 3                                        # 杠杆倍数（3x：名义金额=余额×95%×3，保证金只用余额的 95%）
POSITION_RATIO = 0.95                               # 全仓开仓比例：每次用合约钱包可用余额的 95% 作为保证金（留 5% 缓冲给手续费）
TP_USDT = 0.0                                       # 止盈阈值（USDT）：0 = 关闭止盈，>0 时浮动盈亏达到该值自动平仓锁利
SL_USDT = 55.0                                      # 止损：浮动盈亏达到 -55 USDT 自动平仓止损
TRAIL_PCT = 0.10                                    # 动态回撤止损/止盈：价格从入场以来最佳价（多=最高/空=最低）回撤达到 10% 即平仓（锁利或止损）
FUNDING_RATE_LIMIT = 0.001                          # 资金费率监控阈值：|资金费率| 超过 0.1%（每8小时）且逆费率方向开仓时拒绝。
                                                    #   资金费率正=多头付费给空头，负=空头付费给多头；顺费率方向开仓可收取资金费，不拦截
PAUSE_FILE = r"d:\bian\manual_pause.flag"          # 手动暂停标记：检测到交易所仓位被外部改动时自动创建，恢复交易需删除此文件
LIMIT_TIMEOUT = 4                                   # 限价单等待秒数：先挂 maker 价省手续费，超时未完全成交自动转市价兜底
MAKER_FEE_RATE = 0.0002                             # 挂单成交（maker）手续费率
TAKER_FEE_RATE = 0.0005                             # 吃单成交（taker）手续费率
POLL_SECONDS = 1                                    # 每 1 秒检查一次（响应最快；注意 API 约 4 次/轮 × 60 轮/分 = 240 次/分，接近限频需留意 429）
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


def get_funding_rate(symbol):
    """查询某币当前资金费率（每 8 小时结算一次，正=多头付费给空头，负=空头付费给多头）。
    公开端点无需签名；查询失败返回 None（None 时不拦截开仓，避免网络抖动误伤）"""
    try:
        r = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}", timeout=10)
        return float(r.json().get("lastFundingRate", 0) or 0)
    except Exception as e:
        print(f"[警告] 资金费率查询失败（{symbol}）：{e}")
        return None


def check_tp_sl(amt, entry, price):
    """止盈止损检查：按当前浮动盈亏判断是否触发（TP_USDT/SL_USDT 为 0 时对应功能关闭）。
    返回 'TP'（止盈）/ 'SL'（止损）/ None（未触发）"""
    if not amt or not entry:
        return None
    side = 1 if amt > 0 else -1
    floating = (price - entry) * abs(amt) * side
    if SL_USDT > 0 and floating <= -SL_USDT:
        return "SL"
    if TP_USDT > 0 and floating >= TP_USDT:
        return "TP"
    return None


def check_trailing(rec_pos, amt, price):
    """动态回撤止损/止盈：跟踪入场以来最佳价格（多=最高价，空=最低价），
    当前价从最佳价回撤达 TRAIL_PCT（10%）即触发平仓。
    返回 (触发?, 本轮最新最佳价, 当前回撤幅度)"""
    if not rec_pos or not amt:
        return False, 0.0, 0.0
    best = float(rec_pos.get("best_price", rec_pos["price"]))
    if amt > 0:  # 做多：最佳价=最高价
        if price > best:
            best = price
        drawdown = (best - price) / best if best > 0 else 0.0
    else:        # 做空：最佳价=最低价
        if price < best:
            best = price
        drawdown = (price - best) / best if best > 0 else 0.0
    return drawdown >= TRAIL_PCT, best, drawdown


def is_paused():
    """是否处于手动暂停状态（检测到交易所仓位被外部改动后自动暂停）"""
    return os.path.exists(PAUSE_FILE)


def pause_bot(reason):
    """写入暂停标记文件，机器人停止自动交易直到用户恢复"""
    with open(PAUSE_FILE, "w", encoding="utf-8") as f:
        f.write(f"{reason} @ {datetime.datetime.now()}\n")


# ---------------- 策略逻辑 ----------------

def close_prices(period, symbol=None):
    """取最近 period 根 K 线的收盘价（合约 K 线）。symbol 缺省用全局 SYMBOL。
    趋势窗口（period == window_bars()）按窗口小时数自动选周期（约 60 根），其余场景用默认 15m。"""
    sym = symbol or SYMBOL
    interval = window_interval()[0] if period == window_bars() else INTERVAL
    klines = client.futures_klines(symbol=sym, interval=interval, limit=period)
    return [float(k[4]) for k in klines]


def sma(prices):
    return sum(prices) / len(prices)


def linreg(prices):
    """对收盘价序列做线性回归。返回 (每根K线斜率%, R²拟合优度)"""
    n = len(prices)
    xs = list(range(n))
    xbar = sum(xs) / n
    ybar = sum(prices) / n
    sxy = sum((x - xbar) * (y - ybar) for x, y in zip(xs, prices))
    sxx = sum((x - xbar) ** 2 for x in xs)
    slope = sxy / sxx
    intercept = ybar - slope * xbar
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, prices))
    ss_tot = sum((y - ybar) ** 2 for y in prices)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope / ybar, r2


def window_interval():
    """当前趋势窗口自动选择 K 线周期，确保约 60 根 K 线参与回归与持仓管理：
    1 小时→1m×60 根、3 小时→3m×60 根、6 小时→5m×72 根、12 小时→15m×48 根。"""
    h = WINDOW_HOURS
    if h <= 1:
        return Client.KLINE_INTERVAL_1MINUTE, 60
    if h <= 3:
        return Client.KLINE_INTERVAL_3MINUTE, 60
    if h <= 6:
        return Client.KLINE_INTERVAL_5MINUTE, int(h * 12)
    return Client.KLINE_INTERVAL_15MINUTE, int(h * 4)


def window_bars():
    """当前趋势窗口对应的 K 线根数（周期随窗口小时数自动选择，约 60 根）"""
    return window_interval()[1]


def interval_minutes():
    """当前趋势窗口选用的 K 线周期分钟数（用于斜率阈值折算）"""
    return {"1m": 1, "3m": 3, "5m": 5, "15m": 15}[window_interval()[0]]


def slope_min():
    """当前周期下的进场最小斜率：按周期折算，保证趋势强度口径一致（基准 15m）"""
    return SLOPE_MIN_PCT * interval_minutes() / 15.0


def slope_exit():
    """当前周期下的离场最小斜率（基准 15m）"""
    return SLOPE_EXIT_MIN * interval_minutes() / 15.0


def check_signal():
    """斜率趋势策略：对最近 window_bars() 根 K 线做线性回归。
    返回 (斜率/根, R²拟合优度, 最新价)。R² 高说明 K 线沿一条直线走（趋势干净）。"""
    slope_pct, r2 = linreg(close_prices(window_bars()))
    return slope_pct, r2, close_prices(1)[0]


def trend_break_progress(r2, slope_pct):
    """趋势破坏进度 0~100 分：0 = 刚进场（趋势最干净），100 = 趋势破坏（触发平仓）。
    平仓条件为 R² 跌破 R2_EXIT 或 |斜率| 跌破 SLOPE_EXIT_MIN（任一先触发即平仓），
    故以两个维度中破坏更严重者计分；仍在进场阈值之上时记为 0 分。"""
    a_slope = abs(slope_pct)
    p_r2 = 0.0
    if r2 < R2_ENTER and R2_ENTER > R2_EXIT:
        p_r2 = min(1.0, max(0.0, (R2_ENTER - r2) / (R2_ENTER - R2_EXIT)))
    p_slope = 0.0
    if a_slope < slope_min() and slope_min() > slope_exit():
        p_slope = min(1.0, max(0.0, (slope_min() - a_slope) / (slope_min() - slope_exit())))
    return min(100, round(max(p_r2, p_slope) * 100))


def scan_top(limit=SCAN_TOP_N):
    """空仓时扫描全市场，按"干净度"（R² 优先，其次斜率强度）返回前 limit 个干净趋势目标。
    返回 list[dict]（symbol/r2/slope_pct/price/vol/score/direction）"""
    try:
        info = client.futures_exchange_info()
        tickers = {t["symbol"]: {"qv": float(t.get("quoteVolume", 0) or 0),
                                 "px": float(t.get("lastPrice", 0) or 0)} for t in client.futures_ticker()}
    except Exception:
        return []
    cands = []
    for s in info["symbols"]:
        if s["contractType"] != "PERPETUAL" or s["status"] != "TRADING" or s["quoteAsset"] != "USDT":
            continue
        sym = s["symbol"]
        tk = tickers.get(sym)
        if not tk or tk["qv"] < MIN_VOLUME:
            continue
        try:
            slope_pct, r2 = linreg(close_prices(window_bars(), sym))
            if r2 >= R2_ENTER and abs(slope_pct) >= slope_min():
                cands.append({"symbol": sym, "r2": r2, "slope_pct": slope_pct,
                              "price": tk["px"], "vol": tk["qv"],
                              "score": r2 * 1000 + abs(slope_pct) * 100000,  # R² 优先，斜率次之
                              "direction": 1 if slope_pct > 0 else -1})
        except Exception:
            continue
        time.sleep(0.02)  # 防接口限频
    cands.sort(key=lambda c: -c["score"])
    return cands[:limit]


def verify_symbol(sym):
    """确认前复核某币当前是否仍是干净趋势。返回 (ok, slope_pct, r2, last)"""
    try:
        slope_pct, r2 = linreg(close_prices(window_bars(), sym))
        last = close_prices(1, sym)[0]
        return (r2 >= R2_ENTER and abs(slope_pct) >= slope_min()), slope_pct, r2, last
    except Exception:
        return False, 0.0, 0.0, 0.0


def pause_reason():
    """读取暂停标记内容（手动操作类型），未暂停返回空串"""
    if not os.path.exists(PAUSE_FILE):
        return ""
    try:
        with open(PAUSE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "暂停中"


def write_status(now, price, realized, floating, total, signal_str, pos_str, paused, last_scan_str, cand_count, break_score):
    """每轮轮询写入运行状态快照，供可视化界面显示状态与心跳检测"""
    st_data = {
        "time": now,
        "symbol": SYMBOL,
        "price": price,
        "realized": realized,
        "floating": floating,
        "total": total,
        "signal": signal_str,
        "position": pos_str,
        "balance": get_wallet_balance(),
        "paused": paused,
        "last_scan": last_scan_str,
        "candidate_count": cand_count,
        "break_score": break_score,   # 趋势破坏进度 0~100：0=刚进场 100=触发趋势破坏平仓
        "open_note": LAST_OPEN_NOTE.get("text", ""),      # 最近一次开仓请求的处理结果（拒绝原因/成功），界面展示
        "open_note_time": LAST_OPEN_NOTE.get("time", ""), # 该结果产生的时间
    }
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(st_data, f, ensure_ascii=False, indent=2)


# ---------------- 交易记录与盈亏 ----------------

def load_trades():
    if not os.path.exists(TRADE_LOG):
        return [], None, None
    try:
        with open(TRADE_LOG, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("trades", []), data.get("open_position", None), data.get("symbol")
    except Exception:
        return [], None, None


def save_trades(trades, open_position):
    with open(TRADE_LOG, "w", encoding="utf-8") as f:
        json.dump({"trades": trades, "open_position": open_position, "symbol": SYMBOL},
                  f, ensure_ascii=False, indent=2)


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

def make_close_record(rec_pos, qty, fill_px, otype, fee, side, now_str, reason=""):
    """生成一条平仓交易记录（多/空通用）。reason 可为 '趋势破坏'/'趋势翻转'/'止盈'/'止损' 等"""
    if side == "LONG":
        pnl = (fill_px - rec_pos["price"]) * qty if rec_pos else 0.0
        buy_cost = rec_pos["cost"] if rec_pos else qty * fill_px
        entry_price = rec_pos["price"] if rec_pos else 0
        sell_revenue = qty * fill_px
    else:  # SHORT
        pnl = (rec_pos["price"] - fill_px) * qty if rec_pos else 0.0
        buy_cost = qty * fill_px
        entry_price = rec_pos["price"] if rec_pos else 0
        sell_revenue = rec_pos["cost"] if rec_pos else qty * fill_px
    rec = {
        "buy_time": rec_pos["time"] if rec_pos else now_str,
        "buy_price": entry_price,
        "buy_qty": qty,
        "buy_cost": buy_cost,
        "sell_time": now_str,
        "sell_price": fill_px,
        "sell_qty": qty,
        "sell_revenue": sell_revenue,
        "pnl": pnl,
        "side": side,
        "entry_type": rec_pos.get("order_type", "—") if rec_pos else "—",
        "exit_type": otype,
        "entry_fee": rec_pos.get("entry_fee", 0) if rec_pos else 0,
        "exit_fee": fee,
    }
    if reason:
        rec["reason"] = reason
    return rec

def main():
    global SYMBOL, BASE_ASSET, WINDOW_HOURS  # 空仓换币时动态切换交易对；趋势窗口由界面选择动态生效
    trades, open_position, saved_symbol = load_trades()
    if saved_symbol and saved_symbol.endswith("USDT"):
        SYMBOL = saved_symbol  # 重启后恢复上次实际交易对，避免错管/漏管持仓
        BASE_ASSET = SYMBOL.replace("USDT", "")
        print(f"已恢复上次交易对：{SYMBOL}")
    set_leverage()
    ensure_one_way_mode()
    print(f"机器人启动 | {SYMBOL} 斜率趋势策略 | 趋势窗口 最近{WINDOW_HOURS}小时（界面可选 1/3/6/12）| 杠杆 {LEVERAGE}x | 全仓模式（保证金用余额的 {POSITION_RATIO*100:.0f}%）| 止盈 {'关闭' if TP_USDT <= 0 else f'{TP_USDT:.0f}U'} / 止损 {SL_USDT if SL_USDT > 0 else '关闭'} | 动态回撤 {TRAIL_PCT*100:.0f}%（从最佳价回撤即平仓）| 资金费率监控（|费率|>{FUNDING_RATE_LIMIT*100:.1f}% 逆方向拒开）| 下单：限价优先（maker），超时 {LIMIT_TIMEOUT}s 转市价 | 空仓时每 {SCAN_INTERVAL}s 扫描全市场 Top{SCAN_TOP_N} 干净趋势目标，开仓需在可视化界面确认，清仓自动 | 界面停止：创建 {os.path.basename(STOP_FILE)}")
    print(f"合约钱包 USDT 余额: {get_wallet_balance():.2f}")
    print(f"当前持仓: {'记录有持仓' if open_position else '记录空仓'} | 历史已平仓 {len(trades)} 笔")
    if is_paused():
        print(f"[警告] 存在暂停标记（{os.path.basename(PAUSE_FILE)}），机器人启动后保持暂停状态，删除该文件后恢复自动交易")
    risk_exit_side = None  # 止盈/止损离场方向：同方向不立即重进，等信号翻向对面再开仓
    last_scan = 0  # 空仓扫描上次执行时间（0 = 启动后立即首次扫描）
    last_scan_str = "启动后未扫描"  # 上次扫描时间字符串（写入状态文件供界面显示）
    cand_count = 0  # 上次扫描到的干净目标数量
    while True:
        try:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 界面"停止程序"按钮：存在 stop_request.flag 即优雅退出
            if os.path.exists(STOP_FILE):
                print(f"[{now}] 收到界面停止请求，机器人退出", flush=True)
                try:
                    os.remove(STOP_FILE)
                except Exception:
                    pass
                sys.exit(0)
            # 读取界面选择的趋势窗口（scan_window.json），本轮回合开始前生效
            if os.path.exists(SCAN_WINDOW_FILE):
                try:
                    with open(SCAN_WINDOW_FILE, "r", encoding="utf-8") as f:
                        wh = json.load(f).get("hours")
                    if wh in WINDOW_OPTIONS and wh != WINDOW_HOURS:
                        WINDOW_HOURS = wh
                        print(f"[{now}] 趋势窗口已切换为最近 {WINDOW_HOURS} 小时（{window_bars()} 根 {window_interval()[0]} K线），下一轮扫描/信号将按新窗口计算", flush=True)
                except Exception:
                    pass
            slope_pct, r2, last = check_signal()
            brk_score = trend_break_progress(r2, slope_pct)
            trend_ok = (r2 >= R2_ENTER) and (abs(slope_pct) >= slope_min())
            sig_dir = (1 if slope_pct > 0 else -1) if trend_ok else 0
            signal_str = {1: "多", -1: "空", 0: "观望"}[sig_dir]
            realized, floating, total = calc_pnl(trades, open_position, last)
            pos_str = f"{open_position['side']}" if open_position else "空仓"
            print(f"[{now}] {SYMBOL} {last:.4f} | 斜率 {slope_pct*100:+.4f}%/根 | R² {r2:.2f} | 信号{signal_str} | 持仓{pos_str} | 趋势破坏进度 {brk_score}分 | 已实现 {realized:.2f}U | 浮动 {floating:+.2f}U | 总盈亏 {total:+.2f}U",
                  flush=True)

            append_pnl_history(now, last, realized, floating, total, signal_str, pos_str)

            amt, entry = get_position()  # 实际交易所持仓
            rec_pos = open_position       # 本地记录持仓

            # 写入运行状态快照（界面状态显示与心跳检测）——放在扫描之前，扫描耗时中心跳保持新鲜
            paused = pause_reason()
            write_status(now, last, realized, floating, total, signal_str, pos_str, paused, last_scan_str, cand_count, brk_score)

            # ---- 空仓扫描（只读，暂停时也照常扫描以保持界面数据新鲜）----
            if amt == 0 and time.time() - last_scan >= SCAN_INTERVAL:
                last_scan = time.time()
                last_scan_str = now
                cands = scan_top()
                cand_count = len(cands)
                recs = [{"rank": i, "symbol": c["symbol"], "direction": c["direction"],
                         "r2": c["r2"], "slope_pct": c["slope_pct"], "price": c["price"],
                         "vol": c["vol"], "score": c["score"]} for i, c in enumerate(cands, 1)]
                with open(SCAN_RESULTS_FILE, "w", encoding="utf-8") as f:
                    json.dump({"time": now, "hours": WINDOW_HOURS, "candidates": recs}, f, ensure_ascii=False, indent=2)
                if cands:
                    c = cands[0]
                    print(f"[{now}] [扫描] 发现 {len(cands)} 个干净趋势目标，Top1 {c['symbol']}：R² {c['r2']:.2f} | 斜率 {c['slope_pct']*100:+.4f}%/根 | {('上升趋势（做多）' if c['slope_pct'] > 0 else '下降趋势（做空）')} | 价格 {c['price']:.6f} | 量 {c['vol']/1000000:.1f}M | 请在可视化界面选择开仓", flush=True)
                else:
                    print(f"[{now}] [扫描] 当前市场无干净趋势目标，继续空仓等待（界面显示 0 个候选）", flush=True)

            # 手动暂停保护：检测交易所仓位被外部改动（手动平仓/手动开仓/手动翻向），自动暂停不自动交易
            if paused:
                print(f"[{now}] [暂停中] {paused}，等待恢复（删除 {os.path.basename(PAUSE_FILE)} 或在界面点击恢复交易）", flush=True)
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

            # ---- 动态回撤止损/止盈：价格从入场以来最佳价回撤达 10% 即平仓（锁利或止损）----
            if amt != 0:
                trail_hit, new_best, dd = check_trailing(rec_pos, amt, last)
                if rec_pos:
                    cur_best = float(rec_pos.get("best_price") or rec_pos["price"])
                    if abs(cur_best - new_best) > 1e-12:
                        rec_pos["best_price"] = new_best
                        save_trades(trades, open_position)  # 持久化最佳价，重启后继续跟踪
                    if trail_hit:
                        pos_side = "LONG" if amt > 0 else "SHORT"
                        fq, fp, otype, fee = smart_close(abs(amt), pos_side)
                        trades.append(make_close_record(rec_pos, fq, fp, otype, fee, pos_side, now, "动态回撤"))
                        print(f"[{now}] >>> 动态回撤平{('多' if pos_side == 'LONG' else '空')} @ {fp:.4f}（{otype}），回撤 {dd*100:.2f}%，盈亏 {trades[-1]['pnl']:+.2f} USDT（手续费 {fee:.4f}U）", flush=True)
                        open_position = None
                        save_trades(trades, open_position)
                        risk_exit_side = pos_side
                        amt = 0

            # ---- 斜率趋势策略进出场 ----
            # 保持条件：R² 与斜率未跌破离场阈值时继续持有
            keep_pos = (r2 >= R2_EXIT) and (abs(slope_pct) >= slope_exit())
            if amt != 0:
                pos_side = "LONG" if amt > 0 else "SHORT"
                if not keep_pos:
                    # 趋势破坏 -> 空仓
                    fq, fp, otype, fee = smart_close(abs(amt), pos_side)
                    trades.append(make_close_record(rec_pos, fq, fp, otype, fee, pos_side, now, "趋势破坏"))
                    print(f"[{now}] >>> 趋势破坏平{('多' if pos_side == 'LONG' else '空')} @ {fp:.4f}（{otype}），盈亏 {trades[-1]['pnl']:+.2f} USDT（手续费 {fee:.4f}U）", flush=True)
                    open_position = None
                    save_trades(trades, open_position)
                    amt = 0
                elif sig_dir != 0 and ((pos_side == "LONG" and sig_dir < 0) or (pos_side == "SHORT" and sig_dir > 0)):
                    # 趋势仍干净但斜率反向 -> 直接翻转
                    fq, fp, otype, fee = smart_close(abs(amt), pos_side)
                    trades.append(make_close_record(rec_pos, fq, fp, otype, fee, pos_side, now, "趋势翻转"))
                    print(f"[{now}] >>> 趋势翻转平{('多' if pos_side == 'LONG' else '空')} @ {fp:.4f}（{otype}），盈亏 {trades[-1]['pnl']:+.2f} USDT（手续费 {fee:.4f}U）", flush=True)
                    open_position = None
                    save_trades(trades, open_position)
                    amt = 0
            # ---- 空仓：处理可视化界面的开仓请求（open_request.json），清仓始终自动 ----
            if amt == 0 and os.path.exists(OPEN_REQUEST_FILE):
                try:
                    with open(OPEN_REQUEST_FILE, "r", encoding="utf-8") as f:
                        req = json.load(f)
                except Exception:
                    req = None
                if req and req.get("symbol"):
                    sym = str(req["symbol"]).upper().strip()
                    os.remove(OPEN_REQUEST_FILE)  # 先删请求，避免重复处理
                    if not sym.endswith("USDT"):
                        LAST_OPEN_NOTE.update({"text": f"请求币种 {sym} 格式非法（需以 USDT 结尾），已忽略", "time": now})
                        print(f"[{now}] [开仓] 请求币种 {sym} 格式非法（需以 USDT 结尾），已忽略", flush=True)
                        continue
                    ok, slope2, r22, price2 = verify_symbol(sym)
                    if not ok:
                        LAST_OPEN_NOTE.update({"text": f"请求币种 {sym} 复核不通过：趋势已不干净（R² {r22:.2f} < 进场阈值 {R2_ENTER}），放弃开仓", "time": now})
                        print(f"[{now}] [开仓] 请求币种 {sym} 趋势已不干净（R² {r22:.2f}），放弃开仓，继续扫描", flush=True)
                        continue
                    side = "LONG" if slope2 > 0 else "SHORT"
                    if risk_exit_side == side:
                        LAST_OPEN_NOTE.update({"text": f"{sym} 当前方向与止盈/止损离场方向相同，暂不开仓，等待方向翻向对面", "time": now})
                        print(f"[{now}] [开仓] {sym} 当前{('上升（做多）' if slope2 > 0 else '下降（做空）')}方向与止盈/止损离场方向相同，暂不开仓，等待方向翻向对面", flush=True)
                        continue
                    # ---- 资金费率监控：逆费率方向开仓且费率超阈值时拒绝（顺费率方向可收资金费，放行）----
                    fund_rate = get_funding_rate(sym)
                    if fund_rate is not None and abs(fund_rate) > FUNDING_RATE_LIMIT:
                        pay = (side == "LONG" and fund_rate > 0) or (side == "SHORT" and fund_rate < 0)
                        if pay:
                            LAST_OPEN_NOTE.update({"text": f"{sym} 资金费率 {fund_rate*100:.3f}%/8h，做{('多' if side == 'LONG' else '空')}需付高额资金费，拒绝开仓（阈值 ±{FUNDING_RATE_LIMIT*100:.1f}%）", "time": now})
                            print(f"[{now}] [拦截] {sym} 资金费率 {fund_rate*100:.3f}%/8h，做{('多' if side == 'LONG' else '空')}需付高额资金费（每 8 小时约扣名义×{fund_rate*100:.2f}%），拒绝开仓。阈值 ±{FUNDING_RATE_LIMIT*100:.1f}%；如改做{('空' if side == 'LONG' else '多')}可收取资金费", flush=True)
                            continue
                        print(f"[{now}] [提示] {sym} 资金费率 {fund_rate*100:.3f}%/8h，顺费率方向开{('多' if side == 'LONG' else '空')}可收资金费，继续开仓", flush=True)
                    SYMBOL = sym
                    BASE_ASSET = SYMBOL.replace("USDT", "")
                    set_leverage()
                    qty_to_trade, balance = calc_full_qty(price2)
                    if qty_to_trade <= 0 or qty_to_trade * price2 / LEVERAGE > balance:
                        LAST_OPEN_NOTE.update({"text": f"{sym} 余额不足无法开{('多' if side == 'LONG' else '空')}（可用 {balance:.2f} USDT）", "time": now})
                        print(f"[{now}] [警告] 余额不足无法开{('多' if side == 'LONG' else '空')}（可用 {balance:.2f} USDT），跳过本轮", flush=True)
                        continue
                    fill_qty, fill_px, otype, entry_fee = smart_open(side, qty_to_trade)
                    risk_exit_side = None
                    open_position = {"time": now, "side": side, "qty": fill_qty, "price": fill_px, "cost": fill_qty * fill_px, "order_type": otype, "entry_fee": entry_fee, "best_price": fill_px}
                    save_trades(trades, open_position)
                    if os.path.exists(SCAN_RESULTS_FILE):
                        os.remove(SCAN_RESULTS_FILE)
                    LAST_OPEN_NOTE.update({"text": f"已开{('多' if side == 'LONG' else '空')} {sym} {fill_qty} {BASE_ASSET} @ {fill_px:.6f}（名义 {fill_qty*fill_px:.2f} USDT）", "time": now})
                    print(f"[{now}] >>> 界面确认开{('多' if side == 'LONG' else '空')} {sym} {fill_qty} {BASE_ASSET} @ {fill_px:.6f}，名义 {fill_qty*fill_px:.2f} USDT（全仓·{otype}）", flush=True)
        except Exception as e:
            print(f"[{now}] 出错: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if "--check" in sys.argv:
        slope_pct, r2, last = check_signal()
        print(f"{SYMBOL} 最新价 {last:.4f} | 斜率 {slope_pct*100:+.4f}%/根 | R² {r2:.2f}（进场≥{R2_ENTER}，离场<{R2_EXIT}）")
        if r2 >= R2_ENTER and abs(slope_pct) >= slope_min():
            print("当前信号:", "上升趋势（应开多）" if slope_pct > 0 else "下降趋势（应开空）")
        else:
            print("当前信号: 观望（趋势不干净，空仓等待）")
        print(f"合约钱包 USDT 余额: {get_wallet_balance():.2f}")
    else:
        main()
