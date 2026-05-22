"""
Экспорт одобренных изменений цен в шаблон Excel WB.
Читает лист «ОЧЕРЕДЬ ИЗМЕНЕНИЙ» (Согласовано=TRUE, Отправлено пусто),
создаёт файл для загрузки вручную через кабинет WB.
"""

import logging
import os
from datetime import datetime

import openpyxl

logger = logging.getLogger(__name__)

SHEET_NAME = "📋 ОЧЕРЕДЬ ИЗМЕНЕНИЙ"
EXPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")

WB_TEMPLATE_SHEET = "Отчет - цены и скидки на товары"
WB_TEMPLATE_HEADERS = [
    "Бренд",
    "Категория",
    "Артикул WB",
    "Артикул продавца",
    "Последний баркод",
    "Остатки WB",
    "Остатки продавца",
    "Оборачиваемость",
    "Текущая цена",
    "Новая цена, RUB",
    "Текущая скидка",
    "Новая скидка",
    "Цена со скидкой",
    "Наличие ошибки",
]
_COL_NM_ID    = 2   # "Артикул WB"     (0-based)
_COL_NEW_PRICE = 9  # "Новая цена, RUB" (0-based)


def get_approved_changes(sheets_writer) -> list[dict]:
    """
    Читает лист ОЧЕРЕДЬ ИЗМЕНЕНИЙ.
    Возвращает строки где Согласовано=TRUE и Отправлено пустое.
    """
    Q = sheets_writer
    rows = sheets_writer._get_queue_rows()
    if len(rows) < 2:
        return []

    result = []
    for i, row in enumerate(rows[1:], start=2):
        if not row or not row[0].strip():
            continue
        if len(row) <= Q._Q_SENT:
            row = row + [""] * (Q._Q_SENT + 1 - len(row))

        approved = row[Q._Q_APPROVE].strip().upper() in ("TRUE", "ИСТИНА", "1")
        sent = row[Q._Q_SENT].strip()
        # Пропускаем только успешно отправленные (дата или "Файл: ...")
        # Строки с "ОШИБКА:" включаем — требуют повторной обработки
        if not approved:
            continue
        if sent and not sent.startswith("ОШИБКА"):
            continue

        try:
            nm_id = int(row[Q._Q_NM_ID])
        except (ValueError, IndexError):
            continue

        raw_new = str(row[Q._Q_NEW]).replace("руб (min)", "").replace("руб", "").strip()
        try:
            new_price = int(float(raw_new))
        except (ValueError, IndexError):
            logger.warning(f"Не удалось разобрать новую цену в строке {i}: {row[Q._Q_NEW]!r}")
            continue

        try:
            current_price = float(str(row[Q._Q_CUR]).strip() or 0)
        except ValueError:
            current_price = 0.0

        result.append({
            "row_index":     i,
            "nm_id":         nm_id,
            "name":          row[Q._Q_NAME],
            "current_price": current_price,
            "new_price":     new_price,
            "reason":        row[Q._Q_REASON],
        })

    return result


def _col_letter(n: int) -> str:
    """1-based column index → буква (1→A, 10→J, ...)."""
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def _batch_update_sent_col(ws, sent_col_1based: int,
                           updates: list[tuple[int, str]]) -> None:
    """Записывает колонку «Отправлено» за один batch-запрос."""
    if not updates:
        return
    data = [
        {"range": f"{_col_letter(sent_col_1based)}{row}", "values": [[val]]}
        for row, val in updates
    ]
    ws.batch_update(data, value_input_option="RAW")


def export_prices_to_wb_template(sheets_writer) -> dict:
    """
    Создаёт Excel-файл в формате шаблона WB для загрузки цен.
    Заполняет только «Артикул WB» и «Новая цена, RUB» —
    остальные поля WB подтягивает сам при загрузке.

    Возвращает {"filename": str, "total": int, "path": str}
    или {"filename": None, "total": 0, "path": None} если нечего экспортировать.
    """
    approved = get_approved_changes(sheets_writer)
    if not approved:
        logger.info("Нет одобренных позиций для экспорта")
        return {"filename": None, "total": 0, "path": None}

    # Создаём Excel
    wb_excel = openpyxl.Workbook()
    ws_excel = wb_excel.active
    ws_excel.title = WB_TEMPLATE_SHEET
    ws_excel.append(WB_TEMPLATE_HEADERS)

    for item in approved:
        row = [""] * len(WB_TEMPLATE_HEADERS)
        row[_COL_NM_ID]     = item["nm_id"]
        row[_COL_NEW_PRICE] = item["new_price"]
        ws_excel.append(row)

    # Сохраняем файл
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename  = f"wb_prices_{timestamp}.xlsx"
    filepath  = os.path.join(EXPORTS_DIR, filename)
    wb_excel.save(filepath)
    logger.info(f"Excel сохранён: {filepath} ({len(approved)} позиций)")

    # Отмечаем в Google Sheets одним batch-запросом
    try:
        ws_queue = sheets_writer._get_sheet(SHEET_NAME)
        updates = [(item["row_index"], f"Файл: {filename}") for item in approved]
        _batch_update_sent_col(ws_queue, sheets_writer._Q_SENT + 1, updates)
        logger.info(f"Колонка «Отправлено» обновлена для {len(updates)} строк")
    except Exception as e:
        logger.error(f"Не удалось обновить колонку «Отправлено»: {e}")

    # Открываем папку в проводнике Windows
    try:
        os.startfile(EXPORTS_DIR)
    except Exception as e:
        logger.warning(f"Не удалось открыть папку: {e}")

    return {"filename": filename, "total": len(approved), "path": filepath}
