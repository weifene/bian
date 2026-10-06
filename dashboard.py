"""
币安 U 本位合约量化交易 - 可视化看板

页面：
  - 智能操作台：机器人状态 / 全市场"干净趋势"扫描 Top10 展示 / 选择币种开仓（全仓，确认后立即执行） / 停止与启动机器人 / 恢复自动交易
  - 账户分析：盈亏概览 / 分时盈亏曲线 / 交易明细 / 手续费统计

与 bot.py 通过文件通信：
  - 读：bot_status.json（状态快照）、scan_results.json（扫描 Top N）、trade_log.json / pnl_history.json
  - 写：open_request.json（开仓请求）、stop_request.flag（停止）

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
    """读取扫描进度快照（机器人扫描时实时写入）"""
    if not os.path.exists(SCAN_PROGRESS_FILE):
        return None
    try:
        with open(SCAN_PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_open_request(sym, side, price, score):
    """向机器人发送开仓请求（含方向/价格/评分，机器人确认后立即全仓开单，不再复核趋势门槛）"""
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


# ---------------- 页面选择 ----------------

page = st.sidebar.radio("页面", ["智能操作台", "账户分析"])
refresh_sec = st.sidebar.slider("数据自动刷新间隔（秒）", 10, 120, 30)
st.sidebar.caption("页面不再整页刷新：仅数据区域按此间隔局部重绘，交互不卡顿、选择不会被重置。")


# ================= 智能操作台 =================

# 状态区（含评分回撤进度）：bot.py 每 1 秒写 bot_status.json，这里按最高频每秒局部刷新
STATUS_REFRESH_SEC = 1


@st.fragment(run_every=STATUS_REFRESH_SEC)
def status_section():
    """机器人状态卡片 + 程序控制（局部自动刷新）"""
    st.title("🤖 智能操作台")
    st.caption("v4 趋势跟随·仅做多：成交量前 30 永续池，1h 回归 R²≥0.70 且 |斜率|≥0.00015，15m 连续确认 4 根后自动市价开多"
               "（每仓 2U×3x=6U 名义，最多 8 仓）；吊灯止损 K=6×ATR(15m,14) 自动平仓（reason=吊灯止损）。")

    status, alive = load_status()

    # ---------- 机器人状态 ----------
    st.subheader("📡 机器人状态")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("运行状态", "🟢 运行中" if alive else "🔴 已停止")
    positions = status.get("positions") or {}
    n_pos = len(positions)
    c2.metric("持仓数", f"{n_pos}" if alive else "—")
    c3.metric("持仓方向", (status.get("position") or "空仓").replace("\n", " | ") if alive else "—")
    c4.metric("信号", status.get("signal", "—") if alive else "—")
    c5.metric("余额 (USDT)", f"{status.get('balance', 0):.2f}" if alive else "—")
    c6.metric("总盈亏 (USDT)", f"{status.get('total', 0):+.2f}" if alive else "—")
    if alive:
        st.caption(f"最新心跳：{status.get('time', '—')} | 已实现 {status.get('realized', 0):+.2f} / 浮动 {status.get('floating', 0):+.2f} | "
                   f"策略：吊灯 K6×ATR(15m,14)·仅多·确认4根")
        if positions:
            try:
                pdl = pd.DataFrame([
                    {"币种": s, "方向": "做多", "数量": f"{p.get('qty', 0):.4f}",
                     "开仓价": f"{p.get('price', 0):.6f}", "名义U": f"{p.get('notional', 0):.2f}",
                     "吊灯距离": (f"{p.get('trail_pct', 0):.0f}%" if p.get('trail_pct') is not None else "—"),
                     "最新价": f"{p.get('last_px', 0):.6f}"}
                    for s, p in positions.items()
                ])
                st.markdown(f"**📦 当前持仓（{n_pos}）**")
                st.dataframe(pdl, use_container_width=True, height=32 * (n_pos + 1))
            except Exception:
                pass
        if positions and any(p.get("trail_pct") is not None for p in positions.values()):
            # 逐仓吊灯止损距离（对应到各持仓）
            trail_lines = []
            for s, p in positions.items():
                tp = p.get("trail_pct")
                trail_lines.append(f"`{s}` 距吊灯线 **{tp:.0f}%**" if tp is not None else f"`{s}` —")
            st.markdown(f"**📉 吊灯止损距离**（逐仓）：{ '　'.join(trail_lines) }")
            st.caption("吊灯线 = 持仓最高价 maxe − 6×ATR(15m,14)；价格距吊灯线越近越易触发，跌破即市价平仓（reason=吊灯止损）。")
        if status.get("open_note"):
            note_txt = status.get("open_note", "")
            if "已开" in note_txt:
                st.success(f"✅ {note_txt}（{status.get('open_note_time', '')}）")
            else:
                st.warning(f"⚠️ 开仓未执行：{note_txt}（{status.get('open_note_time', '')}）"
                           f"　—— 确认后立即开单，拦截仅因方向 / 资金费率 / 余额不足")
        st.success("机器人运行正常，持仓自动管理 + 空仓自动扫描中。")
        # ---------- 扫描执行进度（实时倒计时/进度条）----------
        sp = load_scan_progress()
        holding = status.get("position", "") not in ("", "空仓")
        if sp:
            phase = sp.get("phase")
            if phase == "scanning":
                cur, tot, found = sp.get("current", 0), sp.get("total", 0), sp.get("found", 0)
                pct = (cur / tot) if tot else 0
                st.markdown(f"**🔄 全市场扫描中**：`{cur}/{tot}`，已发现 **{found}** 个干净趋势目标")
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
            st.caption("扫描进度暂无数据（机器人未启动或尚未开始扫描）。")
    else:
        st.info("看板未检测到机器人心跳。若机器人已停止，可点击下方“启动机器人”；启动后约 10 秒内显示状态。")

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
    st.caption("每 4 小时（0/4/8/12/16/20 点）按 24h 成交量更新选币池（前 30 只 USDT 永续）；"
               "每 15 分钟对新 15m bar 评估：1h 线性回归 R²≥0.70 且 |斜率|≥0.00015 且为上升趋势，"
               "连续 4 根确认后自动市价开多。")

    # ---------- 选币池表格（纯展示，fragment 自动刷新，不影响上方交互控件）----------
    scan_table_fragment()

    st.info("🤖 开仓自动执行（无手动确认），每仓 2U×3x=6U 名义，同时最多 8 仓；余额不足自动停止。"
            "吊灯止损 K=6×ATR(15m,14) 自动平仓。")


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
               f"每 15 分钟评估：满足 1h 趋势且连续确认 4 根即自动开多，吊灯止损平仓。")


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
