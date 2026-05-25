"""
Analytics Engine — ядро системы принятия решений
Считает оборачиваемость, статусы SKU и генерирует рекомендации.
"""

import logging
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Пороговые значения (можно менять в config.py)
# ──────────────────────────────────────────────────────────────

THRESHOLDS = {
    "critical_turnover_days": 90,       # > 90 дней → критичный остаток
    "slow_turnover_days": 60,           # 60–90 дней → замедленная оборачиваемость
    "normal_turnover_days": 30,         # 14–30 дней → норма
    "price_raise_turnover_days": 30,    # < 30 дней → можно поднимать цену
    "min_sales_for_analysis": 1,        # мин. продаж/нед для анализа
    "clearance_ineffective_discount": 25,  # скидка > 25% при нулевом росте
    "sales_growth_threshold": 0.10,     # рост продаж > 10% = позитивная динамика
    "storage_to_revenue_critical": 0.15,   # хранение > 15% выручки → проблема
    "ad_drr_too_high": 0.25,            # ДРР > 25% → снизить ставки
    "ad_drr_good": 0.10,                # ДРР < 10% → масштабировать
}


FLOOR_PRICES: dict[str, int] = {
    "Подушки декоративные": 450,
    "Валики":               450,
    "Сухоцветы":            800,
}
DEFAULT_FLOOR_PRICE = 200


def calc_price_decrease(turnover_days: float, current_price: float,
                        category: str, has_no_sales_14d: bool = False) -> dict | None:
    """
    Рассчитывает рекомендованное снижение цены с поэтапной логикой WB.

    WB запрещает снижать цену более чем вдвое за одно обновление.
    Если целевая цена < current_price/2 — нужно два шага:
      Шаг 1 (эта неделя):  current_price / 2
      Шаг 2 (след. неделя): целевая цена

    Возвращает:
      {"decrease_pct", "new_price", "final_price",
       "is_floor_price", "is_step1"} или None.
    """
    if has_no_sales_14d:
        decrease_pct = 30
    elif turnover_days > 180:
        decrease_pct = 30
    elif turnover_days > 90:
        decrease_pct = 20
    elif turnover_days > 60:
        decrease_pct = 10
    else:
        return None

    raw_price   = current_price * (1 - decrease_pct / 100)
    floor_price = FLOOR_PRICES.get(category, DEFAULT_FLOOR_PRICE)
    wb_min      = current_price / 2  # WB: нельзя снижать более чем вдвое

    # Целевая цена — ограничена floor, но НЕ лимитом WB (покажем пользователю итог)
    target = max(raw_price, floor_price)
    is_floor_price = (target > raw_price)

    if target < wb_min:
        # Двухэтапное снижение
        step1 = int(round(wb_min / 10) * 10)
        final = int(round(target / 10) * 10)
        return {"decrease_pct": decrease_pct,
                "new_price":    step1,
                "final_price":  final,
                "is_floor_price": is_floor_price,
                "is_step1":     True}
    else:
        # Одноэтапное снижение — достигаем цели сразу
        final = int(round(target / 10) * 10)
        return {"decrease_pct": decrease_pct,
                "new_price":    final,
                "final_price":  final,
                "is_floor_price": is_floor_price,
                "is_step1":     False}


