"""
币安 U 本位合约量化交易 - 可视化看板

页面：
  - 智能操作台：机器人状态 / 选币池与趋势信号展示 / 自动开仓（每仓2U×3x=6U，最多8仓） / 停止与启动机器人
  - 账户分析：盈亏概览 / 分时盈亏曲线 / 交易明细 / 手续费统计

与 bot.py 通过文件通信：
  - 读：bot_status.json（状态快照）、scan_results.json（扫描 Top N）、trade_log.json / pnl_history.json
  - 写：stop_request.flag（停止）

性能：使用 st.fragment(run_every=...) 局部自动刷新（仅数据区定时重绘），
不再用 JS 整页刷新，交互不卡顿、所选币种不会被刷新重置。

用法：streamlit run dashboard.py
"""
import os
import json
import time
import sys
import base64
import subprocess
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# 代理配置（与 bot.py 保持一致）
PROXY = "socks5h://127.0.0.1:7892"
if PROXY:
    os.environ["HTTP_PROXY"] = PROXY
    os.environ["HTTPS_PROXY"] = PROXY

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG = os.path.join(BASE_DIR, "trade_log.json")
PNL_LOG = os.path.join(BASE_DIR, "pnl_history.json")
SCAN_RESULTS_FILE = os.path.join(BASE_DIR, "scan_results.json")
SCAN_PROGRESS_FILE = os.path.join(BASE_DIR, "scan_progress.json")
OPEN_REQUEST_FILE = os.path.join(BASE_DIR, "open_request.json")
STOP_FILE = os.path.join(BASE_DIR, "stop_request.flag")
STATUS_FILE = os.path.join(BASE_DIR, "bot_status.json")
SCAN_WINDOW_FILE = os.path.join(BASE_DIR, "scan_window.json")
VOL_POOL_FILE = os.path.join(BASE_DIR, "vol_pool.json")
BOT_PY = os.path.join(BASE_DIR, "bot.py")

# 成交类型中文化（bot.py 写入的 maker/taker/mixed）
ORDER_TYPE_CN = {"maker": "限价", "taker": "市价", "mixed": "混合"}

st.set_page_config(page_title="币安 U 本位合约交易看板", layout="wide")


# ---------------- 从 bot.py 读取实盘策略参数（单一数据源，避免页面文案与代码漂移）----------------
import re as _re

_BOT_PARAM_CACHE = {"mtime": 0, "params": {}}


