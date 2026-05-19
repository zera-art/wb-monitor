"""
Supply Analytics — рекомендации к поставке на 28 дней по кластерам складов WB.
"""

import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ── Кластеры складов ──────────────────────────────────────────────────────────

CLUSTERS: dict[str, list[str]] = {
    "Центр": ["Коледино", "Электросталь", "Подольск"],
    "СПБ":   ["Шушары"],
    "Казань":["Казань"],
    "Юг":    ["Краснодар", "Ростов"],
    "Урал":  ["Екатеринбург"],
}

# Обратная карта: подстрока склада → кластер
_WAREHOUSE_TO_CLUSTER: dict[str, str] = {}
for _cluster, _warehouses in CLUSTERS.items():
    for _wh in _warehouses:
        _WAREHOUSE_TO_CLUSTER[_wh.lower()] = _cluster

EXCLUDED_CATEGORIES = {"Комплексные пищевые добавки"}
SUPPLY_HORIZON_DAYS = 28
MIN_SUPPLY_QTY = 5


def _warehouse_cluster(warehouse_name: str) -> str | None:
    """Определяет кластер по названию склада (частичное совпадение)."""
    wl = warehouse_name.lower()
    for key, cluster in _WAREHOUSE_TO_CLUSTER.items():
        if key in wl:
            return cluster
    return None


# ── Получение данных из WB API ────────────────────────────────────────────────

def get_sales_history_90days(wb_client) -> list[dict]:
    """
    Заказы за 90 дней по каждому nmId/складу.
    Использует GET /api/v1/supplier/orders с flag=1 (фильтр по дате создания).
    """
    date_from = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    try:
        from wb_client import WBAPIError
        raw = wb_client._get(
            wb_client.BASE_STATS,
            "/api/v1/supplier/orders", "stats",
            {"dateFrom": date_from, "flag": 1},
        )
    except Exception as e:
        logger.warning(f"Ошибка get_sales_history_90days: {e}")
        return []
    orders = raw if isinstance(raw, list) else []
    result = []
    for row in orders:
        if row.get("isCancel", False):
            continue
        result.append({
            "nmId":           row.get("nmId"),
            "warehouseName":  row.get("warehouseName", ""),
            "regionName":     row.get("regionName", ""),
            "oblastOkrugName":row.get("oblastOkrugName", ""),
            "date":           row.get("date", "")[:10],
            "quantity":       int(row.get("quantity", 1) or 1),
        })
    logger.info(f"История продаж 90д: {len(result)} заказов")
    return result


def get_stock_by_warehouse(wb_client) -> dict[int, dict[str, int]]:
    """
    Остатки по складам.
    Возвращает: {nmId → {warehouseName: quantity}}.
    """
    try:
        stocks = wb_client.get_stocks()
    except Exception as e:
        logger.warning(f"Ошибка get_stock_by_warehouse: {e}")
        return {}

    result: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in stocks:
        nm_id = row.get("nmId")
        wh    = row.get("warehouseName", "")
        qty   = int(row.get("quantity", 0) or 0)
        if nm_id and qty > 0:
            result[nm_id][wh] += qty

    return {nm_id: dict(wh_map) for nm_id, wh_map in result.items()}


def get_barcodes(wb_client) -> dict[int, str]:
    """
    Баркоды по nmId.
    Возвращает: {nmId → "баркод1,баркод2"}.
    """
    try:
        cards = wb_client.get_all_cards()
    except Exception as e:
        logger.warning(f"Ошибка get_barcodes: {e}")
        return {}

    result: dict[int, str] = {}
    for card in cards:
        nm_id = card.get("nmID") or card.get("nmId")
        if not nm_id:
            continue
        barcodes = []
        for size in card.get("sizes", []):
            for sku in size.get("skus", []):
                barcodes.append(str(sku))
        result[nm_id] = ",".join(barcodes) if barcodes else ""
    return result


# ── ABC-анализ ────────────────────────────────────────────────────────────────

