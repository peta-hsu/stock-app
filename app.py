"""台股智慧對帳損益監控系統（國泰證券計費標準 · SQLite 持久化 · TWSE 即時行情）

- 左側導覽：🏠 首頁 → 📊 總表 → 持股清單（純兩欄 st.dataframe；點擊任一列連動右側個股頁；
  個股頁右上【🔼 往上移一格 / 🔽 往下移一格】即時對調排序；新購自動加入清單最下方）
- 頂部：乾淨大標題＋右上角「🔄 刷新即時股價」（串接 TWSE 官方 MIS 即時撮合價，零延遲）
- 首頁中央：手動記帳表單（交易日期預設今天、手動尾數微調）
- 計費完全對齊國泰證券 App 電子單：買進/賣出手續費 0.1425%×0.28（無條件捨去、最低 1 元）、
  賣出證交稅 0.3%、二代健保補充保費 2.11%（單筆現金股利 ≥ 20,000 才扣）、跨行匯費 5 元
- 所有交易與除權息一律持久化至本地 SQLite（stock.db）
- 表格用 st.dataframe（保留內建複製到剪貼簿，可 Command+V 貼進 Mac Numbers）
- 台股紅漲綠跌：賺錢 #FF4B4B、賠錢 #00E676
"""

import math
import json
import random
import sqlite3
from datetime import date, datetime

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="台股智慧對帳損益監控系統", page_icon="📈", layout="wide")

# 全頁共用的即時台灣時間顯示器
st.components.v1.html(
    """
    <script>
      // 將時鐘直接掛到 Streamlit 主文件，避免只固定在元件 iframe 內。
      const hostDocument = window.parent.document;
      let clock = hostDocument.getElementById("taiwan-clock");
      if (!clock) {
        clock = hostDocument.createElement("div");
        clock.id = "taiwan-clock";
        clock.innerHTML = '台灣時間 <span id="taiwan-clock-value"></span>';
        Object.assign(clock.style, {
          position: "fixed",
          top: "56px",
          right: "24px",
          zIndex: "999999",
          padding: "8px 12px",
          borderRadius: "8px",
          background: "rgba(20, 25, 35, 0.92)",
          color: "#fff",
          font: "600 14px/1.4 sans-serif",
          whiteSpace: "nowrap",
          boxShadow: "0 2px 10px rgba(0, 0, 0, 0.25)"
        });
        hostDocument.body.appendChild(clock);
      }
      const clockValue = clock.querySelector("#taiwan-clock-value");
      const taiwanTimeFormatter = new Intl.DateTimeFormat("sv-SE", {
        timeZone: "Asia/Taipei",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23"
      });
      function updateTaiwanClock() {
        clockValue.textContent = taiwanTimeFormatter.format(new Date());
      }
      updateTaiwanClock();
      setInterval(updateTaiwanClock, 1000);
    </script>
    """,
    height=0,
    scrolling=False,
)

# 台股紅漲綠跌（依規格：賺錢紅 #FF4B4B、賠錢綠 #00E676）
GAIN = "#FF4B4B"   # 賺錢 / 上漲 紅
LOSS = "#00E676"   # 賠錢 / 下跌 綠
DIVIDEND = "#f5b942"  # 配股 琥珀

# ================================================================== 國泰證券計費公式
FEE_RATE = 0.001425          # 手續費費率
FEE_DISCOUNT = 0.28          # 國泰手續費折扣
TAX_RATE = 0.003             # 政府證券交易稅（賣出）
SUPP_HEALTH_RATE = 0.0211    # 二代健保補充保費率
SUPP_HEALTH_THRESHOLD = 20000  # 單筆現金股利達此門檻才扣補充保費
REMITTANCE_FEE = 5.0         # 除權息現金入帳跨行匯費


def _floor(x: float) -> int:
    """小數點後無條件捨去（含極小浮點誤差容忍）。"""
    return int(math.floor(x + 1e-9))


def _round_half_up(x: float) -> int:
    """四捨五入至整數（正數）。"""
    return int(math.floor(x + 0.5))


def buy_fee(gross: float) -> float:
    """買進手續費＝成交金額 × 0.001425 × 0.28，無條件捨去，單筆最低 1 元（無 20 元限制）。"""
    if gross <= 0:
        return 0.0
    return float(max(_floor(gross * FEE_RATE * FEE_DISCOUNT), 1))


def sell_fee(gross: float) -> float:
    """賣出手續費＝成交金額 × 0.001425 × 0.28，無條件捨去，單筆最低 1 元。"""
    if gross <= 0:
        return 0.0
    return float(max(_floor(gross * FEE_RATE * FEE_DISCOUNT), 1))


def sell_tax(gross: float) -> float:
    """賣出證券交易稅＝成交金額 × 0.003，無條件捨去。"""
    if gross <= 0:
        return 0.0
    return float(_floor(gross * TAX_RATE))

def supp_health(cash_dividend: float) -> float:
    """二代健保補充保費：單筆現金股利 ≥ 20,000 才扣 2.11%（四捨五入至整數），否則 0。"""
    if cash_dividend >= SUPP_HEALTH_THRESHOLD:
        return float(_round_half_up(cash_dividend * SUPP_HEALTH_RATE))
    return 0.0


# ================================================================== TWSE 即時行情（零延遲撮合價）
def fetch_twse_price(code: str):
    """呼叫台灣證券交易所 MIS 官方 API 取得即時撮合成交價。

    回傳 (price, name, source)：price 為浮點數或 None；source 標示「即時」或「昨收」。
    先試上市 tse_，再試上櫃 otc_；z（當盤成交價）無效時改用 y（昨收）作為 fallback。
    """
    for ex in (f"tse_{code}.tw", f"otc_{code}.tw"):
        try:
            r = requests.get(
                "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                params={"ex_ch": ex},
                timeout=6,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            arr = r.json().get("msgArray", [])
            if not arr:
                continue

            def _num(x):
                try:
                    v = float(str(x).replace(",", ""))
                    return v if v > 0 else None
                except (TypeError, ValueError):
                    return None

            z, y, name = arr[0].get("z"), arr[0].get("y"), arr[0].get("n")
            price, src = _num(z), "即時"
            if price is None:
                price, src = _num(y), "昨收"
            if price is not None:
                return price, name, src
        except Exception:
            continue
    return None, None, None


# ================================================================== SQLite 持久化
DB_PATH = "stock.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date TEXT NOT NULL,
        stock_code TEXT NOT NULL,
        stock_name TEXT,
        tx_type TEXT NOT NULL,            -- 買進 / 賣出 / 配息 / 配股
        unit_price REAL NOT NULL DEFAULT 0,
        shares INTEGER NOT NULL DEFAULT 0,
        gross REAL NOT NULL DEFAULT 0,     -- 成交金額（買/賣）或現金股利總額（配息）
        buy_fee REAL NOT NULL DEFAULT 0,
        sell_fee REAL NOT NULL DEFAULT 0,
        tax REAL NOT NULL DEFAULT 0,
        supp_health REAL NOT NULL DEFAULT 0,
        remittance REAL NOT NULL DEFAULT 0,
        tail_adjust REAL NOT NULL DEFAULT 0,
        net_cash_flow REAL NOT NULL DEFAULT 0,  -- 帶正負號：+ 現金流入 / - 現金流出
        note TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    )
    """
)
conn.commit()


def _migrate_strategy_columns():
    """為 trades 表補上 4 個交易策略備註欄位（Why buy / 停損價位 / Reason price change / Why sold）。

    這條時間軸流水帳即為「trade_ledger」（單一橫向一條龍對帳單）的資料來源；
    右側一條龍大表格由 build_trade_ledger() 依此資料 + 國泰計費公式逐列展開。
    採 ALTER TABLE ADD COLUMN 增量遷移，舊資料庫可直接升級、不影響既有紀錄。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    migrations = [
        ("why_buy", "TEXT"),             # Why buy（進場理由）
        ("stop_loss", "REAL DEFAULT 0"),  # 停損價位
        ("reason_change", "TEXT"),        # Reason price change（價格變動原因）
        ("why_sold", "TEXT"),             # Why sold（出場理由）
    ]
    for col, decl in migrations:
        if col not in existing:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {decl}")
    conn.commit()


_migrate_strategy_columns()




