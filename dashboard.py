"""
币安 U 本位合约量化交易 - 可视化看板

页面：
  - 智能操作台：机器人状态 / 全市场"干净趋势"扫描 Top10 展示 / 选择币种开仓（全仓，机器人复核后执行） / 停止与启动机器人 / 恢复自动交易
  - 账户分析：盈亏概览 / 分时盈亏曲线 / 交易明细 / 手续费统计

与 bot.py 通过文件通信：
  - 读：bot_status.json（状态快照）、scan_results.json（扫描 Top N）、trade_log.json / pnl_history.json
  - 写：open_request.json（开仓请求）、stop_request.flag（停止）、删除 manual_pause.flag（恢复交易）

用法：streamlit run dashboard.py
"""
import os
import json
import time
import sys
import subprocess
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from streamlit.components.v1 import html

# 代理配置（与 bot.py 保持一致）
PROXY = "socks5h://127.0.0.1:7892"
if PROXY:
    os.environ["HTTP_PROXY"] = PROXY
    os.environ["HTTPS_PROXY"] = PROXY

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG = os.path.join(BASE_DIR, "trade_log.json")
PNL_LOG = os.path.join(BASE_DIR, "pnl_history.json")
SCAN_RESULTS_FILE = os.path.join(BASE_DIR, "scan_results.json")
OPEN_REQUEST_FILE = os.path.join(BASE_DIR, "open_request.json")
STOP_FILE = os.path.join(BASE_DIR, "stop_request.flag")
STATUS_FILE = os.path.join(BASE_DIR, "bot_status.json")
SCAN_WINDOW_FILE = os.path.join(BASE_DIR, "scan_window.json")
PAUSE_FILE = os.path.join(BASE_DIR, "manual_pause.flag")
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