def calc_abc(sales_data: list[dict],
             excluded_categories: set = None,
             category_by_nm: dict = None) -> dict[int, str]:
    """
    ABC по количеству заказов за 90 дней.
    Возвращает {nmId: "A" | "B" | "C"}.
    Категории из excluded_categories исключаются.
    """
    if excluded_categories is None:
        excluded_categories = EXCLUDED_CATEGORIES

    sales_by_nm: dict[int, int] = defaultdict(int)
    for row in sales_data:
        nm_id = row.get("nmId")
        if not nm_id:
            continue
        if category_by_nm and category_by_nm.get(nm_id, "") in excluded_categories:
            continue
        sales_by_nm[nm_id] += row.get("quantity", 1)

    if not sales_by_nm:
        return {}

    sorted_skus = sorted(sales_by_nm.items(), key=lambda x: -x[1])
    total = sum(v for _, v in sorted_skus)
    if total == 0:
        return {nm_id: "C" for nm_id, _ in sorted_skus}

    abc: dict[int, str] = {}
    cumulative = 0
    for nm_id, sales in sorted_skus:
        cumulative += sales
        pct = cumulative / total * 100
        if pct <= 80:
            abc[nm_id] = "A"
        elif pct <= 95:
            abc[nm_id] = "B"
        else:
            abc[nm_id] = "C"

    return abc


# ── Сезонность ────────────────────────────────────────────────────────────────

def apply_seasonality(wb_category: str, month: int = None) -> float:
    """
    Возвращает коэффициент для корректировки средних продаж/день.
    Значения < 1.0 снижают прогноз (низкий сезон или искажение распродажи).
    """
    if month is None:
        month = datetime.now().month

    if wb_category in ("Подушки декоративные", "Валики"):
        return 0.5 if 5 <= month <= 8 else 1.0
    else:
        if month == 5:
            return 1 / 1.5  # май: распродажа WB искажает спрос вверх
        elif 6 <= month <= 8:
            return 0.7       # лето: сниженный спрос
        else:
            return 1.0


# ── Основной расчёт ───────────────────────────────────────────────────────────

