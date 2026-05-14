"""
Конфигурация системы мониторинга WB
Заполните переменные своими ключами.
"""

import os

# ──────────────────────────────────────────────────────────────
# API Ключи Wildberries
# Лучше хранить в переменных окружения или GitHub Secrets
# ──────────────────────────────────────────────────────────────

WB_KEYS = {
    # Статистика: /api/v1/supplier/stocks, /sales, /orders, /reportDetailByPeriod
    "stats_key": os.getenv("WB_KEY_STATS", "YOUR_STATS_KEY_HERE"),

    # Контент: /content/v2/get/cards/list
    "content_key": os.getenv("WB_KEY_CONTENT", "YOUR_CONTENT_KEY_HERE"),

    # Цены: /public/api/v1/info
    "prices_key": os.getenv("WB_KEY_PRICES", "YOUR_PRICES_KEY_HERE"),

    # Аналитика: /api/v2/nm-report/detail
    "analytics_key": os.getenv("WB_KEY_ANALYTICS", "YOUR_ANALYTICS_KEY_HERE"),

    # Реклама: /adv/v2/fullstats
    "ads_key": os.getenv("WB_KEY_ADS", "YOUR_ADS_KEY_HERE"),
}

# ──────────────────────────────────────────────────────────────
# Google Sheets
# ──────────────────────────────────────────────────────────────

GOOGLE_CREDENTIALS_PATH = os.getenv(
    "GOOGLE_CREDENTIALS_PATH",
    "google_credentials.json"  # путь к JSON файлу service account
)

# ID таблицы из URL: https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "YOUR_SPREADSHEET_ID_HERE")

# ──────────────────────────────────────────────────────────────
# Пороговые значения (можно перенести в лист ⚙️ НАСТРОЙКИ)
# ──────────────────────────────────────────────────────────────

THRESHOLDS = {
    "critical_turnover_days": 90,
    "slow_turnover_days": 60,
    "normal_turnover_days": 30,
    "price_raise_turnover_days": 30,
    "min_sales_for_analysis": 1,
    "clearance_ineffective_discount": 25,
    "sales_growth_threshold": 0.10,
    "storage_to_revenue_critical": 0.15,
    "ad_drr_too_high": 0.25,
    "ad_drr_good": 0.10,
}

# ──────────────────────────────────────────────────────────────
# Опции запуска
# ──────────────────────────────────────────────────────────────

# Глубина анализа продаж (дней назад)
SALES_LOOKBACK_DAYS = 28

# Глубина истории хранения
STORAGE_LOOKBACK_DAYS = 7

# Реклама — период статистики
ADS_LOOKBACK_DAYS = 7

# Включить уведомления в Telegram
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
