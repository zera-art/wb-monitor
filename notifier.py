"""
Telegram-уведомления для критичных SKU
Опциональный модуль — включается через TELEGRAM_ENABLED=true
"""

import logging
import os
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_LAST_UPDATE_FILE = Path("last_tg_update_id.txt")


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

    def send_supply_report(self, recommendations: list[dict], doc_url: str = ""):
        """Отправляет итоговый отчёт по рекомендациям к поставке."""
        n_total  = len(recommendations)
        n_urgent = sum(1 for r in recommendations if r.get("priority", "").startswith("🔴"))
        n_plan   = sum(1 for r in recommendations if r.get("priority", "").startswith("🟡"))
        text = (
            f"✅ Отчёт обновлён\n"
            f"📦 SKU к поставке: {n_total}\n"
            f"🔴 Срочно: {n_urgent} | 🟡 Плановая: {n_plan}\n"
        )
        if doc_url:
            text += f"👉 {doc_url}"
        self.send(text)

    def send_supply_ready_monday(self, n_urgent: int, n_total: int):
        """Понедельник: напоминание о рекомендациях к поставке."""
        self.send(
            f"📦 Рекомендации к поставке готовы\n"
            f"🔴 Срочно: {n_urgent} | Всего: {n_total} SKU\n"
            f"Используйте /поставки для получения отчёта"
        )

    def poll_supply_command(self) -> bool:
        """
        Однократно проверяет Telegram на наличие команды /поставки.
        Использует файл last_tg_update_id.txt для отслеживания обработанных сообщений.
        Возвращает True если команда найдена и ещё не обработана.
        """
        last_id = 0
        if _LAST_UPDATE_FILE.exists():
            try:
                last_id = int(_LAST_UPDATE_FILE.read_text().strip())
            except (ValueError, OSError):
                last_id = 0

        try:
            resp = requests.get(
                f"{self.base_url}/getUpdates",
                params={"offset": last_id + 1, "timeout": 0, "limit": 50},
                timeout=15,
            )
            resp.raise_for_status()
            updates = resp.json().get("result", [])
        except Exception as e:
            logger.warning(f"Ошибка poll_supply_command: {e}")
            return False

        found = False
        max_id = last_id
        for upd in updates:
            upd_id = upd.get("update_id", 0)
            if upd_id > max_id:
                max_id = upd_id
            msg = upd.get("message") or upd.get("channel_post", {})
            text = msg.get("text", "").strip().lower()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if chat_id == str(self.chat_id) and "/поставки" in text:
                found = True

        if max_id > last_id:
            try:
                _LAST_UPDATE_FILE.write_text(str(max_id))
            except OSError:
                pass

        return found
