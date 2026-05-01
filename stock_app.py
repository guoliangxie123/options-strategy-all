import streamlit as st
import re
import json
import requests
import pandas as pd
from datetime import datetime
from longport.openapi import Config, TradeContext, QuoteContext

st.set_page_config(page_title="Sell Put 风险监控室", page_icon="🛡️", layout="wide")
st.title("🛡️ Sell Put 深度风险与资金监控看板")

# --- 1. 从云端 Secrets 获取配置 ( 绝不把密钥写在代码里 ) ---
try:
    APP_KEY = st.secrets["LONGPORT_APP_KEY"]
    APP_SECRET = st.secrets["LONGPORT_APP_SECRET"]
    ACCESS_TOKEN = st.secrets["LONGPORT_ACCESS_TOKEN"]
    FEISHU_WEBHOOK = st.secrets.get("FEISHU_WEBHOOK", "")
except Exception as e:
    st.error("⚠️ 密钥未配置！请在 Streamlit Cloud 的 Secrets 中配置 LONGPORT_APP_KEY 等环境变量。")
    st.stop()


# --- 2. 全局单例连接池 ---
@st.cache_resource
def get_longport_config():
    return Config(app_key=APP_KEY, app_secret=APP_SECRET, access_token=ACCESS_TOKEN)


@st.cache_resource
def get_trade_context():
    return TradeContext(get_longport_config())


@st.cache_resource
def get_quote_context():
    return QuoteContext(get_longport_config())


# --- 3. 核心数据获取逻辑 ---
def get_account_summary():
    ctx = get_trade_context()
    try:
        balances = ctx.account_balance(currency='USD')
        summary = {"total_cash": 0.0, "net_assets": 0.0, "init_margin": 0.0, "buy_power": 0.0, "maint_margin": 0.0}
        for bal in balances:
            summary["total_cash"] += float(bal.total_cash)
            summary["net_assets"] += float(bal.net_assets)
            summary["init_margin"] += float(bal.init_margin)
            summary["buy_power"] += float(bal.buy_power)
            summary["maint_margin"] += float(bal.maintenance_margin)
        return summary
    except Exception as e:
        st.error(f"获取账户信息失败: {e}")
        return None


def get_my_sell_puts():
    ctx = get_trade_context()
    sell_puts, total_notional = [], 0.0
    resp = ctx.stock_positions()

    for channel in resp.channels:
        for pos in channel.positions:
            if pos.quantity < 0 and "Put" in pos.symbol_name:
                # 解析长桥期权代号，例如 AAPL240119P150000.US
                match = re.match(r'^([A-Z0-9]+)(\d{6})P(\d+)\.([A-Z]+)$', pos.symbol)
                if match:
                    underlying = f"{match.group(1)}.{match.group(4)}"
                    raw_date = match.group(2)
                    expiry_str = f"20{raw_date[0:2]}-{raw_date[2:4]}-{raw_date[4:6]}"
                    strike_price = float(match.group(3)) / 1000
                    dte = max((datetime.strptime(expiry_str, '%Y-%m-%d') - datetime.now()).days, 0)
                    notional = strike_price * 100 * abs(int(pos.quantity))
                    total_notional += notional
                else:
                    underlying, expiry_str, strike_price, dte, notional = "UNKNOWN", "N/A", 0.0, 0, 0.0

                sell_puts.append({
                    "symbol": pos.symbol, "underlying": underlying, "strike_price": strike_price,
                    "expiry_date": expiry_str, "dte": dte, "qty": abs(int(pos.quantity)),
                    "cost": abs(float(pos.cost_price)), "notional": notional
                })
    return sell_puts, total_notional


def get_quotes(symbols, is_option=False):
    if not symbols: return {}
    ctx = get_quote_context()
    try:
        symbols = list(set([s for s in symbols if s != "UNKNOWN"]))
        quotes = ctx.option_quote(symbols) if is_option else ctx.quote(symbols)
        return {q.symbol: float(q.last_done) for q in quotes}
    except Exception as e:
        return {}


def send_to_feishu(text):
    if not FEISHU_WEBHOOK:
        st.sidebar.warning("未配置飞书 Webhook 链接！")
        return
    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        requests.post(FEISHU_WEBHOOK, data=json.dumps(payload), headers={'Content-Type': 'application/json'}, timeout=5)
        st.sidebar.success("✅ 飞书推送成功！")
    except Exception as e:
        st.sidebar.error(f"❌ 飞书推送失败: {e}")


