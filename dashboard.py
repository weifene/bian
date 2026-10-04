"""
币安现货量化交易 - 账户分析仪表盘

功能：
  - 总盈亏概览（已实现 + 浮动）
  - 历史交易记录表
  - 按交易笔数的盈亏曲线
  - 胜率、平均盈亏、最大盈利/亏损统计

用法：streamlit run dashboard.py
"""
import os
import json
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# 代理配置（与 bot.py 保持一致）
PROXY = "socks5h://127.0.0.1:7892"
if PROXY:
    os.environ["HTTP_PROXY"] = PROXY
    os.environ["HTTPS_PROXY"] = PROXY
    os.environ["ALL_PROXY"] = PROXY

TRADE_LOG = os.path.join(os.path.dirname(__file__), "trade_log.json")
PNL_LOG = os.path.join(os.path.dirname(__file__), "pnl_history.json")
SYMBOL = "ARBUSDT"  # 与 bot.py 保持一致（U 本位合约）
BASE_ASSET = SYMBOL.replace("USDT", "")

st.set_page_config(page_title="币安交易账户分析", layout="wide")
st.title("📊 币安现货交易账户分析")


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

# ============ 顶部概览卡片 ============
col1, col2, col3, col4 = st.columns(4)

realized_pnl = df["pnl"].sum() if not df.empty else 0
total_trades = len(df)
win_trades = (df["pnl"] > 0).sum() if not df.empty else 0
win_rate = (win_trades / total_trades * 100) if total_trades > 0 else 0

col1.metric("已实现盈亏", f"{realized_pnl:+.2f} USDT")
col2.metric("总交易次数", f"{total_trades} 笔")
col3.metric("胜率", f"{win_rate:.1f}%")
col4.metric("平均单笔盈亏", f"{(realized_pnl / total_trades):+.2f} USDT" if total_trades > 0 else "0.00 USDT")

# ============ 浮动盈亏 ============
if open_pos:
    side_cn = "做多" if open_pos.get("side") == "LONG" else "做空"
    st.info(f"🟢 当前持仓中：{side_cn} {open_pos['qty']:.4f} {BASE_ASSET} @ {open_pos['price']:.4f}，名义 {open_pos['cost']:.2f} USDT")
else:
    st.success("✅ 当前空仓，无浮动盈亏")

# ============ 分时盈亏曲线 ============
st.subheader("🕐 分时盈亏曲线（每 60 秒快照）")
pnl_df = load_pnl_history()
if not pnl_df.empty:
    fig_pnl = go.Figure()
    fig_pnl.add_trace(go.Scatter(x=pnl_df["dt"], y=pnl_df["total"], name="总盈亏", line=dict(color="#26a69a")))
    fig_pnl.add_trace(go.Scatter(x=pnl_df["dt"], y=pnl_df["realized"], name="已实现", line=dict(color="#42a5f5")))
    fig_pnl.update_layout(title="账户分时盈亏 (USDT)", xaxis_title="时间", yaxis_title="盈亏 (USDT)",
                          hovermode="x unified")
    st.plotly_chart(fig_pnl, use_container_width=True)
    st.caption(f"共 {len(pnl_df)} 条快照，从 {pnl_df['time'].iloc[0]} 到 {pnl_df['time'].iloc[-1]}")
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
    display_df["盈亏(USDT)"] = display_df["盈亏(USDT)"].map(lambda x: f"{x:+.4f}")
    st.dataframe(display_df, use_container_width=True)
else:
    st.info("暂无已完成交易记录，等待第一笔平仓...")

st.caption(f"数据来源：{TRADE_LOG} 与 {PNL_LOG}")