def calc_supply_recommendation(orders: list[dict],
                                wb_client=None,
                                cards: list[dict] = None) -> list[dict]:
    """
    Рассчитывает рекомендации к поставке по кластерам.
    orders — уже загруженный список заказов (orders_28d из main.py).
    Возвращает список словарей для update_supply_sheet() и supply_doc_writer.
    """
    logger.info("📦 Расчёт рекомендаций к поставке...")

    # Отменённые заказы исключаем здесь (orders_28d содержит и отменённые)
    sales_history = [o for o in orders if not o.get("isCancel", False)]
    lookback_days = len({o.get("date", "")[:10] for o in sales_history if o.get("date")}) or 28
    # Нормализуем к реальному количеству уникальных дней (не менее 28)
    lookback_days = max(lookback_days, 28)
    logger.info(f"  → {len(sales_history)} заказов, горизонт расчёта {lookback_days} дней")

    stock_by_wh     = get_stock_by_warehouse(wb_client) if wb_client else {}
    barcodes        = get_barcodes(wb_client) if wb_client else {}

    # Карточки (название, категория)
    if cards is None:
        try:
            cards = wb_client.get_all_cards()
        except Exception as e:
            logger.warning(f"Ошибка загрузки карточек для поставок: {e}")
            cards = []

    name_by_nm: dict[int, str] = {}
    category_by_nm: dict[int, str] = {}
    vendor_by_nm: dict[int, str] = {}
    for card in cards:
        nm_id = card.get("nmID") or card.get("nmId")
        if not nm_id:
            continue
        name_by_nm[nm_id]     = (card.get("title") or card.get("subjectName") or "")
        category_by_nm[nm_id] = card.get("subjectName") or card.get("objectName") or ""
        vendor_by_nm[nm_id]   = card.get("vendorCode", "")

    # ABC-анализ
    abc = calc_abc(sales_history, category_by_nm=category_by_nm)

    # Продажи/день по кластерам: {nm_id → {cluster: total_orders}}
    sales_by_cluster: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in sales_history:
        nm_id = row.get("nmId")
        wh    = row.get("warehouseName", "")
        cluster = _warehouse_cluster(wh)
        if nm_id and cluster:
            sales_by_cluster[nm_id][cluster] += row.get("quantity", 1)

    # Остаток по кластерам: {nm_id → {cluster: total_stock}}
    stock_by_cluster: dict[int, dict[str, int]] = {}
    for nm_id, wh_map in stock_by_wh.items():
        cluster_stock: dict[str, int] = defaultdict(int)
        for wh, qty in wh_map.items():
            cluster = _warehouse_cluster(wh)
            if cluster:
                cluster_stock[cluster] += qty
        stock_by_cluster[nm_id] = dict(cluster_stock)

    # Актуальный месяц для сезонности
    current_month = datetime.now().month

    recommendations: list[dict] = []

    all_nm_ids = set(sales_by_cluster.keys()) | set(stock_by_cluster.keys())

    for nm_id in all_nm_ids:
        category = category_by_nm.get(nm_id, "")

        # Исключения
        if category in EXCLUDED_CATEGORIES:
            continue

        # Только A и B
        abc_class = abc.get(nm_id, "C")
        if abc_class == "C":
            continue

        seasonality = apply_seasonality(category, current_month)
        cluster_sales = sales_by_cluster.get(nm_id, {})
        cluster_stock = stock_by_cluster.get(nm_id, {})

        for cluster in CLUSTERS:
            total_orders_28d = cluster_sales.get(cluster, 0)
            sales_per_day    = (total_orders_28d / lookback_days) * seasonality
            current_stock    = cluster_stock.get(cluster, 0)
            target_stock     = sales_per_day * SUPPLY_HORIZON_DAYS

            # Дней до нуля
            if sales_per_day > 0:
                days_to_zero = current_stock / sales_per_day
            else:
                days_to_zero = 999 if current_stock > 0 else 0

            needed_28d = max(0, int(target_stock - current_stock))

            # Фильтры
            if needed_28d < MIN_SUPPLY_QTY:
                continue

            # Если остаток соседнего склада в кластере покрывает 28 дней — пропустить
            total_cluster_stock = sum(cluster_stock.get(cl, 0) for cl in [cluster])
            if sales_per_day > 0 and total_cluster_stock / sales_per_day >= SUPPLY_HORIZON_DAYS:
                continue

            # Приоритет
            if days_to_zero < 14:
                priority = "🔴 СРОЧНО"
            elif days_to_zero <= SUPPLY_HORIZON_DAYS:
                priority = "🟡 ПЛАНОВАЯ"
            else:
                priority = "🟢 ЗАПАС"

            # Основной склад кластера
            main_wh = CLUSTERS[cluster][0]

            recommendations.append({
                "nm_id":          nm_id,
                "vendor_code":    vendor_by_nm.get(nm_id, str(nm_id)),
                "barcode":        barcodes.get(nm_id, ""),
                "name":           name_by_nm.get(nm_id, ""),
                "category":       category,
                "abc":            abc_class,
                "cluster":        cluster,
                "warehouse":      main_wh,
                "stock":          current_stock,
                "sales_per_day":  round(sales_per_day, 3),
                "needed_28d":     needed_28d,
                "recommended_qty":needed_28d,
                "days_to_zero":   round(days_to_zero, 1),
                "priority":       priority,
            })

    # Сортировка: 🔴 → 🟡 → 🟢, внутри по убыванию потребности
    order = {"🔴 СРОЧНО": 0, "🟡 ПЛАНОВАЯ": 1, "🟢 ЗАПАС": 2}
    recommendations.sort(key=lambda r: (
        order.get(r["priority"], 9),
        -r["needed_28d"],
    ))

    logger.info(
        f"Рекомендации к поставке: {len(recommendations)} позиций "
        f"({sum(1 for r in recommendations if r['priority']=='🔴 СРОЧНО')} срочных)"
    )
    return recommendations
