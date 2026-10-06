"""
币安 U 本位合约 趋势跟随量化交易机器人（实盘，仅做多）
======================================================
策略来源：回测隔离配置（backtest_v4.py 的 ISOLATE 分支 + v3 信号已验证）。
- 选币池：成交量前 30 的 USDT 永续（每 4 小时整点刷新）。
- 入场信号：1h 线性回归 R²≥0.70 且 |斜率|≥0.00015，且 slope>0（仅做多）；
            需 15m K 线连续满足趋势 4 根（CONF=4）才开仓，无手动确认。
- 仓位：每仓 2U 保证金 × 3x = 6U 名义，最多同时 MAX_POS=8 仓（100U 账户，对齐回测基准）。
- 离场：吊灯追踪（Chandelier） K=6×ATR(15m,14)，价格跌破 maxe-6×ATR 即市价平仓，
        reason=吊灯止损；无止盈/固定止损/评分回撤。
- 回撤闸门：刻意移除（回测中造成"死亡螺旋"全面亏损，是 v4 原版亏损元凶）。

回测：90 天 / 30 币 / K6 确认4 仅多  → 年化 +39.9%、最大回撤 19.3%、570 笔、胜率 38%。

风险提示：本程序使用真实资金自动交易，合约有强平风险。请先用小金额验证。
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

SYMBOL = "CARVUSDT"                                # 界面展示主标的（策略不依赖单币）
INTERVAL = Client.KLINE_INTERVAL_15MINUTE          # 通用取价/ATR 用 15m K 线

# ---- 交易参数（对齐回测隔离配置：100U 账户基准） ----
LEVERAGE = 3                                        # 杠杆倍数
POS_MARGIN_USDT = 2.0                               # 每仓保证金 2U，×杠杆 = 名义 6U
MAX_POS = 8                                         # 同时最大持仓数（回测基准 100U/6U 名义/8 仓）
VOL_POOL_N = 30                                     # 选币池：成交量前 N 的 USDT 永续
VOL_POOL_FILE = r"d:\bian\vol_pool.json"            # 选币池缓存（每 4 小时整点刷新）

# ---- 趋势信号参数（回测最优：R²=0.85 斜率=0.0001 K=5.0 确认=2，年化51.1%/回撤19.5%） ----
R2_ENTRY = 0.85                                     # 1h 回归 R² 门槛（更严格，过滤弱趋势）
SL_H = 0.0001                                       # 1h 每根斜率门槛（波动比例）
H_WIN = 24                                          # 1h 回归窗口根数
CONF = 2                                            # 15m 连续满足趋势的确认根数（减少确认，更快进场）

# ---- 吊灯止损参数 ----
K = 5.0                                             # 吊灯 ATR 倍数（回测最优：比6更紧，锁定利润更快）
ATR_N = 14                                          # ATR 周期（15m K 线）

# ---- 时序与节流 ----
ENTRY_INTERVAL = 900                                # 每 15 分钟（新 15m bar 收盘）评估一次开仓
POLL_SECONDS = 5                                    # 主循环轮询秒数（持仓吊灯跟踪频率）

# ---- 风控 / 接口 ----
FUNDING_RATE_LIMIT = 0.005                          # 资金费率阈值：开多时 |正费率(多头付费)| 超此值跳过
TAKER_FEE_RATE = 0.0005                             # 吃单成交（taker）手续费率
MAKER_FEE_RATE = 0.0002                             # 挂单成交（maker）手续费率（保留，dashboard 可能读取）

# ---- 文件 ----
STATUS_FILE = r"d:\bian\bot_status.json"            # 轮询写入运行状态快照（界面状态显示与心跳检测）
STOP_FILE = r"d:\bian\stop_request.flag"            # 界面"停止程序"按钮：存在此文件优雅退出
VOL_POOL_FILE  = VOL_POOL_FILE
TRADE_LOG = os.path.join(os.path.dirname(__file__), "trade_log.json")  # 交易记录（含持仓）
PNL_LOG = os.path.join(os.path.dirname(__file__), "pnl_history.json")   # 分时盈亏记录
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


# ---------------- 合约辅助函数（工程层，复用） ----------------

def set_leverage(symbol=None):
    sym = symbol or SYMBOL
    try:
        client.futures_change_leverage(symbol=sym, leverage=LEVERAGE)
    except Exception as e:
        print(f"[警告] 设置杠杆失败：{e}")


def ensure_one_way_mode():
    """确保合约账户为单向持仓模式（策略仅做多，单向即可）。
    双向模式下下单不带 positionSide 会报 -4061，故启动时自动校正。"""
    try:
        mode = client.futures_get_position_mode()
        if mode.get("dualSidePosition"):
            client.futures_change_position_mode(dualSidePosition=False)
            print("已切为单向持仓模式")
        else:
            print("合约账户已是单向持仓模式")
    except Exception as e:
        print(f"[警告] 检查/切换持仓模式失败：{e}")


def futures_round_qty(qty, price, symbol=None):
    """按合约交易对允许的最小下单量取整，并保证名义价值 >= 最小下单要求"""
    sym = symbol or SYMBOL
    step, min_notional = 0.1, 5.0
    try:
        info = client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == sym:
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        step = float(f["stepSize"])
                    elif f["filterType"] == "MIN_NOTIONAL":
                        min_notional = float(f["notional"])
                break
    except Exception:
        pass
    qty = int(qty / step) * step
    dec = len(str(step).split(".")[1]) if "." in str(step) else 0
    qty = round(qty, dec)
    while qty * price < min_notional:   # 名义不足最小要求则向上补足一个 step
        qty = round(qty + step, dec)
    return qty


def get_position(symbol=None):
    """返回某品种当前持仓 (数量, 方向)。数量正=多 负=空，0=空仓"""
    sym = symbol or SYMBOL
    pos = client.futures_position_information(symbol=sym)
    amt = float(pos[0]["positionAmt"]) if pos else 0.0
    entry = float(pos[0]["entryPrice"]) if pos and amt else 0.0
    return amt, entry


def get_all_positions():
    """返回当前所有非零持仓 {symbol: {"amt":,"entry":,"unrealized":,"side":}}"""
    out = {}
    try:
        for p in client.futures_position_information():
            amt = float(p.get("positionAmt") or 0)
            if amt != 0 and p.get("symbol", "").endswith("USDT"):
                out[p["symbol"]] = {
                    "amt": amt,
                    "entry": float(p.get("entryPrice") or 0),
                    "unrealized": float(p.get("unRealizedProfit") or 0),
                    "side": "LONG" if amt > 0 else "SHORT",
                }
    except Exception:
        pass
    return out


def get_wallet_balance():
    """返回 U 本位合约钱包 USDT 可用余额"""
    try:
        for b in client.futures_account_balance():
            if b["asset"] == "USDT":
                return float(b["balance"])
    except Exception:
        pass
    return 0.0


def market_order(qty, side, symbol=None):
    """市价下单，返回 (成交数量, 平均成交价, 订单id)。side: 'LONG' 买 / 'SHORT' 卖"""
    sym = symbol or SYMBOL
    order_side = Client.SIDE_BUY if side == "LONG" else Client.SIDE_SELL
    order = client.futures_create_order(
        symbol=sym, side=order_side, type=Client.ORDER_TYPE_MARKET, quantity=qty
    )
    oid = order["orderId"]
    if not order.get("avgPrice") or not order.get("executedQty"):
        order = client.futures_get_order(symbol=sym, orderId=oid)
    return float(order["executedQty"]), float(order["avgPrice"]), oid


def order_market_position(symbol, side, margin_usdt=POS_MARGIN_USDT):
    """按固定保证金（默认 2U）× 杠杆，对指定 symbol **市价**开仓。
    返回 (成交数量, 成交均价, 名义金额, 手续费估算)。"""
    notional = margin_usdt * LEVERAGE
    px = get_futures_price_for(symbol)
    qty = futures_round_qty(notional / px, px, symbol)
    fill_qty, fill_px, _ = market_order(qty, side, symbol)
    fee = fill_qty * fill_px * TAKER_FEE_RATE
    return fill_qty, fill_px, fill_qty * fill_px, fee


def get_futures_price_for(symbol):
    """取指定合约最新价（用合约 K 线）"""
    klines = client.futures_klines(symbol=symbol, interval=INTERVAL, limit=1)
    return float(klines[0][4])


def get_funding_rate(symbol):
    """查询某币当前资金费率（每 8 小时结算一次，正=多头付费给空头，负=空头付费给多头）。
    公开端点无需签名；查询失败返回 None（None 时不拦截开仓，避免网络抖动误伤）"""
    try:
        r = requests.get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}", timeout=10)
        return float(r.json().get("lastFundingRate", 0) or 0)
    except Exception:
        return None


# ---------------- 趋势跟随策略：信号 ---------------

def fetch_ohlc(symbol, interval, limit):
    """拉取合约 K 线，返回 (hi[], lo[], close[])"""
    ks = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    return ([float(k[2]) for k in ks], [float(k[3]) for k in ks], [float(k[4]) for k in ks])


def atr_simple(hi, lo, cl, n):
    """简单 ATR（Wilder 平权版本，对齐回测 atr_simple）：最近 n 根平均真实波幅"""
    trs = []
    for i in range(1, len(cl)):
        trs.append(max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])))
    return sum(trs[-n:]) / n if trs else 0.0


def linreg(prices):
    """对收盘价序列做线性回归。返回 (每根K线斜率比例, R²拟合优度)"""
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


def trend_1h(symbol):
    """1h 回归：拉最近 H_WIN 根 1h 收盘，返回 (R², 每根1h斜率, 最新收盘)"""
    _, _, cl = fetch_ohlc(symbol, Client.KLINE_INTERVAL_1HOUR, H_WIN)
    slope, r2 = linreg(cl)
    return r2, slope, cl[-1]


def trend_status(symbol):
    """返回 (是否处于趋势, 1h斜率)。"是否处于趋势"= R²≥R2_ENTRY 且 |斜率|≥SL_H，**不分方向**
    （对齐回测 sig）；做多方向在开仓时单独用 slope>0 判断。
    确认计数用"趋势强度"连续累计、开仓时才要求方向，以忠实复刻回测 hot+dir 逻辑。"""
    try:
        r2, slope, _ = trend_1h(symbol)
    except Exception:
        return False, 0.0
    return (r2 >= R2_ENTRY and abs(slope) >= SL_H), slope


# ---------------- 趋势跟随策略：选币池 ---------------

def ensure_vol_pool():
    """每 4 小时整点刷新选币池：按 24h quoteVolume 降序取成交量前 VOL_POOL_N 的 USDT 永续。
    当前 4h 段已选过则复用池文件；失败返回 []。"""
    bucket = int(time.time() // (4 * 3600))
    try:
        with open(VOL_POOL_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("bucket") == bucket and d.get("symbols"):
            return d["symbols"]
    except Exception:
        pass
    try:
        info = client.futures_exchange_info()
        vol = {t["symbol"]: float(t.get("quoteVolume", 0) or 0) for t in client.futures_ticker()}
    except Exception:
        return []
    eligible = [s["symbol"] for s in info["symbols"]
                if s["contractType"] == "PERPETUAL" and s["status"] == "TRADING"
                and s["quoteAsset"] == "USDT"]
    pool = [s for s in sorted(eligible, key=lambda s: vol.get(s, 0), reverse=True)[: VOL_POOL_N]
            if vol.get(s, 0) > 0]
    if pool:
        try:
            with open(VOL_POOL_FILE, "w", encoding="utf-8") as f:
                json.dump({"bucket": bucket, "symbols": pool,
                           "picked": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                          f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return pool


# ---------------- 趋势跟随策略：开仓 / 持仓管理 ---------------

def try_open_entries(now, positions, trades, pool, confirm):
    """每 15 分钟评估一次：对选币池逐币，1h 趋势成立则累积确认根数，连续 CONF 根后仅做多开仓。
    已持仓不计入、达到 MAX_POS 或余额不足即停止。"""
    held = set(positions)
    for sym, p in get_all_positions().items():
        if p["side"] == "LONG":
            held.add(sym)
    if len(held) >= MAX_POS:
        return
    for sym in pool:
        if sym in held:
            continue
        bal = get_wallet_balance()
        if bal < POS_MARGIN_USDT * 1.1:
            print(f"[{now}] 余额 {bal:.2f}U 不足开新仓（需 ≥{POS_MARGIN_USDT*1.1:.1f}U），停止开仓", flush=True)
            return
        active, slope = trend_status(sym)
        if not active:                 # 趋势强度不达标：确认计数清零（对齐回测 sig）
            confirm[sym] = 0
            continue
        confirm[sym] = confirm.get(sym, 0) + 1   # 趋势强度连续累计（不分方向，对齐回测 hot）
        if confirm[sym] < CONF or slope <= 0:    # 未确认满4根 或 当前非上升趋势(仅多) 则不开
            continue
        # 资金费率风控：开多由多头付费；正费率过高则跳过（避免缴纳高额资金费）
        rate = get_funding_rate(sym)
        if rate is not None and rate > FUNDING_RATE_LIMIT:
            print(f"[{now}] {sym} 正资金费率 {rate*100:.3f}%/8h 过高，跳过开多", flush=True)
            confirm[sym] = 0
            continue
        try:
            set_leverage(sym)
            fill_qty, fill_px, notional, fee = order_market_position(sym, "LONG")
            if fill_qty <= 0:
                confirm[sym] = 0
                continue
            hi, lo, cl = fetch_ohlc(sym, INTERVAL, ATR_N + 1)
            a = atr_simple(hi, lo, cl, ATR_N)
            positions[sym] = {
                "time": now, "side": "LONG", "qty": fill_qty, "price": fill_px,
                "cost": fill_qty * fill_px, "notional": notional,
                "order_type": "taker", "entry_fee": fee,
                "maxe": fill_px, "atr": a, "confirm": confirm[sym],
            }
            confirm[sym] = 0
            save_trades(trades, positions)
            print(f"[{now}] >>> 自动开多 {sym} {fill_qty} @ {fill_px:.6f}（名义 {notional:.2f}U，确认{CONF}根，市价·taker）", flush=True)
        except Exception as e:
            print(f"[{now}] 开仓 {sym} 失败：{e}", flush=True)
            confirm[sym] = 0


def manage_positions(now, positions, trades):
    """吊灯跟踪平仓：每仓追踪持仓期最高价 maxe，当前 15m 最低价跌破 maxe - K×ATR 即市价平仓。"""
    act = get_all_positions()
    # 交易所已无仓位：同步移除本地记录（可能被外部平掉）
    for sym in list(positions):
        if sym not in act:
            del positions[sym]
            save_trades(trades, positions)
            print(f"[{now}] [持仓] {sym} 交易所已无仓位，移除本地记录", flush=True)
    for sym, pos in list(positions.items()):
        try:
            hi, lo, cl = fetch_ohlc(sym, INTERVAL, ATR_N + 1)
            a = atr_simple(hi, lo, cl, ATR_N)
            if a <= 0:
                continue
            maxe = max(pos.get("maxe") or cl[-1], hi[-1])
            pos["maxe"] = maxe
            pos["atr"] = a
            trail = maxe - K * a
            pos["trail"] = trail
            pos["trail_pct"] = (hi[-1] - trail) / trail * 100 if trail > 0 else 999.0
            if lo[-1] <= trail:
                qty = abs(pos.get("qty") or act[sym]["amt"])
                fq, fp, _, fee = market_close_position(sym, qty, "LONG")
                trades.append(make_close_record(pos, fq, fp, "taker", fee, "LONG", now, "吊灯止损"))
                del positions[sym]
                save_trades(trades, positions)
                print(f"[{now}] >>> 吊灯止损平多 {sym} @ {fp:.6f}（市价，回撤触发），盈亏 {trades[-1]['pnl']:+.2f}U", flush=True)
        except Exception as e:
            print(f"[{now}] [持仓] {sym} 吊灯跟踪出错：{e}", flush=True)


# ---------------- 状态 / 交易记录 ---------------

def write_status(now, realized, floating, total, signal_str, positions, cand_count):
    """每轮轮询写入运行状态快照，供可视化界面显示状态与心跳检测。"""
    st_data = {
        "time": now,
        "symbol": SYMBOL,
        "balance": get_wallet_balance(),
        "realized": realized,
        "floating": floating,
        "total": total,
        "signal": signal_str,
        "position": "\n".join(f"{s}:{p.get('side','?')}" for s, p in positions.items()) or "空仓",
        "positions": positions,
        "candidate_count": cand_count,
        "strategy": "v4趋势跟随·仅多·吊灯K6×ATR",
        "open_note": "",
        "open_note_time": "",
    }
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(st_data, f, ensure_ascii=False, indent=2)


def load_trades():
    """读取交易记录。返回 (trades, positions, 主symbol)。"""
    if not os.path.exists(TRADE_LOG):
        return [], {}, ""
    try:
        with open(TRADE_LOG, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("trades", []), data.get("positions", {}) or {}, data.get("symbol", "")
    except Exception:
        return [], {}, ""


def save_trades(trades, positions):
    """持久化交易记录：trades 历史列表 + positions 多持仓 dict。"""
    with open(TRADE_LOG, "w", encoding="utf-8") as f:
        json.dump({"trades": trades, "positions": positions, "symbol": SYMBOL},
                  f, ensure_ascii=False, indent=2)


def calc_pnl(trades, positions, prices_map):
    """盈亏：已实现 + 多持仓浮动总和。prices_map: {symbol: 最新价}。"""
    realized = sum(t.get("pnl", 0) for t in trades)
    floating = 0.0
    for sym, pos in (positions or {}).items():
        qty = pos.get("qty") or 0
        entry = pos.get("price") or 0
        px = prices_map.get(sym, entry)
        side = 1 if pos.get("side") == "LONG" else -1
        floating += (px - entry) * qty * side
    return realized, floating, realized + floating


def append_pnl_history(now, price, realized, floating, total, signal, position):
    """每轮轮询追加一条分时盈亏快照到 pnl_history.json（最多保留 10000 条）"""
    rec = {"time": now, "price": round(price, 6), "realized": round(realized, 4),
           "floating": round(floating, 4), "total": round(total, 4),
           "signal": signal, "position": position}
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


def make_close_record(rec_pos, qty, fill_px, otype, fee, side, now_str, reason=""):
    """生成一条平仓交易记录。reason 例：'吊灯止损'"""
    if side == "LONG":
        pnl = (fill_px - rec_pos["price"]) * qty if rec_pos else 0.0
        buy_cost = rec_pos["cost"] if rec_pos else qty * fill_px
        entry_price = rec_pos["price"] if rec_pos else 0
        sell_revenue = qty * fill_px
    else:
        pnl = (rec_pos["price"] - fill_px) * qty if rec_pos else 0.0
        buy_cost = qty * fill_px
        entry_price = rec_pos["price"] if rec_pos else 0
        sell_revenue = rec_pos["cost"] if rec_pos else qty * fill_px
    rec = {
        "buy_time": rec_pos["time"] if rec_pos else now_str,
        "buy_price": entry_price, "buy_qty": qty, "buy_cost": buy_cost,
        "sell_time": now_str, "sell_price": fill_px, "sell_qty": qty,
        "sell_revenue": sell_revenue, "pnl": pnl, "side": side,
        "entry_type": rec_pos.get("order_type", "—") if rec_pos else "—",
        "exit_type": otype,
        "entry_fee": rec_pos.get("entry_fee", 0) if rec_pos else 0,
        "exit_fee": fee,
    }
    if reason:
        rec["reason"] = reason
    return rec


def market_close_position(symbol, qty, position_side):
    """按指定 symbol **市价**平仓。position_side: 'LONG'/'SHORT'。
    返回 (成交数量, 成交均价, 名义成交额, 手续费估算)。"""
    close_side = "SHORT" if position_side == "LONG" else "LONG"
    qty = futures_round_qty(qty, get_futures_price_for(symbol), symbol)
    fill_qty, fill_px, _ = market_order(qty, close_side, symbol)
    fee = fill_qty * fill_px * TAKER_FEE_RATE
    return fill_qty, fill_px, fill_qty * fill_px, fee


# ---------------- 主循环 ----------------

def main():
    trades, positions, _saved = load_trades()
    print(f"机器人启动 | v4 趋势跟随·仅多 | 成交量前{VOL_POOL_N}·吊灯K{K}×ATR·确认{CONF}根 | 每仓 {POS_MARGIN_USDT:.0f}U×{LEVERAGE}x=(名义{POS_MARGIN_USDT*LEVERAGE:.0f}U) | 最多{MAX_POS}仓 | 每{ENTRY_INTERVAL//60}分钟评估开仓 | reason=吊灯止损")
    print(f"合约钱包 USDT 余额: {get_wallet_balance():.2f} | 历史已平仓 {len(trades)} 笔 | 当前持仓 {len(positions)} 个")
    ensure_one_way_mode()
    confirm = {}            # 内存：各币趋势连续确认根数
    pool = []
    last_entry = 0
    last_pool = 0
    cand_count = 0
    while True:
        try:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if os.path.exists(STOP_FILE):
                print(f"[{now}] 收到界面停止请求，机器人退出", flush=True)
                try:
                    os.remove(STOP_FILE)
                except Exception:
                    pass
                sys.exit(0)

            # 一、持仓吊灯跟踪止损
            manage_positions(now, positions, trades)

            # 二、周期刷新选币池（每 4 小时）与评估开仓（每 15 分钟）
            if time.time() - last_pool >= 4 * 3600:
                pool = ensure_vol_pool()
                last_pool = time.time()
                last_entry = 0     # 池刷新后立即评估一次
                print(f"[{now}] [选币池] 刷新成交量前{VOL_POOL_N}：{pool if pool else '空'}", flush=True)
            if time.time() - last_entry >= ENTRY_INTERVAL:
                last_entry = time.time()
                if not pool:
                    pool = ensure_vol_pool()
                try_open_entries(now, positions, trades, pool, confirm)

            # 三、汇总盈亏与状态
            prices = {}
            for sym in list(positions):
                try:
                    prices[sym] = get_futures_price_for(sym)
                except Exception:
                    pass
            realized, floating, total = calc_pnl(trades, positions, prices)
            sig_str = f"{len(positions)}仓" if positions else "空仓"
            hold_info = " ".join(f"{s}(距吊灯{positions[s].get('trail_pct',0):.0f}%)" for s in positions) or "空仓"
            print(f"[{now}] 持仓 {len(positions)} | 已实现 {realized:.2f}U | 浮动 {floating:+.2f}U | 总盈亏 {total:+.2f}U | {hold_info}", flush=True)
            write_status(now, realized, floating, total, sig_str, positions, cand_count)
            append_pnl_history(now, 0.0, realized, floating, total, sig_str, sig_str)
        except Exception as e:
            print(f"[{now}] 出错: {e}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if "--check" in sys.argv:
        import random
        pool = ensure_vol_pool() or []
        print(f"钱包余额: {get_wallet_balance():.2f}U | 选币池前{VOL_POOL_N}: {len(pool)} 币", flush=True)
        for sym in pool[:3]:
            try:
                r2, slope, last = trend_1h(sym)
                a = atr_simple(*fetch_ohlc(sym, INTERVAL, ATR_N + 1), ATR_N)
                ok = "可开多" if (r2 >= R2_ENTRY and abs(slope) >= SL_H and slope > 0) else "观望"
                print(f"  {sym}: R²{r2:.2f} | 斜率{slope*100:+.5f}%/根 | 收盘{last:.5f} | ATR{a:.5f} | {ok}", flush=True)
            except Exception as e:
                print(f"  {sym}: 读取失败 {e}", flush=True)
    else:
        main()