"""
Google Sheets Writer — professional formatting edition.
"""

import logging
import time
from datetime import datetime

import gspread
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials

from analytics import (SKUMetrics, SHEET_HEADERS, build_summary,
                       apply_price_raised_status,
                       calc_price_raise, calc_price_decrease)

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

# Column indices in SHEET_HEADERS (0-based)
TURNOVER_COL  = SHEET_HEADERS.index("Оборачиваемость")
SPP_PRICE_COL = SHEET_HEADERS.index("Цена СПП")


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
        "📋 ОЧЕРЕДЬ ИЗМЕНЕНИЙ",
        "📊 ЭФФЕКТИВНОСТЬ",
        "🚚 ПОСТАВКИ",
        "🚚 ПОСТАВКИ Коледино",
        "🚚 ПОСТАВКИ Шушары",
        "🚚 ПОСТАВКИ Казань",
        "🚚 ПОСТАВКИ Краснодар",
        "🚚 ПОСТАВКИ Екатеринбург",
    ]

    # Column indices in ОЧЕРЕДЬ ИЗМЕНЕНИЙ (0-based)
    _Q_NM_ID   = 0
    _Q_NAME    = 1
    _Q_CAT     = 2
    _Q_CUR     = 3
    _Q_NEW     = 4
    _Q_PCT     = 5
    _Q_REASON  = 6
    _Q_DATE    = 7
    _Q_APPROVE = 8
    _Q_SENT    = 9

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

    def update_all(self, metrics: dict[int, SKUMetrics]) -> list[SKUMetrics]:
        """Обновить все листы. Возвращает отфильтрованный отсортированный список SKU."""
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
        return sorted_display

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

        spp_col_fmt = {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_id,
                        "startRowIndex": 2,
                        "startColumnIndex": SPP_PRICE_COL,
                        "endColumnIndex": SPP_PRICE_COL + 1,
                    }],
                    "booleanRule": {
                        "condition": {"type": "NOT_BLANK"},
                        "format": {"backgroundColor": _hex("#c8e6c9")},
                    },
                },
                "index": 0,
            }
        }
        self._batch(
            [self._unhide_all_cols_req(sheet_id)]
            + self._delete_cf_rules(sheet_id)
            + fmt_reqs
            + [
                spp_col_fmt,
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
                # Цена СПП: светло-зелёный
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [{
                                "sheetId": sheet_id,
                                "startRowIndex": DATA_START,
                                "startColumnIndex": SPP_PRICE_COL,
                                "endColumnIndex": SPP_PRICE_COL + 1,
                            }],
                            "booleanRule": {
                                "condition": {"type": "NOT_BLANK"},
                                "format": {"backgroundColor": _hex("#c8e6c9")},
                            },
                        },
                        "index": 1,
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
            [""],

            # Раздел 4
            ["РАЗДЕЛ 4 — КЛАСТЕРЫ СКЛАДОВ WB", "", "", ""],
            ["Кластер", "Склады WB", "Целевой склад поставки", "Регионы для расчёта спроса"],
            ["Центр",         "Коледино, Электросталь, Подольск", "Коледино",      "По складу отгрузки"],
            ["СПБ",           "Шушары",                           "Шушары",        "По складу отгрузки"],
            ["Казань",        "Казань",                           "Казань",        "По складу отгрузки"],
            ["Юг",            "Краснодар, Ростов",                "Краснодар",     "По складу отгрузки"],
            ["Урал и Сибирь", "Екатеринбург, Новосибирск",        "Екатеринбург",  "Уральский, Дальневосточный, Сибирский"],
        ]

        self._write(ws, rows)
        time.sleep(1)

        # Форматирование
        reqs = [self._unhide_all_cols_req(sheet_id)]
        # Заголовок
        reqs.append(self._cell_req(sheet_id, 0, 1, 0, 4, {
            "textFormat": {"bold": True, "fontSize": 13},
        }))
        # Заголовки разделов (строки 2, 13, 23, 28)
        for row_idx in [2, 13, 23, 28]:
            reqs.append(self._cell_req(sheet_id, row_idx, row_idx + 1, 0, 4, {
                "backgroundColor": _hex(HEADER_BG),
                "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 11},
            }))
        # Заголовки колонок разделов 1, 2 и 4
        for row_idx in [3, 14, 29]:
            reqs.append(self._header_req(sheet_id, row_idx, 4))
        # Заморозить первую строку
        reqs.append(self._freeze_req(sheet_id, 1))
        reqs.append(self._resize_req(sheet_id, 4))
        self._batch(reqs)
        logger.info("Лист ПРАВИЛА обновлён")

    # ── Лист ОЧЕРЕДЬ ИЗМЕНЕНИЙ ───────────────────────────────────────────────

    _QUEUE_HEADERS = [
        "Артикул", "Название", "Категория",
        "Текущая цена", "Новая цена", "Изменение %",
        "Причина", "Дата добавления", "Согласовано", "Отправлено",
    ]

    def update_price_queue(self, sorted_display: list[SKUMetrics]) -> dict:
        """
        Заполняет лист ОЧЕРЕДЬ ИЗМЕНЕНИЙ на понедельник.
        Очищает только если все позиции прошлой недели уже отправлены.
        Возвращает {"skipped": True} если очистка не произошла, иначе {total, n_up, n_down}.
        """
        if not self._queue_all_sent():
            logger.info("Очередь изменений: есть неотправленные позиции, пропускаем перезапись")
            return {"skipped": True}

        ws = self._get_sheet("📋 ОЧЕРЕДЬ ИЗМЕНЕНИЙ")
        sheet_id = ws.id
        today_str = datetime.now().strftime("%d.%m.%Y")

        rows = [self._QUEUE_HEADERS]
        n_up = n_down = 0

        for m in sorted_display:
            if "РАСПРОДАЖА НЕЭФФЕКТИВНА" in m.status:
                continue

            if "ПОДНЯТЬ ЦЕНУ" in m.status:
                result = calc_price_raise(m.turnover_days, m.sales_growth_pct, m.final_price)
                if not result:
                    continue
                rows.append([
                    m.nm_id, m.name[:40], m.category,
                    m.final_price, result["new_price"],
                    f"+{result['raise_pct']}%",
                    "Поднять цену", today_str, False, "",
                ])
                n_up += 1

            elif "МЁРТВЫЙ" in m.status or "ЗАМЕДЛЕННАЯ" in m.status:
                has_no_sales = m.sales_7d < 0.5 and m.sales_prev_7d < 0.5
                dec = calc_price_decrease(m.turnover_days, m.final_price, m.category,
                                          has_no_sales_14d=has_no_sales)
                if not dec:
                    continue
                new_p_display = (f"{dec['new_price']} руб (min)"
                                 if dec["is_floor_price"] else dec["new_price"])
                rows.append([
                    m.nm_id, m.name[:40], m.category,
                    m.final_price, new_p_display,
                    f"-{dec['decrease_pct']}%",
                    "Снизить цену", today_str, False, "",
                ])
                n_down += 1

        ws.clear()
        if len(rows) > 1:
            ws.update("A1", rows, value_input_option="USER_ENTERED")
        else:
            ws.update("A1", [self._QUEUE_HEADERS], value_input_option="USER_ENTERED")

        time.sleep(1)

        # Форматирование и чекбоксы
        n_data = len(rows) - 1
        reqs = [
            self._unhide_all_cols_req(sheet_id),
            self._header_req(sheet_id, 0, len(self._QUEUE_HEADERS)),
            self._freeze_req(sheet_id, 1),
            self._resize_req(sheet_id, len(self._QUEUE_HEADERS)),
        ]
        if n_data > 0:
            # Чекбоксы в колонке "Согласовано"
            reqs.append({
                "setDataValidation": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": 1 + n_data,
                        "startColumnIndex": self._Q_APPROVE,
                        "endColumnIndex": self._Q_APPROVE + 1,
                    },
                    "rule": {
                        "condition": {"type": "BOOLEAN"},
                        "strict": True,
                        "showCustomUi": True,
                    },
                }
            })
        self._batch(reqs)

        total = n_up + n_down
        logger.info(f"Очередь изменений: {total} SKU (↑{n_up} повышений, ↓{n_down} снижений)")
        return {"total": total, "n_up": n_up, "n_down": n_down}

    def queue_all_sent(self) -> bool:
        """True если очередь пуста или все позиции имеют заполненное поле «Отправлено»."""
        rows = self._get_queue_rows()
        data = [r for r in rows[1:] if r and r[0].strip()]
        if not data:
            return True
        return all(
            len(r) > self._Q_SENT and r[self._Q_SENT].strip()
            for r in data
        )

    def _queue_all_sent(self) -> bool:
        return self.queue_all_sent()

    def queue_pending_count(self) -> int:
        """Количество позиций где «Отправлено» пустое (ещё не отправлены)."""
        rows = self._get_queue_rows()
        return sum(
            1 for r in rows[1:]
            if r and r[0].strip()
            and not (len(r) > self._Q_SENT and r[self._Q_SENT].strip())
        )

    def queue_unapproved_count(self) -> int:
        """Количество позиций ожидающих согласования (Согласовано != TRUE, Отправлено пустое)."""
        rows = self._get_queue_rows()
        count = 0
        for r in rows[1:]:
            if not r or not r[0].strip():
                continue
            approved = (len(r) > self._Q_APPROVE
                        and r[self._Q_APPROVE].strip().upper() in ("TRUE", "ИСТИНА", "1"))
            sent = len(r) > self._Q_SENT and r[self._Q_SENT].strip()
            if not approved and not sent:
                count += 1
        return count

    def _get_queue_rows(self) -> list[list]:
        try:
            ws = self._get_sheet("📋 ОЧЕРЕДЬ ИЗМЕНЕНИЙ")
            return ws.get_all_values()
        except Exception:
            return []

    def queue_has_data(self) -> bool:
        """True если в очереди есть хотя бы одна строка с данными."""
        rows = self._get_queue_rows()
        return any(r and r[0].strip() for r in rows[1:])

    # ── Лист ЭФФЕКТИВНОСТЬ ────────────────────────────────────────────────────

    _EFF_HEADERS = [
        "Артикул", "Название", "Категория", "Действие",
        "Цена до", "Цена после", "Изменение %", "Дата изменения",
        "Точка", "Заказы/нед", "Оборачиваемость", "Итог",
    ]

    def record_price_change(self, nm_id: int, name: str, category: str,
                            action: str, price_before: float, price_after: float,
                            orders_week: float, turnover_days: float):
        """Записывает базовую строку в ЭФФЕКТИВНОСТЬ при изменении цены."""
        try:
            ws = self._get_sheet("📊 ЭФФЕКТИВНОСТЬ")
            existing = ws.get_all_values()
            if not existing:
                ws.append_row(self._EFF_HEADERS)
            pct = (price_after / price_before - 1) * 100 if price_before > 0 else 0
            pct_str = f"+{pct:.0f}%" if pct >= 0 else f"{pct:.0f}%"
            row = [
                nm_id, name[:40], category, action,
                price_before, price_after, pct_str,
                datetime.now().strftime("%d.%m.%Y"),
                "База",
                round(orders_week, 1), round(turnover_days, 0), "⏳",
            ]
            ws.append_row(row, value_input_option="USER_ENTERED")
            logger.info(f"Эффективность: записана база для nmID={nm_id}")
        except Exception as e:
            logger.warning(f"Ошибка record_price_change nmID={nm_id}: {e}")

    def update_effectiveness_checkpoints(self, metrics: dict[int, SKUMetrics]):
        """
        Проверяет ЭФФЕКТИВНОСТЬ и добавляет строки День 7 / День 14 если прошло нужное время.
        Итог:
        - Повышение ✅ если turnover_days > 21 (нормализовалось)
        - Снижение ✅ если orders/нед выросли > 20%
        """
        try:
            ws = self._get_sheet("📊 ЭФФЕКТИВНОСТЬ")
            rows = ws.get_all_values()
        except Exception:
            return

        if len(rows) < 2:
            return

        headers = rows[0]
        try:
            i_nm      = headers.index("Артикул")
            i_name    = headers.index("Название")
            i_cat     = headers.index("Категория")
            i_action  = headers.index("Действие")
            i_before  = headers.index("Цена до")
            i_after   = headers.index("Цена после")
            i_pct     = headers.index("Изменение %")
            i_date    = headers.index("Дата изменения")
            i_point   = headers.index("Точка")
            i_orders  = headers.index("Заказы/нед")
            i_turn    = headers.index("Оборачиваемость")
        except ValueError:
            return

        now = datetime.now()
        existing_keys: set[tuple] = set()
        for r in rows[1:]:
            if len(r) > max(i_nm, i_date, i_point):
                existing_keys.add((r[i_nm], r[i_date], r[i_point]))

        new_rows = []
        for r in rows[1:]:
            if len(r) <= max(i_nm, i_date, i_point, i_orders):
                continue
            if r[i_point] != "База":
                continue
            nm_id_str  = r[i_nm]
            date_str   = r[i_date]
            action     = r[i_action]
            base_orders = float(r[i_orders]) if r[i_orders] else 0

            try:
                change_dt = datetime.strptime(date_str, "%d.%m.%Y")
                nm_id     = int(nm_id_str)
            except (ValueError, IndexError):
                continue

            days_passed = (now - change_dt).days
            m = metrics.get(nm_id)
            if not m:
                continue

            for point_name, min_days in [("День 7", 7), ("День 14", 14)]:
                key = (nm_id_str, date_str, point_name)
                if key in existing_keys or days_passed < min_days:
                    continue

                if point_name == "День 7":
                    result = "⏳ ждём"
                else:
                    if action == "Повышение":
                        result = "✅ работает" if m.turnover_days > 21 else "❌ нет эффекта"
                    else:
                        pct_chg = ((m.avg_weekly_sales - base_orders) / base_orders * 100
                                   if base_orders > 0 else 0)
                        result = "✅ работает" if pct_chg > 20 else "❌ нет эффекта"

                new_rows.append([
                    nm_id, m.name[:40], m.category, action,
                    r[i_before], r[i_after], r[i_pct],
                    date_str, point_name,
                    round(m.avg_weekly_sales, 1), round(m.turnover_days, 0),
                    result,
                ])

        if new_rows:
            ws.append_rows(new_rows, value_input_option="USER_ENTERED")
            logger.info(f"Эффективность: добавлено {len(new_rows)} строк")

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

    # ── Лист ПОСТАВКИ ────────────────────────────────────────────────────────

    # Сводный лист — все кластеры
    _SUPPLY_HEADERS = [
        "Приоритет", "Артикул", "Баркод", "Название", "Категория", "ABC",
        "Кластер", "Склад WB", "Остаток", "Продажи/день",
        "Потребность 28д", "Рекомендовать (шт)", "Дней до нуля",
    ]

    # Листы по складам — без Кластер/Склад WB (они вынесены в заголовок)
    _SUPPLY_WH_HEADERS = [
        "Приоритет", "Артикул", "Баркод", "Название", "Категория", "ABC",
        "Остаток на складе", "Продажи/день", "Потребность 28д",
        "Рекомендовать (шт)", "Дней до нуля",
    ]

    # Целевой склад → кластер
    _SUPPLY_WAREHOUSES: dict[str, str] = {
        "Коледино":     "Центр",
        "Шушары":       "СПБ",
        "Казань":       "Казань",
        "Краснодар":    "Юг",
        "Екатеринбург": "Урал и Сибирь",
    }

    SUPPLY_PRIORITY_COLORS = {
        "🔴": "#ffcdd2",
        "🟡": "#fff9c4",
        "🟢": "#e8f5e9",
    }

    def update_supply_sheet(self, recommendations: list[dict]):
        """Обновляет сводный лист ПОСТАВКИ и отдельный лист для каждого склада."""
        self._update_supply_summary(recommendations)
        for warehouse, cluster in self._SUPPLY_WAREHOUSES.items():
            wh_recs = [r for r in recommendations if r.get("warehouse") == warehouse]
            self._update_supply_warehouse_sheet(warehouse, cluster, wh_recs)

    def _update_supply_summary(self, recommendations: list[dict]):
        """Сводный лист 🚚 ПОСТАВКИ — все SKU по всем складам."""
        ws = self._get_sheet("🚚 ПОСТАВКИ")
        ws.clear()
        sheet_id = ws.id

        today_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        title = f"🚚 РЕКОМЕНДАЦИИ К ПОСТАВКЕ — обновлено {today_str}"

        clusters_count: dict[str, int] = {}
        prio_count = {"🔴": 0, "🟡": 0, "🟢": 0}
        for r in recommendations:
            clusters_count[r.get("cluster", "")] = clusters_count.get(r.get("cluster", ""), 0) + 1
            for emoji in prio_count:
                if r.get("priority", "").startswith(emoji):
                    prio_count[emoji] += 1

        clusters_str = " | ".join(f"{k}: {v}" for k, v in sorted(clusters_count.items()))
        summary = (f"Итого SKU: {len(recommendations)} | "
                   f"🔴 Срочно: {prio_count['🔴']} | "
                   f"🟡 Плановая: {prio_count['🟡']} | "
                   f"🟢 Запас: {prio_count['🟢']}")

        ncols = len(self._SUPPLY_HEADERS)
        rows = [[title], [summary], [clusters_str], [""], self._SUPPLY_HEADERS]
        fmt_reqs = [
            self._cell_req(sheet_id, 0, 1, 0, ncols, {"textFormat": {"bold": True, "fontSize": 12}}),
            self._cell_req(sheet_id, 1, 3, 0, ncols, {"textFormat": {"bold": True, "fontSize": 10}}),
            self._header_req(sheet_id, 4, ncols),
        ]

        ri = 5
        for r in self._sorted_recs(recommendations):
            rows.append([
                r.get("priority", ""),
                r.get("vendor_code", r.get("nm_id", "")),
                r.get("barcode", ""),
                r.get("name", "")[:40],
                r.get("category", ""),
                r.get("abc", ""),
                r.get("cluster", ""),
                r.get("warehouse", ""),
                r.get("stock", 0),
                round(r.get("sales_per_day", 0), 2),
                r.get("needed_28d", 0),
                r.get("recommended_qty", 0),
                round(r.get("days_to_zero", 0), 1),
            ])
            fmt_reqs.extend(self._prio_row_reqs(sheet_id, ri, ncols, r.get("priority", "")))
            ri += 1

        self._write(ws, rows)
        time.sleep(1)
        self._batch([self._freeze_req(sheet_id, 5), self._resize_req(sheet_id, ncols)] + fmt_reqs)
        logger.info(f"Лист ПОСТАВКИ (сводный): {len(recommendations)} SKU")

    def _update_supply_warehouse_sheet(self, warehouse: str, cluster: str, recs: list[dict]):
        """Лист поставки для конкретного склада."""
        sheet_name = f"🚚 ПОСТАВКИ {warehouse}"
        ws = self._get_sheet(sheet_name)
        ws.clear()
        sheet_id = ws.id

        today_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        prio_count = {"🔴": 0, "🟡": 0, "🟢": 0}
        abc_count  = {"A": 0, "B": 0}
        for r in recs:
            for emoji in prio_count:
                if r.get("priority", "").startswith(emoji):
                    prio_count[emoji] += 1
            abc = r.get("abc", "")
            if abc in abc_count:
                abc_count[abc] += 1

        ncols = len(self._SUPPLY_WH_HEADERS)
        rows = [
            [f"📦 ПОСТАВКИ НА СКЛАД {warehouse} — обновлено {today_str}"],
            [f"Кластер: {cluster} | Горизонт: 28 дней | Сезонность учтена"],
            [f"Итого SKU: {len(recs)} | 🔴 Срочно: {prio_count['🔴']} | "
             f"🟡 Плановая: {prio_count['🟡']} | 🟢 Запас: {prio_count['🟢']}"],
            [f"Категория A: {abc_count['A']} SKU | Категория B: {abc_count['B']} SKU"],
            [""],
            self._SUPPLY_WH_HEADERS,
        ]
        fmt_reqs = [
            self._cell_req(sheet_id, 0, 1, 0, ncols, {"textFormat": {"bold": True, "fontSize": 12}}),
            self._cell_req(sheet_id, 1, 4, 0, ncols, {"textFormat": {"bold": True, "fontSize": 10}}),
            self._header_req(sheet_id, 5, ncols),
        ]

        ri = 6
        for r in self._sorted_recs(recs):
            rows.append([
                r.get("priority", ""),
                r.get("vendor_code", r.get("nm_id", "")),
                r.get("barcode", ""),
                r.get("name", "")[:40],
                r.get("category", ""),
                r.get("abc", ""),
                r.get("stock", 0),
                round(r.get("sales_per_day", 0), 2),
                r.get("needed_28d", 0),
                r.get("recommended_qty", 0),
                round(r.get("days_to_zero", 0), 1),
            ])
            fmt_reqs.extend(self._prio_row_reqs(sheet_id, ri, ncols, r.get("priority", "")))
            ri += 1

        self._write(ws, rows)
        time.sleep(1)
        self._batch([self._freeze_req(sheet_id, 6), self._resize_req(sheet_id, ncols)] + fmt_reqs)
        logger.info(f"Лист {sheet_name}: {len(recs)} SKU")

    @staticmethod
    def _sorted_recs(recs: list[dict]) -> list[dict]:
        return sorted(recs, key=lambda x: (
            0 if x.get("priority", "").startswith("🔴") else
            1 if x.get("priority", "").startswith("🟡") else 2,
            -x.get("needed_28d", 0),
        ))

    def _prio_row_reqs(self, sheet_id: int, ri: int, ncols: int, priority: str) -> list[dict]:
        for emoji, color in self.SUPPLY_PRIORITY_COLORS.items():
            if priority.startswith(emoji):
                return [self._cell_req(sheet_id, ri, ri + 1, 0, ncols, {"backgroundColor": _hex(color)})]
        return []

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
