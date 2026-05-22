"""
Исправляет Excel-файл: если новая цена < базовая_цена/2 — заменяет на базовая_цена/2.
Базовые цены (до скидки) берёт из листа «📦 ВСЕ SKU» в Google Sheets:
  col 0 = nmID, col 9 = Цена (со скидкой), col 10 = Скидка %
  base_price = final_price / (1 - discount/100)
"""
import os, sys, logging
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(
                        open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False))])
logger = logging.getLogger(__name__)

import openpyxl
from sheets_writer import SheetsWriter

XLSX = r"c:\Users\user\Downloads\wb_monitor\exports\wb_prices_20260522_163534.xlsx"

# -- Шаг 1: загружаем базовые цены из листа «ВСЕ SKU» -----------------
sheets = SheetsWriter("google_credentials.json", os.getenv("SPREADSHEET_ID"))
ws_all = sheets._get_sheet("📦 ВСЕ SKU")
all_rows = ws_all.get_all_values()

# SHEET_HEADERS начинается с 3-й строки (индекс 2), данные с 4-й (индекс 3)
base_prices: dict[int, float] = {}
for row in all_rows[3:]:
    if not row or not row[0].strip():
        continue
    try:
        nm_id        = int(row[0])
        final_price  = float(str(row[9]).replace(",", ".").strip() or 0)
        discount_pct = float(str(row[10]).replace(",", ".").strip() or 0)
    except (ValueError, IndexError):
        continue
    if final_price <= 0:
        continue
    # Базовая цена до скидки
    factor = 1 - discount_pct / 100
    base_price = final_price / factor if factor > 0 else final_price
    base_prices[nm_id] = base_price

logger.info(f"Загружено {len(base_prices)} базовых цен из Google Sheets")

# -- Шаг 2: открываем Excel и исправляем ---------------------------
wb    = openpyxl.load_workbook(XLSX)
ws    = wb.active    # "Отчет - цены и скидки на товары"

COL_NM_ID     = 3   # "Артикул WB"      (1-based)
COL_NEW_PRICE = 10  # "Новая цена, RUB" (1-based)

fixed = 0
skipped_no_price = 0

for row in ws.iter_rows(min_row=2):
    nm_cell  = row[COL_NM_ID - 1]
    new_cell = row[COL_NEW_PRICE - 1]

    if nm_cell.value is None:
        continue

    try:
        nm_id     = int(nm_cell.value)
        new_price = float(str(new_cell.value).split()[0]) if new_cell.value else None
    except (ValueError, TypeError):
        continue

    if new_price is None:
        continue

    base = base_prices.get(nm_id)
    if base is None:
        skipped_no_price += 1
        continue

    wb_min = base / 2
    if new_price < wb_min:
        fixed_price = int(round(wb_min / 10) * 10)
        logger.info(f"  nmID={nm_id}: {new_price:.0f} → {fixed_price} (min {wb_min:.0f}, база {base:.0f})")
        new_cell.value = fixed_price
        fixed += 1

wb.save(XLSX)
logger.info(f"Исправлено: {fixed} строк | Без цены в Sheets: {skipped_no_price}")
logger.info(f"Файл сохранён: {XLSX}")

import os as _os
_os.startfile(r"c:\Users\user\Downloads\wb_monitor\exports")
