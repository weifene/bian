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
MIN_VOLUME = 2000000                                 # 选币扫描：24h 成交量下限（USDT），过滤空气币
SCAN_INTERVAL = 15                                    # 只扫 top20 高波动币，扫描间隔（秒）：每 15 秒扫一轮
SCAN_TOP_N = 20                                       # 扫描的高波动币数量：按日内振幅排序取前 20 只
SCAN_RESULTS_FILE = r"d:\bian\scan_results.json"     # 扫描结果（Top 20）写入此文件，供可视化界面读取展示
SCAN_PROGRESS_FILE = r"d:\bian\scan_progress.json"  # 扫描进度实时写入此文件（当前/总数/已发现/下次时间），供界面显示刷新进度
QUEUE_SNAPSHOT_FILE = r"d:\bian\scan_queue_snapshot.json"  # 上次扫描候选队列快照（评分降序 symbol 列表），用于判断队列变化 ≥10% 触发补仓
TOP20_POOL_FILE = r"d:\bian\top20_pool.json"      # 当日高波动 top20 候选池（每天更新一次），池内每 15 秒高频评分扫描
OPEN_REQUEST_FILE = r"d:\bian\open_request.json"     # 可视化界面选定的开仓币种写入这里，机器人读取后开仓（开仓后自动删除）
LAST_OPEN_NOTE = {"text": "", "time": ""}             # 最近一次界面开仓请求的处理结果（拒绝原因/成功信息），写入状态快照供界面显示
STOP_FILE = r"d:\bian\stop_request.flag"             # 可视化界面"停止程序"按钮：存在此文件机器人优雅退出
STATUS_FILE = r"d:\bian\bot_status.json"             # 每轮轮询写入运行状态快照（界面状态显示与心跳检测）
LEVERAGE = 3                                        # 杠杆倍数（3x：每仓 6U 保证金 → 名义金额 ≈ 18U）
TARGET_POSITIONS = 5                                # 自动组合持仓目标数：最多同时持有 5 个品种
POS_MARGIN_USDT = 6.0                               # 每个品种下单保证金（USDT）固定 6U，×杠杆 = 名义金额
ENTRY_SCORE = 800                                   # 自动开仓评分门槛：评分 > 800 才考虑补仓
QUEUE_CHANGE_PCT = 0.10                             # 触发补仓的扫描队列变化阈值：本次候选队列较上次变化 ≥10% 才补仓
TRAIL_PCT = 0.10                                    # 动态回撤止损/止盈：价格从入场以来最佳价（多=最高/空=最低）回撤达到 10% 即平仓（锁利或止损）
SCORE_DROP_PCT = 0.40                               # 评分制卖出：持仓期间跟踪最高评分（R²×1000+|斜率|×100000），评分较峰值下降 40% 即平仓
                                                    #   （从 25% 放宽到 40%，降低对实时评分抖动敏感度，减少"刚开就平"的手续费损耗；峰值用开仓当时实时评分口径）
FUNDING_RATE_LIMIT = 0.005                          # 资金费率监控阈值：|资金费率| 超过 0.1%（每8小时）且逆费率方向开仓时拒绝。
                                                    #   资金费率正=多头付费给空头，负=空头付费给多头；顺费率方向开仓可收取资金费，不拦截
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

def set_leverage(symbol=None):
    sym = symbol or SYMBOL
    try:
        client.futures_change_leverage(symbol=sym, leverage=LEVERAGE)
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


def futures_round_qty(qty, price, symbol=None):
    """按合约交易对允许的最小下单量取整，并保证名义价值 >= 最小下单要求"""
    sym = symbol or SYMBOL
    step, min_notional = 0.1, 5.0
    info = client.futures_exchange_info()
    for s in info["symbols"]:
        if s["symbol"] == sym:
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


def get_position(symbol=None):
    """返回某品种当前持仓 (数量, 方向)。数量正=多 负=空，0=空仓"""
    sym = symbol or SYMBOL
    pos = client.futures_position_information(symbol=sym)
    amt = float(pos[0]["positionAmt"]) if pos else 0.0
    entry = float(pos[0]["entryPrice"]) if pos and amt else 0.0
    return amt, entry


