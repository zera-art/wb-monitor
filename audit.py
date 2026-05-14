"""
Аудит качества данных: сравнивает 5 случайных SKU в Google Sheets
с прямыми запросами к WB API и перепроверяет все формулы расчётов.
"""
import os, sys, random, json
from datetime import datetime, timedelta
from collections import defaultdict

from dotenv import load_dotenv
load_dotenv()

# UTF-8 stdout для кириллицы и спецсимволов
sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)

import requests
import gspread
from google.oauth2.service_account import Credentials
from config import (
    WB_KEYS, GOOGLE_CREDENTIALS_PATH, SPREADSHEET_ID,
    SALES_LOOKBACK_DAYS,
)

# -- helpers ------------------------------------------------------------------

def d(n=0):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")

def get(url, key, params=None):
    import time
    for attempt in range(6):
        r = requests.get(url,
            headers={"Authorization": key, "Content-Type": "application/json"},
            params=params, timeout=60)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 65))
            print(f"  rate limit, ждём {wait}с…")
            time.sleep(wait)
            continue
        if r.status_code == 204:
            return []
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Не удалось получить {url} после 6 попыток")

def post(url, key, body):
    r = requests.post(url,
        headers={"Authorization": key, "Content-Type": "application/json"},
        json=body, timeout=60)
    if r.status_code == 204:
        return []
    r.raise_for_status()
    return r.json()

STATS_KEY     = WB_KEYS["stats_key"]
PRICES_KEY    = WB_KEYS["prices_key"]

BASE_STATS  = "https://statistics-api.wildberries.ru"
BASE_PRICES = "https://discounts-prices-api.wildberries.ru"

OK   = "✅"
FAIL = "❌"
WARN = "⚠️ "

# -- 1. Читаем лист «ВСЕ SKU» -------------------------------------------------

print("Подключаемся к Google Sheets…")
creds = Credentials.from_service_account_file(
    GOOGLE_CREDENTIALS_PATH,
    scopes=["https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"])
gc = gspread.authorize(creds)
ss = gc.open_by_key(SPREADSHEET_ID)
ws = ss.worksheet("📦 ВСЕ SKU")

all_rows = ws.get_all_values()
# Row 0=title, Row 1=empty, Row 2=headers, Row 3+=data
header_row = all_rows[2]
data_rows  = all_rows[3:]

COL = {h: i for i, h in enumerate(header_row)}

# Индексы колонок по SHEET_HEADERS
C_NMID    = COL.get("nmID", 0)
C_NAME    = COL.get("Название", 2)
C_STOCK   = COL.get("Остаток", 4)
C_S7D     = COL.get("Продажи 7д", 5)
C_S28D    = COL.get("Продажи 28д", 6)
C_AVG     = COL.get("Ср. прод/нед", 7)
C_TURN    = COL.get("Оборачив. (дней)", 8)
C_DELTA   = COL.get("Δ к прошлой нед.", 9)
C_PRICE   = COL.get("Цена итог.", 10)
C_DISC    = COL.get("Скидка %", 11)
C_STOR    = COL.get("Хранение 7д, ₽", 12)
C_FORECAST= COL.get("Прогноз распродажи", 16)
C_STATUS  = COL.get("Статус", 17)

sheets_by_nm = {}
for row in data_rows:
    try:
        nm = int(row[C_NMID])
        sheets_by_nm[nm] = row
    except (ValueError, IndexError):
        pass

all_nm_ids = list(sheets_by_nm.keys())
print(f"Найдено {len(all_nm_ids)} SKU в Sheets.")

# -- 2. Выбираем 5 случайных nmID ---------------------------------------------

random.seed(42)
sample = random.sample(all_nm_ids, min(5, len(all_nm_ids)))
print(f"\nВыбраны nmID для аудита: {sample}\n")

# -- 3. Загружаем данные из WB API ---------------------------------------------

print("Загружаем остатки (dateFrom=2010-01-01)…")
stocks_raw = get(f"{BASE_STATS}/api/v1/supplier/stocks", STATS_KEY,
                 {"dateFrom": "2010-01-01"})

print("Загружаем продажи текущей недели…")
sales_cur_raw = get(f"{BASE_STATS}/api/v1/supplier/sales", STATS_KEY,
                    {"dateFrom": d(7), "dateTo": d(0), "flag": 1})

print("Загружаем продажи прошлой недели…")
sales_prv_raw = get(f"{BASE_STATS}/api/v1/supplier/sales", STATS_KEY,
                    {"dateFrom": d(14), "dateTo": d(7), "flag": 1})

