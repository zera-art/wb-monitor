"""
Google Sheets Writer — professional formatting edition.
"""

import logging
import time
from datetime import datetime

import gspread
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials

from analytics import SKUMetrics, SHEET_HEADERS, build_summary, apply_price_raised_status

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

NUM_COLS = len(SHEET_HEADERS)

# ── Color helpers ─────────────────────────────────────────────────────────────

def _hex(color: str) -> dict:
    h = color.lstrip("#")
    return {"red": int(h[0:2], 16) / 255,
            "green": int(h[2:4], 16) / 255,
            "blue": int(h[4:6], 16) / 255}

WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
HEADER_BG = "#1a2f4a"

GROUP_COLORS = {
    1: "#d32f2f",
    2: "#e65100",
    3: "#1565c0",
}

# Partial keyword → row background color for РЕШЕНИЯ data rows
STATUS_ROW_COLORS = {
    "КРИТИЧНЫЙ":      "#ffebee",
    "МЁРТВЫЙ":        "#fce4ec",
    "НЕЭФФЕКТИВНА":   "#fff3e0",
    "НОРМАЛИЗАЦИЯ":   "#e8f5e9",
    "ПОДНЯТЬ ЦЕНУ":   "#c8e6c9",
    "ЦЕНА ПОДНЯТА":   "#bbdefb",
    "МАСШТАБИРОВАТЬ": "#e3f2fd",
    "МОНИТОРИНГ":     "#fffde7",
}

# Turnover column index in SHEET_HEADERS (0-based)
TURNOVER_COL = SHEET_HEADERS.index("Оборачиваемость")


def _status_color(status: str) -> str | None:
    upper = status.upper()
    for key, color in STATUS_ROW_COLORS.items():
        if key in upper:
            return color
    return None


def _pct_color(pct: float) -> str:
    if pct > 10:
        return "#ffcdd2"
    if pct > 5:
        return "#ffe0b2"
    return "#c8e6c9"


def _rub(n: int | float) -> str:
    return f"{int(n):,}".replace(",", " ")  # неразрывный пробел как разделитель


# ── SheetsWriter ─────────────────────────────────────────────────────────────

