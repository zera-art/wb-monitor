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
    "price_raise_turnover_days": 14,    # < 14 дней → можно поднимать цену
    "min_sales_for_analysis": 1,        # мин. продаж/нед для анализа
    "clearance_ineffective_discount": 25,  # скидка > 25% при нулевом росте
    "sales_growth_threshold": 0.10,     # рост продаж > 10% = позитивная динамика
    "storage_to_revenue_critical": 0.15,   # хранение > 15% выручки → проблема
    "ad_drr_too_high": 0.25,            # ДРР > 25% → снизить ставки
    "ad_drr_good": 0.10,                # ДРР < 10% → масштабировать
}


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

        # Новая цена: только для ПОДНЯТЬ ЦЕНУ и ЦЕНА ПОДНЯТА
        if "ПОДНЯТЬ ЦЕНУ" in self.status:
            result = calc_price_raise(self.turnover_days, self.sales_growth_pct, self.final_price)
            new_price_cell = result["new_price"] if result else ""
        elif "ЦЕНА ПОДНЯТА" in self.status:
            new_price_cell = f"✓ {self.final_price:.0f}"
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
            self.forecast_date,
            self.status,
            new_price_cell,
            self.recommendation,
        ]


SHEET_HEADERS = [
    "Артикул WB", "Название", "Категория",
    "Остаток", "Заказы 7д", "Заказы 28д", "Ср. заказов/нед",
    "Оборачиваемость", "Δ к прошлой неделе",
    "Цена", "Скидка %",
    "Прогноз распродажи",
    "Статус", "Новая цена", "Рекомендация",
]


# ──────────────────────────────────────────────────────────────
# Логика классификации
# ──────────────────────────────────────────────────────────────