print(f"Загружаем детальный отчёт ({SALES_LOOKBACK_DAYS}д)…")
report_raw = get(f"{BASE_STATS}/api/v5/supplier/reportDetailByPeriod", STATS_KEY,
                 {"dateFrom": d(SALES_LOOKBACK_DAYS), "dateTo": d(0),
                  "rrdid": 0, "limit": 100_000})

print("Загружаем цены…")
prices_raw_all = []
offset = 0
while True:
    chunk = get(f"{BASE_PRICES}/api/v2/list/goods/filter", PRICES_KEY,
                {"limit": 1000, "offset": offset})
    goods = chunk.get("data", {}).get("listGoods", []) if isinstance(chunk, dict) else []
    if not goods:
        break
    prices_raw_all.extend(goods)
    if len(goods) < 1000:
        break
    offset += 1000

print("Данные загружены.\n")

# -- 4. Агрегируем по nmID -----------------------------------------------------

# Остатки
stock_by_nm = defaultdict(int)
for row in stocks_raw:
    nm = row.get("nmId")
    if nm:
        stock_by_nm[nm] += int(row.get("quantity", 0) or 0)

# Продажи
def agg_sales(rows):
    by_nm = defaultdict(lambda: {"qty": 0, "rev": 0.0})
    for row in rows:
        nm = row.get("nmId")
        if not nm:
            continue
        price = float(row.get("finishedPrice", 0) or row.get("priceWithDisc", 0) or 0)
        by_nm[nm]["qty"] += 1
        by_nm[nm]["rev"] += price
    return by_nm

sales_cur = agg_sales(sales_cur_raw)
sales_prv = agg_sales(sales_prv_raw)

# Продажи 28д из отчёта
sales_28d_by_nm = defaultdict(float)
storage_by_nm   = defaultdict(float)
doc_types_seen   = defaultdict(set)    # для диагностики

for row in report_raw:
    nm = row.get("nm_id")
    if not nm:
        continue
    doc = row.get("doc_type_name", "")
    qty = float(row.get("quantity", 0) or 0)
    storage = float(row.get("storage_fee", 0) or 0)
    penalty = float(row.get("penalty", 0) or 0)
    doc_types_seen[nm].add(doc)
    storage_by_nm[nm] += storage + penalty
    if doc in ("Продажа", "Продажа (возврат продавца)"):
        sales_28d_by_nm[nm] += qty

# Цены
prices_by_nm = {}
for g in prices_raw_all:
    nm = g.get("nmID")
    if not nm:
        continue
    sizes = g.get("sizes") or []
    base   = sizes[0].get("price", 0) if sizes else 0
    final  = sizes[0].get("discountedPrice", 0) if sizes else 0
    prices_by_nm[nm] = {
        "price":    base,
        "discount": g.get("discount", 0),
        "final":    final,
    }

# -- 5. Аудит каждого SKU -----------------------------------------------------

issues = []   # [(nmid, field, expected, got, severity)]

def num(s, default=0.0):
    try:
        t = str(s).replace(" ", "").replace(",", ".")
        t = t.replace("∞", "999").replace("—", "0").strip("%")
        sign = -1.0 if t.startswith("▼") else 1.0
        return sign * float(t.lstrip("▲▼"))
    except Exception:
        return default

print("=" * 72)
print(f"{'АУДИТ ДАННЫХ':^72}")
print("=" * 72)