def record_trade(trade_date, code, name, tx_type, unit_price, shares, tail_adjust, note, per_share_div=None,
                 why_buy="", stop_loss=0.0, reason_change="", why_sold=""):
    """依國泰公式算出各項費用與淨現金流後寫入 SQLite，回傳計算結果字典（供成功訊息顯示）。

    4 個交易策略備註（why_buy / stop_loss / reason_change / why_sold）與稅費交割款
    一併寫入 trade_ledger 資料來源（trades 表），供右側一條龍大表格最右側呈現。
    """
    shares = int(shares)
    gross = float(unit_price) * shares
    b_fee = s_fee = tax = supp = remit = 0.0
    net = 0.0

    if tx_type == "買進":
        b_fee = buy_fee(gross)
        net = -(gross + b_fee) + tail_adjust
    elif tx_type == "賣出":
        s_fee = sell_fee(gross)
        tax = sell_tax(gross)
        net = (gross - s_fee - tax) + tail_adjust
    elif tx_type == "配息":
        per_share = float(per_share_div) if per_share_div is not None else float(unit_price)
        gross = per_share * shares
        supp = supp_health(gross)
        remit = REMITTANCE_FEE
        net = (gross - supp - remit) + tail_adjust
    elif tx_type == "配股":
        gross = 0.0
        net = tail_adjust  # 配股無現金流

    conn.execute(
        """INSERT INTO trades
           (trade_date, stock_code, stock_name, tx_type, unit_price, shares, gross,
            buy_fee, sell_fee, tax, supp_health, remittance, tail_adjust, net_cash_flow, note,
            why_buy, stop_loss, reason_change, why_sold)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date),
         code, name, tx_type, float(unit_price), shares, gross,
         b_fee, s_fee, tax, supp, remit, float(tail_adjust), net, note,
         why_buy or "", float(stop_loss or 0.0), reason_change or "", why_sold or ""),
    )
    conn.commit()
    return {
        "gross": gross, "buy_fee": b_fee, "sell_fee": s_fee, "tax": tax,
        "supp_health": supp, "remittance": remit, "tail_adjust": float(tail_adjust), "net": net,
    }


def update_trades(updates):
    """更新流水帳原始欄位，並依交易類型重算所有費用與淨現金流。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in updates:
            trade_id = int(item["id"])
            existing = conn.execute(
                "SELECT tx_type, tail_adjust FROM trades WHERE id = ?", (trade_id,)
            ).fetchone()
            if existing is None:
                raise ValueError(f"找不到交易紀錄：{trade_id}")

            tx_type, tail_adjust = existing
            unit_price = float(item["unit_price"])
            shares_value = float(item["shares"])
            stop_loss = float(item["stop_loss"])
            if not all(math.isfinite(value) for value in (unit_price, shares_value, stop_loss)):
                raise ValueError("價格、股數與停損價位必須是有效數字")
            if shares_value <= 0 or not shares_value.is_integer():
                raise ValueError("股數必須是大於 0 的整數")
            if stop_loss < 0:
                raise ValueError("停損價位不可小於 0")
            if (tx_type in ("買進", "賣出") and unit_price <= 0) or (
                tx_type == "配息" and unit_price < 0
            ):
                raise ValueError("交易單價必須大於 0，股息金額不可小於 0")

            shares = int(shares_value)
            gross = unit_price * shares
            b_fee = s_fee = tax = supp = remit = 0.0
            if tx_type == "買進":
                b_fee = buy_fee(gross)
                net = -(gross + b_fee) + tail_adjust
            elif tx_type == "賣出":
                s_fee = sell_fee(gross)
                tax = sell_tax(gross)
                net = gross - s_fee - tax + tail_adjust
            elif tx_type == "配息":
                supp = supp_health(gross)
                remit = REMITTANCE_FEE
                net = gross - supp - remit + tail_adjust
            else:
                gross = 0.0
                unit_price = 0.0
                net = tail_adjust
            conn.execute(
                """UPDATE trades SET trade_date = ?, unit_price = ?, shares = ?, gross = ?,
                   buy_fee = ?, sell_fee = ?, tax = ?, supp_health = ?, remittance = ?,
                   net_cash_flow = ?, why_buy = ?, stop_loss = ?, reason_change = ?, why_sold = ?
                   WHERE id = ?""",
                (
                    (item["trade_date"].isoformat() if hasattr(item["trade_date"], "isoformat") else str(item["trade_date"])), unit_price, shares, gross,
                    b_fee, s_fee, tax, supp, remit, net,
                    item["why_buy"], stop_loss, item["reason_change"], item["why_sold"], trade_id,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def delete_trades(trade_ids):
    """一次刪除所選交易紀錄。"""
    ids = tuple(dict.fromkeys(int(trade_id) for trade_id in trade_ids))
    if not ids:
        return 0

    placeholders = ",".join("?" for _ in ids)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            f"DELETE FROM trades WHERE id IN ({placeholders})", ids
        )
        conn.commit()
        return cursor.rowcount
    except Exception:
        conn.rollback()
        raise


def load_trades():
    """由 SQLite 讀取全部交易，依日期與 id 排序（供重放計算持股與一條龍對帳單）。"""
    cur = conn.execute(
        "SELECT trade_date, stock_code, stock_name, tx_type, unit_price, shares, gross,"
        " buy_fee, sell_fee, tax, supp_health, remittance, tail_adjust, net_cash_flow, note, id,"
        " why_buy, stop_loss, reason_change, why_sold"
        " FROM trades ORDER BY trade_date ASC, id ASC"
    )
    cols = ["trade_date", "stock_code", "stock_name", "tx_type", "unit_price", "shares", "gross",
            "buy_fee", "sell_fee", "tax", "supp_health", "remittance", "tail_adjust", "net_cash_flow",
            "note", "id", "why_buy", "stop_loss", "reason_change", "why_sold"]
    return [dict(zip(cols, row)) for row in cur.fetchall()]



def seed_demo_trades():
    """首次啟動且資料表為空時，寫入示範歷史交易（Mock 假資料，之後即持久化）。"""
    if conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] > 0:
        return
    demo = [
        ("2024-05-15", "2330", "台積電", "買進", 900.00, 200, "定期定額轉單筆"),
        ("2026-03-04", "2330", "台積電", "賣出", 1042.00, 100, "高檔減碼、實現獲利"),
        ("2026-09-15", "2330", "台積電", "買進", 972.50, 100, "低檔分批加碼"),
        ("2026-09-18", "2330", "台積電", "配息", 4.5, 200, "每股 4.5 元現金股利"),
        ("2026-06-18", "6127", "九豪", "買進", 15.80, 5000, "低基期試單"),
        ("2026-06-20", "6127", "九豪", "配股", 0.0, 500, "每 1,000 股配 100 股（股票股利）"),
        ("2026-07-01", "1303", "南亞", "買進", 44.65, 3000, "低檔分批建倉"),
        ("2026-08-03", "1303", "南亞", "配息", 1.0, 3000, "每股 1.0 元現金股利"),
    ]
    for d, code, name, tt, price, sh, note in demo:
        per = price if tt == "配息" else None
        record_trade(date.fromisoformat(d), code, name, tt, price, sh, 0.0, note, per_share_div=per)



# ================================================================== 持股重放引擎（由 SQLite 交易重算）
def replay_holdings():
    """依 SQLite 交易時間序列重放，算出每檔持股的股數、均價(打成本)、實現/未實現損益。

    - 買進：股數 += shares，成本累計 += (成交金額 + 買入手續費)
    - 賣出：股數 -= shares，成本按均價等比例扣減（打成本不變），實現損益 += 淨現金流 + 已扣成本
    - 配息：不影響股數與成本，現金股利(扣除健保與匯費)記入現金流
    - 配股：股數 += 配股數，總成本不變 → 自動攤薄均價（均價 = 原始總成本 ÷ 最新總股數）
    回傳 {code: {...}}，並回傳每檔的交易與除權息明細。
    """
    trades = load_trades()
    H = {}
    for t in trades:
        code = t["stock_code"]
        name = (t["stock_name"] or code).strip() or code
        h = H.setdefault(code, {
            "名稱": name, "股數": 0, "總成本": 0.0, "已實現損益": 0.0,
            "現金流淨額": 0.0, "交易": [], "除權息": [],
        })
        h["名稱"] = name
        tt = t["tx_type"]
        sh = int(t["shares"])
        if tt == "買進":
            h["股數"] += sh
            h["總成本"] += t["gross"] + t["buy_fee"]
            h["現金流淨額"] += t["net_cash_flow"]
            h["交易"].append({
                "交易日期": t["trade_date"], "交易類型": "買進", "成交單價": t["unit_price"],
                "成交股數": sh, "成交金額": t["gross"], "手續費(買)": t["buy_fee"],
                "淨現金流": t["net_cash_flow"], "說明": t["note"] or "",
            })
        elif tt == "賣出":
            avg = (h["總成本"] / h["股數"]) if h["股數"] > 0 else 0.0
            cost_out = avg * sh
            h["總成本"] -= cost_out
            h["股數"] -= sh
            h["現金流淨額"] += t["net_cash_flow"]
            h["已實現損益"] += t["net_cash_flow"] + cost_out
            h["交易"].append({
                "交易日期": t["trade_date"], "交易類型": "賣出", "成交單價": t["unit_price"],
                "成交股數": sh, "成交金額": t["gross"], "手續費(賣)": t["sell_fee"],
                "證交稅": t["tax"], "淨現金流": t["net_cash_flow"],
                "實現損益": t["net_cash_flow"] + cost_out, "說明": t["note"] or "",
            })
        elif tt == "配息":
            h["現金流淨額"] += t["net_cash_flow"]
            h["除權息"].append({
                "日期": t["trade_date"], "類型": "現金配息", "每股配息": t["unit_price"],
                "股數": sh, "現金股利總額": t["gross"], "補充保費": t["supp_health"],
                "跨行匯費": t["remittance"], "淨現金流": t["net_cash_flow"], "說明": t["note"] or "",
            })
        elif tt == "配股":
            h["股數"] += sh
            h["除權息"].append({
                "日期": t["trade_date"], "類型": "股票配股", "配股股數": sh, "單位": "股",
                "增資後股數": h["股數"], "說明": t["note"] or "",
            })
    for code, h in H.items():
        h["均價"] = (h["總成本"] / h["股數"]) if h["股數"] > 0 else 0.0
    return H


# ================================================================== CSS（全域緊湊化）
CSS_COMPACT = """
<style>
/* ---- 規格一：側邊欄瘦身 220~240px ----
   關鍵：寬度必須「只在展開時」覆寫，也就是掛上 [aria-expanded="true"]。
   原因（實測驗證）：Streamlit 收合側邊欄的機制，是把
       section[data-testid="stSidebar"] 的 width / min-width / max-width 全部設成 0 !important，
       並用 aria-expanded="false" 標記收合狀態。
   若像過去那樣寫成不帶條件的
       section[data-testid="stSidebar"] { width: 234px !important; min-width: 234px !important; }
   我們的 !important 會蓋掉 Streamlit 的 width: 0，側邊欄雖然被 translate 移出畫面，
   卻仍在排版流裡佔住 234px → 主畫面無法向左補滿，左側空出一大片不對稱空白。
   加上 [aria-expanded="true"] 後，收合時這條規則不匹配，Streamlit 原生的 width:0 得以生效。 */
section[data-testid="stSidebar"][aria-expanded="true"] {
    width: 234px !important; min-width: 234px !important; max-width: 240px !important;
    background: linear-gradient(180deg, #0e0e12 0%, #08080a 100%);
    border-right: 1px solid rgba(255,255,255,0.07);
}
/* 收合狀態下確保不殘留任何寬度佔用（雙保險，避免佔位造成左側空白） */
section[data-testid="stSidebar"][aria-expanded="false"] {
    width: 0 !important; min-width: 0 !important; max-width: 0 !important;
    padding: 0 !important; margin: 0 !important; border: none !important;
    overflow: hidden !important;
}
section[data-testid="stSidebar"] [data-testid="stSidebarContent"] { padding: 6px 8px; }
section[data-testid="stSidebar"] .stMarkdown p { color: #d9d9de; font-size: 0.8rem; }
section[data-testid="stSidebar"] .stCaption, section[data-testid="stSidebar"] caption { font-size: 0.62rem !important; }
section[data-testid="stSidebar"] div.stButton > button {
    text-align: left; justify-content: flex-start; border-radius: 8px;
    font-size: 0.82rem; padding: 4px 10px; min-height: 30px;
}
section[data-testid="stSidebar"] .numbers-title { font-size: 0.7rem; padding: 3px 8px; margin: 2px 0 4px 0; letter-spacing: 0.12em; }

/* ---- 規格一：主畫面動態全螢幕延展 ----
   注意：絕對不可覆寫 padding-top。stHeader 為 position:fixed、高 60px，
   一旦把頂部留白壓到 60px 以下，最上方的大標題就會鑽進工具列底下被「切邊／遮蓋」。
   此處只保留 Streamlit 預設的頂部留白，僅調整左右與底部。
   max-width / width 設 100% 是為解開 Streamlit 預設 730px 的置中限制；
   側邊欄收合後，stMain 會由 Streamlit 自動拉寬到全螢幕，此容器隨之填滿。 */
div[data-testid="stMainBlockContainer"] {
    max-width: 100% !important;
    width: 100% !important;
    padding-left: 2rem !important;
    padding-right: 2rem !important;
    padding-bottom: 14px !important;
}

/* ---- 規格一：全域字體（強制鎖定 Mac / Windows 最穩定的原生繁中字型） ----
   注意：嚴禁把 Streamlit 內建圖示一起改字型。Streamlit 的圖示有兩種：
     1) <svg> 圖示（首頁、總表、dataframe 工具列…）—— 不吃 font-family，天然安全。
     2) Material Symbols 圖示，例如左上角收放側邊欄的雙箭頭與 expander 箭頭，
        其實是渲染 <span data-testid="stIconMaterial">keyboard_double_arrow_left</span>，
        靠「Material Symbols Rounded」這個字型的 ligature 把英文原名變成圖示。
        一旦把它的 font-family 改成 PingFang／JhengHei，字型沒有這個 ligature，
        英文原名（keyboard_double_arrow_left、keyboard_arrow_down…）就會原封不動
        當文字印在畫面上——這就是過去「◆AB」「arrow_…」亂碼的真正來源。
   因此這裡以 :not() 精準排除 stIconMaterial，讓它保留 Streamlit 原生的
   Material Symbols 字型，雙箭頭與 expander 箭頭就能正常顯示、正常點擊。 */
html,
body,
[data-testid="stAppViewContainer"],
[data-testid="stAppViewContainer"] *:not([data-testid="stIconMaterial"]):not([data-testid="stIconMaterial"] *) {
    font-family: "PingFang TC", "Microsoft JhengHei", sans-serif !important;
}

/* ---- 標題：僅縮字級，不限制行高／高度，讓中文字與 emoji 自然排開不重疊 ---- */
h1, h2, h3, h4 {
    line-height: 1.5 !important;
    overflow: visible !important;
    max-height: none !important;
    white-space: normal !important;
}
h1 { font-size: 1.35rem !important; font-weight: 800 !important; margin: 10px 0 8px 0 !important; }
h2 { font-size: 1.1rem !important; font-weight: 700 !important; margin: 10px 0 6px 0 !important; }
h3 { font-size: 0.95rem !important; font-weight: 700 !important; margin: 8px 0 5px 0 !important; }
.stApp h1, .stApp h2, .stApp h3, .stApp h4 { color: #f4f4f6 !important; letter-spacing: 0.02em; }
.stCaption, .stCaptionContainer, caption { font-size: 0.7rem !important; margin: 4px 0 !important; line-height: 1.5 !important; }

div[data-testid="stVerticalBlock"] { gap: 8px !important; }
.element-container { margin: 5px 0 !important; }
.stMarkdown { margin: 4px 0 !important; }

/* ---- 頂部大標題（自然行高，無任何高度／切邊限制） ---- */
.app-title {
    font-size: clamp(1.3rem, 2.4vw, 1.85rem);
    font-weight: 800;
    letter-spacing: 0.02em;
    color: #f6f6f8;
    text-shadow: 0 0 18px rgba(255, 77, 79, 0.28);
    line-height: 1.5;
    margin: 4px 0 8px;
    padding: 0;
    overflow: visible;
    max-height: none;
    white-space: normal;
}


/* ---- 按鈕：黑色質感 ---- */
div.stButton > button {
    border-radius: 8px; border: 1px solid rgba(255,255,255,0.12);
    background: rgba(255,255,255,0.04); color: #e2e2e7; font-weight: 600; letter-spacing: 0.03em;
    transition: transform 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease;
}
div.stButton > button:hover {
    border-color: rgba(255,79,79,0.55); color: #fff;
    box-shadow: 0 6px 18px rgba(0,0,0,0.4); transform: translateY(-1px);
}

/* ---- 指標卡片：緊湊 ---- */
[data-testid="stMetric"] {
    background: linear-gradient(150deg, rgba(255,255,255,0.06), rgba(255,255,255,0.015));
    border: 1px solid rgba(255,255,255,0.09); border-radius: 12px;
    padding: 8px 10px !important; box-shadow: 0 10px 24px rgba(0,0,0,0.4); backdrop-filter: blur(6px);
}
[data-testid="stMetricLabel"] { color: #a1a1aa; font-weight: 600; letter-spacing: 0.06em; font-size: 0.7rem !important; }
[data-testid="stMetricValue"] { color: #fafafa; font-weight: 800; font-size: 1.25rem !important; }
[data-testid="stMetricDelta"] { font-weight: 700; font-size: 0.75rem !important; }

/* ---- 表格 ---- */
[data-testid="stDataFrame"] { border: 1px solid rgba(255,255,255,0.08); border-radius: 10px; overflow: hidden; }

/* ---- 側邊欄持股清單（按鈕式，取代 canvas 選取）---- */
.nav-h {
    display: inline-block; margin: 6px 0 4px 0; background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.10); border-left: 3px solid #ff4d4f;
    border-radius: 8px; padding: 4px 10px; color: #b9b9c2; font-size: 0.72rem;
    font-weight: 700; letter-spacing: 0.14em;
}
/* 清單按鈕：左對齊、緊湊、代號等寬對齊 */
section[data-testid="stSidebar"] div[data-testid="stButton"] > button {
    text-align: left !important; justify-content: flex-start !important;
    padding: 5px 10px !important; font-size: 0.82rem !important;
    border-radius: 8px !important; letter-spacing: 0.01em;
}
section[data-testid="stSidebar"] div[data-testid="stVerticalBlock"] > div[data-testid="stElementContainer"] {
    margin-bottom: -6px;   /* 清單項目間距壓緊，視覺上貼近表格 */
}

/* ---- 表單：緊湊 ---- */
[data-testid="stForm"] {
    background: linear-gradient(150deg, rgba(255,255,255,0.05), rgba(255,255,255,0.015));
    border: 1px solid rgba(255,255,255,0.09); border-radius: 12px; padding: 10px 12px;
    box-shadow: 0 10px 24px rgba(0,0,0,0.4);
}
div.stFormSubmitButton > button {
    background: linear-gradient(145deg, #ff5f57, #d93a34); border: none; border-radius: 10px;
    color: #fff; font-weight: 700; letter-spacing: 0.05em;
}
div.stFormSubmitButton > button:hover { box-shadow: 0 8px 20px rgba(229,72,77,0.35); transform: translateY(-1px); }
.stNumberInput input, .stTextInput input, .stDateInput input { font-size: 0.82rem !important; }
.stNumberInput label, .stTextInput label, .stDateInput label, .stRadio label { font-size: 0.78rem !important; }

/* ---- Numbers 風格標題 ---- */
.numbers-title {
    display: inline-block; margin: 2px 0 4px 0; background: rgba(255,255,255,0.05);
    border: 1px solid rgba(255,255,255,0.10); border-left: 3px solid #ff4d4f;
    border-radius: 8px; padding: 4px 10px; color: #b9b9c2; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.14em;
}

/* ============ 保留 Streamlit 原生圖示（不自行繪製任何假按鈕） ============
   左上角收放側邊欄的雙箭頭、expander 的箭頭，一律使用 Streamlit 原生元件與
   原生 Material Symbols 字型。此處不做 font-size: 0 隱藏，也不掛 ::after
   偽元素畫替代符號——過去那樣做只會遮蔽／取代掉原生圖示。

   唯一需要保證的是「不擋到原生按鈕」：
     - stHeader 為 position:fixed（高約 60px），內部收放鈕靠它自身的 stacking
       context 浮在最上層；這裡不設定任何 position / z-index / pointer-events，
       也就不會蓋住或攔截滑鼠事件。
     - 主容器僅保留 Streamlit 預設的頂部留白（見上方 stMainBlockContainer），
       確保內容不會往上鑽進工具列、壓到左上角按鈕。
   另外補上 expander 標題不被切邊的保護（純排版，不影響圖示）。 */
[data-testid="stExpander"] details > summary {
    overflow: visible !important;
    white-space: normal !important;
    line-height: 1.5 !important;
}
</style>
"""
st.markdown(CSS_COMPACT, unsafe_allow_html=True)

STYLER_BASE = [
    {"selector": "table", "props": [("background-color", "transparent"), ("color", "inherit")]},
    {"selector": "th", "props": [("background-color", "rgba(255,255,255,0.04)"), ("color", "#b9b9c2"),
                                 ("border-bottom", "1px solid rgba(255,255,255,0.12)"), ("font-weight", "600")]},
    {"selector": "td", "props": [("border-bottom", "1px solid rgba(255,255,255,0.06)")]},
    {"selector": "tbody tr:last-child td", "props": [("border-top", "2px solid rgba(255,255,255,0.25)"), ("font-weight", "700")]},
]

MARKET_PILLS = [
    ("加權指數", "22,350.12 ▲0.85%", GAIN),
    ("台指期近月", "22,385 ▲0.72%", GAIN),
    ("美元／台幣", "31.285 ▼0.12%", LOSS),
    ("櫃買指數", "248.67 ▼0.35%", LOSS),
]


def tint(values, color_of):
    """依規則回傳每一格的文字顏色（Styler 用）。"""
    return [f"color: {color_of(v)}; font-weight: 600;" for v in values]


def _ledger_row_style(tx_types):
    """一條龍對帳單依交易類型為整列文字上色。"""
    BUY_STYLE = "color:#00E676; font-weight:600;"
    SELL_STYLE = "color:#FF4B4B; font-weight:600;"
    DIV_STYLE = "color:#FFFFFF;"

    def style_fn(df):
        out = pd.DataFrame("", index=df.index, columns=df.columns)
        summary_columns = {
            "交易日期", "剩餘成本", "庫存股數", "打平成本預估單價", "預留即時現價",
        }
        for i, tt in enumerate(tx_types):
            if i >= len(out):
                break
            if tt == "買進":
                out.iloc[i] = BUY_STYLE
            elif tt == "賣出":
                out.iloc[i] = SELL_STYLE
            elif tt == "結轉":
                out.iloc[i] = DIV_STYLE
                for column in out.columns:
                    if column not in summary_columns and column not in ("刪除", "_trade_id"):
                        out.loc[out.index[i], column] = "color:#07070a;"
            else:
                out.iloc[i] = DIV_STYLE
        return out

    return style_fn



# ================================================================== 初始化與 Session 狀態
seed_demo_trades()  # 首次啟動寫入示範 Mock 交易（之後持久化於 stock.db）

if "page" not in st.session_state:
    st.session_state.page = "home"  # "home" | "stats" | 股票代號
if "current_selected_stock" not in st.session_state:
    st.session_state.current_selected_stock = None  # 目前檢視的個股代號（None = 首頁／總表）
if "sidebar_codes" not in st.session_state:
    st.session_state.sidebar_codes = []  # 由 DB 持股自動帶入（於下方同步）
if "prices" not in st.session_state:
    st.session_state.prices = {}  # 現價快取（TWSE 刷新時更新）
if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = "尚未刷新"
if "price_src" not in st.session_state:
    st.session_state.price_src = {}  # 代號 -> 「即時」/「昨收」

# 由 SQLite 重放持股（每次 rerun 都重算，確保持久化資料即時反映）
HOLDINGS = replay_holdings()
_all_codes = [c for c, h in HOLDINGS.items() if h["股數"] > 0] + \
             [c for c, h in HOLDINGS.items() if h["股數"] <= 0]

# 同步側邊欄清單：保留既有順序，移除已無持股且無紀錄者，追加 DB 新出現者
_existing = st.session_state.sidebar_codes
_new_order = [c for c in _existing if c in HOLDINGS]
_new_order += [c for c in _all_codes if c not in _new_order]
st.session_state.sidebar_codes = _new_order
codes = st.session_state.sidebar_codes


def get_stock(code: str):
    """取得個股重放結果（以 SQLite 為準）。"""
    return HOLDINGS.get(code)


# 常見股號名稱備援：記帳時「股票名稱」欄留空或誤填代號時使用（連線刷新後由 TWSE 官方名稱校正）
COMMON_NAMES = {
    "2330": "台積電", "2317": "鴻海", "2454": "聯發科", "2303": "聯電",
    "2412": "中華電", "2881": "富邦金", "2882": "國泰金", "1301": "台塑",
    "1303": "南亞", "1326": "台化", "2002": "中鋼", "2603": "長榮",
    "2609": "陽明", "2615": "萬海", "2610": "華航", "2891": "中信金",
    "1216": "統一", "2207": "和泰車", "2464": "盟立", "6127": "九豪",
    "2308": "台達電", "2357": "華碩", "2382": "廣達", "3231": "緯創",
    "3008": "大立光", "2886": "元大金", "2884": "玉山金", "2892": "第一金",
    "3711": "ASE 日月光", "2379": "瑞昱", "3034": "聯詠", "6669": "緯穎",
}


def stock_company_name(code: str) -> str:
    info = HOLDINGS.get(code) or {}
    name = (info.get("名稱") or code).strip()
    # 名欄留空／誤填代號時，回退到常見股號靜態對照（連線刷新 TWSE 後會被官方名稱校正）
    if not name or name == code:
        name = COMMON_NAMES.get(code, code)
    return name


_REVERSE_NAMES = {name: code for code, name in COMMON_NAMES.items()}


def _sync_code_to_name():
    code = st.session_state.get("manual_stock_code", "").strip()
    if not code:
        st.session_state["manual_stock_name"] = ""
        return
    name = stock_company_name(code)
    # 查無此股時，stock_company_name 會回傳代號本身；避免將代號誤寫為公司名稱（如 2320 2320）
    if name == code:
        name = ""
    st.session_state["manual_stock_name"] = name


def _sync_name_to_code():
    name = st.session_state.get("manual_stock_name", "").strip()
    code = _REVERSE_NAMES.get(name, "") if name else ""
    st.session_state["manual_stock_code"] = code


def stock_label(code: str) -> str:
    return f"{code} {stock_company_name(code)}"


def current_price(code: str) -> float:
    """現價：優先 TWSE 快取；否則用最近一筆買進單價當 fallback。"""
    if code in st.session_state.prices:
        return st.session_state.prices[code]
    h = HOLDINGS.get(code)
    if h and h["交易"]:
        buys = [t["成交單價"] for t in h["交易"] if t["交易類型"] == "買進" and t["成交單價"] > 0]
        if buys:
            return buys[-1]
    return 0.0


# ================================================================== 導覽 / 互動
def go_home():
    st.session_state.page = "home"


def go_stats():
    st.session_state.page = "stats"


def pick_stock(code: str):
    """側邊欄持股清單點擊：切換到該檔流水帳。

    使用 st.button 的 on_click 回呼，而非 st.dataframe(on_select="rerun")。原因（已實測驗證）：
    st.dataframe 的列選取在 Streamlit 1.64 由 glide-data-grid 以 <canvas> 繪製，
    選取事件繫結在 canvas 上；實測確認在一般自動化／無頭瀏覽器環境中
    點擊 canvas 不會觸發 on_select（連完全沒有自訂 CSS 的極簡 app 行為也相同），
    造成「點了左側清單但右側畫面完全不動」。改用 st.button 後點擊 100% 可靠。
    """
    st.session_state.page = code
    st.session_state.current_selected_stock = code


def move_stock(idx: int, delta: int):
    """側邊欄持股清單即時置換：將第 idx 檔與前一檔（delta=-1）／後一檔（delta=+1）對調。

    邊界（第一檔 🔼、最後一檔 🔽）由按鈕 disabled 擋掉，這裡再用 0 <= j < len 雙重防呆。
    """
    cs = st.session_state.sidebar_codes
    j = idx + delta
    if 0 <= j < len(cs):
        cs[idx], cs[j] = cs[j], cs[idx]
        st.rerun()


def refresh_prices():
    """「🔄 刷新即時股價」：串接 TWSE MIS 官方 API 取得零延遲撮合價。"""
    updated = 0
    for code in st.session_state.sidebar_codes:
        price, name, src = fetch_twse_price(code)
        if price is not None:
            st.session_state.prices[code] = round(price, 2)
            st.session_state.price_src[code] = src or "即時"
            if name:
                h = HOLDINGS.get(code)
                if h:
                    h["名稱"] = name  # 用官方名稱校正
            updated += 1
        else:
            st.session_state.price_src[code] = "無法連線"
    st.session_state.last_refresh = datetime.now().strftime("%H:%M:%S")
    st.session_state.refresh_count = updated


def holding_row(code: str):
    """計算單檔持股市值與損益（以重放後的股數/均價 + 現價）。"""
    h = get_stock(code)
    if h is None or h["股數"] <= 0:
        return None
    price = current_price(code)
    shares = h["股數"]
    cost = h["均價"] * shares
    market = shares * price
    pnl = market - cost
    return {
        "代號": code, "名稱": h["名稱"], "股數": shares, "均價": h["均價"], "現價": price,
        "市值": market, "損益金額": pnl, "損益比例": (pnl / cost if cost else 0.0),
        "已實現損益": h["已實現損益"], "總成本": h["總成本"],
    }


# ---------------------------------------------------------------- 橫向一條龍對帳單（trade_ledger）
# 單一橫向寬表格欄位順序，100% 對齊 Numbers 截圖（image_UzsKQ2.png）：
#   基礎｜買進區(綠)｜賣出區(紅)｜除權息區(白)｜庫存打成本區(粉/灰)｜策略備註區(灰)
LEDGER_COLUMNS = [
    "交易日期",
    # 買進區
    "購買單價", "購買股數", "購買金額", "手續費", "購買成本",
    # 賣出區
    "賣出單價", "賣出股數", "賣出金額", "證券商手續費", "政府證券交易稅", "實收金額",
    # 除權息區
    "股息發放", "配股股數", "20K以上二代健保2.11%", "(匯費/手續費)", "實際領到",
    # 庫存打成本區
    "剩餘成本", "庫存股數", "打平成本預估單價", "帳上收益", "預留即時現價", "投資報酬率",
    # 策略備註區
    "Why buy", "停損價位", "Reason price change", "Why sold",
]
LEDGER_BUY_COLS = ["購買單價", "購買股數", "購買金額", "手續費", "購買成本"]
LEDGER_SELL_COLS = ["賣出單價", "賣出股數", "賣出金額", "證券商手續費", "政府證券交易稅", "實收金額"]
LEDGER_DIV_COLS = ["股息發放", "配股股數", "20K以上二代健保2.11%", "(匯費/手續費)", "實際領到"]
LEDGER_INV_COLS = ["剩餘成本", "庫存股數", "打平成本預估單價", "帳上收益", "預留即時現價", "投資報酬率"]
LEDGER_NOTE_COLS = ["Why buy", "停損價位", "Reason price change", "Why sold"]
def build_trade_ledger(code: str):
    """把單一股票的所有交易展開成「橫向一條龍對帳單」（list[dict]，對齊 LEDGER_COLUMNS）。

    核心會計（國泰 28 折真實計費）：
      - 買進：手續費＝成交金額×0.001425×0.28（無條件捨去、最低 1 元）；購買成本＝購買金額＋手續費；
              其餘賣出／除權息欄位補 0。
      - 賣出：證券商手續費（最低 1 元）、政府證交稅＝賣出金額×0.003（無條件捨去）；
              實收金額＝賣出金額－手續費－證交稅。
      - 配息：股息總額 ≥ 20,000 才扣 2.11% 二代健保（四捨五入）；(匯費/手續費) 固定扣 5 元。
      - 庫存打成本區：依歷次買賣／配股（配股單價 0、股數照加）加權平均滾動攤銷，逐列帶出
              剩餘成本、庫存股數、打平成本預估單價；並以 TWSE 即時價算帳上收益與投資報酬率。
    回傳 (ledger_rows, latest_state)。
    """
    trades = [t for t in load_trades() if t["stock_code"] == code]
    price = current_price(code)  # 預留即時現價（TWSE 快取 / fallback）

    running_cost = 0.0     # 剩餘成本（加權平均滾動）
    running_shares = 0     # 庫存股數
    rows = []
    for t in trades:
        tt = t["tx_type"]
        sh = int(t["shares"])
        up = float(t["unit_price"])
        gross = float(t["gross"])

        r = {c: None for c in LEDGER_COLUMNS}       # 預設 None（畫面呈現乾淨空白）
        r["交易日期"] = t["trade_date"]
        r["預留即時現價"] = round(price, 2)
        r["_trade_id"] = t["id"]
        r["_unit_price"] = up
        r["_shares"] = sh

        if tt == "買進":
            buy_amount = gross
            fee = buy_fee(buy_amount)                # ×0.001425×0.28、無條件捨去、最低 1 元
            buy_cost = buy_amount + fee
            running_cost += buy_cost                 # 加權平均滾動攤銷
            running_shares += sh
            r["購買單價"], r["購買股數"] = up, sh
            r["購買金額"], r["手續費"], r["購買成本"] = buy_amount, fee, buy_cost

        elif tt == "賣出":
            sell_amount = gross
            s_fee = sell_fee(sell_amount)            # 證券商手續費（最低 1 元）
            tax = sell_tax(sell_amount)              # ×0.003、無條件捨去
            net_received = sell_amount - s_fee - tax # 實收金額
            avg = (running_cost / running_shares) if running_shares > 0 else 0.0
            cost_out = avg * sh                      # 依均價等比例釋出成本
            running_cost -= cost_out
            running_shares -= sh
            r["賣出單價"], r["賣出股數"] = up, sh
            r["賣出金額"], r["證券商手續費"] = sell_amount, s_fee
            r["政府證券交易稅"], r["實收金額"] = tax, net_received

        elif tt == "配息":
            supp = supp_health(gross)                # 股息 ≥ 20,000 才扣 2.11%（四捨五入）
            remit = REMITTANCE_FEE                   # 固定 5 元跨行匯費
            net_div = gross - supp - remit           # 實際領到
            r["股息發放"] = gross
            r["20K以上二代健保2.11%"] = supp
            r["(匯費/手續費)"] = remit
            r["實際領到"] = net_div
            # 配息不影響股數與成本

        elif tt == "配股":
            running_shares += sh                     # 配股：股數照加、單價 0 → 均價自動攤薄
            r["配股股數"] = sh

        # 庫存打成本區（逐列帶出滾動後狀態）
        avg_cost = (running_cost / running_shares) if running_shares > 0 else 0.0
        r["剩餘成本"] = running_cost
        r["庫存股數"] = running_shares
        r["打平成本預估單價"] = round(avg_cost, 2)
        r["帳上收益"] = (price - avg_cost) * running_shares if running_shares > 0 else 0.0
        r["投資報酬率"] = ((price - avg_cost) / avg_cost) if (running_shares > 0 and avg_cost > 0) else 0.0

        # 策略備註區
        r["Why buy"] = t.get("why_buy") or ""
        r["停損價位"] = round(float(t.get("stop_loss") or 0.0), 2)
        r["Reason price change"] = t.get("reason_change") or ""
        r["Why sold"] = t.get("why_sold") or ""
        r["_tx_type"] = tt  # 供色彩分流（不顯示、不匯出）

        rows.append(r)

    latest = {
        "剩餘成本": running_cost,
        "庫存股數": running_shares,
        "打平成本預估單價": round((running_cost / running_shares) if running_shares > 0 else 0.0, 2),
        "現價": price,
    }
    return rows, latest



def show_dataframe(styler_or_df, column_config=None, caption=None):
    """所有表格一律走 st.dataframe（保留 Streamlit 內建複製功能，可貼到 Mac Numbers）。"""
    st.dataframe(styler_or_df, width="stretch", hide_index=True, column_config=column_config)
    if caption:
        st.caption(caption)


# ---------------------------------------------------------------- 側邊欄選取 → 連動右側
# 頁面狀態以 st.session_state.page 為唯一真相（個股頁 = 股票代號）。
# current_selected_stock 僅為「目前是否在個股頁」的鏡像，給樣式／邏輯判斷用。
if st.session_state.page in ("home", "stats"):
    st.session_state.current_selected_stock = None
else:
    st.session_state.current_selected_stock = st.session_state.page

# ---------------------------------------------------------------- 頂部標題列
# 左：唯一大標題；右：「🔄 刷新即時股價」（TWSE 即時）。明暗主題由 Streamlit 原生選單控制。
head_left, head_right = st.columns([4, 1], vertical_alignment="center")
with head_left:
    st.markdown('<div class="app-title">📈 台股智慧對帳損益監控系統</div>', unsafe_allow_html=True)
with head_right:
    st.button(
        "🔄 刷新即時股價", key="btn_refresh", width="stretch", on_click=refresh_prices,
        help=f"串接 TWSE 官方 MIS 即時撮合價（零延遲）。上次更新：{st.session_state.last_refresh}",
    )
st.caption(
    f"TWSE 即時行情上次更新：{st.session_state.last_refresh}｜台股配色：紅＝賺／漲，綠＝賠／跌"
)


# ================================================================== 頁面路由
if st.session_state.page == "home":
    # ------------------------------------------------------------ 首頁
    st.header("📈 首頁總覽")
    st.caption("國泰證券計費標準 · 數據持久化於 stock.db · 中央為手動記帳表單")

    # 大盤指數裝飾列（st.html 輸出，確保膠囊標籤正確渲染）
    pills_html = "".join(
        f"""<span style="background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
            border-radius: 999px; padding: 5px 13px; color: #a1a1aa; font-size: 0.8rem;
            margin: 0 8px 6px 0; display: inline-block;">{name} <b style="color: {color};">{val}</b></span>"""
        for name, val, color in MARKET_PILLS
    )
    st.html(f'<div style="margin-bottom: 6px;">{pills_html}</div>')

    # 三張大數據卡片
    rows = [r for c in codes if (r := holding_row(c)) is not None]
    total_cost = sum(r["總成本"] for r in rows)
    total_market = sum(r["市值"] for r in rows)
    total_pnl = total_market - total_cost
    total_realized = sum(r["已實現損益"] for r in rows)
    pnl_pct = total_pnl / total_cost if total_cost else 0.0

    col_invested, col_market, col_pnl = st.columns(3, gap="large")
    with col_invested:
        st.metric("累計投入資金", value=f"NT$ {total_cost:,.0f}", border=True,
                  help="歷次買進總成本合計（成交金額 + 手續費）")
    with col_market:
        st.metric("目前庫存總市值", value=f"NT$ {total_market:,.0f}", border=True,
                  help="各檔股數 × TWSE 即時價")
    with col_pnl:
        st.metric("預估未實現損益", value=f"NT$ {total_pnl:+,.0f}", delta=f"{pnl_pct * 100:+.2f}%",
                  delta_color="inverse", border=True, help="紅＝賺錢、綠＝賠錢（台股配色）")

    # 已實現損益 + 外匯總損益（若為 0 也顯示，保持三卡之上多一列提示）
    st.markdown(
        f'<div style="font-size:0.78rem;color:#a1a1aa;margin:2px 0 6px 0;">'
        f'已實現損益：<b style="color:{GAIN if total_realized >= 0 else LOSS};">NT$ {total_realized:+,.0f}</b>'
        f'　（賣出結算 + 除權息現金流）</div>',
        unsafe_allow_html=True,
    )


    # -------------------- 手動記帳表單（國泰計費 · 三大卡片下方 · expander 預設展開） --------------------
    st.divider()
    with st.expander("🖊️ 手動新增股票交易 / 除權息紀錄表單", expanded=True):
        with st.container():
            f_row1 = st.columns(3)
            with f_row1[0]:
                trade_date = st.date_input("交易日期", value=date.today())
            with f_row1[1]:
                stock_code = st.text_input(
                    "股票代號",
                    key="manual_stock_code",
                    placeholder="例如：2330",
                    on_change=_sync_code_to_name,
                )
            with f_row1[2]:
                stock_name = st.text_input(
                    "股票名稱",
                    key="manual_stock_name",
                    placeholder="例如：台積電",
                    on_change=_sync_name_to_code,
                )
            f_row2 = st.columns(4)
            with f_row2[0]:
                tx_type = st.radio("交易類型", ("買進", "賣出", "配息", "配股"), horizontal=True)
            with f_row2[1]:
                unit_price = st.number_input("成交單價 / 每股配息（元）", min_value=0.0,
                                             value=0.0, step=1.0, format="%.0f",
                                             help="買賣＝成交單價；配息＝每股配息；配股＝0（不影響單價）")
            with f_row2[2]:
                shares = st.number_input("成交股數 / 配息股數（股）", min_value=0, value=0,
                                         step=1000, format="%d")
            with f_row2[3]:
                tail_adjust = st.number_input("手動尾數微調（元）", min_value=-999999,
                                              max_value=999999, value=0, step=1, format="%d",
                                              help="直接加減最終淨現金流，對齊國泰 App 的 1-2 元微幅出入")
            # ---- 4 大交易策略備註（與稅費交割款一同寫入 trade_ledger / SQLite）----
            st.markdown('<div class="nav-h">交易策略備註</div>', unsafe_allow_html=True)
            s_row1 = st.columns(2)
            with s_row1[0]:
                why_buy = st.text_input("Why buy（進場理由）", key="f_why_buy",
                                        placeholder="例如：月線翻多、法說會利多…")
            with s_row1[1]:
                stop_loss = st.number_input("停損價位", min_value=0.0, value=0.0, step=0.05,
                                            format="%.2f", key="f_stop_loss",
                                            help="跌破此價位即出場；0 表示未設定")
            s_row2 = st.columns(2)
            with s_row2[0]:
                reason_change = st.text_input("Reason price change（價格變動原因）", key="f_reason_change",
                                              placeholder="例如：大盤回調、同業獲利下修…")
            with s_row2[1]:
                why_sold = st.text_input("Why sold（出場理由）", key="f_why_sold",
                                         placeholder="例如：達目標價、觸及停損…")
            submitted = st.button("提交紀錄", type="primary", width="stretch")

        if submitted:
            code = stock_code.strip()
            if not code:
                st.error("請輸入股票代號")
            elif shares <= 0:
                st.error("股數需大於 0")
            elif tx_type in ("買進", "賣出") and unit_price <= 0:
                st.error("成交單價需大於 0")
            elif tx_type == "配息" and unit_price <= 0:
                st.error("每股配息需大於 0")
            else:
                res = record_trade(
                    trade_date, code,
                    stock_name.strip() or stock_company_name(code) or code,
                    tx_type, unit_price, shares, tail_adjust, "手動記帳",
                    per_share_div=unit_price,
                    why_buy=why_buy, stop_loss=stop_loss,
                    reason_change=reason_change, why_sold=why_sold,
                )
                st.rerun()  # 重放持股、刷新左側清單與右側一條龍對帳單
        st.caption("國泰計費：手續費 0.1425%×0.28（無條件捨去、最低 1 元）、證交稅 0.3%、"
                   "補充保費 2.11%（單筆現金股利 ≥ 20,000 才扣）、匯費 5 元。資料寫入 stock.db 永久保存。")



elif st.session_state.page == "stats":
    # ------------------------------------------------------------ 簡易統計總表
    st.title("📊 簡易統計總表")
    st.caption("仿 Numbers 樣式：含標題列、內文列與最後的加總列。現價為 TWSE 即時/昨收。")

    rows = [r for c in codes if (r := holding_row(c)) is not None]
    if rows:
        total_cost = sum(r["總成本"] for r in rows)
        total_market = sum(r["市值"] for r in rows)
        total_pnl = total_market - total_cost
        total_row = {
            "代號": "", "名稱": "總計", "股數": sum(r["股數"] for r in rows),
            "均價": float("nan"), "現價": float("nan"), "市值": total_market,
            "損益金額": total_pnl, "損益比例": (total_pnl / total_cost if total_cost else 0.0),
        }
        stats_df = pd.DataFrame(rows + [total_row])

        def _fmt_stat_number(v, signed=False, force_int=False):
            if pd.isna(v):
                return "—"
            if v == 0:
                return "0"
            if force_int or float(v).is_integer():
                return f"{int(v):,d}"
            text = f"{v:+,.2f}" if signed else f"{v:,.2f}"
            return text.rstrip("0").rstrip(".")

        def _fmt_stat_ratio(v):
            if pd.isna(v):
                return "—"
            if v == 0:
                return "0"
            return f"{v * 100:+.2f}%"

        stats_styler = (
            stats_df.style
            .set_table_styles(STYLER_BASE)
            .apply(tint, subset=["損益金額", "損益比例"], color_of=lambda v: GAIN if v >= 0 else LOSS)
            .format({
                "股數": lambda v: _fmt_stat_number(v, force_int=True),
                "均價": _fmt_stat_number,
                "現價": _fmt_stat_number,
                "市值": _fmt_stat_number,
                "損益金額": lambda v: _fmt_stat_number(v, signed=True),
                "損益比例": _fmt_stat_ratio,
                "已實現損益": lambda v: _fmt_stat_number(v, signed=True),
                "總成本": _fmt_stat_number,
            }, na_rep="—")
        )
        show_dataframe(
            stats_styler,
            column_config={
                "代號": st.column_config.TextColumn("代號"),
                "名稱": st.column_config.TextColumn("名稱"),
                "股數": st.column_config.TextColumn("股數"),
                "均價": st.column_config.TextColumn("均價（NT$）"),
                "現價": st.column_config.TextColumn("現價（NT$）"),
                "市值": st.column_config.TextColumn("市值（NT$）"),
                "損益金額": st.column_config.TextColumn("損益金額（NT$）"),
                "損益比例": st.column_config.TextColumn("損益比例"),
            },
            caption="紅字＝賺錢、綠字＝賠錢（台股配色）。可用表格右上角 ⋯ 複製貼到 Numbers。",
        )
    else:
        st.info("目前尚無持股，請於首頁手動記帳表單新增交易。")

elif get_stock(st.session_state.page) is not None:
    # ------------------------------------------------------------ 個股明細頁
    code = st.session_state.current_selected_stock  # 動態連動：來自側邊欄點選
    info = get_stock(code)
    _cur_idx = st.session_state.sidebar_codes.index(code) if code in st.session_state.sidebar_codes else 0
    _last_idx = max(len(st.session_state.sidebar_codes) - 1, 0)

    # ---------- 頂部標題 + 排序置換工具列 ----------
    _title_col, _up_col, _down_col = st.columns([5, 1.4, 1.4], vertical_alignment="center")
    with _title_col:
        st.title(f"📊 {stock_label(code)}")
    with _up_col:
        st.button("🔼 往上移一格", key=f"move_up_{code}", width="stretch",
                  disabled=_cur_idx == 0, on_click=move_stock, args=(_cur_idx, -1),
                  help="與前一檔對調，左側清單順序即時更新")
    with _down_col:
        st.button("🔽 往下移一格", key=f"move_down_{code}", width="stretch",
                  disabled=_cur_idx == _last_idx, on_click=move_stock, args=(_cur_idx, 1),
                  help="與後一檔對調，左側清單順序即時更新")

    _src = st.session_state.price_src.get(code, "")
    st.caption(f"個股明細 · 數據持久化於 stock.db" + (f" · 現價來源：TWSE {_src}" if _src else ""))

    row = holding_row(code)
    d_cost, d_market, d_pnl = st.columns(3, gap="large")
    with d_cost:
        st.metric("持股均價（打成本）", value=f"NT$ {row['均價']:,.2f}" if row else "—",
                  delta=f"庫存 {row['股數']:,} 股" if row else None, border=True,
                  help="(歷次買進總金額 + 手續費) ÷ 剩餘股數；配股後自動攤薄")
    with d_market:
        st.metric("目前市值", value=f"NT$ {row['市值']:,.0f}" if row else "—",
                  delta=f"現價 {row['現價']:,.2f}" if row else None, border=True,
                  help="股數 × TWSE 即時價")
    with d_pnl:
        if row:
            st.metric("未實現損益", value=f"NT$ {row['損益金額']:+,.0f}", delta=f"{row['損益比例'] * 100:+.2f}%",
                      delta_color="inverse", border=True, help="紅＝賺錢、綠＝賠錢")
        else:
            st.metric("未實現損益", value="—", border=True)

    st.divider()

    # ---------- 橫向一條龍對帳單（單一 st.dataframe，欄位 100% 對齊 Numbers） ----------
    st.subheader("🧮 交易對帳單（橫向一條龍 · trade_ledger）")
    st.caption("買進文字為綠色、賣出文字為紅色、除權息文字為白色。計費為國泰 28 折真實試算；按左上「複製」可貼到 Numbers。")

    ledger_rows, latest = build_trade_ledger(code)
    if ledger_rows:
        trade_ids = []
        trade_sources = {}
        for ledger_row in ledger_rows:
            trade_id = ledger_row.pop("_trade_id")
            trade_ids.append(trade_id)
            trade_sources[trade_id] = {
                "tx_type": ledger_row.pop("_tx_type", "配息"),
                "unit_price": ledger_row.pop("_unit_price"),
                "shares": ledger_row.pop("_shares"),
                "gross": ledger_row.get("股息發放", 0),
            }
        tx_types = [trade_sources[trade_id]["tx_type"] for trade_id in trade_ids]
        ledger_df = pd.DataFrame(ledger_rows, index=trade_ids)[LEDGER_COLUMNS]
        ledger_df["交易日期"] = pd.to_datetime(ledger_df["交易日期"]).dt.strftime("%Y-%m-%d")
        summary_row = {column: 0 for column in LEDGER_COLUMNS}
        summary_row.update({
            "交易日期": "最新結轉",
            "剩餘成本": latest["剩餘成本"],
            "庫存股數": latest["庫存股數"],
            "打平成本預估單價": latest["打平成本預估單價"],
            "預留即時現價": latest["現價"],
        })
        for column in ("Why buy", "Reason price change", "Why sold"):
            summary_row[column] = ""
        summary_df = pd.DataFrame([summary_row], columns=LEDGER_COLUMNS)
        display_df = ledger_df.copy()
        ledger_formats = {
            **{
                column: lambda value: "" if value == 0 else f"{value:,.2f}"
                for column in (
                    "購買單價", "賣出單價", "打平成本預估單價",
                    "預留即時現價", "停損價位",
                )
            },
            "投資報酬率": lambda value: "" if value == 0 else f"{value * 100:+.2f}%",
            "帳上收益": lambda value: "" if value == 0 else f"{value:+,.0f}",
            **{
                column: lambda value: "" if value == 0 else f"{value:,.0f}"
                for column in (
                    "購買股數", "購買金額", "手續費", "購買成本",
                    "賣出股數", "賣出金額", "證券商手續費", "政府證券交易稅", "實收金額",
                    "股息發放", "配股股數", "20K以上二代健保2.11%", "(匯費/手續費)",
                    "實際領到", "剩餘成本", "庫存股數",
                )
            },
        }
        delete_df = ledger_df.copy()
        delete_df.insert(0, "_trade_id", trade_ids)
        delete_df.insert(0, "刪除", False)
        delete_df.reset_index(drop=True, inplace=True)
        delete_styler = (
            delete_df.style
            .set_table_styles(STYLER_BASE)
            .apply(_ledger_row_style(tx_types), axis=None)
            .format(ledger_formats, na_rep="—")
        )
        clipboard_df = pd.concat(
            [ledger_df.astype(object), summary_df.astype(object)], ignore_index=True
        )
        summary_visible_columns = {
            "交易日期", "剩餘成本", "庫存股數", "打平成本預估單價", "預留即時現價",
        }
        for column in LEDGER_COLUMNS:
            if column not in summary_visible_columns:
                clipboard_df.loc[len(clipboard_df) - 1, column] = ""
        clipboard_tsv = json.dumps(
                        clipboard_df.to_csv(sep="\t", index=False, header=True, na_rep="")
                )
        # ---------------- 複製按鈕與快捷鍵（並排） ----------------
        _c0, _c1, _c2, _c3, _c4 = st.columns([1, 1, 1, 1, 1])
        with _c0:
            st.components.v1.html(
                            f"""
                            <div style="display:flex;align-items:center;gap:10px;height:36px;font-family:sans-serif;">
                                <button id="copy-ledger" type="button" style="
                                    padding:5px 12px;border:1px solid rgba(255,255,255,0.16);border-radius:8px;
                                    background:rgba(255,255,255,0.06);color:#e2e2e7;font-size:13px;font-weight:600;
                                    cursor:pointer;">
                                    複製表格
                                </button>
                                <span id="copy-ledger-status" style="color:#a1a1aa;font-size:12px;"></span>
                            </div>
                            <script>
                                const copyButton = document.getElementById("copy-ledger");
                                const copyStatus = document.getElementById("copy-ledger-status");
                                const clipboardText = {clipboard_tsv};
                                copyButton.addEventListener("click", async () => {{
                                    try {{
                                        try {{
                                            await navigator.clipboard.writeText(clipboardText);
                                        }} catch (error) {{
                                            const textArea = document.createElement("textarea");
                                            textArea.value = clipboardText;
                                            textArea.style.position = "fixed";
                                            textArea.style.left = "-9999px";
                                            document.body.appendChild(textArea);
                                            textArea.select();
                                            const copied = document.execCommand("copy");
                                            textArea.remove();
                                            if (!copied) throw error;
                                        }}
                                        copyStatus.textContent = "已複製，可貼到 Numbers";
                                    }} catch (error) {{
                                        copyStatus.textContent = "複製失敗，請檢查瀏覽器剪貼簿權限";
                                    }}
                                }});
                            </script>
                            """,
                            height=42,
                            scrolling=False,
                    )
        with _c1:
            show_buy = st.toggle("買股資訊", value=True, key=f"toggle_buy_{code}")
        with _c2:
            show_sell = st.toggle("賣股資訊", value=True, key=f"toggle_sell_{code}")
        with _c3:
            show_inv = st.toggle("庫存資訊", value=True, key=f"toggle_inv_{code}")
        with _c4:
            show_note = st.toggle("其他", value=True, key=f"toggle_note_{code}")

        _core_cols = {"交易日期", "購買單價", "購買股數"}
        _visible_set = set(_core_cols)
        if show_buy:
            _visible_set.update(LEDGER_BUY_COLS)
        if show_sell:
            _visible_set.update(LEDGER_SELL_COLS)
        if show_inv:
            _visible_set.update(LEDGER_INV_COLS)
        if show_note:
            _visible_set.update(LEDGER_NOTE_COLS)
        _visible_cols = [c for c in LEDGER_COLUMNS if c in _visible_set]

        # ---------------- 可編輯對帳單 ----------------
        _restore_key = f"ledger_restore_version_{code}"
        _restore_version = st.session_state.get(_restore_key, 0)

        edit_df = ledger_df[_visible_cols].copy()
        edit_df.insert(0, "_trade_id", trade_ids)
        edit_df.insert(0, "刪除", False)
        edit_df.reset_index(drop=True, inplace=True)

        _editable = {
            "交易日期", "購買單價", "購買股數", "手續費",
            "賣出單價", "賣出股數", "證券商手續費", "政府證券交易稅",
            "股息發放", "配股股數", "20K以上二代健保2.11%", "(匯費/手續費)",
            "Why buy", "停損價位", "Reason price change", "Why sold",
        }
        _disabled = ["_trade_id"] + [c for c in _visible_cols if c not in _editable]

        _column_config = {"_trade_id": None, "刪除": st.column_config.CheckboxColumn("刪除")}
        for _c in _visible_cols:
            if _c in {"Why buy", "Reason price change", "Why sold"}:
                _column_config[_c] = st.column_config.TextColumn(_c)
            elif _c == "交易日期":
                _column_config[_c] = st.column_config.TextColumn(_c)
            else:
                _column_config[_c] = st.column_config.NumberColumn(_c)

        # 零值轉空白：非核心欄位的 0/None/NaN 全部轉為空白格子
        # - 數值欄位 (NumberColumn)：轉為 float64，0/None/NaN → float("nan")，渲染為空白
        # - 文字欄位 (TextColumn)：None/NaN/"None" 字串 → ""，渲染為空白
        _zero_blank_cols = [
            c for c in edit_df.columns
            if c not in _core_cols and c not in ("_trade_id", "刪除", "交易日期")
        ]
        for _zc in _zero_blank_cols:
            if _zc in {"Why buy", "Reason price change", "Why sold"}:
                edit_df[_zc] = edit_df[_zc].fillna("").astype(str)
                edit_df[_zc] = edit_df[_zc].replace({"None": "", "nan": ""})
            else:
                edit_df[_zc] = pd.to_numeric(edit_df[_zc], errors="coerce")
                edit_df[_zc] = edit_df[_zc].where(edit_df[_zc] != 0, float("nan"))

        edited_df = st.data_editor(
            edit_df,
            width="stretch",
            hide_index=True,
            column_config=_column_config,
            disabled=_disabled,
            num_rows="fixed",
            key=f"ledger_editor_{code}_{_restore_version}",
        )

        _bs, _br, _bd = st.columns([1.6, 1.6, 2])
        with _bs:
            if st.button("儲存修改", key=f"save_ledger_{code}", type="primary"):
                updates = []
                for _, row in edited_df.iterrows():
                    tid = int(row["_trade_id"])
                    if tid not in trade_sources:
                        continue
                    src = trade_sources[tid]
                    updates.append({
                        "id": tid,
                        "trade_date": row["交易日期"],
                        "unit_price": float(row["購買單價"]) if pd.notna(row.get("購買單價")) else src["unit_price"],
                        "shares": int(float(row["購買股數"])) if pd.notna(row.get("購買股數")) else src["shares"],
                        "stop_loss": float(row["停損價位"]) if pd.notna(row.get("停損價位")) else 0.0,
                        "why_buy": str(row.get("Why buy", "") or ""),
                        "reason_change": str(row.get("Reason price change", "") or ""),
                        "why_sold": str(row.get("Why sold", "") or ""),
                    })
                if updates:
                    update_trades(updates)
                    st.session_state[_restore_key] = _restore_version + 1
                    st.rerun()
        with _br:
            if st.button("回復原始資料", key=f"restore_ledger_{code}"):
                st.session_state[_restore_key] = _restore_version + 1
                st.rerun()
        with _bd:
            selected_delete_ids = [
                int(trade_id)
                for trade_id, selected in zip(edited_df["_trade_id"], edited_df["刪除"])
                if int(trade_id) in trade_sources and not pd.isna(selected) and bool(selected)
            ]
            if st.button(
                "刪除所選交易",
                key=f"delete_trades_{code}",
                disabled=not selected_delete_ids,
                type="secondary",
            ):
                delete_trades(selected_delete_ids)
                st.session_state[_restore_key] = _restore_version + 1
                st.rerun()

        summary_display_df = pd.DataFrame([{
            "項目": "最新結轉",
            "庫存股數": latest["庫存股數"],
            "打平成本預估單價": latest["打平成本預估單價"],
            "預留即時現價": latest["現價"],
            "剩餘成本": latest["剩餘成本"],
        }])
        summary_styler = (
            summary_display_df.style
            .set_table_styles(STYLER_BASE)
            .format({
                "庫存股數": "{:,}",
                "打平成本預估單價": "{:,.2f}",
                "預留即時現價": "{:,.2f}",
                "剩餘成本": "{:,.0f}",
            })
        )
        st.dataframe(
            summary_styler,
            width="stretch",
            hide_index=True,
            column_config={
                "項目": st.column_config.TextColumn("項目"),
                "庫存股數": st.column_config.NumberColumn("庫存股數", format="%d"),
                "打平成本預估單價": st.column_config.NumberColumn("打平成本預估單價 (NT$)", format="%.2f"),
                "預留即時現價": st.column_config.NumberColumn("現價 (NT$)", format="%.2f"),
                "剩餘成本": st.column_config.NumberColumn("剩餘成本 (NT$)", format="%,.0f"),
            },
            height=90,
        )

        with st.expander("修改交易紀錄", expanded=False):
            def _trade_option(trade_id):
                source = trade_sources[trade_id]
                tx_type = source["tx_type"]
                if tx_type == "配息":
                    detail = f"股息 {source['gross']:,.2f}"
                elif tx_type == "配股":
                    detail = f"配股 {source['shares']:,} 股"
                else:
                    detail = f"{source['unit_price']:,.2f} × {source['shares']:,} 股"
                return f"{ledger_df.loc[trade_id, '交易日期']}｜{tx_type}｜{detail}"

            selected_trade_id = st.selectbox(
                "選擇要修改的交易",
                options=trade_ids,
                format_func=_trade_option,
                key=f"edit_trade_select_{code}",
            )
            selected_source = trade_sources[selected_trade_id]
            selected_row = ledger_df.loc[selected_trade_id]
            selected_type = selected_source["tx_type"]

            with st.form(f"edit_trade_form_{code}_{selected_trade_id}"):
                edited_date = st.date_input(
                    "交易日期",
                    value=date.fromisoformat(selected_row["交易日期"]),
                    key=f"edit_date_{code}_{selected_trade_id}",
                )
                edit_price = float(selected_source["unit_price"])
                edit_shares = int(selected_source["shares"])
                edit_dividend = float(selected_source["gross"]) if selected_source["gross"] is not None else 0.0
                if selected_type == "買進":
                    price_col, shares_col = st.columns(2)
                    with price_col:
                        edit_price = st.number_input(
                            "購買單價", min_value=0.01, value=edit_price, step=0.01,
                            format="%.2f", key=f"edit_buy_price_{code}_{selected_trade_id}",
                        )
                    with shares_col:
                        edit_shares = st.number_input(
                            "購買股數", min_value=1, value=edit_shares, step=1,
                            key=f"edit_buy_shares_{code}_{selected_trade_id}",
                        )
                elif selected_type == "賣出":
                    price_col, shares_col = st.columns(2)
                    with price_col:
                        edit_price = st.number_input(
                            "賣出單價", min_value=0.01, value=edit_price, step=0.01,
                            format="%.2f", key=f"edit_sell_price_{code}_{selected_trade_id}",
                        )
                    with shares_col:
                        edit_shares = st.number_input(
                            "賣出股數", min_value=1, value=edit_shares, step=1,
                            key=f"edit_sell_shares_{code}_{selected_trade_id}",
                        )
                elif selected_type == "配息":
                    edit_dividend = st.number_input(
                        "股息發放", min_value=0.0, value=edit_dividend, step=1.0,
                        format="%.2f", key=f"edit_dividend_{code}_{selected_trade_id}",
                    )
                elif selected_type == "配股":
                    edit_shares = st.number_input(
                        "配股股數", min_value=1, value=edit_shares, step=1,
                        key=f"edit_bonus_shares_{code}_{selected_trade_id}",
                    )

                note_col, reason_col = st.columns(2)
                with note_col:
                    why_buy = st.text_input(
                        "Why buy", value=str(selected_row["Why buy"] or ""),
                        key=f"edit_why_buy_{code}_{selected_trade_id}",
                    )
                    reason_change = st.text_input(
                        "Reason price change", value=str(selected_row["Reason price change"] or ""),
                        key=f"edit_reason_change_{code}_{selected_trade_id}",
                    )
                with reason_col:
                    stop_loss = st.number_input(
                        "停損價位", min_value=0.0, value=float(selected_row["停損價位"]),
                        step=0.05, format="%.2f", key=f"edit_stop_loss_{code}_{selected_trade_id}",
                    )
                    why_sold = st.text_input(
                        "Why sold", value=str(selected_row["Why sold"] or ""),
                        key=f"edit_why_sold_{code}_{selected_trade_id}",
                    )
                save_edit = st.form_submit_button("儲存修改並重算", type="primary", width="stretch")

            if save_edit:
                if selected_type == "配息":
                    edit_price = edit_dividend / edit_shares if edit_shares else 0.0
                try:
                    update_trades([{
                        "id": selected_trade_id,
                        "trade_date": edited_date,
                        "unit_price": edit_price,
                        "shares": edit_shares,
                        "why_buy": why_buy,
                        "stop_loss": stop_loss,
                        "reason_change": reason_change,
                        "why_sold": why_sold,
                    }])
                    st.rerun()
                except (TypeError, ValueError) as error:
                    st.error(str(error))

    else:
        st.info("尚無交易紀錄，請於首頁手動記帳表單新增。")


else:
    # ---------- 防呆：頁面狀態異常時回首頁 ----------
    st.session_state.page = "home"
    st.rerun()


# ---------------------------------------------------------------- 左側導覽列（側邊欄）
# 放在腳本末端：表單提交後同一次 rerun 即會出現新加入的股票
with st.sidebar:
    st.markdown('<div class="numbers-title">股票總覽</div>', unsafe_allow_html=True)
    st.button("🏠 首頁", key="nav_home", width="stretch",
              type="primary" if st.session_state.page == "home" else "secondary", on_click=go_home)
    st.button("📊 總表（簡易統計總表）", key="nav_stats", width="stretch",
              type="primary" if st.session_state.page == "stats" else "secondary", on_click=go_stats)
    st.divider()
    # ---- 持股清單：用 st.button 清單（見下方說明為何不用 st.dataframe 選取）----
    st.markdown('<div class="nav-h">持股清單</div>', unsafe_allow_html=True)
    st.caption("點擊任一檔即可切換至該公司流水帳。")

    _name_counts = {}
    for _c in st.session_state.sidebar_codes:
        _name = stock_company_name(_c)
        _name_counts[_name] = _name_counts.get(_name, 0) + 1

    for _i, _c in enumerate(st.session_state.sidebar_codes):
        _active = st.session_state.page == _c
        _name = stock_company_name(_c)
        if _name_counts[_name] > 1:
            _name = "名稱待確認"
        st.button(
            f"{_c}  {_name}",
            key=f"nav_pick_{_c}",
            width="stretch",
            type="primary" if _active else "secondary",
            on_click=pick_stock,
            args=(_c,),
        )

    st.caption("上下排序可在個股頁右上角的【🔼 往上移一格 / 🔽 往下移一格】調整。")

    # ---- 管理個股清單：刪除 / 修改代號與名稱 ----
    st.divider()
    _sm_key = "sidebar_manage_mode"
    if _sm_key not in st.session_state:
        st.session_state[_sm_key] = False

    if st.button(
        "🔧 管理個股清單" if not st.session_state[_sm_key] else "✖ 關閉管理",
        key="sidebar_manage_toggle",
        width="stretch",
    ):
        st.session_state[_sm_key] = not st.session_state[_sm_key]
        st.rerun()

    if st.session_state[_sm_key]:
        st.caption("修改代號/名稱後按「儲存修改」同步至 stock.db；勾選刪除會移除該股所有交易紀錄。")
        _sm_edit = {}
        _sm_del = {}
        for _si, _sc in enumerate(list(st.session_state.sidebar_codes)):
            _s1, _s2, _s3 = st.columns([2, 3, 1])
            with _s1:
                _nc = st.text_input("代號", value=_sc, key=f"sm_code_{_si}", label_visibility="collapsed")
            with _s2:
                _nn = st.text_input("名稱", value=stock_company_name(_sc), key=f"sm_name_{_si}", label_visibility="collapsed")
            with _s3:
                _dl = st.checkbox("刪除", key=f"sm_del_{_si}")
            _sm_edit[_sc] = (_nc.strip(), _nn.strip())
            _sm_del[_sc] = bool(_dl)

        _sc1, _sc2 = st.columns(2)
        with _sc1:
            if st.button("💾 儲存修改", key="sm_save_btn", type="primary", width="stretch"):
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for _old_c, (_new_c, _new_name) in _sm_edit.items():
                        if _sm_del.get(_old_c):
                            conn.execute("DELETE FROM trades WHERE stock_code = ?", (_old_c,))
                            if _old_c in st.session_state.sidebar_codes:
                                st.session_state.sidebar_codes.remove(_old_c)
                        elif _new_c != _old_c or _new_name != stock_company_name(_old_c):
                            conn.execute(
                                "UPDATE trades SET stock_code = ?, stock_name = ? WHERE stock_code = ?",
                                (_new_c, _new_name or _new_c, _old_c),
                            )
                            if _old_c in st.session_state.sidebar_codes:
                                _idx = st.session_state.sidebar_codes.index(_old_c)
                                st.session_state.sidebar_codes[_idx] = _new_c
                    conn.commit()
                    st.session_state[_sm_key] = False
                    st.rerun()
                except Exception:
                    conn.rollback()
                    raise
        with _sc2:
            if st.button("↩ 回復原始資料", key="sm_restore_btn", width="stretch"):
                st.session_state[_sm_key] = False
                st.rerun()

    st.divider()
    st.caption("數據持久化於 stock.db（SQLite）。表格右上角 ⋯ 可複製貼到 Numbers。")

    