def read_bot_params():
    """直接解析 bot.py 文本中的策略常量（不 import，避免触发交易所客户端初始化）。
    返回 dict：R2_ENTRY / SL_H / H_WIN / CONF / K / ATR_N / LEVERAGE /
              POS_MARGIN_USDT / MAX_POS / VOL_POOL_N / ENTRY_INTERVAL / POLL_SECONDS / FUNDING_RATE_LIMIT。
    文件未变则走缓存。"""
    try:
        mtime = os.path.getmtime(BOT_PY)
    except OSError:
        return {}
    if mtime == _BOT_PARAM_CACHE["mtime"] and _BOT_PARAM_CACHE["params"]:
        return _BOT_PARAM_CACHE["params"]
    want = {"R2_ENTRY", "SL_H", "H_WIN", "CONF", "K", "ATR_N", "LEVERAGE",
            "POS_MARGIN_USDT", "MAX_POS", "VOL_POOL_N", "ENTRY_INTERVAL",
            "POLL_SECONDS", "FUNDING_RATE_LIMIT"}
    out = {}
    try:
        with open(BOT_PY, "r", encoding="utf-8-sig") as f:
            for line in f:
                m = _re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([0-9.]+)\s*(?:#.*)?$", line)
                if m and m.group(1) in want:
                    val = m.group(2)
                    out[m.group(1)] = float(val) if "." in val else int(val)
    except Exception:
        return {}
    _BOT_PARAM_CACHE["mtime"] = mtime
    _BOT_PARAM_CACHE["params"] = out
    return out


# ---------------- 通用数据读取 ----------------

def load_status():
    """读取机器人状态快照。返回 (dict 或 None, 是否运行中)。
    状态文件每轮轮询（1 秒）更新一次，超过 30 秒未更新视为已停止"""
    if not os.path.exists(STATUS_FILE):
        return None, False
    try:
        age = time.time() - os.path.getmtime(STATUS_FILE)
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data, age < 30
    except Exception:
        return None, False


def load_window():
    """读取当前趋势窗口小时数（scan_window.json，默认 3 小时）"""
    try:
        with open(SCAN_WINDOW_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("hours", 3)
    except Exception:
        return 3


def load_scan():
    """读取机器人扫描结果（Top N 干净趋势目标）"""
    if not os.path.exists(SCAN_RESULTS_FILE):
        return None
    try:
        with open(SCAN_RESULTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_scan_progress():
    """读取扫描进度快照（机器人扫描时实时写入）。
    若文件太旧（超过 90 秒未更新），视为已无实时进度，返回 None，
    避免展示过期/误导性的“候选择 / 下一轮倒计时”。"""
    if not os.path.exists(SCAN_PROGRESS_FILE):
        return None
    if time.time() - os.path.getmtime(SCAN_PROGRESS_FILE) > 90:
        return None
    try:
        with open(SCAN_PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_open_request(sym, side, price, score):
    """向机器人发送开仓请求（含方向/价格/评分，机器人按请求对指定币种开多，
    每仓 2U×3x=6U 名义，最多 8 仓；当前 v4 已为全自动开仓，此接口暂未使用）"""
    with open(OPEN_REQUEST_FILE, "w", encoding="utf-8") as f:
        json.dump({"symbol": sym, "side": side, "price": price, "score": score,
                   "time": time.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False, indent=2)


def start_bot():
    """以分离进程方式启动机器人（独立于本看板运行），输出重定向到 bot_console.log"""
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0
    log = open(os.path.join(BASE_DIR, "bot_console.log"), "a", encoding="utf-8")
    subprocess.Popen([sys.executable, BOT_PY], cwd=BASE_DIR, creationflags=flags,
                     stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, close_fds=True)


def detect_bot_processes():
    """检测当前正在运行的 bot.py 进程。返回 (进程数, [(PID, 启动时间), ...])；
    进程数 = -1 表示检测失败（不拦截启动）。"""
    try:
        script = ("$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                  "Where-Object { $_.CommandLine -match 'bot\\.py' }; if ($p) { "
                  "$p | ForEach-Object { \"$($_.ProcessId)|$($_.CreationDate)\" } }")
        # 用 UTF-16LE base64 编码避免 Windows 命令行引号转义问题
        enc = base64.b64encode(script.encode("utf-16-le")).decode()
        out = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", enc],
                             capture_output=True, text=True, timeout=15).stdout
        procs = []
        for ln in out.splitlines():
            ln = ln.strip()
            if "|" in ln:
                pid, _, ts = ln.partition("|")
                if pid.strip().isdigit():
                    procs.append((pid.strip(), ts.strip()))
        return len(procs), procs
    except Exception:
        return -1, []


# ---------------- 小白友好的"程序在做什么"文案 ----------------

def plain_program_status(status, alive, sp, holding):
    """根据机器人状态 + 扫描进度，产出大白话说人话的『当前在做什么』。
    返回 (emoji, 主文案)。"""
    if not alive:
        return ("🛑",
                "程序当前**未运行**。点击下方的『🚀 启动机器人』后，它才会开始扫描币种、自动买卖。")
    if holding:
        n = len(status.get("positions") or {})
        return ("📉",
                f"正在**盯守 {n} 个持仓**的“吊灯保护线”。程序每 **5 秒**用最新价核对一次："
                f"价格越涨，保护线抬得越高（= 锁定浮盈）；一旦跌穿保护线，就**自动卖出止损**，不让亏损失控。")
    # 空仓：看扫描进度（sp 为 None = 无实时进度，回退到普通“运行中”说明）
    if sp:
        phase = sp.get("phase")
        if phase == "scanning":
            return ("🔎",
                    f"当前**空仓**，正在评估选币池 {sp.get('current', 0)}/{sp.get('total', 0)} 只币的上涨趋势，"
                    f"符合条件的**候选**会在连续确认后自动买入。")
        if phase == "done":
            remain = int(sp.get('next_ts', 0) - time.time())
            found = sp.get('found', 0)
            if remain <= 0:
                return ("⏰",
                        f"当前**空仓**。刚筛选出 **{found}** 只上涨趋势**候选**，已到新一轮评估时间点，"
                        f"程序正在后台核对，符合条件的才真正买入。")
            mm, ss = divmod(remain, 60)
            return ("⏳",
                    f"当前**空仓**。上一轮筛出 **{found}** 只上涨趋势**候选币**（≠ 已买入，还需连续 2 根确认才开仓）；"
                    f"距离下一轮评估还有 **{mm:02d}:{ss:02d}**，符合条件的会自动买入。")
        if phase == "error":
            return ("⚠️", "上一轮开仓评估因接口异常失败，程序会稍后自动重试，无需你处理。")
    return ("📡",
            f"**运行中**。当前**空仓**，程序按固定节奏（每 15 分钟）在后台评估选币池里的上涨趋势，"
            f"满足条件（上升趋势 + 连续 2 根确认）就自动买入；有持仓时每 5 秒自动盯守吊灯止损。")


CONSOLE_LOG = os.path.join(BASE_DIR, "bot_console.log")
# 只展示这些“动作”日志，忽略每 5 秒一次的普通心跳
ACTION_KEYWORDS = ("自动开多", "吊灯", "选币池", "资金费率", "机器人", "已无仓位",
                   "移除本地", "开仓失败", "切为", "开平", "已开")


def load_recent_actions(n=6):
    """读取 bot_console.log 末尾，提取最近 n 条『程序动作』（开仓/止损/刷池/启动等），
    新→旧返回 [(时间, 内容), ...]。"""
    if not os.path.exists(CONSOLE_LOG):
        return []
    try:
        with open(CONSOLE_LOG, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return []
    out = []
    for ln in reversed(lines[-300:]):
        content = ln.rstrip()
        if not content.strip():
            continue
        # 跳过每 5 秒一次的心跳行（"持仓 N | 已实现 … | 总盈亏 …"），它不算“动作”
        if "已实现" in content and "总盈亏" in content and " | " in content:
            continue
        if not any(k in content for k in ACTION_KEYWORDS):
            continue
        if content.startswith("[") and "] " in content:
            ts, _, text = content[1:].partition("] ")
        else:
            ts, text = "—", content
        if text:
            out.append((ts.strip(), text.strip()))
        if len(out) >= n:
            break
    return out


# ---------------- 页面选择 ----------------

page = st.sidebar.radio("页面", ["智能操作台", "账户分析"])
refresh_sec = st.sidebar.slider("数据自动刷新间隔（秒）", 10, 120, 30)
st.sidebar.caption("页面不再整页刷新：仅数据区域按此间隔局部重绘，交互不卡顿、选择不会被重置。")


# ================= 智能操作台 =================

# 状态区（含吊灯止损距离）：bot.py 每 1 秒写 bot_status.json，这里按最高频每秒局部刷新
STATUS_REFRESH_SEC = 1


@st.fragment(run_every=STATUS_REFRESH_SEC)
def status_section():
    """机器人状态卡片 + 程序控制（局部自动刷新）"""
    st.title("🤖 智能操作台")

    # ---- 小白一句话讲清机器人怎么赚钱 ----
    with st.expander("👶 一分钟看懂：它是怎么帮我在币圈赚钱的？（小白必看）", expanded=True):
        st.markdown(
            "这台程序帮你做**趋势跟随**，好比“**只坐上升的电梯，电梯一掉头就立刻下**”：\n\n"
            "- **买什么**：只从**成交量最高的 30 只币**里挑，而且只买**正在上涨、连续 2 根确认上升趋势**的币，自动**市价买入**。\n"
            "- **怎么赚**：买进后让它跟着趋势“**跑**”，涨得越高**越不急着卖**，让利润放大。\n"
            "- **什么时候卖**：头顶挂一条“**吊灯保护线**”——价格越高、线抬得越高（等于**锁定浮盈**）；一旦价格掉头跌穿这条线，就**自动卖出止损**，不让亏损扩大。\n"
            "- **仓位多大**：每只只投 **2U 保证金 × 3 倍杠杆**（约 6U），最多同时持 **8 只**，分散风险。\n"
            "- **你要做什么**：几乎全是**全自动**——程序自己盯盘、自己买、自己止损。你只需偶尔回来看一眼本页。\n\n"
            "⚠️ **风险提醒**：这是**真实资金 + 杠杆**自动交易，行情极端可能**强平**。请务必先用小金额验证。"
        )

    # ---- 当前策略完整规则（参数实时读取 bot.py，永远与实盘一致）----
    bp = read_bot_params()
    r2 = bp.get("R2_ENTRY", 0.85)
    sl = bp.get("SL_H", 0.0001)
    hwin = bp.get("H_WIN", 24)
    conf = bp.get("CONF", 2)
    kk = bp.get("K", 5.0)
    atrn = bp.get("ATR_N", 14)
    lev = int(bp.get("LEVERAGE", 3))
    margin = bp.get("POS_MARGIN_USDT", 2.0)
    maxpos = int(bp.get("MAX_POS", 8))
    pooln = int(bp.get("VOL_POOL_N", 30))
    entry_min = int(bp.get("ENTRY_INTERVAL", 900)) // 60
    poll_s = int(bp.get("POLL_SECONDS", 5))
    fund = bp.get("FUNDING_RATE_LIMIT", 0.005)

    with st.expander(f"📋 当前策略完整规则（v4 趋势跟随·仅做多 ｜ 实时读取 bot.py，改代码这里自动同步）", expanded=False):
        st.markdown(
            f"**一句话**：在成交量最高的 **{pooln}** 只 USDT 永续里，只做**多头**——"
            f"1 小时级别走出干净的**上涨直线**、且 15 分钟图连续确认后**市价买入**；"
            f"买入后用一条不断上移的“吊灯保护线”跟随，价格**跌穿就市价卖出**，让利润跑、把亏损截断。"
        )
        rule_rows = [
            ("🐟 选币池", f"成交量前 **{pooln}** 的 USDT 永续合约，每 **4 小时**整点自动更新一次"),
            ("📈 趋势判断", f"用最近 **{hwin} 根 1 小时 K线**做线性回归：拟合优度 **R² ≥ {r2:g}**（越接近1=走势越像一条直线），且每根斜率 **≥ {sl:g}**"),
            ("✅ 开仓确认", f"15 分钟图上**连续 {conf} 根**满足上升趋势才买入（确认次数越少进场越快、但假信号也越多）"),
            ("↗️ 方向", "**只做多（只买涨）**，不做空；斜率必须为正才开仓"),
            ("💰 每仓大小", f"每仓保证金 **{margin:g} U × {lev} 倍杠杆 = 名义 {margin*lev:g} U**，同时最多持 **{maxpos}** 仓"),
            ("🛑 卖出(止损/止盈)", f"**吊灯线 = 持仓以来最高价 − {kk:g} × ATR({atrn},15分钟)**；价格跌破立即**市价卖出**。涨得越多线抬得越高＝自动锁定利润"),
            ("⏱️ 运行节奏", f"每 **{entry_min} 分钟**评估一次开新仓；持仓时每 **{poll_s} 秒**检查一次吊灯线"),
            ("🧾 下单方式", "开仓、平仓**全部用市价单**（吃单 taker，手续费约 0.05%/边），保证触发即成交、不挂单等待"),
            ("💸 资金费率", f"开多时若当前资金费率（多头付给空头）高于 **{fund*100:g}%/8h** 则跳过该币，避免承担高额费率"),
            ("🚫 没有的东西", "**无固定止盈价、无评分回撤、无做空、无加仓/马丁**；唯一离场信号就是吊灯线"),
        ]
        for name, desc in rule_rows:
            st.markdown(f"- {name}：{desc}")

        st.markdown("##### 📊 这套参数的最近一次回测成绩（90 天 × 30 币，仅做多）")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("90天收益", "+10.7% ~ +17.6%")
        m2.metric("年化（保守口径）", "+51%")
        m3.metric("最大回撤", "≈19%")
        m4.metric("交易笔数", "≈310")
        m5.metric("胜率", "≈38%")
        st.caption(
            "说明：网格扫描 500 组参数后，在“年化≥50% 且 回撤≤20%”的达标组里选了年化最高的一组（R²={r2:g} / 斜率 {sl:g} / "
            "K {kk:g} / 确认 {conf}）。同参数两次复跑年化在 51%~93% 之间（最后一根未收盘K线口径差异），**页面按保守的 51% 展示**；"
            "胜率约 38% 是趋势策略的常态——靠少数大赚覆盖多次小亏。回测为历史数据，**不代表未来收益**，熊市/长期横盘时仅做多趋势策略可能连续止损。"
            .format(r2=r2, sl=sl, kk=kk, conf=conf)
        )

    status, alive = load_status()
    sp = load_scan_progress()
    holding = status.get("position", "") not in ("", "空仓")

    # ---- 当前在做什么（大白话）：放最显眼位置 ----
    emoji, plain_txt = plain_program_status(status, alive, sp, holding)
    st.markdown("### 🧭 程序现在正在做什么")
    st.info(f"{emoji}　{plain_txt}")

    # ---------- 机器人状态 ----------
    st.subheader("📡 机器人状态（数字速览）")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("运行状态", "🟢 运行中" if alive else "🔴 已停止")
    positions = status.get("positions") or {}
    n_pos = len(positions)
    c2.metric("持仓数", f"{n_pos}" if alive else "—")
    c3.metric("持仓方向", (status.get("position") or "空仓").replace("\n", " | ") if alive else "—")
    c4.metric("余额 (USDT)", f"{status.get('balance', 0):.2f}" if alive else "—")
    c5.metric("总盈亏 (USDT)", f"{status.get('total', 0):+.2f}" if alive else "—")
    c6.metric("已实现盈亏", f"{status.get('realized', 0):+.2f}" if alive else "—")
    st.caption("提示：总盈亏 = 已实现（已卖出落袋的钱）+ 浮动（还没卖、跟着行情浮动的钱）。")
    if alive:
        st.caption(f"最新心跳：{status.get('time', '—')}｜当前信号 **{status.get('signal', '—')}**｜"
                   f"策略：吊灯 K5×ATR(15m,14)·仅做多·确认2根·R²0.85")
        if positions:
            try:
                pdl = pd.DataFrame([
                    {"币种": s, "方向": "做多", "数量": f"{p.get('qty', 0):.4f}",
                     "开仓价": f"{p.get('price', 0):.6f}", "名义U": f"{p.get('notional', 0):.2f}",
                     "吊灯保护线距离": (f"{p.get('trail_pct', 0):.0f}%" if p.get('trail_pct') is not None else "—"),
                     "最新价": f"{p.get('last_px', 0):.6f}"}
                    for s, p in positions.items()
                ])
                st.markdown(f"**📦 当前持仓（{n_pos}）**")
                st.dataframe(pdl, use_container_width=True, height=32 * (n_pos + 1))
                st.caption("看懂这栏：**吊灯保护线距离 = 当前价比上方‘止损线’高出百分之几**，数字越接近 0，离被自动止损越近。")
            except Exception:
                pass
        if positions and any(p.get("trail_pct") is not None for p in positions.values()):
            trail_lines = []
            for s, p in positions.items():
                tp = p.get("trail_pct")
                trail_lines.append(f"`{s}` 距吊灯线 **{tp:.0f}%**" if tp is not None else f"`{s}` —")
            st.markdown(f"**📉 吊灯保护线距离**（逐仓）：{ '　'.join(trail_lines) }")
            st.caption("吊灯线 = 持仓最高价 − 5×ATR；价格涨、线跟涨（锁浮盈），价格跌破线就自动市价止损。")
        if status.get("open_note"):
            note_txt = status.get("open_note", "")
            if "已开" in note_txt:
                st.success(f"✅ {note_txt}（{status.get('open_note_time', '')}）")
            else:
                st.warning(f"⚠️ 开仓未执行：{note_txt}（{status.get('open_note_time', '')}）"
                           f"　—— 确认后立即开单，拦截仅因方向 / 资金费率 / 余额不足")
        st.success("✅ 机器人运行正常：持仓自动盯守，空仓自动扫描。")
        # ---------- 扫描执行进度（实时倒计时/进度条）----------
        if sp:
            phase = sp.get("phase")
            if phase == "scanning":
                cur, tot, found = sp.get("current", 0), sp.get("total", 0), sp.get("found", 0)
                pct = (cur / tot) if tot else 0
                st.markdown(f"**🔄 正在扫描清洗趋势目标**：`{cur}/{tot}`，已发现 **{found}** 只")
                st.progress(pct)
            elif phase == "done":
                next_ts = sp.get("next_ts", 0)
                remain = max(0, int(next_ts - time.time()))
                mm, ss = divmod(remain, 60)
                found = sp.get("found", 0)
                stn = sp.get("time", "—")
                if holding:
                    st.caption(f"✅ 上次扫描 {stn}，找到 {found} 个候选；当前持仓中不进行新扫描。")
                else:
                    st.caption(f"✅ 上次扫描 {stn}，找到 {found} 个候选；距离下次扫描 **{mm:02d}:{ss:02d}**。")
            elif phase == "error":
                st.error("⚠️ 扫描失败（接口异常），下轮重试。")
        else:
            st.caption("当前无实时扫描进度条：程序在后台按固定节奏（每 15 分钟）评估开仓，状态以上方为准。")
        # ---------- 最近动态（机器人刚刚做了什么）----------
        acts = load_recent_actions()
        if acts:
            st.markdown("**🗒️ 最近动态（机器人刚刚做了什么）**")
            for ts, text in acts:
                st.markdown(f"- `{ts}`　{text}")
    else:
        st.info("看板未检测到机器人心跳。若机器人已停止，可点击下方『🚀 启动机器人』；启动后约 10 秒内显示状态。")
        acts = load_recent_actions(n=8)
        if acts:
            st.markdown("**🗒️ 机器人停止前的最近动态**")
            for ts, text in acts:
                st.markdown(f"- `{ts}`　{text}")

    # ---------- 程序控制 ----------
    st.subheader("🛑 程序控制")
    cc1, cc2, cc3 = st.columns(3)
    with cc1:
        st.info("机器人检测到仓位被外部改动时不再自动暂停，始终自动交易")
    with cc2:
        if alive:
            if st.button("⏹ 停止程序", type="primary", use_container_width=True):
                with open(STOP_FILE, "w", encoding="utf-8") as f:
                    f.write(f"界面停止 @ {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                st.success("已发送停止指令，机器人将在下一轮（≤1 秒）优雅退出。")
        else:
            if st.button("🚀 启动机器人", use_container_width=True):
                cnt, procs = detect_bot_processes()
                if cnt == 1:
                    st.warning(f"检测到机器人已在运行（PID {procs[0][0]}，启动于 {procs[0][1]}），无需再次启动。")
                elif cnt > 1:
                    pids = "、".join(p[0] for p in procs)
                    st.error(f"检测到 **{cnt} 个并发 bot.py 进程**（PID {pids}）！为避免重复开单/平仓，请先清理多余进程"
                             f"再点击启动（可在任务管理器结束多余进程，或告诉我帮你清理）。")
                else:
                    start_bot()
                    st.success("已启动机器人，等待首轮心跳（约 1 分钟）。")
    with cc3:
        st.caption("停止/启动不影响已持仓：机器人重启后会按交易记录继续管理持仓。")


@st.fragment(run_every=15)
def process_check():
    """机器人进程自检（低频刷新）：显示当前正在运行的 bot.py 数量与 PID/启动时间，
    避免用户反复点击启动导致并发。放在程序控制区下方单独展示。"""
    cnt, procs = detect_bot_processes()
    if cnt < 0:
        st.caption("❓ 进程自检失败（无法枚举进程），可直接点击下方“启动/停止”管理机器人。")
    elif cnt == 0:
        st.caption("🟡 进程自检：当前 **没有** 运行中的 bot.py 进程。")
    elif cnt == 1:
        st.caption(f"🟢 进程自检：**1 个** bot.py 运行中（PID {procs[0][0]}，启动于 {procs[0][1]}）。正常，无需再点启动。")
    else:
        pids = "、".join(p[0] for p in procs)
        st.error(f"🔴 进程自检：检测到 **{cnt} 个并发 bot.py 进程**（PID {pids}）！请清理多余进程，避免重复下单。")


def scan_controls():
    """扫描区（主流程，交互控件不进 fragment，避免自动刷新卡顿/回弹）：
    选币池与信号由 scan_table_fragment 局部自动刷新。"""
    st.subheader("🔍 选币池 / 趋势信号")
    with st.expander("👶 这里在看什么？", expanded=False):
        st.markdown(
            "下面这排币 = 机器人**用来挑猎物的“鱼池”**（成交量最高的 30 只，每 4 小时由程序自动更新一次）。\n\n"
            "- 带 🟢 **已开** = 已经买入、正在持有的币（它们也继续留在池里盯守）。\n"
            "- 机器人每 **15 分钟**挨个“体检”这些币：谁能稳定**向上走（上升趋势）且连续 2 根确认**，就直接**市价买入**。\n"
            "- 不用你手动选——程序看到机会就自己动手。你只要看它对不对、效果好不好。"
        )
    st.caption("逻辑：成交量最高 30 只 → 1h 上升趋势 R²≥0.85 且 |斜率|≥0.0001 → 15m 连续确认 2 根 → 自动市价开多。")

    # ---------- 选币池表格（纯展示，fragment 自动刷新，不影响上方交互控件）----------
    scan_table_fragment()

    st.info("🤖 开仓自动执行（无手动确认），每仓 2U×3x=6U 名义，同时最多 8 仓；余额不足自动停止。"
            "吊灯止损 K=5×ATR(15m,14) 自动平仓。")


@st.fragment(run_every=refresh_sec)
def scan_table_fragment():
    """展示 v4 选币池：成交量前 30 的 USDT 永续（每 4 小时刷新），以及池内已开仓标记。"""
    st.markdown("**🗂️ 选币池（成交量前 30 USDT 永续，每 4 小时整点刷新）**")
    try:
        with open(VOL_POOL_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        syms = d.get("symbols", [])
        picked = d.get("picked", "—")
    except Exception:
        syms, picked = [], "—"
    status, _ = load_status()
    held = set((status.get("positions") or {}).keys())
    if not syms:
        st.caption("选币池暂无数据（机器人启动后自动生成）。")
        return
    rows = [syms[i:i + 5] for i in range(0, len(syms), 5)]
    for row in rows:
        st.markdown("　".join(f"`{s}` 🟢已开" if s in held else f"`{s}`" for s in row))
    st.caption(f"更新时间：{picked} | 共 {len(syms)} 币（🟢已开 = 当前持仓计入池内）。"
               f"每 15 分钟评估：满足 1h 趋势且连续确认 2 根即自动开多，吊灯止损平仓。")


# ================= 账户分析 =================

@st.fragment(run_every=refresh_sec)
def account_section():
    """账户分析（局部自动刷新：分时曲线每 refresh_sec 秒更新一次）"""
    # 读取交易记录（当前币种以机器人状态快照为准，取不到则用 CARVUSDT）
    status, _ = load_status()
    SYMBOL = status.get("symbol", "CARVUSDT") if status else "CARVUSDT"
    BASE_ASSET = SYMBOL.replace("USDT", "")

    def load_data():
        if not os.path.exists(TRADE_LOG):
            return pd.DataFrame(), None
        with open(TRADE_LOG, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 兼容新多持仓 positions dict（旧版单持仓 open_position 迁移判断）
        positions = data.get("positions") or {}
        if not positions and data.get("open_position"):
            positions = {data.get("symbol", "CARVUSDT"): data["open_position"]}
        return pd.DataFrame(data.get("trades", [])), positions

    def load_pnl_history():
        """读取分时盈亏记录（bot.py 每轮轮询追加一条）"""
        if not os.path.exists(PNL_LOG):
            return pd.DataFrame()
        with open(PNL_LOG, "r", encoding="utf-8") as f:
            data = json.load(f)
        df = pd.DataFrame(data)
        if not df.empty:
            df["dt"] = pd.to_datetime(df["time"])
        return df

    df, open_pos = load_data()

    if df.empty and open_pos is None:
        st.title(f"📊 账户分析（当前交易对 {SYMBOL}）")
        st.info("暂无交易记录。请先运行 bot.py 开始交易。")
        return

    st.title(f"📊 账户分析（当前交易对 {SYMBOL}）")

    # ============ 顶部概览卡片 ============
    col1, col2, col3, col4, col5, col6 = st.columns(6)

    # 手续费：每笔 entry_fee + exit_fee，加上当前持仓开仓手续费
    for c in ("entry_fee", "exit_fee"):
        if c not in df.columns:
            df[c] = 0.0
        else:
            df[c] = df[c].fillna(0.0)
    df["fee"] = df["entry_fee"] + df["exit_fee"]
    total_fee = float(df["fee"].sum())
    if open_pos:
        total_fee += sum(float(p.get("entry_fee", 0) or 0) for p in open_pos.values())

    realized_pnl = df["pnl"].sum() if not df.empty else 0
    total_trades = len(df)
    win_trades = (df["pnl"] > 0).sum() if not df.empty else 0
    win_rate = (win_trades / total_trades * 100) if total_trades > 0 else 0
    net_realized = realized_pnl - total_fee

    col1.metric("已实现盈亏", f"{realized_pnl:+.2f} USDT")
    col2.metric("净盈亏(扣手续费)", f"{net_realized:+.2f} USDT")
    col3.metric("累计手续费", f"{total_fee:.2f} USDT")
    col4.metric("总交易次数", f"{total_trades} 笔")
    col5.metric("胜率", f"{win_rate:.1f}%")
    col6.metric("平均单笔盈亏", f"{(realized_pnl / total_trades):+.2f} USDT" if total_trades > 0 else "0.00 USDT")

    # ============ 浮动盈亏 ============
    if open_pos:
        info_parts = []
        for s, p in open_pos.items():
            side_cn = "做多" if p.get("side") == "LONG" else "做空"
            otype_cn = ORDER_TYPE_CN.get(p.get("order_type"), "—")
            info_parts.append(f"**{s.replace('USDT','')}** {side_cn} {p.get('qty', 0):.4f}@{p.get('price', 0):.6f}({otype_cn})")
        st.markdown(f"🟢 持仓中（{len(open_pos)} 仓）：{'　｜　'.join(info_parts)}")
    else:
        st.success("✅ 当前空仓，无浮动盈亏")

    # ============ 分时盈亏曲线 ============
    st.subheader("🕐 分时盈亏曲线")
    pnl_df = load_pnl_history()
    if not pnl_df.empty:
        fig_pnl = go.Figure()
        fig_pnl.add_trace(go.Scatter(x=pnl_df["dt"], y=pnl_df["total"], name="总盈亏", line=dict(color="#26a69a")))
        fig_pnl.add_trace(go.Scatter(x=pnl_df["dt"], y=pnl_df["realized"], name="已实现", line=dict(color="#42a5f5")))
        # 用点标注持仓方向：绿=做多 红=做空 灰=空仓
        pos_colors = {"LONG": "#26a69a", "SHORT": "#ef5350", "空仓": "#90a4ae"}
        pos_series = pnl_df["position"] if "position" in pnl_df.columns else pd.Series(["空仓"] * len(pnl_df))
        fig_pnl.add_trace(go.Scatter(
            x=pnl_df["dt"], y=pnl_df["total"], mode="markers",
            marker=dict(size=5, color=[pos_colors.get(p, "#90a4ae") for p in pos_series]),
            name="持仓方向（绿=做多 红=做空）",
        ))
        fig_pnl.update_layout(title="账户分时盈亏 (USDT)", xaxis_title="时间", yaxis_title="盈亏 (USDT)",
                              hovermode="x unified")
        st.plotly_chart(fig_pnl, use_container_width=True)
        st.caption(f"共 {len(pnl_df)} 条快照，从 {pnl_df['time'].iloc[0]} 到 {pnl_df['time'].iloc[-1]}；"
                   f"曲线上的点颜色代表持仓方向：绿=做多 红=做空 灰=空仓")
    else:
        st.info("暂无分时盈亏记录，机器人每轮轮询后自动写入 pnl_history.json")

    # ============ 盈亏曲线 ============
    st.subheader("📈 累计盈亏曲线")
    if not df.empty:
        df_plot = df.copy()
        df_plot["累计盈亏"] = df_plot["pnl"].cumsum()
        df_plot["序号"] = range(1, len(df_plot) + 1)
        fig = px.area(df_plot, x="序号", y="累计盈亏", title="累计已实现盈亏 (USDT)")
        fig.update_traces(line_color="#26a69a", fill="tozeroy")
        # 标注每笔的方向：▲做多（绿）/ ▼做空（红）
        side_colors = ["#26a69a" if s == "LONG" else "#ef5350" for s in df["side"]]
        fig.add_trace(go.Scatter(
            x=df_plot["序号"], y=df_plot["累计盈亏"], mode="markers+text",
            text=["做多" if s == "LONG" else "做空" for s in df["side"]],
            textposition="top center",
            textfont=dict(size=11, color=side_colors),
            marker=dict(size=10, color=side_colors,
                        symbol=["triangle-up" if s == "LONG" else "triangle-down" for s in df["side"]]),
            name="方向标注（▲多 ▼空）",
        ))
        st.plotly_chart(fig, use_container_width=True)

        # ============ 单笔盈亏分布 ============
        st.subheader("💰 单笔盈亏分布")
        fig2 = go.Figure()
        fig2.add_trace(go.Bar(
            x=list(range(1, len(df_plot) + 1)),
            y=df_plot["pnl"],
            marker_color=["#26a69a" if v >= 0 else "#ef5350" for v in df_plot["pnl"]],
        ))
        fig2.update_layout(title="每笔交易盈亏", xaxis_title="交易序号", yaxis_title="盈亏 (USDT)")
        st.plotly_chart(fig2, use_container_width=True)

        # ============ 统计数据 ============
        st.subheader("📋 统计数据")
        c1, c2, c3 = st.columns(3)
        c1.metric("最大盈利", f"{df['pnl'].max():+.2f} USDT")
        c2.metric("最大亏损", f"{df['pnl'].min():+.2f} USDT")
        c3.metric("总投入本金", f"{df['buy_cost'].sum():.2f} USDT")

        # ============ 交易明细 ============
        st.subheader("📝 交易明细")
        display_df = df[["buy_time", "buy_price", "sell_time", "sell_price", "buy_qty", "pnl"]].copy()
        display_df.columns = ["买入时间", "买入价", "卖出时间", "卖出价", f"数量({BASE_ASSET})", "盈亏(USDT)"]
        display_df["开仓方式"] = df["entry_type"].map(ORDER_TYPE_CN).fillna("—") if "entry_type" in df.columns else "—"
        display_df["平仓方式"] = df["exit_type"].map(ORDER_TYPE_CN).fillna("—") if "exit_type" in df.columns else "—"
        display_df["手续费(USDT)"] = df["fee"].map(lambda x: f"{x:+.4f}")
        display_df["净盈亏(USDT)"] = (df["pnl"] - df["fee"]).map(lambda x: f"{x:+.4f}")
        display_df["盈亏(USDT)"] = display_df["盈亏(USDT)"].map(lambda x: f"{x:+.4f}")
        display_df["平仓原因"] = df["reason"].fillna("—") if "reason" in df.columns else "—"
        st.dataframe(display_df, use_container_width=True)

        # 成交方式统计（限价/市价笔数）
        if "entry_type" in df.columns:
            all_types = list(df["entry_type"]) + list(df["exit_type"])
            n_maker = all_types.count("maker")
            n_taker = all_types.count("taker")
            n_mixed = all_types.count("mixed")
            st.caption(f"成交方式统计：限价(maker) {n_maker} 次 / 市价(taker) {n_taker} 次 / 混合 {n_mixed} 次"
                       f"（每笔含开仓+平仓共 2 次下单）")
            st.caption("手续费按成交方式估算：限价(maker) 0.02%，市价(taker) 0.05%（旧交易无手续费字段显示 0.00）")
    else:
        st.info("暂无已完成交易记录，等待第一笔平仓...")

    st.caption(f"数据来源：{TRADE_LOG}、{PNL_LOG}、{STATUS_FILE}")


# ---------------- 页面分发 ----------------

if page == "智能操作台":
    status_section()
    process_check()
    scan_controls()
else:
    account_section()