def calc_price_raise(turnover_days: float, demand_delta_pct: float,
                     current_price: float) -> dict | None:
    """
    Рассчитывает рекомендованный подъём цены.
    Возвращает {"raise_pct": int, "new_price": int} или None если поднимать не нужно.
    Новая цена округляется до 10 руб вверх.
    """
    if turnover_days < 7:
        raise_pct = 28
    elif turnover_days < 14:
        raise_pct = 20 if demand_delta_pct > 0 else 13
    elif turnover_days < 21:
        raise_pct = 11 if demand_delta_pct > 0 else 8
    elif turnover_days < 30:
        raise_pct = 7
    else:
        return None

    raw_price = current_price * (1 + raise_pct / 100)
    new_price = int((raw_price + 9) // 10 * 10)   # вверх до 10 руб
    return {"raise_pct": raise_pct, "new_price": new_price}


# ──────────────────────────────────────────────────────────────
# Датаклассы
# ──────────────────────────────────────────────────────────────

@dataclass
class SKUMetrics:
    nm_id: int
    vendor_code: str = ""
    name: str = ""
    category: str = ""

    # Остатки
    stock: int = 0

    # Продажи
    sales_7d: float = 0.0       # продажи за 7 дней (шт)
    sales_14d: float = 0.0
    sales_28d: float = 0.0
    sales_90d: float = 0.0      # продажи за 90 дней (шт)
    revenue_7d: float = 0.0     # выручка 7д
    revenue_28d: float = 0.0

    # Прошлая неделя (для динамики)
    sales_prev_7d: float = 0.0
    revenue_prev_7d: float = 0.0

    # Цены
    price: float = 0.0          # цена до скидки
    discount: float = 0.0       # скидка %
    final_price: float = 0.0    # итоговая цена

    # Хранение
    storage_cost_7d: float = 0.0  # стоимость хранения за 7 дней

    # СПП (скидка постоянного покупателя)
    spp_pct: float = 0.0
    spp_price: float = 0.0


    # Реклама
    ad_spend_7d: float = 0.0
    ad_orders_7d: float = 0.0
    ad_revenue_7d: float = 0.0

    # Воронка
    views_7d: int = 0
    cart_7d: int = 0
    orders_7d: int = 0

    # Вычисляемые поля (заполняются методом compute())
    avg_weekly_sales: float = 0.0
    turnover_days: float = 0.0
    sales_growth_pct: float = 0.0
    storage_to_revenue_pct: float = 0.0
    ad_drr: float = 0.0         # доля рекламных расходов
    days_to_stockout: float = 0.0
    forecast_date: str = ""

    status: str = ""
    recommendation: str = ""
    priority: int = 0           # 1=срочно, 2=важно, 3=мониторинг

    def compute(self):
        """Рассчитать все производные метрики."""
        # Средненедельные продажи (по 28-дневному окну, более стабильно)
        self.avg_weekly_sales = self.sales_28d / 4 if self.sales_28d > 0 else self.sales_7d

        # Оборачиваемость
        if self.avg_weekly_sales > 0:
            self.turnover_days = (self.stock / self.avg_weekly_sales) * 7
        else:
            self.turnover_days = 999 if self.stock > 0 else 0

        # Прогноз распродажи
        if self.avg_weekly_sales > 0 and self.stock > 0:
            weeks_left = self.stock / self.avg_weekly_sales
            self.days_to_stockout = weeks_left * 7
            forecast_dt = datetime.now() + timedelta(days=self.days_to_stockout)
            self.forecast_date = "'" + forecast_dt.strftime("%d.%m.%Y")
        else:
            self.forecast_date = "—"

        # Динамика продаж
        if self.sales_prev_7d > 0:
            self.sales_growth_pct = (
                (self.sales_7d - self.sales_prev_7d) / self.sales_prev_7d
            ) * 100
        else:
            self.sales_growth_pct = 100.0 if self.sales_7d > 0 else 0.0

        # Доля хранения в выручке
        if self.revenue_7d > 0:
            self.storage_to_revenue_pct = (
                self.storage_cost_7d / self.revenue_7d
            ) * 100
        else:
            self.storage_to_revenue_pct = 100.0 if self.storage_cost_7d > 0 else 0.0

        # ДРР (доля рекламных расходов)
        if self.revenue_7d > 0:
            self.ad_drr = (self.ad_spend_7d / self.revenue_7d) * 100
        else:
            self.ad_drr = 100.0 if self.ad_spend_7d > 0 else 0.0

        # Определить статус и рекомендацию
        self.status, self.recommendation, self.priority = _classify(self)

    def to_row(self) -> list:
        """Конвертировать в строку для Google Sheets."""
        def fmt_pct(v): return f"▲{v:.1f}%" if v > 0 else (f"▼{abs(v):.1f}%" if v < 0 else "0%")
        def fmt_days(v): return f"{v:.0f}" if v < 900 else "нет продаж"

        if "ПОДНЯТЬ ЦЕНУ" in self.status:
            result = calc_price_raise(self.turnover_days, self.sales_growth_pct, self.price)
            new_price_cell = result["new_price"] if result else ""
        elif "ЦЕНА ПОДНЯТА" in self.status:
            new_price_cell = f"✓ {self.final_price:.0f}"
        elif "МЁРТВЫЙ" in self.status or "ЗАМЕДЛЕННАЯ" in self.status or "КРИТИЧНЫЙ" in self.status:
            has_no_sales = self.sales_7d < 0.5 and self.sales_prev_7d < 0.5
            dec = calc_price_decrease(self.turnover_days, self.price, self.category,
                                      has_no_sales_14d=has_no_sales)
            if dec:
                new_price_cell = (f"{dec['new_price']} руб (min)"
                                  if dec["is_floor_price"] else dec["new_price"])
            else:
                new_price_cell = ""
        elif "НУЖНА ПОСТАВКА" in self.status:
            if 0 <= self.turnover_days < 21 and self.final_price > 0:
                result = calc_price_raise(self.turnover_days, self.sales_growth_pct, self.final_price)
                new_price_cell = result["new_price"] if result else ""
            else:
                new_price_cell = ""
        elif "НЕТ В НАЛИЧИИ" in self.status:
            new_price_cell = ""
        elif "ВЫВЕДЕН" in self.status:
            new_price_cell = ""
        else:
            new_price_cell = ""

        return [
            self.nm_id,
            self.name[:40] if self.name else "—",
            self.category[:30] if self.category else "—",
            self.stock,
            round(self.sales_7d, 1),
            round(self.sales_28d, 1),
            round(self.avg_weekly_sales, 1),
            fmt_days(self.turnover_days),
            fmt_pct(self.sales_growth_pct),
            self.final_price,
            self.discount,
            round(self.spp_pct, 1) if self.spp_pct > 0 else "",
            self.spp_price if self.spp_price > 0 else "",
            self.forecast_date,
            self.status,
            new_price_cell,
            self.recommendation,
        ]


SHEET_HEADERS = [
    "Артикул WB", "Название", "Категория",
    "Остаток", "Заказы 7д", "Заказы 28д", "Ср. заказов/нед",
    "Оборачиваемость", "Δ к прошлой неделе",
    "Цена", "Скидка %", "СПП %", "Цена СПП",
    "Прогноз распродажи",
    "Статус", "Новая цена", "Рекомендация",
]


# ──────────────────────────────────────────────────────────────
# Логика классификации
# ──────────────────────────────────────────────────────────────

def _classify(m: SKUMetrics) -> tuple[str, str, int]:
    """
    Возвращает (статус, рекомендация, приоритет).
    Приоритет: 1=срочно, 2=важно, 3=мониторинг, 4=ОК, 5=нет в наличии, 6=выведен.

    Порядок проверки статусов:
    0a. НЕТ В НАЛИЧИИ: stock==0 AND sales_28d==0 AND sales_90d>0  → приоритет 5
    0b. ВЫВЕДЕН ИЗ ПРОДАЖИ: stock==0 AND sales_28d==0 AND sales_90d==0  → приоритет 6
    1.  НУЖНА ПОСТАВКА: (stock==0 AND sales_28d>0) ИЛИ (sales_7d>0 AND stock<sales_7d)
    2.  МЁРТВЫЙ ОСТАТОК: stock>0 AND sales_28d==0
    3.  КРИТИЧНЫЙ ОСТАТОК: turnover_days>90
    4.  ЗАМЕДЛЕННАЯ ОБОРАЧИВАЕМОСТЬ: 60<turnover_days<=90
    5.  РАСПРОДАЖА НЕЭФФЕКТИВНА: discount>20 AND sales_growth_pct<=10
    6.  ПОДНЯТЬ ЦЕНУ: 0<turnover_days<21 AND stock>0
    7.  НОРМАЛИЗАЦИЯ: sales_growth_pct>20
    8.  МОНИТОРИНГ: всё остальное
    """

    # 0a. Нет в наличии: нулевой остаток, нет продаж за 28д, но были за 90д
    if m.stock == 0 and m.sales_28d == 0 and m.sales_90d > 0:
        return (
            "⬜ НЕТ В НАЛИЧИИ",
            "Остаток = 0, продаж нет 28+ дней, но были продажи в последние 90 дней. "
            "Пополнить запас при необходимости.",
            5
        )

    # 0b. Выведен из продажи: нулевой остаток, нет продаж ни за 28д, ни за 90д
    if m.stock == 0 and m.sales_28d == 0 and m.sales_90d == 0:
        return (
            "📦 ВЫВЕДЕН ИЗ ПРОДАЖИ",
            "Остаток = 0, продаж нет 90+ дней. Товар перенесён в АРХИВ.",
            6
        )

    # 1. Нужна поставка: остаток = 0 при наличии заказов, или остаток < продаж за 7 дней
    if (m.stock == 0 and m.sales_28d > 0) or (m.sales_7d > 0 and m.stock < m.sales_7d):
        base = f"Остаток {m.stock} шт < продажи за 7д ({m.sales_7d:.0f} шт). Срочно пополнить запас."
        # Если оборачиваемость <21д — поднять цену, чтобы замедлить продажи до прихода поставки
        if 0 <= m.turnover_days < 21 and m.final_price > 0:
            result = calc_price_raise(m.turnover_days, m.sales_growth_pct, m.final_price)
            if result:
                new_p = result["new_price"]
                rec = (f"{base} Товар заканчивается. Поднять цену до {new_p} руб "
                       f"чтобы замедлить продажи до прихода поставки.")
                return ("🔵 НУЖНА ПОСТАВКА", rec, 1)
        return ("🔵 НУЖНА ПОСТАВКА", base, 1)

    # 2. Мёртвый остаток: есть товар, но нет заказов за 28 дней
    if m.stock > 0 and m.sales_28d < 1:
        dec = calc_price_decrease(999.0, m.price, m.category, has_no_sales_14d=True)
        if dec and dec["is_floor_price"]:
            price_rec = f"Снизить до минимума {dec['new_price']} руб — ниже нельзя"
        elif dec:
            price_rec = f"Снизить цену до {dec['new_price']} руб (-{dec['decrease_pct']}%)"
        else:
            price_rec = "Снизить цену на 30%"
        return (
            "⚫ МЁРТВЫЙ ОСТАТОК",
            f"Заказов нет за 28 дней. {price_rec}. Проверить карточку, фото, SEO.",
            1
        )

    # 3. Критичный остаток: оборачиваемость > 90 дней
    if m.turnover_days > 90:
        dec = calc_price_decrease(m.turnover_days, m.price, m.category)
        if dec and dec["is_floor_price"]:
            price_rec = f"Снизить до минимума {dec['new_price']} руб — ниже нельзя"
        elif dec:
            price_rec = f"Снизить цену до {dec['new_price']} руб (-{dec['decrease_pct']}%)"
        else:
            price_rec = "Снизить цену"
        return (
            "🔴 КРИТИЧНЫЙ ОСТАТОК",
            f"Оборачиваемость {m.turnover_days:.0f} дней. Хранение съедает маржу. {price_rec}.",
            1
        )

    # 4. Замедленная оборачиваемость: 60–90 дней
    if 60 < m.turnover_days <= 90:
        has_no_sales = m.sales_7d < 0.5 and m.sales_prev_7d < 0.5
        dec = calc_price_decrease(m.turnover_days, m.price, m.category,
                                  has_no_sales_14d=has_no_sales)
        if dec and dec["is_floor_price"]:
            price_rec = f"Снизить до минимума {dec['new_price']} руб — ниже нельзя"
        elif dec:
            price_rec = f"Снизить цену до {dec['new_price']} руб (-{dec['decrease_pct']}%)"
        else:
            price_rec = "Снизить цену на 10–15%"
        ad_hint = (
            "Реклама уже идёт. Проверить ставки и релевантность фраз."
            if m.ad_spend_7d > 0 else
            "Реклама не запущена — запустить автокампанию или поисковую рекламу."
        )
        return (
            "🟠 ЗАМЕДЛЕННАЯ ОБОРАЧИВАЕМОСТЬ",
            f"Оборачиваемость {m.turnover_days:.0f} дней. {price_rec}. {ad_hint}",
            2
        )

    # 5. Распродажа неэффективна: скидка > 20% и продажи не выросли
    if m.discount > 20 and m.sales_growth_pct <= 10:
        return (
            "⚠️ РАСПРОДАЖА НЕЭФФЕКТИВНА",
            "Скидка есть, но продажи не растут. "
            "Проблема не в цене: проверить позицию в поиске, CTR карточки, отзывы. "
            "Усилить рекламу или пересмотреть контент.",
            1
        )

    # 6. Поднять цену: оборачиваемость < 21 дня
    if 0 < m.turnover_days < 21 and m.stock > 0:
        result = calc_price_raise(m.turnover_days, m.sales_growth_pct, m.final_price)
        if result:
            new_p = result["new_price"]
            pct   = result["raise_pct"]
            td    = m.turnover_days
            if td < 7:
                rec = (f"Оборачиваемость {td:.0f} дней — товар улетает. "
                       f"Поднять цену до {new_p} руб (+{pct}%)")
            elif td < 14:
                if m.sales_growth_pct > 0:
                    rec = f"Поднять цену до {new_p} руб (+{pct}%). Спрос растёт — действовать уверенно"
                else:
                    rec = f"Поднять цену до {new_p} руб (+{pct}%). Спрос замедляется — действовать осторожно"
            else:
                rec = f"Поднять цену до {new_p} руб (+{pct}%)"
        else:
            rec = f"Оборачиваемость {m.turnover_days:.0f} дней."
        return ("🟢 ПОДНЯТЬ ЦЕНУ", rec, 3)

    # 7. Нормализация: спрос растёт > 20%
    if m.sales_growth_pct > 20:
        return (
            "✅ НОРМАЛИЗАЦИЯ",
            f"Продажи растут +{m.sales_growth_pct:.0f}% к прошлой неделе. "
            "Остаток нормализуется. Готовиться к плавному повышению цены.",
            3
        )

    # 8. Мониторинг
    return (
        "🟡 МОНИТОРИНГ",
        f"Оборачиваемость {m.turnover_days:.0f} дней. Показатели в норме. "
        "Продолжать мониторинг.",
        4
    )


# ──────────────────────────────────────────────────────────────
# Агрегация данных из WB API → SKUMetrics
# ──────────────────────────────────────────────────────────────

def build_sku_metrics(
    stocks: list[dict],
    orders: list[dict],        # заказы за 28д (qty: sales_7d / prev_7d / 28d)
    sales: list[dict],         # выкупы flag=1 текущие 7д (только revenue)
    prev_sales: list[dict],    # выкупы flag=1 прошлые 7д (только revenue)
    report_detail: list[dict],
    prices: list[dict],
    storage: list[dict],
    ad_stats: list[dict],
    cards: list[dict],
) -> dict[int, SKUMetrics]:
    """
    Собирает все источники данных в единый словарь {nmID: SKUMetrics}.
    qty-метрики (продажи 7д/28д) — из /orders (совпадает с лк WB).
    revenue — из /sales flag=1 (подтверждённые выкупы).
    """

    metrics: dict[int, SKUMetrics] = {}

    def get_or_create(nm_id: int) -> SKUMetrics:
        if nm_id not in metrics:
            metrics[nm_id] = SKUMetrics(nm_id=nm_id)
        return metrics[nm_id]

    # --- Карточки: название, категория, vendorCode ---
    for card in cards:
        nm_id = card.get("nmID") or card.get("nmId")
        if not nm_id:
            continue
        m = get_or_create(nm_id)
        m.vendor_code = card.get("vendorCode", "")
        # Название из первой характеристики или subjectName
        m.name = (card.get("title") or
                  card.get("subjectName") or
                  card.get("imt_name") or "")
        m.category = card.get("subjectName") or card.get("objectName") or ""

    # --- Остатки ---
    stock_by_nm: dict[int, int] = defaultdict(int)
    for row in stocks:
        nm_id = row.get("nmId")
        qty = row.get("quantity", 0) or 0
        if nm_id:
            stock_by_nm[nm_id] += qty
    for nm_id, qty in stock_by_nm.items():
        get_or_create(nm_id).stock = qty

    # --- Заказы 28д → qty-метрики (sales_7d, sales_prev_7d, sales_28d) ---
    _aggregate_orders(orders, metrics)

    # --- Выкупы → revenue (revenue_7d, revenue_prev_7d) ---
    _aggregate_sales(sales, metrics, target="current")
    _aggregate_sales(prev_sales, metrics, target="prev")

    # --- Финансовый отчёт (хранение из отчёта как fallback) ---
    _aggregate_report(report_detail, metrics)

    # --- Цены ---
    for row in prices:
        nm_id = row.get("nmId")
        if not nm_id:
            continue
        m = get_or_create(nm_id)
        m.price = float(row.get("price", 0) or 0)
        m.discount = float(row.get("discount", 0) or 0)
        final = row.get("finalPrice")
        m.final_price = float(final) if final is not None else round(m.price * (1 - m.discount / 100), 0)

    # --- Хранение ---
    storage_by_nm: dict[int, float] = defaultdict(float)
    for row in storage:
        nm_id = row.get("nmId") or row.get("nmID")
        cost = float(row.get("cost", 0) or 0)
        if nm_id:
            storage_by_nm[nm_id] += cost
    for nm_id, cost in storage_by_nm.items():
        get_or_create(nm_id).storage_cost_7d = cost

    # --- Реклама ---
    _aggregate_ads(ad_stats, metrics)

    # --- Compute всё ---
    for m in metrics.values():
        m.compute()

    return metrics


def _aggregate_orders(orders: list[dict], metrics: dict):
    """Заказы из /orders → qty-метрики.
    Отменённые (isCancel=True) исключаются.
    Разбивка по полю date (дата размещения заказа) на окна 7д / 14д / 28д / 90д.
    Ожидается, что orders содержит заказы за 91д (передаётся из main.py).
    """
    cutoff_7d  = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    cutoff_14d = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    cutoff_28d = (datetime.now() - timedelta(days=28)).strftime("%Y-%m-%d")
    cutoff_90d = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

    by_nm: dict[int, dict] = defaultdict(
        lambda: {"cur": 0, "prev": 0, "total": 0, "total_90d": 0}
    )
    for row in orders:
        if row.get("isCancel", False):
            continue
        nm_id = row.get("nmId")
        if not nm_id:
            continue
        order_date = row.get("date", "")[:10]
        if order_date >= cutoff_28d:
            by_nm[nm_id]["total"] += 1
        if order_date >= cutoff_90d:
            by_nm[nm_id]["total_90d"] += 1
        if order_date >= cutoff_7d:
            by_nm[nm_id]["cur"] += 1
        elif order_date >= cutoff_14d:
            by_nm[nm_id]["prev"] += 1

    for nm_id, data in by_nm.items():
        m = metrics.get(nm_id)
        if not m:
            m = SKUMetrics(nm_id=nm_id)
            metrics[nm_id] = m
        m.sales_7d      = data["cur"]
        m.sales_prev_7d = data["prev"]
        m.sales_28d     = data["total"]
        m.sales_90d     = data["total_90d"]


def _aggregate_sales(sales: list[dict], metrics: dict, target: str):
    """Выкупы /sales flag=1 → только revenue. target: 'current' | 'prev'"""
    by_nm: dict[int, float] = defaultdict(float)
    for row in sales:
        nm_id = row.get("nmId")
        if not nm_id:
            continue
        if not row.get("saleID", "").startswith("S"):
            continue  # исключаем возвраты (R...)
        price = float(row.get("finishedPrice", 0) or 0)
        by_nm[nm_id] += price

    for nm_id, revenue in by_nm.items():
        m = metrics.get(nm_id)
        if not m:
            m = SKUMetrics(nm_id=nm_id)
            metrics[nm_id] = m
        if target == "current":
            m.revenue_7d += revenue
        else:
            m.revenue_prev_7d += revenue


def _aggregate_report(report: list[dict], metrics: dict):
    """Из детального отчёта берём только хранение (fallback если paid_storage недоступен).
    sales_28d теперь приходит из /orders — отчёт для qty не используется.
    """
    by_nm: dict[int, float] = defaultdict(float)
    for row in report:
        nm_id = row.get("nm_id")
        if not nm_id:
            continue
        storage = float(row.get("storage_fee", 0) or 0)
        penalty = float(row.get("penalty", 0) or 0)
        by_nm[nm_id] += storage + penalty

    for nm_id, cost in by_nm.items():
        if nm_id in metrics and metrics[nm_id].storage_cost_7d == 0:
            metrics[nm_id].storage_cost_7d = cost


def _aggregate_ads(ad_stats: list[dict], metrics: dict):
    """Суммируем рекламные расходы по nmID.
    Формат ad_stats: [{"nmId": int, "spend": float}] — из /adv/v1/upd.
    """
    for item in ad_stats:
        nm_id = item.get("nmId")
        if nm_id and nm_id in metrics:
            metrics[nm_id].ad_spend_7d += float(item.get("spend", 0) or 0)


# ──────────────────────────────────────────────────────────────
# Сводная статистика для дашборда
# ──────────────────────────────────────────────────────────────

def build_summary(metrics: dict[int, SKUMetrics]) -> dict:
    """Итоговые цифры для первого листа дашборда."""
    all_m = list(metrics.values())
    if not all_m:
        return {}

    by_status = defaultdict(int)
    for m in all_m:
        by_status[m.status] += 1

    total_stock_value = sum(
        m.stock * m.final_price for m in all_m if m.final_price > 0
    )
    total_storage_7d = sum(m.storage_cost_7d for m in all_m)
    total_revenue_7d = sum(m.revenue_7d for m in all_m)
    total_ad_spend_7d = sum(m.ad_spend_7d for m in all_m)

    urgent = [m for m in all_m if m.priority == 1]

    return {
        "total_skus": len(all_m),
        "total_stock_value": round(total_stock_value),
        "total_storage_7d": round(total_storage_7d),
        "total_revenue_7d": round(total_revenue_7d),
        "total_ad_spend_7d": round(total_ad_spend_7d),
        "overall_drr": round(
            total_ad_spend_7d / total_revenue_7d * 100 if total_revenue_7d else 0, 1
        ),
        "storage_to_revenue": round(
            total_storage_7d / total_revenue_7d * 100 if total_revenue_7d else 0, 1
        ),
        "urgent_count": len(urgent),
        "status_breakdown": dict(by_status),
        "updated_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
    }


def apply_price_raised_status(metrics: dict[int, "SKUMetrics"],
                               history: dict[int, dict]):
    """
    Пост-обработка: присваивает статус ЦЕНА ПОДНЯТА на основе данных предыдущего запуска.
    history: {nm_id: {status, price, raise_date?, price_before?, turnover_before?}}

    Правила:
    - prev=ПОДНЯТЬ ЦЕНУ + текущая цена выросла >5% → ЦЕНА ПОДНЯТА
    - prev=ЦЕНА ПОДНЯТА + цена упала >3%           → возврат к результату _classify
    - prev=ЦЕНА ПОДНЯТА + оборачиваемость >21д     → НОРМАЛИЗАЦИЯ
    - prev=ЦЕНА ПОДНЯТА + всё остальное            → держать ЦЕНА ПОДНЯТА
    """

    def _parse_raise_date(week_str: str):
        try:
            return datetime.strptime(week_str.replace("Нед. ", ""), "%d.%m.%Y")
        except (ValueError, AttributeError):
            return None

    def _build_rec(price_before: float, price_now: float,
                   turnover_before: float, turnover_now: float,
                   days_since: int) -> str:
        base = (f"Цена поднята {days_since} дн. назад "
                f"(с {price_before:.0f} → {price_now:.0f} ₽). "
                f"Оборачиваемость: было {turnover_before:.0f}д, сейчас {turnover_now:.0f}д")
        if days_since < 3:
            verdict = "⏳ ждём реакции"
        elif turnover_before > 0 and turnover_now < turnover_before * 0.95:
            verdict = "✅ работает"
        elif days_since >= 7:
            verdict = "⚠️ нет эффекта, проверить конкурентов"
        else:
            verdict = "⏳ ждём реакции"
        return f"{base}. {verdict}"

    now = datetime.now()

    for nm_id, m in metrics.items():
        prev = history.get(nm_id)
        if not prev:
            continue
        prev_status   = prev.get("status", "")
        prev_price    = float(prev.get("price", 0) or 0)
        price_before  = float(prev.get("price_before", 0) or prev_price)
        turnover_before = float(prev.get("turnover_before", 0) or 0)
        raise_date    = _parse_raise_date(prev.get("raise_date", ""))
        days_since    = (now - raise_date).days if raise_date else 0

        if prev_price <= 0:
            continue

        if "ПОДНЯТЬ ЦЕНУ" in prev_status:
            if m.final_price > prev_price * 1.05:
                m.status = "🔄 ЦЕНА ПОДНЯТА"
                m.recommendation = _build_rec(
                    price_before or prev_price, m.final_price,
                    turnover_before, m.turnover_days, days_since,
                )
                m.priority = 3

        elif "ЦЕНА ПОДНЯТА" in prev_status:
            if m.final_price < prev_price * 0.97:
                pass  # цена упала — _classify уже назначил нужный статус
            elif m.turnover_days > 21:
                m.status = "✅ НОРМАЛИЗАЦИЯ"
                m.recommendation = (
                    "Оборачиваемость снизилась после подъёма цены. "
                    "Цена стабилизирована. Продолжать мониторинг."
                )
                m.priority = 3
            else:
                m.status = "🔄 ЦЕНА ПОДНЯТА"
                m.recommendation = _build_rec(
                    price_before or prev_price, m.final_price,
                    turnover_before, m.turnover_days, days_since,
                )
                m.priority = 3