for nm_id in sample:
    sh = sheets_by_nm.get(nm_id)
    if not sh:
        print(f"\n[{nm_id}] НЕТ В SHEETS — пропускаем")
        continue

    name = sh[C_NAME][:35]
    print(f"\n{'-'*72}")
    print(f"[{nm_id}] {name}")
    print(f"{'-'*72}")

    rows_ok = []
    rows_fail = []

    def check(field, api_val, sheets_val, tol=0.05, note=""):
        api_n    = round(float(api_val), 2)
        sheets_n = round(num(sheets_val), 2)
        if api_n == 0 and sheets_n == 0:
            mark = OK
        elif api_n == 0:
            rel = abs(sheets_n)
            mark = OK if rel < 1 else FAIL
        else:
            rel = abs(api_n - sheets_n) / abs(api_n)
            mark = OK if rel <= tol else FAIL

        line = (f"  {mark} {field:<28} API={api_n:>10.1f}  "
                f"Sheets={sheets_n:>10.1f}  {note}")
        if mark == OK:
            rows_ok.append(line)
        else:
            rows_fail.append(line)
            issues.append((nm_id, field, api_n, sheets_n, note))
        return api_n, sheets_n

    # --- Остаток ---
    api_stock = stock_by_nm.get(nm_id, 0)
    check("Остаток", api_stock, sh[C_STOCK])

    # --- Продажи 7д ---
    api_s7  = sales_cur.get(nm_id, {}).get("qty", 0)
    check("Продажи 7д", api_s7, sh[C_S7D])

    # --- Продажи 28д ---
    api_s28 = sales_28d_by_nm.get(nm_id, 0)
    s28_sh, _ = check("Продажи 28д", api_s28, sh[C_S28D])

    # --- Ср. прод/нед ---
    api_avg = api_s28 / 4 if api_s28 > 0 else api_s7
    check("Ср. прод/нед", api_avg, sh[C_AVG])

    # --- Оборачиваемость ---
    if api_avg > 0:
        api_turn = (api_stock / api_avg) * 7
    else:
        api_turn = 999 if api_stock > 0 else 0
    # Sheets хранит оборачиваемость как целое (fmt_days округляет),
    # поэтому используем абсолютный допуск ±1 день вместо относительного
    api_n, sh_n = round(float(api_turn), 2), round(num(sh[C_TURN]), 2)
    diff_abs = abs(api_n - sh_n)
    mark = "✅" if diff_abs <= 1.0 else "❌"
    line = (f"  {mark} {'Оборачив. (дней)':<28} API={api_n:>10.1f}  "
            f"Sheets={sh_n:>10.1f}  (допуск ±1д, рел.δ={diff_abs:.1f})")
    (rows_ok if mark == "✅" else rows_fail).append(line)
    if mark == "❌":
        issues.append((nm_id, "Оборачив. (дней)", api_n, sh_n, ""))

    # --- Динамика продаж ---
    api_sp  = sales_prv.get(nm_id, {}).get("qty", 0)
    if api_sp > 0:
        api_delta = ((api_s7 - api_sp) / api_sp) * 100
    else:
        api_delta = 100.0 if api_s7 > 0 else 0.0
    check("Δ продаж %", api_delta, sh[C_DELTA], tol=0.05,
          note=f"cur={api_s7} prev={api_sp}")

    # --- Цена и скидка ---
    p = prices_by_nm.get(nm_id, {})
    check("Цена итог.", p.get("final", 0), sh[C_PRICE])
    check("Скидка %",   p.get("discount", 0), sh[C_DISC])

    # --- Прогноз распродажи ---
    if api_avg > 0 and api_stock > 0:
        days_left = (api_stock / api_avg) * 7
        api_forecast = (datetime.now() + timedelta(days=days_left)).strftime("%d.%m.%Y")
    else:
        api_forecast = "—"
    sh_forecast = sh[C_FORECAST]
    match = OK if api_forecast == sh_forecast else FAIL
    line = (f"  {match} {'Прогноз распродажи':<28} "
            f"API={api_forecast}  Sheets={sh_forecast}")
    if match == OK:
        rows_ok.append(line)
    else:
        rows_fail.append(line)
        issues.append((nm_id, "Прогноз распродажи", api_forecast, sh_forecast, ""))

    # --- Тип документов в отчёте (диагностика) ---
    doc_types = doc_types_seen.get(nm_id, set())
    if "Возврат" in doc_types:
        rows_fail.append(
            f"  {WARN} {'doc_type в отчёте':<28} есть возвраты — "
            f"убедись что вычитаются: {doc_types}"
        )

    for r in rows_fail:
        print(r)
    for r in rows_ok:
        print(r)

# -- 6. Итоговый отчёт --------------------------------------------------------

print(f"\n{'='*72}")
print(f"{'ИТОГОВЫЕ РАСХОЖДЕНИЯ':^72}")
print(f"{'='*72}")

if not issues:
    print("  ✅ Все проверенные метрики совпадают с допуском ≤5%")
else:
    for nm, field, api_v, sh_v, note in issues:
        diff = (api_v - sh_v)
        print(f"  ❌ [{nm}] {field}: API={api_v:.1f} | Sheets={sh_v:.1f} "
              f"| Δ={diff:+.1f}  {note}")

# -- 7. Проверяем расчёт returns в отчёте -------------------------------------

print(f"\n{'-'*72}")
print("Диагностика типов документов в детальном отчёте (sample nmIDs):")
all_doc_types = defaultdict(int)
for row in report_raw:
    doc = row.get("doc_type_name", "UNKNOWN")
    all_doc_types[doc] += 1
for dt, cnt in sorted(all_doc_types.items(), key=lambda x: -x[1]):
    print(f"  {dt:<40} {cnt:>6} строк")
