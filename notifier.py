"""
Telegram-уведомления для критичных SKU
Опциональный модуль — включается через TELEGRAM_ENABLED=true
"""

import logging
import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def send(self, text: str):
        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": self.chat_id, "text": text,
                      "parse_mode": "HTML"},
                timeout=10,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Ошибка Telegram: {e}")

    def send_daily_report(self, summary: dict, urgent_skus: list):
        """Отправить ежедневный дайджест."""
        text = (
            f"<b>📊 WB Мониторинг — {summary.get('updated_at', '')}</b>\n\n"
            f"💰 Выручка 7д: <b>{summary.get('total_revenue_7d', 0):,} ₽</b>\n"
            f"📦 Стоимость остатков: <b>{summary.get('total_stock_value', 0):,} ₽</b>\n"
            f"🏭 Хранение 7д: <b>{summary.get('total_storage_7d', 0):,} ₽</b> "
            f"({summary.get('storage_to_revenue', 0)}% выручки)\n"
            f"📢 Реклама 7д: <b>{summary.get('total_ad_spend_7d', 0):,} ₽</b> "
            f"(ДРР {summary.get('overall_drr', 0)}%)\n\n"
        )

        if urgent_skus:
            text += f"<b>🚨 СРОЧНО ({len(urgent_skus)} SKU):</b>\n"
            for m in urgent_skus[:10]:  # топ-10 срочных
                text += (
                    f"• <code>{m.nm_id}</code> {m.name[:25]} — "
                    f"{m.status} "
                    f"(остаток: {m.stock} шт, оборачив: {m.turnover_days:.0f}д)\n"
                )
            if len(urgent_skus) > 10:
                text += f"... и ещё {len(urgent_skus) - 10} SKU\n"

        text += "\n🔗 Открыть дашборд в Google Sheets"
        self.send(text)

    def send_price_queue_ready(self, total: int, n_up: int, n_down: int):
        """Понедельник: новый список изменений цен сформирован."""
        text = (
            f"📋 Список изменений цен готов: {total} SKU\n"
            f"⬆️ Повышение: {n_up} | ⬇️ Снижение: {n_down}\n"
            f"Согласуйте → лист ОЧЕРЕДЬ ИЗМЕНЕНИЙ"
        )
        self.send(text)

    def send_price_queue_reminder(self, n_pending: int):
        """Вторник/среда: напоминание о несогласованных позициях."""
        self.send(f"⏰ Ожидает согласования: {n_pending} SKU")

    def send_prices_sent(self, n_up: int, n_down: int, total: int):
        """После успешной отправки цен на WB."""
        text = (
            f"✅ Цены обновлены: {total} SKU\n"
            f"⬆️ Повышено: {n_up} | ⬇️ Снижено: {n_down}"
        )
        self.send(text)