def get_all_positions():
    """返回当前所有非零持仓 {symbol: {"amt":, "entry":, "unrealized":}}（单向模式，USD 本位永续）"""
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
    # 偶发：下单成功但响应缺成交字段（连接异常导致），回查订单补全
    if not order.get("avgPrice") or not order.get("executedQty"):
        order = client.futures_get_order(symbol=sym, orderId=oid)
    return float(order["executedQty"]), float(order["avgPrice"]), oid


def order_market_position(symbol, side, margin_usdt=POS_MARGIN_USDT):
    """按固定保证金（默认 6U）× 杠杆，对指定 symbol **市价**开仓。
    side: 'LONG'/'SHORT'。返回 (成交数量, 成交均价, 名义金额, 手续费估算)。
    symbol 需为某 USDT 永续。"""
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
    except Exception as e:
        print(f"[警告] 资金费率查询失败（{symbol}）：{e}")
        return None


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


def scan_windows(symbol):
    """拉取 30 根 1 分钟 K 线（覆盖最近 30 分钟），回归评分单一 30 分钟窗口 + 日内振幅。
    返回 dict：{"score","r2","slope_pct","direction","ampl"}（ampl=30 分钟窗口内振幅%）；失败返回 {}"""
    try:
        klines = client.futures_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=31)
    except Exception:
        return {}
    if len(klines) < 30:
        return {}
    closes = [float(k[4]) for k in klines[-30:]]
    # 日内振幅：窗口内（最高-最低）/现价（%），供界面评估波动强度（用于 top20 高波动排序）
    hi = max(float(k[2]) for k in klines[-30:])
    lo = min(float(k[3]) for k in klines[-30:])
    last_px = closes[-1]
    ampl = (hi - lo) / last_px * 100 if last_px > 0 else 0.0
    slope_pct, r2 = linreg(closes)
    return {"slope_pct": slope_pct, "r2": r2,
            "score": r2 * 1000 + abs(slope_pct) * 100000,  # R² 优先，斜率次之
            "direction": 1 if slope_pct > 0 else -1,
            "ampl": ampl}


def score_for(symbol):
    """按单一 30m 窗口（60 根 30m）计算某币评分与方向、最新价，用于持仓管理与自动补仓。
    返回 dict：{score, direction('LONG'/'SHORT'), price}；失败返回 None。"""
    wins = scan_windows(symbol)
    if not wins:
        return None
    score = float(wins.get("score") or 0)
    direction = "LONG" if wins.get("direction", 1) > 0 else "SHORT"
    # 取最新价
    try:
        klines = client.futures_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1MINUTE, limit=1)
        price = float(klines[0][4])
    except Exception:
        price = 0.0
    return {"score": score, "direction": direction, "price": price}


def window_interval():
    """当前趋势窗口：1 分钟 K 线 × 30 根（覆盖最近 30 分钟），单一窗口。"""
    return Client.KLINE_INTERVAL_1MINUTE, 30


def window_bars():
    """当前趋势窗口对应的 K 线根数（固定 30 根）"""
    return window_interval()[1]


def interval_minutes():
    """当前趋势窗口选用的 K 线周期分钟数（固定 1 分钟，用于斜率阈值折算）"""
    return 1


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


def trend_score(r2, slope_pct):
    """趋势评分 = R²×1000 + |斜率|×100000（与界面/扫描/回测同口径），越高趋势越干净"""
    return r2 * 1000 + abs(slope_pct) * 100000


def trend_break_progress(score, peak_score):
    """评分回撤进度 0~100 分：0 = 评分在峰值（趋势最干净），100 = 评分较峰值下降达到
    SCORE_DROP_PCT（触发评分回撤平仓）。峰值=持仓期间最高评分。"""
    if not peak_score or peak_score <= 0:
        return 0
    drop_ratio = max(0.0, (peak_score - score) / peak_score)
    return min(100, round(drop_ratio / SCORE_DROP_PCT * 100))


