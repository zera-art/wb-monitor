"""
Отправка согласованных изменений цен на Wildberries.
Читает лист «ОЧЕРЕДЬ ИЗМЕНЕНИЙ», отправляет одобренные позиции через WB API.
"""

import logging
import time
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

SHEET_NAME = "📋 ОЧЕРЕДЬ ИЗМЕНЕНИЙ"
WB_PRICES_URL = "https://discounts-prices-api.wildberries.ru/api/v2/upload/task"


def get_approved_changes(sheets_writer) -> list[dict]:
    """
    Читает лист ОЧЕРЕДЬ ИЗМЕНЕНИЙ.
    Возвращает строки где Согласовано=TRUE и Отправлено пустое.
    """
    Q = sheets_writer  # alias for column constants
    rows = sheets_writer._get_queue_rows()
    if len(rows) < 2:
        return []

    result = []
    for i, row in enumerate(rows[1:], start=2):   # i = 1-based sheet row
        if not row or not row[0].strip():
            continue
        if len(row) <= Q._Q_SENT:
            row = row + [""] * (Q._Q_SENT + 1 - len(row))

        approved = row[Q._Q_APPROVE].strip().upper() in ("TRUE", "ИСТИНА", "1")
        sent = row[Q._Q_SENT].strip()
        if not approved or sent:
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
            "row_index": i,
            "nm_id":     nm_id,
            "name":      row[Q._Q_NAME],
            "current_price": current_price,
            "new_price": new_price,
            "reason":    row[Q._Q_REASON],
        })

    return result


def send_prices_to_wb(approved_items: list[dict],
                      sheets_writer,
                      prices_key: str) -> dict:
    """
    Отправляет одобренные изменения цен через WB API.
    POST /api/v2/upload/task — меняет только базовую цену, скидку не трогает.
    После успеха записывает дату в колонку «Отправлено».
    При ошибке пишет «ОШИБКА: {текст}».
    Возвращает {n_up, n_down, total}.
    """
    if not approved_items:
        return {"n_up": 0, "n_down": 0, "total": 0}

    try:
        ws = sheets_writer._get_sheet(SHEET_NAME)
    except Exception as e:
        logger.error(f"Не удалось открыть лист очереди: {e}")
        return {"n_up": 0, "n_down": 0, "total": 0}

    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    n_up = n_down = 0

    for item in approved_items:
        nm_id     = item["nm_id"]
        new_price = item["new_price"]
        row_idx   = item["row_index"]
        cur_price = item["current_price"]

        payload = {"data": [{"nmID": nm_id, "price": new_price}]}
        try:
            resp = requests.post(
                WB_PRICES_URL,
                headers={
                    "Authorization": prices_key,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            ws.update_cell(row_idx, sheets_writer._Q_SENT + 1, now_str)
            if new_price > cur_price:
                n_up += 1
            else:
                n_down += 1
            logger.info(f"✅ Цена отправлена: nmID={nm_id}, "
                        f"{cur_price:.0f} → {new_price} руб")
        except Exception as e:
            err_msg = f"ОШИБКА: {str(e)[:80]}"
            try:
                ws.update_cell(row_idx, sheets_writer._Q_SENT + 1, err_msg)
            except Exception:
                pass
            logger.error(f"✗ Ошибка отправки nmID={nm_id}: {e}")

        time.sleep(0.5)   # не превышать rate limit WB API

    total = n_up + n_down
    logger.info(f"Отправка завершена: {total} SKU (↑{n_up} повышений, ↓{n_down} снижений)")
    return {"n_up": n_up, "n_down": n_down, "total": total}
