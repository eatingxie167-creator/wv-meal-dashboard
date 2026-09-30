#!/usr/bin/env python3
"""
產生 google_daily.csv —— 儀表板的 Google 廣告資料來源。

為什麼不直接用 Google 試算表的 wv_daily：
  wv_daily 那份匯出報表綁的是「轉換次數 / 轉換價值」口徑，
  但「購買」這個轉換動作在 SEM品牌字 與 Demand Gen 沒有被設為廣告活動層級
  轉換目標，所以那兩條的購買數與購買金額在該口徑下恆為 0。
  改用「所有轉換 / 所有轉換價值」才拿得到完整數字（同時也含瀏覽後轉換）。

欄位刻意與 wv_daily 完全一致，儀表板的 transformHeader 對照表不用改；
差別只在背後綁的指標換成 all_conversions / all_conversions_value。

用法：
    cd <repo>
    python3 build_google_csv.py          # 產生 google_daily.csv
    python3 build_google_csv.py --check  # 只印統計、不寫檔

憑證讀取上層目錄的 .env（GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET /
GOOGLE_REFRESH_TOKEN / GOOGLE_DEVELOPER_TOKEN），不會寫進這個 repo。
"""
import csv
import datetime
import json
import os
import subprocess
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(os.path.dirname(HERE), ".env")
OUT_PATH = os.path.join(HERE, "google_daily.csv")

CUSTOMER_ID = "9438435772"
LOGIN_CUSTOMER_ID = "1909930701"
API_VERSION = "v22"
# 本波起始日。wv_daily 從 6/01 一路累積，但這支只輸出本波，
# 免得上一波（兒童月捐）的花費混進 ROAS 分母。
START_DATE = "2026-09-22"
# ⚠ GAQL 的 segments.date 不接受單邊條件，只寫 >= 會靜默回傳 0 筆，
#   必須給完整區間。結束日取今天（API 端沒有未來資料，多給無妨）。
END_DATE = datetime.date.today().isoformat()

# 轉換動作 -> CSV 欄位前綴。順序即欄位順序，與 wv_daily 一致。
ACTIONS = ["加入購物車", "聯絡人", "註冊", "購買", "開始結帳"]

HEADER = (
    ["Campaign Name", "Day", "Impressions", "Clicks", "Cost"]
    + ACTIONS
    + [f"{a} value" for a in ACTIONS]
)


def load_env():
    env = {}
    with open(ENV_PATH) as fh:
        for line in fh:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k] = v.strip().strip('"').strip("'")
    missing = [
        k
        for k in (
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "GOOGLE_REFRESH_TOKEN",
            "GOOGLE_DEVELOPER_TOKEN",
        )
        if k not in env
    ]
    if missing:
        sys.exit(f"[錯誤] .env 缺少：{', '.join(missing)}")
    return env


def access_token(env):
    out = subprocess.run(
        [
            "curl", "-sS", "https://oauth2.googleapis.com/token",
            "-d", f"client_id={env['GOOGLE_CLIENT_ID']}",
            "-d", f"client_secret={env['GOOGLE_CLIENT_SECRET']}",
            "-d", f"refresh_token={env['GOOGLE_REFRESH_TOKEN']}",
            "-d", "grant_type=refresh_token",
        ],
        capture_output=True, text=True,
    ).stdout
    try:
        return json.loads(out)["access_token"]
    except Exception:
        sys.exit(f"[錯誤] 取得 access token 失敗：{out[:400]}")


