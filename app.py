"""股票記帳表格 Web App（Streamlit + SQLite）

功能：
- 左側側邊欄：交易表單（交易日期、股票代號、股票名稱、買進/賣出、成交單價、股數、手續費）
- 主畫面上方：「累計投入資金」與「淨現金流變動」卡片
- 主畫面下方：「歷史交易明細」表格

資料儲存於本地 SQLite 資料庫 stock.db（與 app.py 同目錄），程式啟動時會自動建表。
"""

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

st.set_page_config(page_title="股票記帳表格", page_icon="📈", layout="wide")

DB_PATH = Path(__file__).resolve().parent / "stock.db"

CREATE_TRANSACTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('買進', '賣出')),
    unit_price REAL NOT NULL,
    shares INTEGER NOT NULL,
    fee REAL NOT NULL,
    amount REAL NOT NULL,
    cash_flow REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
)
"""

SELECT_TRANSACTIONS = """
SELECT
    trade_date AS 交易日期,
    stock_code AS 股票代號,
    stock_name AS 股票名稱,
    side AS 方向,
    unit_price AS 成交單價,
    shares AS 股數,
    fee AS 手續費,
    amount AS 成交總額,
    cash_flow AS 現金流
FROM transactions
ORDER BY id
"""


def get_connection() -> sqlite3.Connection:
    """取得 SQLite 連線（指定欄名為查詢結果的 key）。"""
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    """啟動時檢查並建立 transactions 資料表（若不存在）。"""
    with get_connection() as conn:
        conn.execute(CREATE_TRANSACTIONS_TABLE)


def insert_trade(record: dict) -> None:
    """寫入一筆交易紀錄。"""
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO transactions (
                trade_date, stock_code, stock_name, side,
                unit_price, shares, fee, amount, cash_flow
            ) VALUES (
                :trade_date, :stock_code, :stock_name, :side,
                :unit_price, :shares, :fee, :amount, :cash_flow
            )
            """,
            record,
        )


def load_trades() -> pd.DataFrame:
    """讀取所有交易紀錄（以中文欄名回傳 DataFrame）。"""
    with get_connection() as conn:
        return pd.read_sql_query(SELECT_TRANSACTIONS, conn)


def clear_trades() -> None:
    """清空 transactions 資料表內的所有資料。"""
    with get_connection() as conn:
        conn.execute("DELETE FROM transactions")


def calc_cash_flow(side: str, price: float, shares: int, fee: float) -> float:
    """計算單筆交易的現金流：買進為負（資金流出）、賣出為正（資金流入），均含手續費。"""
    amount = price * shares
    if side == "買進":
        return -(amount + fee)
    return amount - fee


def fmt_nt(value: float) -> str:
    """將金額格式化為 NT$ 樣式（負號置於最左側）。"""
    sign = "-" if value < 0 else ""
    return f"{sign}NT${abs(value):,.2f}"


init_db()
trades_df = load_trades()


# ---------------------------------------------------------------- 側邊欄：交易表單
with st.sidebar:
    st.header("📝 新增交易")

    with st.form("trade_form"):
        trade_date = st.date_input("交易日期", value=date.today())
        stock_code = st.text_input("股票代號", placeholder="例如：2330")
        stock_name = st.text_input("股票名稱", placeholder="例如：台積電")
        side = st.radio("買進/賣出", ("買進", "賣出"), horizontal=True)
        unit_price = st.number_input(
            "成交單價", min_value=0.0, value=0.0, step=0.01, format="%.2f"
        )
        shares = st.number_input("股數", min_value=0, value=0, step=100, format="%d")
        fee = st.number_input("手續費", min_value=0.0, value=0.0, step=1.0, format="%.2f")
        submitted = st.form_submit_button("提交紀錄", type="primary", width="stretch")

    if submitted:
        errors = []
        if not stock_code.strip():
            errors.append("請輸入股票代號")
        if not stock_name.strip():
            errors.append("請輸入股票名稱")
        if unit_price <= 0:
            errors.append("成交單價需大於 0")
        if shares <= 0:
            errors.append("股數需大於 0")

        if errors:
            for message in errors:
                st.error(message)
        else:
            insert_trade(
                {
                    "trade_date": trade_date.isoformat(),
                    "stock_code": stock_code.strip(),
                    "stock_name": stock_name.strip(),
                    "side": side,
                    "unit_price": float(unit_price),
                    "shares": int(shares),
                    "fee": float(fee),
                    "amount": float(unit_price) * int(shares),
                    "cash_flow": calc_cash_flow(
                        side, float(unit_price), int(shares), float(fee)
                    ),
                }
            )
            trades_df = load_trades()  # 重新讀取，讓下方卡片與表格立即更新
            st.success(f"已新增{side}：{stock_code.strip()} {stock_name.strip()}")

    st.divider()
    if st.button(
        "🧹 清除所有紀錄",
        width="stretch",
        disabled=trades_df.empty,
    ):
        clear_trades()
        st.rerun()


# --------------------------------------------------------------------- 主畫面
st.title("📈 股票記帳表格")

total_invested = float(
    (trades_df.loc[trades_df["方向"] == "買進", "成交總額"]
     + trades_df.loc[trades_df["方向"] == "買進", "手續費"]).sum()
)
net_cash_flow = float(trades_df["現金流"].sum())
buy_count = int((trades_df["方向"] == "買進").sum())
sell_count = int(len(trades_df) - buy_count)

col_invested, col_net = st.columns(2)
with col_invested:
    st.metric(
        "累計投入資金",
        value=fmt_nt(total_invested),
        border=True,
        help="所有「買進」的成交總額加計手續費",
    )
with col_net:
    st.metric(
        "淨現金流變動",
        value=fmt_nt(net_cash_flow),
        border=True,
        help="賣出實收 − 買進實付（含手續費）",
    )

st.caption(
    f"共 {len(trades_df)} 筆交易（買進 {buy_count} 筆、賣出 {sell_count} 筆）｜"
    "累計投入資金 = 買進成交總額 + 手續費；淨現金流變動 = 賣出實收 − 買進實付（含手續費）"
)

st.subheader("歷史交易明細")
if trades_df.empty:
    st.info("尚無交易紀錄，請先在左側側邊欄新增一筆交易。")
else:
    st.dataframe(
        trades_df,
        width="stretch",
        hide_index=True,
        column_config={
            "交易日期": st.column_config.TextColumn("交易日期", width="small"),
            "股票代號": st.column_config.TextColumn("股票代號", width="small"),
            "股票名稱": st.column_config.TextColumn("股票名稱"),
            "方向": st.column_config.TextColumn("方向", width="small"),
            "成交單價": st.column_config.NumberColumn("成交單價（NT$）", format="%.2f"),
            "股數": st.column_config.NumberColumn("股數", format="%d"),
            "手續費": st.column_config.NumberColumn("手續費（NT$）", format="%.2f"),
            "成交總額": st.column_config.NumberColumn("成交總額（NT$）", format="%.2f"),
            "現金流": st.column_config.NumberColumn("現金流（NT$）", format="%.2f"),
        },
    )
    st.caption("現金流：買進為負（流出）、賣出為正（流入），皆已計入手續費。")