# --- 4. 界面渲染 ---
def main():
    st.sidebar.header("🕹️ 控制面板")
    if st.sidebar.button("🔄 刷新全部数据", use_container_width=True):
        st.rerun()

    with st.spinner("正在连接长桥服务器获取底层数据..."):
        account_info = get_account_summary()
        my_puts, total_notional = get_my_sell_puts()

    # ---- 第一部分：资金核心看板 ----
    if account_info:
        net = account_info['net_assets']
        bp = account_info['buy_power']
        margin_usage = (account_info['init_margin'] / net * 100) if net > 0 else 0
        leverage = total_notional / bp if bp > 0 else 0

        st.markdown("### 💰 账户水位监控")
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("💵 净资产", f"${net:,.2f}")
        c2.metric("🛡️ 剩余购买力", f"${bp:,.2f}")

        # 杠杆和保证金着色预警
        margin_color = "normal" if margin_usage < 60 else "inverse"
        c3.metric("⚠️ 保证金使用率", f"{margin_usage:.2f}%", delta="爆仓风险较高" if margin_usage > 80 else "安全",
                  delta_color=margin_color)
        c4.metric("🏗️ 总名义价值暴露", f"${total_notional:,.2f}")
        c5.metric("⚖️ 裸卖杠杆倍数", f"{leverage:.2f}x", delta="杠杆极高" if leverage > 2 else "稳健",
                  delta_color="inverse" if leverage > 2 else "normal")

    st.divider()

    # ---- 第二部分：持仓风险明细 ----
    st.markdown("### 📊 Sell Put 持仓阵列")
    if not my_puts:
        st.info("当前账户没有任何 Sell Put 持仓。享受空仓的宁静吧！☕")
        return

    underlyings = [p['underlying'] for p in my_puts]
    opt_symbols = [p['symbol'] for p in my_puts]

    price_map = get_quotes(underlyings, is_option=False)
    opt_price_map = get_quotes(opt_symbols, is_option=True)

    display_data = []
    feishu_body = ""

    for p in my_puts:
        cur_p = price_map.get(p['underlying'], 0.0)
        opt_cur_p = opt_price_map.get(p['symbol'], 0.0)

        buffer_pct = ((cur_p - p['strike_price']) / cur_p * 100) if cur_p > 0 else 0.0
        profit_pct = ((p['cost'] - opt_cur_p) / p['cost'] * 100) if p['cost'] > 0 else 0.0

        # 判断状态
        if buffer_pct < 5 or p['dte'] < 7:
            status = "🔴 危险 ( 高危/末日 )"
            status_emoji = "🔴"
        elif buffer_pct < 12:
            status = "🟡 关注 ( 跌破警戒 )"
            status_emoji = "🟡"
        else:
            status = "🟢 安全"
            status_emoji = "🟢"

        target_tag = "🔥 已达标" if profit_pct >= 50 else ""

        display_data.append({
            "状态": status,
            "标的": p['underlying'],
            "数量": p['qty'],
            "标的现价": f"${cur_p:.2f}",
            "行权价": f"${p['strike_price']:.2f}",
            "安全垫 (%)": round(buffer_pct, 2),
            "浮盈 (%)": round(profit_pct, 2),
            "成本价": f"${p['cost']:.2f}",
            "最新期权价": f"${opt_cur_p:.2f}",
            "剩余天数 (DTE)": p['dte'],
            "标记": target_tag
        })

        feishu_body += (f"{status_emoji} | {p['underlying']} {target_tag}\n"
                        f" ├ 现价: {cur_p:.2f} | 行权: {p['strike_price']:.2f} | 安全垫: {buffer_pct:.2f}%\n"
                        f" └ 盈亏: {profit_pct:.2f}% ( 现价:{opt_cur_p:.2f}/成本:{p['cost']:.2f}) | DTE: {p['dte']} 天\n\n")

    # 渲染数据表格（带颜色高亮）
    df = pd.DataFrame(display_data).sort_values(by=["浮盈 (%)", "安全垫 (%)"])

    def highlight_risk(row):
        if "危险" in row['状态']: return ['background-color: #ffcccc'] * len(row)
        if "关注" in row['状态']: return ['background-color: #fff4cc'] * len(row)
        if "已达标" in row['标记']: return ['background-color: #ccffcc'] * len(row)
        return [''] * len(row)

    st.dataframe(df.style.apply(highlight_risk, axis=1), use_container_width=True, hide_index=True)

    # ---- 第三部分：飞书推送功能 ----
    if st.sidebar.button("📨 推送当前报告至飞书", use_container_width=True):
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        header = (f"📊 Sell Put 风险看板\n⏰ 时间: {now_str}\n"
                  f"💰 净资产: ${net:,.2f} | 🛡️ 购买力: ${bp:,.2f}\n"
                  f"⚠️ 保证金: {margin_usage:.2f}% | ⚖️ 杠杆: {leverage:.2f}x\n"
                  f"--------------------------\n")
        send_to_feishu(header + feishu_body)


if __name__ == "__main__":
    main()