def load_scan():
    """读取机器人扫描结果（Top N 干净趋势目标）"""
    if not os.path.exists(SCAN_RESULTS_FILE):
        return None
    try:
        with open(SCAN_RESULTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_open_request(sym):
    """向机器人发送开仓请求（机器人复核趋势后全仓开单）"""
    with open(OPEN_REQUEST_FILE, "w", encoding="utf-8") as f:
        json.dump({"symbol": sym, "time": time.strftime("%Y-%m-%d %H:%M:%S")}, f,
                  ensure_ascii=False, indent=2)


def start_bot():
    """以分离进程方式启动机器人（独立于本看板运行），输出重定向到 bot_console.log"""
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0
    log = open(os.path.join(BASE_DIR, "bot_console.log"), "a", encoding="utf-8")
    subprocess.Popen([sys.executable, BOT_PY], cwd=BASE_DIR, creationflags=flags,
                     stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, close_fds=True)


# ---------------- 页面选择 ----------------

page = st.sidebar.radio("页面", ["智能操作台", "账户分析"])
refresh_sec = st.sidebar.slider("自动刷新间隔（秒）", 10, 300, 30)


# ================= 智能操作台 =================

if page == "智能操作台":
    # 定时整页刷新（Streamlit 无内置定时器，用 JS 刷新页面实现自动更新）
    # 一旦用户勾选"我确认"（进入开仓确认流程），暂停自动刷新——防止刷新把所选币种悄悄重置
    if not st.session_state.get("hold_refresh"):
        html(f"""<script>setTimeout(function(){{window.parent.location.reload()}}, {refresh_sec*1000});</script>""", height=0)

    st.title("🤖 智能操作台")
    st.caption("机器人空仓时每 2 分钟扫描全市场“干净趋势”目标写入本页；开仓需你确认后由机器人复核并全仓下单，"
               "清仓（趋势破坏 / 止损）始终自动执行。")

    status, alive = load_status()
    paused = bool(status and status.get("paused"))

    # ---------- 机器人状态 ----------
    st.subheader("📡 机器人状态")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("运行状态", "🟢 运行中" if alive else "🔴 已停止")
    c2.metric("当前币种", status.get("symbol", "—") if alive else "—")
    c3.metric("持仓方向", status.get("position", "—") if alive else "—")
    c4.metric("信号", status.get("signal", "—") if alive else "—")
    c5.metric("余额 (USDT)", f"{status.get('balance', 0):.2f}" if alive else "—")
    c6.metric("总盈亏 (USDT)", f"{status.get('total', 0):+.2f}" if alive else "—")
    if alive:
        st.caption(f"最新心跳：{status.get('time', '—')} | 现价 {status.get('price', 0):.4f} | "
                   f"已实现 {status.get('realized', 0):+.2f} / 浮动 {status.get('floating', 0):+.2f} | "
                   f"上次扫描：{status.get('last_scan', '—')}（{status.get('candidate_count', 0)} 个候选）")
        if status.get("break_score") is not None:
            brk = int(status.get("break_score", 0))
            holding = status.get("position", "") != "空仓"
            st.markdown(f"**📉 趋势破坏进度**：`{brk} 分`"
                        f"（{'持仓中，距触发趋势破坏平仓还有 **' + str(100 - brk) + ' 分**' if holding else '当前空仓，反映本币趋势健康度'}）")
            st.progress(min(100, max(0, brk)) / 100)
            st.caption("0 分 = 刚进场（趋势最干净）→ 100 分 = 趋势破坏（R² 跌破离场阈值或斜率走平，触发自动平仓）")
        if status.get("open_note"):
            note_txt = status.get("open_note", "")
            if "已开" in note_txt:
                st.success(f"✅ {note_txt}（{status.get('open_note_time', '')}）")
            else:
                st.warning(f"⚠️ 开仓未执行：{note_txt}（{status.get('open_note_time', '')}）"
                           f"　—— 界面表格是扫描时的趋势，机器人下单前会实时复核，趋势已变则不进场")
        if paused:
            st.warning(f"⚠️ 暂停中：{status.get('paused', '')}。机器人当前只扫描、不交易，恢复请点下方按钮。")
        else:
            st.success("机器人运行正常，持仓自动管理 + 空仓自动扫描中。")
    else:
        st.info("看板未检测到机器人心跳。若机器人已停止，可点击下方“启动机器人”；启动后约 10 秒内显示状态。")

    # ---------- 程序控制 ----------
    st.subheader("🛑 程序控制")
    cc1, cc2, cc3 = st.columns(3)
    with cc1:
        if paused and alive:
            if st.button("▶️ 恢复自动交易", use_container_width=True):
                if os.path.exists(PAUSE_FILE):
                    os.remove(PAUSE_FILE)
                    st.success("已删除暂停标记，机器人下一轮（≤1 秒）恢复自动交易")
                else:
                    st.info("暂停标记已不存在")
        else:
            st.info("当前未暂停（暂停由检测到手动操作触发）")
    with cc2:
        if alive:
            if st.button("⏹ 停止程序", type="primary", use_container_width=True):
                with open(STOP_FILE, "w", encoding="utf-8") as f:
                    f.write(f"界面停止 @ {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                st.success("已发送停止指令，机器人将在下一轮（≤1 秒）优雅退出。")
        else:
            if st.button("🚀 启动机器人", use_container_width=True):
                start_bot()
                st.success("已启动机器人，等待首轮心跳（约 1 分钟）。")
    with cc3:
        st.caption("停止/启动不影响已持仓：机器人重启后会按交易记录继续管理持仓。")

    # ---------- 扫描结果 ----------
    st.subheader("🔍 全市场扫描结果（干净趋势 Top 10）")

    # ---------- 趋势窗口选择（写入 scan_window.json，机器人每轮读取并生效）----------
    def load_window():
        if not os.path.exists(SCAN_WINDOW_FILE):
            return 12
        try:
            with open(SCAN_WINDOW_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("hours", 12)
        except Exception:
            return 12

    scan = load_scan()
    cur_window = (scan or {}).get("hours") or load_window()
    opts = [1, 3, 6, 12]
    w1, w2 = st.columns([1, 4])
    with w1:
        wh = st.selectbox("趋势窗口（小时）", opts, index=opts.index(cur_window) if cur_window in opts else 3)
        if wh != cur_window:
            with open(SCAN_WINDOW_FILE, "w", encoding="utf-8") as f:
                json.dump({"hours": wh}, f)
            st.success(f"已切换为最近 {wh} 小时，机器人下一轮（≤1 秒）生效")
    with w2:
        st.caption(f"当前趋势窗口：最近 **{cur_window} 小时**（扫描选币 / 开仓复核 / 持仓信号统一使用，"
                   f"对应 {cur_window * 4} 根 15 分钟 K 线）。"
                   f"窗口越短趋势越灵敏（1 小时适合短线），越长越稳（12 小时过滤噪声）。")
    if scan and scan.get("candidates"):
        cands = scan["candidates"]
        df_scan = pd.DataFrame(cands)
        df_scan["方向"] = df_scan["direction"].map({1: "做多（上升）", -1: "做空（下降）"})
        df_scan["R²"] = df_scan["r2"].round(3)
        df_scan["斜率%/根"] = (df_scan["slope_pct"] * 100).map(lambda x: f"{x:+.4f}")
        df_scan["现价"] = df_scan["price"].map(lambda x: f"{x:.6f}")
        df_scan["24h量(M U)"] = (df_scan["vol"] / 1e6).map(lambda x: f"{x:.1f}")
        df_scan["评分"] = df_scan["score"].round(1)
        show = df_scan[["rank", "symbol", "方向", "R²", "斜率%/根", "现价", "24h量(M U)", "评分"]]
        show.columns = ["#", "币种", "方向", "R²", "斜率%/根", "现价", "24h量(M U)", "评分"]
        st.dataframe(show.set_index("#"), use_container_width=True)
        st.caption(f"扫描时间：{scan.get('time', '—')} | R² 越接近 1 说明 K 线越贴近直线（趋势越干净），"
                   f"斜率正=向上、负=向下。每 2 分钟更新一次。")

        # ---------- 选择开仓 ----------
        st.markdown("#### 选择要开仓的币种")
        syms = [c["symbol"] for c in cands]
        dir_map = {c["symbol"]: c["direction"] for c in cands}
        sel = st.selectbox("候选币种（按干净度评分从高到低）", syms, key="sel_sym")
        dir_txt = "上升趋势 → 做多" if dir_map[sel] == 1 else "下降趋势 → 做空"
        st.caption(f"该币当前为 **{dir_txt}**。开仓实际方向由机器人复核时的实时斜率决定，以机器人判定为准。")
        st.caption("若复核不通过（趋势已不干净 / 资金费率超 0.1% 且逆费率方向），机器人会拒绝开仓并在运行日志中说明原因。")
        agree = st.checkbox("我确认：全仓开单（可用余额 95% × 3 倍杠杆），由机器人复核趋势后自动执行")
        if agree:
            st.session_state["hold_refresh"] = True   # 勾选后停止整页刷新，锁定所选币种
        st.caption(f"本次将发送的开仓币种：**{sel}**（请核对与下拉框一致再点击按钮）")
        if st.button(f"✅ 确认开仓 {sel}", type="primary", disabled=not agree, use_container_width=True):
            write_open_request(sel)
            st.session_state["hold_refresh"] = False  # 请求已发出，恢复自动刷新
            st.success(f"开仓请求已发送（{sel}），机器人将在下一轮（≤1 秒）复核后全仓开单。")
        if os.path.exists(OPEN_REQUEST_FILE):
            st.warning("⚠️ 已有待处理开仓请求，机器人处理前请勿重复发送。"
                       "（若机器人暂停中或有持仓，请求会保留到恢复后处理）")
    else:
        st.info("暂无扫描数据。机器人需处于空仓状态并完成一轮扫描（空仓后每 2 分钟一次）才会写入结果；"
                "若正在持仓中，将保留上一次扫描结果或为空。")
        st.caption("提示：机器人仍处于暂停状态时不会扫描，请在下方“程序控制”恢复交易。")

    st.stop()


# ================= 账户分析 =================

# 读取交易记录（当前币种以机器人状态快照为准，取不到则用 CARVUSDT）
status, _ = load_status()
SYMBOL = status.get("symbol", "CARVUSDT") if status else "CARVUSDT"
BASE_ASSET = SYMBOL.replace("USDT", "")


def load_data():
    if not os.path.exists(TRADE_LOG):
        return pd.DataFrame(), None
    with open(TRADE_LOG, "r", encoding="utf-8") as f:
        data = json.load(f)
    return pd.DataFrame(data.get("trades", [])), data.get("open_position", None)


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
    st.info("暂无交易记录。请先运行 bot.py 开始交易。")
    st.stop()

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
    total_fee += float(open_pos.get("entry_fee", 0) or 0)

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
    side_cn = "做多" if open_pos.get("side") == "LONG" else "做空"
    otype_cn = ORDER_TYPE_CN.get(open_pos.get("order_type"), "—")
    st.info(f"🟢 当前持仓中：{side_cn} {open_pos['qty']:.4f} {BASE_ASSET} @ {open_pos['price']:.4f}，名义 {open_pos['cost']:.2f} USDT（开仓方式：{otype_cn}）")
else:
    st.success("✅ 当前空仓，无浮动盈亏")

# ============ 分时盈亏曲线 ============
st.subheader("🕐 分时盈亏曲线（每 1 秒快照）")
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