def make_query(env, token):
    headers = [
        f"Authorization: Bearer {token}",
        f"developer-token: {env['GOOGLE_DEVELOPER_TOKEN']}",
        "Content-Type: application/json",
        f"login-customer-id: {LOGIN_CUSTOMER_ID}",
    ]
    url = (
        f"https://googleads.googleapis.com/{API_VERSION}"
        f"/customers/{CUSTOMER_ID}/googleAds:searchStream"
    )

    def query(gaql):
        args = ["curl", "-sS", url]
        for h in headers:
            args += ["-H", h]
        args += ["-d", json.dumps({"query": gaql})]
        out = subprocess.run(args, capture_output=True, text=True).stdout
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            sys.exit(f"[錯誤] 回應不是 JSON：{out[:400]}")
        # ⚠ searchStream 成功時回傳「陣列」，但失敗時錯誤可能是
        #   {"error": ...} 或 [{"error": ...}] 兩種形狀。只檢查 dict 會把
        #   錯誤當成空結果吞掉，導致 CSV 少資料卻不報錯。兩種都要擋。
        chunks = payload if isinstance(payload, list) else [payload]
        for chunk in chunks:
            if isinstance(chunk, dict) and "error" in chunk:
                sys.exit(f"[錯誤] API：{json.dumps(chunk['error'], ensure_ascii=False)[:600]}")
        rows = []
        for chunk in chunks:
            rows += chunk.get("results", [])
        if not rows:
            sys.exit("[錯誤] 查詢回傳 0 筆，請確認日期區間與帳號權限：\n  " + gaql[:200])
        return rows

    return query


def build(query):
    # key = (廣告活動名稱, 日期)
    table = defaultdict(lambda: {"imp": 0, "clk": 0, "cost": 0.0,
                                 "conv": defaultdict(float), "val": defaultdict(float)})

    for r in query(
        "SELECT campaign.name, segments.date, metrics.impressions, "
        "metrics.clicks, metrics.cost_micros FROM campaign "
        f"WHERE segments.date BETWEEN '{START_DATE}' AND '{END_DATE}' "
        "AND metrics.impressions > 0"
    ):
        row = table[(r["campaign"]["name"], r["segments"]["date"])]
        m = r["metrics"]
        row["imp"] += int(m.get("impressions", 0))
        row["clk"] += int(m.get("clicks", 0))
        row["cost"] += int(m.get("costMicros", 0)) / 1e6

    # 注意：segments.conversion_action_name 不支援放在 WHERE 裡篩選
    # （會靜默回傳 0 筆），一律全撈回來在這裡過濾。
    for r in query(
        "SELECT campaign.name, segments.date, segments.conversion_action_name, "
        "metrics.all_conversions, metrics.all_conversions_value FROM campaign "
        f"WHERE segments.date BETWEEN '{START_DATE}' AND '{END_DATE}'"
    ):
        name = r["segments"].get("conversionActionName", "")
        if name not in ACTIONS:
            continue
        row = table[(r["campaign"]["name"], r["segments"]["date"])]
        m = r["metrics"]
        row["conv"][name] += float(m.get("allConversions", 0))
        row["val"][name] += float(m.get("allConversionsValue", 0))

    records = []
    for (camp, day) in sorted(table):
        row = table[(camp, day)]
        records.append(
            [camp, day, row["imp"], row["clk"], f"{row['cost']:.6f}"]
            + [f"{row['conv'][a]:g}" for a in ACTIONS]
            + [f"{row['val'][a]:g}" for a in ACTIONS]
        )
    return records


def summarise(records):
    idx = {name: HEADER.index(name) for name in HEADER}
    tot = defaultdict(float)
    for rec in records:
        tot["Cost"] += float(rec[idx["Cost"]])
        tot["Clicks"] += float(rec[idx["Clicks"]])
        for a in ACTIONS:
            tot[a] += float(rec[idx[a]])
            tot[a + " value"] += float(rec[idx[a + " value"]])
    print(f"  期間 {START_DATE} ~ {END_DATE}，共 {len(records)} 列")
    print(f"  花費        {tot['Cost']:>12,.0f}")
    print(f"  點擊        {tot['Clicks']:>12,.0f}")
    print(f"  加入購物車   {tot['加入購物車']:>12,.2f}   價值 {tot['加入購物車 value']:>12,.0f}")
    print(f"  購買        {tot['購買']:>12,.2f}   價值 {tot['購買 value']:>12,.0f}")


def main():
    env = load_env()
    query = make_query(env, access_token(env))
    records = build(query)
    if not records:
        sys.exit("[錯誤] 沒有撈到任何資料")
    print("Google Ads 所有轉換口徑：")
    summarise(records)
    if "--check" in sys.argv:
        print("\n(--check 模式，未寫檔)")
        return
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(records)
    print(f"\n已寫入 {OUT_PATH}")


if __name__ == "__main__":
    main()