def _write_scan_progress(phase, current=0, total=0, found=0, next_ts=0):
    """写入扫描进度快照，供界面实时显示扫描执行情况"""
    try:
        with open(SCAN_PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump({"phase": phase, "current": current, "total": total,
                       "found": found, "next_ts": next_ts,
                       "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f,
                      ensure_ascii=False, indent=2)
    except Exception:
        pass


def ensure_top20_pool():
    """每日只更新一次高波动 top20 候选池：按日内振幅（24h 高点-低点/现价）排序取前 SCAN_TOP_N 只。
    当天已选过则直接复用池文件（不重复全市场拉取），次日自动重选。
    返回当且候选池 symbol 列表；失败返回 []。"""
    today = datetime.date.today().isoformat()
    # 已是今天选过的池，直接复用
    try:
        with open(TOP20_POOL_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == today and d.get("symbols"):
            return d["symbols"]
    except Exception:
        pass
    # 重新选池：全市场拉取一次，按振幅排序
    try:
        info = client.futures_exchange_info()
        tickers = {t["symbol"]: {"qv": float(t.get("quoteVolume", 0) or 0),
                                 "px": float(t.get("lastPrice", 0) or 0),
                                 "high": float(t.get("highPrice", 0) or 0),
                                 "low": float(t.get("lowPrice", 0) or 0)} for t in client.futures_ticker()}
    except Exception:
        return []
    eligible = [s for s in info["symbols"]
                if s["contractType"] == "PERPETUAL" and s["status"] == "TRADING"
                and s["quoteAsset"] == "USDT"
                and tickers.get(s["symbol"], {}).get("qv", 0) >= MIN_VOLUME]

    def amp(sym):
        tk = tickers.get(sym, {})
        hi, lo, px = tk.get("high", 0), tk.get("low", 0), tk.get("px", 0)
        return (hi - lo) / px * 100 if px > 0 and hi > 0 else 0.0

    top = sorted(eligible, key=lambda s: -amp(s["symbol"]))[: SCAN_TOP_N]
    syms = [s["symbol"] for s in top]
    if syms:
        try:
            with open(TOP20_POOL_FILE, "w", encoding="utf-8") as f:
                json.dump({"date": today, "symbols": syms, "picked": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                          f, ensure_ascii=False, indent=2)
            print(f"[池] 今日高波动 top{len(syms)} 候选池已更新：{' '.join(s.replace('USDT','') for s in syms)}", flush=True)
        except Exception:
            pass
    return syms


def scan_top(limit=None):
    """对当日高波动 top20 候选池（ensure_top20_pool，每天一次）逐一做单一 30m 窗口评分。
    入选条件：30m 窗口满足进场要求（R²>=R2_ENTER 且 |斜率|>=该周期折算阈值）。
    返回 list[dict]，含 score/r2/slope/direction/ampl 等字段，评分降序。界面按评分排序展示。
    扫描过程中实时写入 SCAN_PROGRESS_FILE，供界面显示进度。"""
    pool = ensure_top20_pool()
    if not pool:
        _write_scan_progress("error")
        return []
    # 拉一次行情用于价格/成交量/振幅展示（复用池，无需每天重选）
    try:
        tickers = {t["symbol"]: {"qv": float(t.get("quoteVolume", 0) or 0),
                                 "px": float(t.get("lastPrice", 0) or 0),
                                 "high": float(t.get("highPrice", 0) or 0),
                                 "low": float(t.get("lowPrice", 0) or 0)} for t in client.futures_ticker()}
    except Exception:
        _write_scan_progress("error")
        return []

    def amp(sym):
        tk = tickers.get(sym, {})
        hi, lo, px = tk.get("high", 0), tk.get("low", 0), tk.get("px", 0)
        return (hi - lo) / px * 100 if px > 0 and hi > 0 else 0.0

    total = len(pool)
    cands = []
    m = interval_minutes()  # 30 分钟窗口，斜率阈值按周期折算
    for i, sym in enumerate(pool, 1):
        tk = tickers.get(sym, {})
        wins = scan_windows(sym)
        if wins:
            if wins["r2"] >= R2_ENTER and abs(wins["slope_pct"]) >= SLOPE_MIN_PCT * m / 15.0:
                cands.append({
                    "symbol": sym,
                    "price": tk["px"], "vol": tk["qv"],
                    "ampl": wins.get("ampl", amp(sym)),      # 日内振幅（%），供界面展示与排序
                    "r2": wins["r2"], "slope_pct": wins["slope_pct"],
                    "score": wins["score"], "direction": wins["direction"],
                })
        time.sleep(0.02)  # 防接口限频
        # 每约 5 只写一次进度（top20 很快，保持界面流畅）
        if i % 5 == 0 or i == total:
            _write_scan_progress("scanning", current=i, total=total, found=len(cands))
    cands.sort(key=lambda c: -c["score"])
    result = cands if limit is None else cands[:limit]
    _write_scan_progress("done", current=total, total=total, found=len(result),
                         next_ts=time.time() + SCAN_INTERVAL)
    return result


def write_status(now, realized, floating, total, signal_str, positions, last_scan_str, cand_count, avg_break_score):
    """每轮轮询写入运行状态快照，供可视化界面显示状态与心跳检测。
    positions: dict{symbol: pos}，avg_break_score: 各持仓评分回撤进度均值（用于状态卡片展示）。"""
    st_data = {
        "time": now,
        "symbol": SYMBOL,
        "balance": get_wallet_balance(),
        "realized": realized,
        "floating": floating,
        "total": total,
        "signal": signal_str,
        "position": "\n".join(f"{s}:{p.get('side','?')}" for s, p in positions.items()) or "空仓",
        "positions": positions,               # 多持仓 dict，界面读取展示表格
        "last_scan": last_scan_str,
        "candidate_count": cand_count,
        "break_score": avg_break_score,       # 各持仓评分回撤进度均值 0~100
        "open_note": LAST_OPEN_NOTE.get("text", ""),      # 最近一次开仓请求的处理结果（拒绝原因/成功），界面展示
        "open_note_time": LAST_OPEN_NOTE.get("time", ""), # 该结果产生的时间
    }
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(st_data, f, ensure_ascii=False, indent=2)


# ---------------- 交易记录与盈亏 ----------------

def load_trades():
    """读取交易记录。返回 (trades 列表, positions dict{symbol:pos}, 主symbol)。
    兼容旧版单持仓 open_position 字段（迁移为 positions）。"""
    if not os.path.exists(TRADE_LOG):
        return [], {}, ""
    try:
        with open(TRADE_LOG, "r", encoding="utf-8") as f:
            data = json.load(f)
        trades = data.get("trades", [])
        positions = data.get("positions", {}) or {}
        # 旧版单持仓兼容：把 open_position 迁移进 positions
        if not positions and data.get("open_position"):
            old = data["open_position"]
            sym = data.get("symbol", "CARVUSDT")
            positions[sym] = old
        return trades, positions, data.get("symbol", "")
    except Exception:
        return [], {}, ""


def save_trades(trades, positions):
    """持久化交易记录：trades 历史列表 + positions 多持仓 dict。全仓共享余额，symbol 记录主标的便于界面定位。"""
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
    """生成一条平仓交易记录（多/空通用）。reason 可为 '评分回撤'/'止盈'/'止损'/'动态回撤' 等"""
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


def market_close_position(symbol, qty, position_side):
    """按指定 symbol **市价**平仓。position_side: 被平仓位方向'LONG'/'SHORT'。
    返回 (成交数量, 成交均价, 名义成交额, 手续费估算)。"""
    close_side = "SHORT" if position_side == "LONG" else "LONG"  # 平多卖、平空买
    qty = futures_round_qty(qty, get_futures_price_for(symbol), symbol)
    fill_qty, fill_px, _ = market_order(qty, close_side, symbol)
    fee = fill_qty * fill_px * TAKER_FEE_RATE
    return fill_qty, fill_px, fill_qty * fill_px, fee


# ---------------- 多持仓组合：队列快照与补仓 ----------------

def load_queue_snapshot():
    """读取上次扫描候选队列快照（评分降序 symbol 列表）；无则返回空列表"""
    try:
        with open(QUEUE_SNAPSHOT_FILE, "r", encoding="utf-8") as f:
            return list(json.load(f).get("queue", []))
    except Exception:
        return []


def save_queue_snapshot(queue):
    """持久化本次扫描候选队列快照（评分降序 symbol 列表）"""
    try:
        with open(QUEUE_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump({"time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "queue": queue},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def queue_change_pct(last_queue, cur_queue):
    """队列变化程度（Jaccard 差异）：(|A∪B| - |A∩B|) / |A∪B|。0=相同，1=完全不同。
    返回 0~1 的小数。任一为空视为 1.0（全新队列）。"""
    if not last_queue:
        return 1.0
    if not cur_queue:
        return 0.0
    sa, sb = set(last_queue), set(cur_queue)
    inter = len(sa & sb)
    union = len(sa | sb)
    if union == 0:
        return 0.0
    return (union - inter) / union


def get_max_margin():
    """估算单仓 6U 保证金 × 杠杆下的名义金额（用于补仓判断）"""
    return POS_MARGIN_USDT * LEVERAGE


def replenish_positions(now, cands, positions, trades):
    """自动补仓：当前持仓数 < 目标数时，从达标队列（评分>ENTRY_SCORE、未持仓、未反向）按评分降序
    逐个市价开 6U 仓，直到满仓或资金不足。返回新持仓数量或 None 表示资金不足需停止。"""
    held = set(positions.keys())
    # 交易所当前已有但本地未记录的仓位也视为已持有（避免重复开）
    for sym, p in get_all_positions().items():
        held.add(sym)
    # 候选评分降序、过滤评分门槛与已持有
    candidates = [c for c in cands
                  if c["symbol"] not in held
                  and c.get("score", 0) > ENTRY_SCORE]
    candidates.sort(key=lambda c: -c["score"])
    for c in candidates:
        if len(positions) >= TARGET_POSITIONS:
            break
        sym = c["symbol"]
        side = "LONG" if c.get("direction", 1) > 0 else "SHORT"
        balance = get_wallet_balance()
        # 每仓保证金 6U：余额需 ≥ POS_MARGIN_USDT（留手续费缓冲），否则资金不足停止
        if balance < POS_MARGIN_USDT * 1.1:
            print(f"[{now}] [补仓] 余额 {balance:.2f}U 不足以再开一仓（需 ≥{POS_MARGIN_USDT*1.1:.1f}U），停止补仓", flush=True)
            return None
        try:
            set_leverage(sym)
            fill_qty, fill_px, notional, fee = order_market_position(sym, side)
            if fill_qty <= 0:
                print(f"[{now}] [补仓] {sym} 市价开仓未成交，跳过", flush=True)
                continue
            # 峰值评分用开仓当时实时评分（与持仓管理同函数同 1h 口径），避免扫描候选"最佳窗口"高分
            # 与实时 1h 评分差异过大而刚开仓就触发回撤平仓
            si = score_for(sym)
            peak = si["score"] if si else c.get("score", 0)
            positions[sym] = {
                "time": now, "side": side, "qty": fill_qty, "price": fill_px,
                "cost": fill_qty * fill_px, "notional": notional,
                "order_type": "taker", "entry_fee": fee,
                "best_price": fill_px, "peak_score": peak,
                "entry_score": c.get("score", 0),
            }
            save_trades(trades, positions)
            print(f"[{now}] >>> 自动开{('多' if side=='LONG' else '空')} {sym} {fill_qty} @ {fill_px:.6f}（名义 {notional:.2f}U，保证金 {POS_MARGIN_USDT:.0f}U，市价·taker）", flush=True)
        except Exception as e:
            print(f"[{now}] [补仓] {sym} 开仓失败：{e}", flush=True)
            continue
    return len(positions)


def main():
    global SYMBOL, BASE_ASSET, WINDOW_HOURS  # 趋势窗口由界面选择动态生效；SYMBOL 为界面展示主标的
    trades, positions, saved_symbol = load_trades()
    if saved_symbol and saved_symbol.endswith("USDT"):
        SYMBOL = saved_symbol  # 恢复上次主标的（用于界面展示与持仓恢复定位）
        BASE_ASSET = SYMBOL.replace("USDT", "")
        print(f"已恢复主标的：{SYMBOL} | 当前持仓 {len(positions)} 个")
    ensure_one_way_mode()
    print(f"机器人启动 | 多品种自动组合持仓策略 | 目标持仓 {TARGET_POSITIONS} 个 | 每仓保证金 {POS_MARGIN_USDT:.0f}U × {LEVERAGE}x（名义≈{POS_MARGIN_USDT*LEVERAGE:.0f}U）| 自动开仓评分>{ENTRY_SCORE} | 队列变化≥{QUEUE_CHANGE_PCT*100:.0f}% 触发补仓 | 卖出=评分较峰值下降 {SCORE_DROP_PCT*100:.0f}%（市价平仓）| 市价下单 | 全仓共享余额 | 界面停止：创建 {os.path.basename(STOP_FILE)}")
    print(f"合约钱包 USDT 余额: {get_wallet_balance():.2f} | 历史已平仓 {len(trades)} 笔")
    last_scan = 0  # 上次全市场扫描时间（0 = 启动后立即首次扫描）
    last_scan_str = "启动后未扫描"  # 上次扫描时间字符串（写入状态文件供界面显示）
    cand_count = 0  # 上次扫描到的干净目标数量
    last_queue = load_queue_snapshot()  # 上次扫描候选队列快照（评分降序 symbol 列表）
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
                        print(f"[{now}] 趋势窗口已切换为最近 {WINDOW_HOURS} 小时", flush=True)
                except Exception:
                    pass

            # ============ 一、管理各持仓：评分回撤达 SCORE_DROP_PCT 即市价平仓 ============
            act_positions = get_all_positions()   # 交易所实际持仓
            prices = {}                            # 各持仓最新价缓存
            for sym in list(positions.keys()):
                si = score_for(sym)
                px = si["price"] if si else 0.0
                score = si["score"] if si else 0.0
                # 该品种在交易所已无实际仓位：同步移除本地记录（可能被外部平掉）
                if sym not in act_positions:
                    del positions[sym]
                    save_trades(trades, positions)
                    print(f"[{now}] [持仓] {sym} 交易所已无仓位，移除本地记录", flush=True)
                    continue
                if si is None:
                    continue
                prices[sym] = px
                pos = positions[sym]
                # 更新峰值评分
                peak = float(pos.get("peak_score") or score)
                if score > peak:
                    peak = score
                    pos["peak_score"] = peak
                # 评分回撤触发 -> 市价平仓
                if peak > 0 and score <= peak * (1 - SCORE_DROP_PCT):
                    pos_side = pos["side"]
                    qt = abs(float(pos.get("qty") or act_positions[sym]["amt"]))
                    fq, fp, _nv, fee = market_close_position(sym, qt, pos_side)
                    trades.append(make_close_record(pos, fq, fp, "taker", fee, pos_side, now, "评分回撤"))
                    del positions[sym]
                    save_trades(trades, positions)
                    print(f"[{now}] >>> 评分回撤平{('多' if pos_side=='LONG' else '空')} {sym} @ {fp:.6f}（市价），评分 {score:.0f} 较峰值降 {(peak-score)/peak*100:.1f}%，盈亏 {trades[-1]['pnl']:+.2f}U", flush=True)
            save_trades(trades, positions)

            # ============ 二、汇总浮动盈亏 ============
            realized = sum(t.get("pnl", 0) for t in trades)
            floating = 0.0
            for sym, pos in positions.items():
                qty = pos.get("qty") or 0
                entry = pos.get("price") or 0
                px = prices.get(sym) or get_futures_price_for(sym)
                side = 1 if pos.get("side") == "LONG" else -1
                floating += (px - entry) * qty * side
            total = realized + floating

            # ============ 三、持仓数 < 目标 时：扫描并补仓 ============
            if len(positions) < TARGET_POSITIONS and time.time() - last_scan >= SCAN_INTERVAL:
                last_scan = time.time()
                last_scan_str = now
                cands = scan_top()
                cand_count = len(cands)
                # 写入扫描结果（界面展示）：单 30m 窗口评分 + 日内振幅
                recs = [{"rank": i, "symbol": c["symbol"], "direction": c.get("direction", 1),
                         "r2": c.get("r2", 0), "slope_pct": c.get("slope_pct", 0), "price": c.get("price", 0),
                         "vol": c.get("vol", 0), "ampl": c.get("ampl", 0), "score": c.get("score", 0)}
                        for i, c in enumerate(cands, 1)]
                with open(SCAN_RESULTS_FILE, "w", encoding="utf-8") as f:
                    json.dump({"time": now, "hours": WINDOW_HOURS, "candidates": recs}, f, ensure_ascii=False, indent=2)
                # 达标队列（评分>ENTRY_SCORE）用于补仓与变化判定
                cur_queue = [c["symbol"] for c in cands if c.get("score", 0) > ENTRY_SCORE]
                chg = queue_change_pct(last_queue, cur_queue)
                if cur_queue:
                    save_queue_snapshot(cur_queue)
                    last_queue = cur_queue
                if cands:
                    print(f"[{now}] [扫描] 发现 {len(cands)} 个目标，达标(>800) {len(cur_queue)} 个，队列变化 {chg*100:.0f}%", flush=True)
                else:
                    print(f"[{now}] [扫描] 当前市场无达标目标，空仓等待", flush=True)
                # 队列变化达阈值或无持仓时补仓
                if len(positions) < TARGET_POSITIONS and (chg >= QUEUE_CHANGE_PCT or len(positions) == 0):
                    replenish_positions(now, cands, positions, trades)

            # ============ 四、平均评分回撤进度 + 状态快照 ============
            brk_scores = []
            for sym in list(positions.keys()):
                si = score_for(sym)
                if not si:
                    continue
                peak = positions[sym].get("peak_score") or si["score"]
                brk_scores.append(trend_break_progress(si["score"], peak))
            avg_break = int(sum(brk_scores) / len(brk_scores)) if brk_scores else 0
            sig_str = f"{len(positions)}仓" if positions else "空仓"
            print(f"[{now}] 持仓 {len(positions)}/{TARGET_POSITIONS} | 已实现 {realized:.2f}U | 浮动 {floating:+.2f}U | 总盈亏 {total:+.2f}U | 评分回撤均值 {avg_break}分", flush=True)
            write_status(now, realized, floating, total, sig_str, positions, last_scan_str, cand_count, avg_break)
            append_pnl_history(now, 0.0, realized, floating, total, sig_str, sig_str)
        except Exception as e:
            print(f"[{now}] 出错: {e}")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    if "--check" in sys.argv:
        slope_pct, r2, last = check_signal()
        score = trend_score(r2, slope_pct)
        print(f"{SYMBOL} 最新价 {last:.4f} | 斜率 {slope_pct*100:+.4f}%/根 | R² {r2:.2f}（进场≥{R2_ENTER}）| 评分 {score:.0f}（卖出=持仓中较峰值下降{SCORE_DROP_PCT*100:.0f}%）")
        if r2 >= R2_ENTER and abs(slope_pct) >= slope_min():
            print("当前信号:", "上升趋势（应开多）" if slope_pct > 0 else "下降趋势（应开空）")
        else:
            print("当前信号: 观望（趋势不干净，空仓等待）")
        print(f"合约钱包 USDT 余额: {get_wallet_balance():.2f}")
    else:
        main()