def _classify(m: SKUMetrics) -> tuple[str, str, int]:
    """
    Возвращает (статус, рекомендация, приоритет).
    Приоритет: 1=срочно (красный), 2=важно (оранжевый), 3=мониторинг (жёлтый), 4=ОК (зелёный).
    """
    t = THRESHOLDS

    # 1. Мёртвый остаток: нет продаж 4+ недель, остаток > 10 шт, оборачиваемость не быстрая
    if (m.stock > 10
            and m.sales_28d < 1
            and m.turnover_days >= t["normal_turnover_days"]):
        return (
            "🔴 МЁРТВЫЙ ОСТАТОК",
            "Продаж нет 4+ недели. Дропнуть цену до -50% или вывезти на самовыкуп. "
            "Проверить карточку, фото, SEO.",
            1
        )

    # 2. Критичная оборачиваемость
    if m.turnover_days > t["critical_turnover_days"]:
        discount_hint = (
            f"Текущая скидка {m.discount:.0f}% — увеличить до {min(m.discount + 15, 50):.0f}%."
            if m.discount < 40 else
            "Скидка уже высокая. Подключить внешний трафик или акцию WB."
        )
        return (
            "🔴 КРИТИЧНЫЙ ОСТАТОК",
            f"Оборачиваемость {m.turnover_days:.0f} дней. "
            f"Стоимость хранения съедает маржу. {discount_hint}",
            1
        )

    # 3. Замедленная оборачиваемость + распродажа не работает
    if (m.turnover_days > t["slow_turnover_days"]
            and m.discount > t["clearance_ineffective_discount"]
            and m.sales_growth_pct < t["sales_growth_threshold"] * 100):
        return (
            "⚠️ РАСПРОДАЖА НЕЭФФЕКТИВНА",
            "Скидка есть, но продажи не растут. "
            "Проблема не в цене: проверить позицию в поиске, CTR карточки, отзывы. "
            "Усилить рекламу или пересмотреть контент.",
            1
        )

    # 4. Замедленная оборачиваемость — нужна реклама
    if m.turnover_days > t["slow_turnover_days"]:
        ad_hint = (
            "Реклама уже идёт. Проверить ставки и релевантность фраз."
            if m.ad_spend_7d > 0 else
            "Реклама не запущена — запустить автокампанию или поисковую рекламу."
        )
        return (
            "🟠 ЗАМЕДЛЕННАЯ ОБОРАЧИВАЕМОСТЬ",
            f"Оборачиваемость {m.turnover_days:.0f} дней. "
            f"Снизить цену на 10–15% или усилить трафик. {ad_hint}",
            2
        )

    # 5. Нормализация — продажи растут, оборачиваемость улучшается
    if (m.turnover_days <= t["normal_turnover_days"]
            and m.sales_growth_pct > t["sales_growth_threshold"] * 100):
        return (
            "✅ НОРМАЛИЗАЦИЯ",
            f"Продажи растут +{m.sales_growth_pct:.0f}% к прошлой неделе. "
            "Остаток нормализуется. Готовиться к плавному повышению цены.",
            3
        )

    # 7. Можно повышать цену
    if m.turnover_days <= t["price_raise_turnover_days"] and m.stock > 0:
        raise_by = max(5, min(15, int(100 / max(m.turnover_days, 1))))
        return (
            "🟢 ПОДНЯТЬ ЦЕНУ",
            f"Оборачиваемость {m.turnover_days:.0f} дней — товар улетает. "
            f"Поднять цену на {raise_by}% без риска падения продаж. "
            "Тестировать +5% каждые 3 дня.",
            3
        )

    # 7б. Реклама даёт хороший ДРР — масштабировать
    if (m.ad_spend_7d > 0
            and m.ad_drr < t["ad_drr_good"] * 100
            and m.turnover_days < t["slow_turnover_days"]):
        return (
            "📈 МАСШТАБИРОВАТЬ РЕКЛАМУ",
            "Реклама окупается хорошо. "
            "Повысить ставки или увеличить бюджет на 20–30%.",
            3
        )

    # 9. Нет остатка
    if m.stock == 0:
        return (
            "⚫ НЕТ В НАЛИЧИИ",
            "Остаток = 0. Проверить, нужно ли пополнение.",
            4
        )

    # 10. Норма
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
    Разбивка по полю date (дата размещения заказа) на окна 7д / 14д / 28д.
    """
    cutoff_7d  = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    cutoff_14d = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")

    by_nm: dict[int, dict] = defaultdict(lambda: {"cur": 0, "prev": 0, "total": 0})
    for row in orders:
        if row.get("isCancel", False):
            continue
        nm_id = row.get("nmId")
        if not nm_id:
            continue
        order_date = row.get("date", "")[:10]
        by_nm[nm_id]["total"] += 1
        if order_date >= cutoff_7d:
            by_nm[nm_id]["cur"] += 1
        elif order_date >= cutoff_14d:
            by_nm[nm_id]["prev"] += 1

    for nm_id, data in by_nm.items():
        m = metrics.get(nm_id)
        if not m:
            m = SKUMetrics(nm_id=nm_id)
            metrics[nm_id] = m
        m.sales_7d    = data["cur"]
        m.sales_prev_7d = data["prev"]
        m.sales_28d   = data["total"]


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
    history: {nm_id: {"status": str, "price": float}}

    Правила:
    - prev=ПОДНЯТЬ ЦЕНУ + текущая цена выросла >5% → ЦЕНА ПОДНЯТА
    - prev=ЦЕНА ПОДНЯТА + цена упала >3%           → возврат к результату _classify (обычно ПОДНЯТЬ ЦЕНУ)
    - prev=ЦЕНА ПОДНЯТА + оборачиваемость >21д     → НОРМАЛИЗАЦИЯ
    - prev=ЦЕНА ПОДНЯТА + всё остальное            → держать ЦЕНА ПОДНЯТА
    """
    for nm_id, m in metrics.items():
        prev = history.get(nm_id)
        if not prev:
            continue
        prev_status = prev.get("status", "")
        prev_price = float(prev.get("price", 0) or 0)
        if prev_price <= 0:
            continue

        if "ПОДНЯТЬ ЦЕНУ" in prev_status:
            if m.final_price > prev_price * 1.05:
                m.status = "💰 ЦЕНА ПОДНЯТА"
                m.recommendation = (
                    f"Цена поднята с {prev_price:.0f} до {m.final_price:.0f} ₽ "
                    f"(+{(m.final_price / prev_price - 1) * 100:.0f}%). "
                    "Мониторить спрос и оборачиваемость."
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
                m.status = "💰 ЦЕНА ПОДНЯТА"
                m.recommendation = (
                    f"Цена удержана на {m.final_price:.0f} ₽. "
                    "Мониторить спрос и оборачиваемость."
                )
                m.priority = 3