class SheetsWriter:
    SHEET_NAMES = [
        "🎯 РЕШЕНИЯ",
        "📊 СВОДКА",
        "📦 ВСЕ SKU",
        "📅 ИСТОРИЯ",
        "⚙️ НАСТРОЙКИ",
        "📋 ПРАВИЛА",
    ]

    def __init__(self, credentials_path: str, spreadsheet_id: str):
        creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.spreadsheet = self.gc.open_by_key(spreadsheet_id)
        self._ensure_sheets()

    def _ensure_sheets(self):
        existing = {ws.title for ws in self.spreadsheet.worksheets()}
        for name in self.SHEET_NAMES:
            if name not in existing:
                self.spreadsheet.add_worksheet(title=name, rows=2000, cols=25)
                logger.info(f"Создан лист: {name}")
                time.sleep(0.5)

    def _get_sheet(self, name: str) -> gspread.Worksheet:
        return self.spreadsheet.worksheet(name)

    # ── Low-level request builders ────────────────────────────────────────────

    def _cell_req(self, sheet_id: int, r1: int, r2: int,
                  c1: int, c2: int, fmt: dict) -> dict:
        return {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": r1, "endRowIndex": r2,
                    "startColumnIndex": c1, "endColumnIndex": c2,
                },
                "cell": {"userEnteredFormat": fmt},
                "fields": "userEnteredFormat",
            }
        }

    def _header_req(self, sheet_id: int, row: int,
                    num_cols: int, bg: str = HEADER_BG) -> dict:
        return self._cell_req(sheet_id, row, row + 1, 0, num_cols, {
            "backgroundColor": _hex(bg),
            "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 10},
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
        })

    def _freeze_req(self, sheet_id: int, rows: int) -> dict:
        return {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": rows},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        }

    def _resize_req(self, sheet_id: int, end_col: int = 25) -> dict:
        return {
            "autoResizeDimensions": {
                "dimensions": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": end_col,
                }
            }
        }

    def _hide_cols_req(self, sheet_id: int, start: int, end: int) -> dict:
        return {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": start,
                    "endIndex": end,
                },
                "properties": {"hiddenByUser": True},
                "fields": "hiddenByUser",
            }
        }

    def _unhide_all_cols_req(self, sheet_id: int, num_cols: int = 25) -> dict:
        return {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": num_cols,
                },
                "properties": {"hiddenByUser": False},
                "fields": "hiddenByUser",
            }
        }

    def _delete_cf_rules(self, sheet_id: int) -> list:
        try:
            meta = self.spreadsheet.fetch_sheet_metadata()
            for s in meta.get("sheets", []):
                if s["properties"]["sheetId"] == sheet_id:
                    n = len(s.get("conditionalFormats", []))
                    return [
                        {"deleteConditionalFormatRule": {
                            "sheetId": sheet_id, "index": 0}}
                        for _ in range(n)
                    ]
        except Exception:
            pass
        return []

    def _delete_banding(self, sheet_id: int) -> list:
        try:
            meta = self.spreadsheet.fetch_sheet_metadata()
            for s in meta.get("sheets", []):
                if s["properties"]["sheetId"] == sheet_id:
                    return [
                        {"deleteBanding": {"bandedRangeId": b["bandedRangeId"]}}
                        for b in s.get("bandedRanges", [])
                    ]
        except Exception:
            pass
        return []

    # ── Public API ────────────────────────────────────────────────────────────

    EXCLUDED_CATEGORIES = {"Комплексные пищевые добавки"}
    EXCLUDED_NAMES = {
        "Цвет бузины черной сушеный",   # ловит "...сушеный 40 гр" и "...сушеный, 40 гр"
        "Лабазник",
        "Корень лопуха",
    }

    def update_all(self, metrics: dict[int, SKUMetrics]):
        # Читаем историю ДО записи нового снапшота — для статуса ЦЕНА ПОДНЯТА
        history = self._read_history_snapshot()
        if history:
            apply_price_raised_status(metrics, history)
            logger.info(f"История: применена к {len(history)} nmID")

        # Фильтруем исключённые категории и товары для всех листов кроме ИСТОРИИ
        display = {nm: m for nm, m in metrics.items()
                   if m.category not in self.EXCLUDED_CATEGORIES
                   and not any(exc in m.name for exc in self.EXCLUDED_NAMES)}
        excluded_count = len(metrics) - len(display)
        if excluded_count:
            logger.info(f"Исключено из дашборда: {excluded_count} SKU "
                        f"(категории: {self.EXCLUDED_CATEGORIES}, "
                        f"товары: {self.EXCLUDED_NAMES})")

        summary = build_summary(display)
        sorted_display = sorted(
            display.values(),
            key=lambda m: (m.priority, -m.storage_cost_7d),
        )
        logger.info(f"Обновляем дашборд: {len(display)} SKU")
        self._update_decisions(sorted_display)
        self._update_summary(summary, sorted_display)
        self._update_all_skus(sorted_display)
        self._setup_rules_sheet()
        # ИСТОРИЯ — все SKU без фильтрации
        history_metrics = sorted(
            metrics.values(), key=lambda m: (m.priority, -m.storage_cost_7d)
        )
        self._save_history_snapshot(build_summary(metrics), history_metrics)
        logger.info("✅ Дашборд обновлён")

    def setup_settings_sheet(self, thresholds: dict):
        ws = self._get_sheet("⚙️ НАСТРОЙКИ")
        ws.clear()
        rows = [
            ["⚙️ НАСТРОЙКИ СИСТЕМЫ"], [""],
            ["Параметр", "Значение", "Описание"],
            ["critical_turnover_days",        thresholds.get("critical_turnover_days", 90),
             "Критичная оборачиваемость (дней)"],
            ["slow_turnover_days",             thresholds.get("slow_turnover_days", 60),
             "Замедленная оборачиваемость (дней)"],
            ["price_raise_turnover_days",      thresholds.get("price_raise_turnover_days", 30),
             "Оборачиваемость для повышения цены"],
            ["clearance_ineffective_discount", thresholds.get("clearance_ineffective_discount", 25),
             "Скидка при неэффективной распродаже (%)"],
            ["ad_drr_too_high",               thresholds.get("ad_drr_too_high", 0.25),
             "ДРР при котором снижать рекламу"],
            ["ad_drr_good",                   thresholds.get("ad_drr_good", 0.10),
             "ДРР при котором масштабировать рекламу"],
        ]
        ws.update("A1", rows)

    # ── Sheet 1: РЕШЕНИЯ ──────────────────────────────────────────────────────

    def _update_decisions(self, metrics: list[SKUMetrics]):
        ws = self._get_sheet("🎯 РЕШЕНИЯ")
        ws.clear()
        sheet_id = ws.id

        p1 = [m for m in metrics if m.priority == 1]
        p2 = [m for m in metrics if m.priority == 2]
        p3 = [m for m in metrics if m.priority == 3]

        title = (f"🎯 РЕШЕНИЯ И ПРИОРИТЕТЫ — "
                 f"обновлено {datetime.now().strftime('%d.%m.%Y %H:%M')}")
        rows = [[title], [""]]
        fmt_reqs: list[dict] = []
        ri = 2  # current 0-based row index

        def add_group(priority: int, label: str, group: list[SKUMetrics]):
            nonlocal ri
            if not group:
                return
            # Group header row
            rows.append([label] + [""] * (NUM_COLS - 1))
            fmt_reqs.append(self._cell_req(sheet_id, ri, ri + 1, 0, NUM_COLS, {
                "backgroundColor": _hex(GROUP_COLORS[priority]),
                "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 11},
            }))
            ri += 1
            # Column headers
            rows.append(SHEET_HEADERS)
            fmt_reqs.append(self._header_req(sheet_id, ri, NUM_COLS))
            ri += 1
            # Data rows
            for m in group:
                rows.append(m.to_row())
                color = _status_color(m.status)
                if color:
                    fmt_reqs.append(self._cell_req(
                        sheet_id, ri, ri + 1, 0, NUM_COLS,
                        {"backgroundColor": _hex(color)},
                    ))
                ri += 1
            rows.append([""])
            ri += 1

        add_group(1, f"🚨 СРОЧНЫЕ ДЕЙСТВИЯ ({len(p1)} SKU)", p1)
        add_group(2, f"⚠️ ТРЕБУЮТ ВНИМАНИЯ ({len(p2)} SKU)", p2)
        add_group(3, f"🔍 МОНИТОРИНГ ({len(p3)} SKU)", p3)

        self._write(ws, rows)
        time.sleep(1)

        self._batch(
            [self._unhide_all_cols_req(sheet_id)]
            + self._delete_cf_rules(sheet_id)
            + fmt_reqs
            + [
                self._freeze_req(sheet_id, 1),
                self._resize_req(sheet_id),
            ]
        )
        logger.info(f"Лист РЕШЕНИЯ: P1={len(p1)}, P2={len(p2)}, P3={len(p3)}")

    # ── Sheet 2: СВОДКА ───────────────────────────────────────────────────────

    def _update_summary(self, summary: dict, metrics: list[SKUMetrics]):
        ws = self._get_sheet("📊 СВОДКА")
        ws.clear()
        sheet_id = ws.id

        # (label, display_value, comment, pct_value_or_None)
        data_rows = [
            ("Всего SKU",                  summary.get("total_skus", 0),
             "Активных позиций", None),
            ("🚨 Требуют срочных действий", summary.get("urgent_count", 0),
             "SKU приоритета 1", None),
        ]

        rows: list[list] = [
            [f"📊 СВОДНАЯ СТАТИСТИКА — {summary.get('updated_at', '')}"],
            [""],
            ["Метрика", "Значение", "Комментарий"],
        ]
        for label, val, comment, _ in data_rows:
            rows.append([label, val, comment])

        rows += [[""], ["РАЗБИВКА ПО СТАТУСАМ", "", ""]]
        status_start = len(rows)
        for status, count in sorted(
                summary.get("status_breakdown", {}).items(), key=lambda x: -x[1]):
            rows.append([status, count, "SKU"])

        rows += [[""], ["ТОП-5 САМЫХ МЕДЛЕННЫХ SKU", "Оборачиваемость", "Статус"]]
        top5 = sorted(metrics, key=lambda m: -m.turnover_days)[:5]
        for m in top5:
            rows.append([
                f"{m.nm_id} {m.name[:30]}",
                f"{m.turnover_days:.0f} дней",
                m.status,
            ])

        self._write(ws, rows)
        time.sleep(1)

        DATA_START = 3   # row index of first data row (0-based)
        fmt_reqs = [
            # Title bold+large
            self._cell_req(sheet_id, 0, 1, 0, 3, {
                "textFormat": {"bold": True, "fontSize": 12},
            }),
            # Column header row
            self._header_req(sheet_id, 2, 3),
            # "Значение" column bold for all data rows
            self._cell_req(sheet_id, DATA_START,
                           DATA_START + len(data_rows), 1, 2,
                           {"textFormat": {"bold": True}}),
            self._freeze_req(sheet_id, 3),
            self._resize_req(sheet_id, 3),
        ]

        # Percentage rows: color the "Значение" cell
        for i, (_, _, _, pct) in enumerate(data_rows):
            if pct is not None:
                fmt_reqs.append(self._cell_req(
                    sheet_id,
                    DATA_START + i, DATA_START + i + 1,
                    1, 2,
                    {
                        "backgroundColor": _hex(_pct_color(pct)),
                        "textFormat": {"bold": True},
                    },
                ))

        self._batch(fmt_reqs)

    # ── Sheet 3: ВСЕ SKU ──────────────────────────────────────────────────────

    def _update_all_skus(self, metrics: list[SKUMetrics]):
        ws = self._get_sheet("📦 ВСЕ SKU")
        ws.clear()
        sheet_id = ws.id

        rows: list[list] = [
            [f"📦 ВСЕ SKU — {datetime.now().strftime('%d.%m.%Y %H:%M')}"],
            [""],
            SHEET_HEADERS,
        ]
        rows.extend(m.to_row() for m in metrics)
        self._write(ws, rows)
        time.sleep(1)

        HEADER_ROW = 2   # 0-based index of SHEET_HEADERS row
        DATA_START  = 3

        reqs = (
            [self._unhide_all_cols_req(sheet_id)]
            + self._delete_banding(sheet_id)
            + self._delete_cf_rules(sheet_id)
            + [
                self._header_req(sheet_id, HEADER_ROW, NUM_COLS),
                # Alternating row banding
                {
                    "addBanding": {
                        "bandedRange": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": DATA_START,
                                "startColumnIndex": 0,
                                "endColumnIndex": NUM_COLS,
                            },
                            "rowProperties": {
                                "firstBandColor":  _hex("#ffffff"),
                                "secondBandColor": _hex("#f8f9fa"),
                            },
                        }
                    }
                },
                # Turnover gradient: green(<30) → amber(30) → red(>90)
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{
                                "sheetId": sheet_id,
                                "startRowIndex": DATA_START,
                                "startColumnIndex": TURNOVER_COL,
                                "endColumnIndex": TURNOVER_COL + 1,
                            }],
                            "gradientRule": {
                                "minpoint": {
                                    "color": _hex("#43a047"),
                                    "type": "NUMBER", "value": "0",
                                },
                                "midpoint": {
                                    "color": _hex("#ffb300"),
                                    "type": "NUMBER", "value": "30",
                                },
                                "maxpoint": {
                                    "color": _hex("#d32f2f"),
                                    "type": "NUMBER", "value": "90",
                                },
                            },
                        },
                        "index": 0,
                    }
                },
                self._freeze_req(sheet_id, DATA_START),   # freeze title+empty+headers
                self._resize_req(sheet_id),
            ]
        )
        self._batch(reqs)

    # ── Sheet 4: ИСТОРИЯ ─────────────────────────────────────────────────────

    def _save_history_snapshot(self, summary: dict, metrics: list[SKUMetrics]):
        ws = self._get_sheet("📅 ИСТОРИЯ")
        existing = ws.get_all_values()

        week_label = datetime.now().strftime("Нед. %d.%m.%Y")
        headers = [
            "Неделя", "nmID", "Арт.", "Название",
            "Остаток", "Продажи 7д", "Оборачив. дней",
            "Δ продаж %", "Цена", "Скидка %",
            "Хранение 7д", "Реклама 7д", "Статус",
        ]
        if not existing:
            ws.append_row(headers)

        new_rows = []
        for m in metrics:
            if m.stock == 0 and m.avg_weekly_sales == 0:
                continue
            new_rows.append([
                week_label, m.nm_id, m.vendor_code, m.name[:40],
                m.stock, round(m.sales_7d, 1), round(m.turnover_days, 0),
                round(m.sales_growth_pct, 1), m.final_price, m.discount,
                round(m.storage_cost_7d, 0), round(m.ad_spend_7d, 0), m.status,
            ])

        for i in range(0, len(new_rows), 500):
            ws.append_rows(new_rows[i:i+500], value_input_option="USER_ENTERED")
            time.sleep(1)

        logger.info(f"История: добавлено {len(new_rows)} строк за {week_label}")

    # ── Sheet: ПРАВИЛА ───────────────────────────────────────────────────────

    def _setup_rules_sheet(self):
        ws = self._get_sheet("📋 ПРАВИЛА")
        ws.clear()
        sheet_id = ws.id

        rows = [
            ["📋 ПРАВИЛА РАБОТЫ СИСТЕМЫ"], [""],

            # Раздел 1
            ["РАЗДЕЛ 1 — СТАТУСЫ И ДЕЙСТВИЯ", "", "", ""],
            ["Статус", "Условие присвоения", "Действие", "Приоритет"],
            ["🔴 КРИТИЧНЫЙ ОСТАТОК",
             "Оборачиваемость > 90 дней",
             "Увеличить скидку на 15% (до 50% макс.) или подключить акцию WB / внешний трафик.",
             "1 — Срочно"],
            ["🔴 МЁРТВЫЙ ОСТАТОК",
             "Остаток > 10 шт, продаж нет 4+ недели, оборачиваемость ≥ 30 дней",
             "Снизить цену до −50% или вывезти на самовыкуп. Проверить карточку, фото, SEO.",
             "1 — Срочно"],
            ["⚠️ РАСПРОДАЖА НЕЭФФЕКТИВНА",
             "Оборачиваемость > 60 дней, скидка > 25%, рост продаж < 10%",
             "Проблема не в цене. Проверить позицию в поиске, CTR карточки, отзывы. Усилить рекламу.",
             "1 — Срочно"],
            ["🟠 ЗАМЕДЛЕННАЯ ОБОРАЧИВАЕМОСТЬ",
             "Оборачиваемость > 60 дней",
             "Снизить цену на 10–15% или усилить трафик. Запустить автокампанию если рекламы нет.",
             "2 — Важно"],
            ["✅ НОРМАЛИЗАЦИЯ",
             "Оборачиваемость ≤ 30 дней, рост продаж > 10%",
             "Продажи растут, остаток нормализуется. Готовиться к плавному повышению цены.",
             "3 — Мониторинг"],
            ["🟢 ПОДНЯТЬ ЦЕНУ",
             "Оборачиваемость ≤ 30 дней, есть остаток",
             "Поднять цену согласно таблице правил (Раздел 2). Тестировать +5% каждые 3 дня.",
             "3 — Мониторинг"],
            ["🔄 ЦЕНА ПОДНЯТА",
             "В прошлом запуске был статус ПОДНЯТЬ ЦЕНУ + текущая цена выросла более чем на 5%",
             "Мониторить спрос. Снимается если цена упала >3% (→ ПОДНЯТЬ ЦЕНУ) или оборачиваемость >21д (→ НОРМАЛИЗАЦИЯ).",
             "3 — Мониторинг"],
            ["🟡 МОНИТОРИНГ",
             "Все остальные случаи",
             "Показатели в норме. Продолжать еженедельный мониторинг.",
             "4 — ОК"],
            [""],

            # Раздел 2
            ["РАЗДЕЛ 2 — ПРАВИЛА ПОДЪЁМА ЦЕНЫ", "", "", ""],
            ["Оборачиваемость (дней)", "Динамика спроса", "Подъём цены", "Примечание"],
            ["< 7 дней",    "любая",   "+28%", "Срочное торможение — товар улетает"],
            ["7–14 дней",   "растёт",  "+20%", "Спрос активный"],
            ["7–14 дней",   "падает",  "+13%", "Осторожно — спрос снижается"],
            ["14–21 день",  "растёт",  "+11%", "Плавная коррекция"],
            ["14–21 день",  "падает",  "+8%",  "Минимальный сигнал"],
            ["21–30 дней",  "любая",   "+7%",  "Профилактическое повышение"],
            ["> 30 дней",   "любая",   "—",    "Не поднимать"],
            [""],

            # Раздел 3
            ["РАЗДЕЛ 3 — ВАЖНО", "", "", ""],
            ["⚠️ Перед подъёмом цены проверяйте Индекс цен WB —", "", "", ""],
            ["если ваша цена станет выше конкурентов, WB может понизить позиции карточки.", "", "", ""],
            ["Цены в колонке «Новая цена» округлены до 10 руб вверх.", "", "", ""],
        ]

        self._write(ws, rows)
        time.sleep(1)

        # Форматирование
        reqs = [self._unhide_all_cols_req(sheet_id)]
        # Заголовок
        reqs.append(self._cell_req(sheet_id, 0, 1, 0, 4, {
            "textFormat": {"bold": True, "fontSize": 13},
        }))
        # Заголовки разделов (строки 2, 13, 21)
        for row_idx in [2, 13, 21]:
            reqs.append(self._cell_req(sheet_id, row_idx, row_idx + 1, 0, 4, {
                "backgroundColor": _hex(HEADER_BG),
                "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 11},
            }))
        # Заголовки колонок разделов 1 и 2
        for row_idx in [3, 14]:
            reqs.append(self._header_req(sheet_id, row_idx, 4))
        # Заморозить первую строку
        reqs.append(self._freeze_req(sheet_id, 1))
        reqs.append(self._resize_req(sheet_id, 4))
        self._batch(reqs)
        logger.info("Лист ПРАВИЛА обновлён")

    # ── Чтение истории для статуса ЦЕНА ПОДНЯТА ──────────────────────────────

    def _read_history_snapshot(self) -> dict[int, dict]:
        """
        Последние записи из ИСТОРИИ per nmID.
        Возвращает {nm_id: {status, price, raise_date?, price_before?, turnover_before?}}.
        raise_date/price_before/turnover_before — из последней строки со статусом ПОДНЯТЬ ЦЕНУ.
        """
        try:
            ws = self._get_sheet("📅 ИСТОРИЯ")
            rows = ws.get_all_values()
        except Exception:
            return {}

        if len(rows) < 2:
            return {}

        headers = rows[0]
        try:
            idx_nm       = headers.index("nmID")
            idx_price    = headers.index("Цена")
            idx_status   = headers.index("Статус")
            idx_turnover = headers.index("Оборачив. дней")
            idx_week     = headers.index("Неделя")
        except ValueError:
            return {}

        max_idx = max(idx_nm, idx_price, idx_status, idx_turnover, idx_week)
        latest: dict[int, dict] = {}       # latest row per nm_id (overwrites each time)
        last_raise: dict[int, dict] = {}   # last "ПОДНЯТЬ ЦЕНУ" row per nm_id

        for row in rows[1:]:
            if len(row) <= max_idx:
                continue
            try:
                nm_id = int(row[idx_nm])
            except (ValueError, IndexError):
                continue

            price   = float(row[idx_price]) if row[idx_price] else 0.0
            status  = row[idx_status]
            week    = row[idx_week]
            try:
                turnover = float(row[idx_turnover]) if row[idx_turnover] else 0.0
            except ValueError:
                turnover = 0.0

            # Rows are chronological — last write wins for latest status/price
            latest[nm_id] = {"status": status, "price": price}

            if "ПОДНЯТЬ ЦЕНУ" in status:
                last_raise[nm_id] = {
                    "raise_date":      week,
                    "price_before":    price,
                    "turnover_before": turnover,
                }

        snapshot: dict[int, dict] = {}
        for nm_id, data in latest.items():
            entry = dict(data)
            if nm_id in last_raise:
                entry.update(last_raise[nm_id])
            snapshot[nm_id] = entry

        return snapshot

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _write(self, ws: gspread.Worksheet, rows: list[list],
               start: str = "A1"):
        if not rows:
            return
        try:
            ws.update(start, rows, value_input_option="USER_ENTERED")
        except APIError as e:
            logger.error(f"Ошибка записи: {e}")
            time.sleep(5)
            ws.update(start, rows, value_input_option="USER_ENTERED")

    def _batch(self, requests: list):
        if not requests:
            return
        try:
            self.spreadsheet.batch_update({"requests": requests})
        except Exception as e:
            logger.warning(f"Ошибка форматирования: {e}")
