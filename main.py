"""
Главный оркестратор системы мониторинга WB
Запускается ежедневно через GitHub Actions или вручную.

Использование:
    python main.py              # полный запуск
    python main.py --dry-run    # без записи в Sheets (проверка API)
    python main.py --sheets-only # только обновить Sheets из кэша
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from config import (
    WB_KEYS, GOOGLE_CREDENTIALS_PATH, SPREADSHEET_ID,
    THRESHOLDS, SALES_LOOKBACK_DAYS, STORAGE_LOOKBACK_DAYS,
    ADS_LOOKBACK_DAYS, TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    GOOGLE_DOC_ID,
)
from wb_client import WBClient, WBAPIError
from analytics import build_sku_metrics, build_summary, THRESHOLDS as ANALYTICS_THRESHOLDS
from sheets_writer import SheetsWriter
from notifier import TelegramNotifier
from wb_price_sender import get_approved_changes, send_prices_to_wb
from supply_analytics import calc_supply_recommendation
from supply_doc_writer import create_or_update_supply_doc

# ──────────────────────────────────────────────────────────────
# Настройка логирования
# ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(open(sys.stdout.fileno(), mode="w",
                                   encoding="utf-8", closefd=False)),
        logging.FileHandler("wb_monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def date_str(days_ago: int = 0) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def run(dry_run: bool = False):
    logger.info("=" * 60)
    logger.info(f"🚀 Запуск WB Monitor — {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    logger.info("=" * 60)

    # Обновить пороги из config
    ANALYTICS_THRESHOLDS.update(THRESHOLDS)

    # ── Инициализация клиентов ──────────────────────────────
    wb = WBClient(**WB_KEYS)

    if not dry_run:
        sheets = SheetsWriter(GOOGLE_CREDENTIALS_PATH, SPREADSHEET_ID)
        sheets.setup_settings_sheet(THRESHOLDS)

    # ── Блок 1: Карточки товаров ───────────────────────────
    logger.info("📋 Загружаем карточки товаров...")
    try:
        cards = wb.get_all_cards()
        logger.info(f"  → Получено {len(cards)} карточек")
    except WBAPIError as e:
        logger.error(f"  ✗ Ошибка карточек: {e}")
        cards = []

    # ── Блок 2: Остатки ────────────────────────────────────
    logger.info("📦 Загружаем остатки...")
    try:
        stocks = wb.get_stocks()  # dateFrom=2010-01-01 — полный срез всех складов
        logger.info(f"  → Получено {len(stocks)} строк остатков")
    except WBAPIError as e:
        logger.error(f"  ✗ Ошибка остатков: {e}")
        stocks = []

    # ── Блок 3: Заказы 35д (qty-метрики, фильтр по date в analytics) ──
    logger.info("📋 Загружаем заказы за 35д (qty по дате размещения)...")
    try:
        orders_raw = wb.get_orders(date_from=date_str(35))
        # Оставляем только заказы, РАЗМЕЩЁННЫЕ в последние 28д (поле date)
        cutoff_28d = date_str(28)
        orders_28d = [o for o in orders_raw if o.get("date", "")[:10] >= cutoff_28d]
        active = sum(1 for o in orders_28d if not o.get("isCancel", False))
        cancelled = len(orders_28d) - active
        logger.info(f"  → Размещено за 28д: {len(orders_28d)} | активных: {active} | отменённых: {cancelled}")
    except WBAPIError as e:
        logger.error(f"  ✗ Ошибка заказов: {e}")
        orders_28d = []

    # ── Блок 3б: Выкупы 7д + прошлая 7д (revenue) ────────
    logger.info("💸 Загружаем выкупы (revenue, flag=1)...")
    try:
        sales_current = wb.get_sales(
            date_from=date_str(7), date_to=date_str(0)
        )
        sales_prev = wb.get_sales(
            date_from=date_str(14), date_to=date_str(7)
        )
        rev_cur  = sum(float(s.get("finishedPrice", 0) or 0) for s in sales_current if s.get("saleID","").startswith("S"))
        rev_prev = sum(float(s.get("finishedPrice", 0) or 0) for s in sales_prev    if s.get("saleID","").startswith("S"))
        logger.info(f"  → Выкупы тек. 7д: {sum(1 for s in sales_current if s.get('saleID','').startswith('S'))} шт / {rev_cur:,.0f} ₽")
        logger.info(f"  → Выкупы пред. 7д: {sum(1 for s in sales_prev if s.get('saleID','').startswith('S'))} шт / {rev_prev:,.0f} ₽")
    except WBAPIError as e:
        logger.error(f"  ✗ Ошибка выкупов: {e}")
        sales_current, sales_prev = [], []

    # ── Блок 4: Детальный отчёт 28д (для avgWeeklySales) ──
    logger.info(f"📊 Загружаем детальный отчёт ({SALES_LOOKBACK_DAYS}д)...")
    try:
        report_detail = wb.get_report_detail(
            date_from=date_str(SALES_LOOKBACK_DAYS),
            date_to=date_str(0),
        )
        logger.info(f"  → Получено {len(report_detail)} строк отчёта")
    except WBAPIError as e:
        logger.error(f"  ✗ Ошибка отчёта: {e}")
        report_detail = []

    # ── Блок 5: Хранение ───────────────────────────────────
    logger.info(f"🏭 Загружаем платное хранение ({STORAGE_LOOKBACK_DAYS}д)...")
    try:
        storage = wb.get_paid_storage(
            date_from=date_str(STORAGE_LOOKBACK_DAYS),
            date_to=date_str(0),
        )
        logger.info(f"  → Получено {len(storage)} строк хранения")
    except WBAPIError as e:
        logger.warning(f"  ✗ Ошибка хранения (не критично): {e}")
        storage = []

    # ── Блок 6: Цены ───────────────────────────────────────
    logger.info("💰 Загружаем цены и скидки...")
    try:
        prices = wb.get_prices(quantity=0)
        logger.info(f"  → Получено {len(prices)} позиций с ценами")
    except WBAPIError as e:
        logger.error(f"  ✗ Ошибка цен: {e}")
        prices = []

    # ── Блок 7: Реклама ────────────────────────────────────
    logger.info("📢 Загружаем данные по рекламе...")
    ad_stats = []
    try:
        nm_ids_for_ads = [int(c.get("nmID", 0)) for c in cards if c.get("nmID")]
        ad_stats = wb.get_ad_stats(
            nm_ids=nm_ids_for_ads,
            date_from=date_str(ADS_LOOKBACK_DAYS),
            date_to=date_str(0),
        )
        total_ad_spend = sum(item.get("spend", 0) for item in ad_stats)
        logger.info(f"  → Рекламные расходы: {len(ad_stats)} SKU | "
                    f"Итого {total_ad_spend:,.0f} ₽")
    except WBAPIError as e:
        logger.warning(f"  ✗ Ошибка рекламы (не критично): {e}")

    # ── Блок 8: СПП ────────────────────────────────────────
    logger.info("🏷️  Загружаем данные СПП (скидка постоянного покупателя)...")
    spp_data = {}
    try:
        spp_data = wb.get_spp_data()
        logger.info(f"  → СПП получено для {len(spp_data)} nmID")
    except Exception as e:
        logger.warning(f"  ✗ Ошибка СПП (не критично): {e}")

    # ── Блок 9: Средняя цена покупки ───────────────────────
    logger.info("💳 Загружаем среднюю цену покупки (buyerPrice)...")
    buyer_price_data: dict = {}
    try:
        buyer_price_data = wb.get_buyer_price_data()
        logger.info(f"  → Ср. цена покупки: {len(buyer_price_data)} nmID")
    except Exception as e:
        logger.warning(f"  ✗ Ошибка ср. цены покупки (не критично): {e}")

    # ── Аналитика ─────────────────────────────────────────
    logger.info("🧮 Считаем метрики и статусы...")
    metrics = build_sku_metrics(
        stocks=stocks,
        orders=orders_28d,
        sales=sales_current,
        prev_sales=sales_prev,
        report_detail=report_detail,
        prices=prices,
        storage=storage,
        ad_stats=ad_stats,
        cards=cards,
    )

    # Применяем СПП к метрикам
    for nm_id, spp in spp_data.items():
        if nm_id in metrics:
            metrics[nm_id].spp_pct   = spp["spp_pct"]
            metrics[nm_id].spp_price = spp["spp_price"]

    # Применяем среднюю цену покупки к метрикам
    for nm_id, bp in buyer_price_data.items():
        if nm_id in metrics:
            metrics[nm_id].buyer_price     = bp["buyer_price"]
            metrics[nm_id].wb_discount_rub = bp["wb_discount_rub"]

    summary = build_summary(metrics)
    logger.info(
        f"  → Обработано {summary.get('total_skus', 0)} SKU | "
        f"Срочных: {summary.get('urgent_count', 0)} | "
        f"Выручка 7д: {summary.get('total_revenue_7d', 0):,} ₽"
    )

    # ── Печать топ-проблемных в консоль ───────────────────
    urgent = [m for m in metrics.values() if m.priority == 1]
    if urgent:
        logger.info("🚨 СРОЧНЫЕ SKU:")
        for m in sorted(urgent, key=lambda x: -x.storage_cost_7d)[:10]:
            logger.info(
                f"  [{m.nm_id}] {m.name[:30]} | "
                f"Остаток: {m.stock} | "
                f"Оборачив: {m.turnover_days:.0f}д | "
                f"{m.status}"
            )

    # ── Кэш данных (для отладки) ──────────────────────────
    cache_path = Path("last_run_cache.json")
    try:
        cache_path.write_text(
            json.dumps({
                "updated_at": datetime.now().isoformat(),
                "summary": summary,
                "urgent_count": len(urgent),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception:
        pass

    today = datetime.now()
    is_monday   = today.weekday() == 0
    is_reminder = today.weekday() in (1, 2)   # вторник или среда

    # ── Запись в Google Sheets ────────────────────────────
    tg: TelegramNotifier | None = None
    if TELEGRAM_ENABLED and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        tg = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

    if not dry_run:
        logger.info("📝 Обновляем Google Sheets...")
        sorted_display: list = []
        try:
            sorted_display = sheets.update_all(metrics)
            logger.info("  ✅ Google Sheets обновлён")
        except Exception as e:
            logger.error(f"  ✗ Ошибка Sheets: {e}")

        # ── Telegram ежедневный дайджест ─────────────────
        if tg:
            try:
                tg.send_daily_report(summary, urgent)
            except Exception as e:
                logger.warning(f"  ✗ Telegram дайджест: {e}")

        # ── Понедельник или очередь пуста → обновить очередь изменений цен ──
        if sorted_display and (is_monday or not sheets.queue_has_data()):
            logger.info("📋 Понедельник — обновляем очередь изменений цен...")
            try:
                q = sheets.update_price_queue(sorted_display)
                if q.get("skipped"):
                    pending = sheets.queue_pending_count()
                    logger.info(f"  Очередь не перезаписана: {pending} позиций не отправлено")
                    if tg:
                        tg.send_price_queue_reminder(pending)
                else:
                    logger.info(f"  Очередь: {q['total']} SKU "
                                f"(↑{q['n_up']} повышений, ↓{q['n_down']} снижений)")
                    if tg:
                        tg.send_price_queue_ready(q["total"], q["n_up"], q["n_down"])
            except Exception as e:
                logger.error(f"  ✗ Ошибка очереди: {e}")

        # ── Каждый день: отправить одобренные позиции ────
        logger.info("💸 Проверяем одобренные изменения цен...")
        try:
            approved = get_approved_changes(sheets)
            if approved:
                logger.info(f"  Найдено {len(approved)} одобренных позиций — отправляем на WB")
                result = send_prices_to_wb(approved, sheets, WB_KEYS["prices_key"])
                # Записать базу в ЭФФЕКТИВНОСТЬ для каждой отправленной позиции
                for item in approved:
                    nm_id = item["nm_id"]
                    m = metrics.get(nm_id)
                    if m and item["current_price"] > 0:
                        action = ("Повышение" if item["new_price"] > item["current_price"]
                                  else "Снижение")
                        sheets.record_price_change(
                            nm_id=nm_id,
                            name=item["name"],
                            category=m.category,
                            action=action,
                            price_before=item["current_price"],
                            price_after=item["new_price"],
                            orders_week=m.avg_weekly_sales,
                            turnover_days=m.turnover_days,
                        )
                if tg and result["total"] > 0:
                    tg.send_prices_sent(result["n_up"], result["n_down"], result["total"])
            else:
                logger.info("  Нет позиций для отправки")
                # Вторник/среда: напоминание о несогласованных позициях
                if is_reminder:
                    unapproved = sheets.queue_unapproved_count()
                    if unapproved > 0:
                        logger.info(f"  Напоминание: {unapproved} позиций ожидают согласования")
                        if tg:
                            tg.send_price_queue_reminder(unapproved)
        except Exception as e:
            logger.error(f"  ✗ Ошибка отправки цен: {e}")

        # ── Ежедневно: проверить контрольные точки эффективности ──
        logger.info("📈 Проверяем контрольные точки эффективности...")
        try:
            sheets.update_effectiveness_checkpoints(metrics)
        except Exception as e:
            logger.warning(f"  ✗ Ошибка эффективности (не критично): {e}")

        # ── Ежедневно: рекомендации к поставке ───────────────────
        logger.info("🚚 Рассчитываем рекомендации к поставке...")
        supply_recs = []
        try:
            supply_recs = calc_supply_recommendation(wb, cards=cards)
            sheets.update_supply_sheet(supply_recs)
            logger.info(f"  → Лист ПОСТАВКИ обновлён: {len(supply_recs)} SKU")
        except Exception as e:
            logger.warning(f"  ✗ Ошибка поставок (не критично): {e}")

        # ── Telegram: /поставки по команде или понедельник ────────
        if tg:
            supply_cmd = False
            try:
                supply_cmd = tg.poll_supply_command()
            except Exception:
                pass

            if supply_cmd or is_monday:
                logger.info("📦 Обновляем Google Doc рекомендаций к поставке...")
                try:
                    doc_url = create_or_update_supply_doc(
                        supply_recs,
                        credentials_path=GOOGLE_CREDENTIALS_PATH,
                        doc_id=GOOGLE_DOC_ID or None,
                    )
                    if supply_cmd:
                        tg.send_supply_report(supply_recs, doc_url)
                    elif is_monday:
                        n_urgent = sum(1 for r in supply_recs
                                       if r.get("priority", "").startswith("🔴"))
                        tg.send_supply_ready_monday(n_urgent, len(supply_recs))
                except Exception as e:
                    logger.warning(f"  ✗ Ошибка Google Doc поставок (не критично): {e}")
    else:
        logger.info("⚠️  DRY RUN — запись в Sheets пропущена")

    logger.info("=" * 60)
    logger.info(f"✅ Готово за {(datetime.now()).strftime('%H:%M:%S')}")
    logger.info("=" * 60)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WB Monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="Только проверить API, не записывать в Sheets")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
