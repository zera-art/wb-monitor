"""
Wildberries API Client
Покрывает все 6 блоков данных: остатки, продажи, хранение, цены, реклама, воронка
"""

import time
import logging
from datetime import datetime, timedelta
from typing import Optional
import requests

logger = logging.getLogger(__name__)


class WBAPIError(Exception):
    pass


class WBClient:
    """
    Единый клиент для всех API Wildberries.
    Автоматически обрабатывает rate limits (429) и повторные попытки.
    """

    BASE_STATS    = "https://statistics-api.wildberries.ru"
    BASE_CONTENT  = "https://content-api.wildberries.ru"
    BASE_PRICES   = "https://discounts-prices-api.wildberries.ru"
    BASE_ANALYTIC = "https://seller-analytics-api.wildberries.ru"
    BASE_ADS      = "https://advert-api.wildberries.ru"

    def __init__(self, stats_key: str, content_key: str,
                 prices_key: str, analytics_key: str, ads_key: str):
        self.keys = {
            "stats":     stats_key,
            "content":   content_key,
            "prices":    prices_key,
            "analytics": analytics_key,
            "ads":       ads_key,
        }

    # ──────────────────────────────────────────────────────────
    # Внутренний HTTP-метод с retry и rate-limit handling
    # ──────────────────────────────────────────────────────────

    def _get(self, base: str, path: str, key_name: str,
             params: dict = None, retries: int = 5) -> dict | list:
        url = base + path
        headers = {"Authorization": self.keys[key_name]}
        for attempt in range(retries):
            try:
                resp = requests.get(url, headers=headers,
                                    params=params, timeout=60)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 60))
                    logger.warning(f"Rate limit hit {url}, ждём {wait}с")
                    time.sleep(wait)
                    continue
                if resp.status_code == 204:
                    return []
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.Timeout:
                logger.warning(f"Timeout {url}, попытка {attempt + 1}/{retries}")
                time.sleep(10 * (attempt + 1))
            except requests.exceptions.HTTPError as e:
                raise WBAPIError(f"HTTP {resp.status_code} при запросе {url}: {e}")
        raise WBAPIError(f"Не удалось получить ответ от {url} после {retries} попыток")

    def _post(self, base: str, path: str, key_name: str,
              payload: dict, retries: int = 5) -> dict | list:
        url = base + path
        headers = {
            "Authorization": self.keys[key_name],
            "Content-Type": "application/json",
        }
        for attempt in range(retries):
            try:
                resp = requests.post(url, headers=headers,
                                     json=payload, timeout=60)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 60))
                    logger.warning(f"Rate limit {url}, ждём {wait}с")
                    time.sleep(wait)
                    continue
                if resp.status_code == 204:
                    return []
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.Timeout:
                logger.warning(f"Timeout {url}, попытка {attempt + 1}/{retries}")
                time.sleep(10 * (attempt + 1))
        raise WBAPIError(f"POST не удался {url}")

    # ──────────────────────────────────────────────────────────
    # БЛОК 1 — Остатки
    # ──────────────────────────────────────────────────────────

    def get_stocks(self, date_from: str = "2010-01-01") -> list[dict]:
        """
        Остатки на складах WB по каждому баркоду.
        dateFrom фильтрует по lastChangeDate — нужна дата далеко в прошлом,
        чтобы получить полный срез по всем складам, а не только изменившиеся.
        """
        params = {"dateFrom": date_from}
        data = self._get(self.BASE_STATS,
                         "/api/v1/supplier/stocks", "stats", params)
        return data if isinstance(data, list) else []

    # ──────────────────────────────────────────────────────────
    # БЛОК 2 — Продажи и отчёты
    # ──────────────────────────────────────────────────────────

    def get_sales(self, date_from: str, date_to: str = None) -> list[dict]:
        """Факт продаж по каждой позиции."""
        if date_to is None:
            date_to = datetime.now().strftime("%Y-%m-%d")
        params = {"dateFrom": date_from, "dateTo": date_to, "flag": 1}
        return self._get(self.BASE_STATS,
                         "/api/v1/supplier/sales", "stats", params) or []

    def get_report_detail(self, date_from: str, date_to: str = None) -> list[dict]:
        """
        Детализированный финансовый отчёт.
        Содержит: продажи, возвраты, комиссии, логистику, хранение.
        """
        if date_to is None:
            date_to = datetime.now().strftime("%Y-%m-%d")
        params = {
            "dateFrom": date_from,
            "dateTo": date_to,
            "rrdid": 0,
            "limit": 100_000,
        }
        return self._get(self.BASE_STATS,
                         "/api/v5/supplier/reportDetailByPeriod",
                         "stats", params) or []

    def get_orders(self, date_from: str) -> list[dict]:
        """Заказы: flag=0 → записи по lastChangeDate.
        Получаем широкое окно (35д), фильтруем по полю date в analytics.
        Каждая строка = 1 заказ; isCancel=True — отменённые.
        """
        params = {"dateFrom": date_from, "flag": 0}
        return self._get(self.BASE_STATS,
                         "/api/v1/supplier/orders", "stats", params) or []

    # ──────────────────────────────────────────────────────────
    # БЛОК 3 — Платное хранение
    # ──────────────────────────────────────────────────────────

    def get_paid_storage(self, date_from: str, date_to: str = None) -> list[dict]:
        """
        Стоимость хранения по nmID.
        WB тарифицирует хранение ежедневно — нужно суммировать за неделю.
        """
        if date_to is None:
            date_to = datetime.now().strftime("%Y-%m-%dT23:59:59")
        if "T" not in date_from:
            date_from += "T00:00:00"
        params = {"dateFrom": date_from, "dateTo": date_to}
        data = self._get(self.BASE_STATS,
                         "/api/v1/paid_storage", "stats", params)
        return data if isinstance(data, list) else []

    # ──────────────────────────────────────────────────────────
    # БЛОК 4 — Цены и скидки
    # ──────────────────────────────────────────────────────────

    def get_prices(self, quantity: int = 0) -> list[dict]:
        """
        Текущие цены и скидки по всем nmID (API v2).
        Нормализует ответ к формату, совместимому с analytics.py:
        nmId, price, discount, finalPrice.
        """
        all_goods = []
        limit = 1000
        offset = 0
        while True:
            params = {"limit": limit, "offset": offset}
            data = self._get(self.BASE_PRICES,
                             "/api/v2/list/goods/filter", "prices", params)
            if isinstance(data, dict):
                goods = data.get("data", {}).get("listGoods", [])
            elif isinstance(data, list):
                goods = data
            else:
                goods = []
            if not goods:
                break
            for g in goods:
                sizes = g.get("sizes") or []
                base_price = sizes[0].get("price", 0) if sizes else 0
                final_price = sizes[0].get("discountedPrice", 0) if sizes else 0
                all_goods.append({
                    "nmId": g.get("nmID"),
                    "price": base_price,
                    "discount": g.get("discount", 0),
                    "finalPrice": final_price,
                })
            if len(goods) < limit:
                break
            offset += limit
        return all_goods

    def set_prices(self, price_list: list[dict]) -> dict:
        """
        Массовое обновление цен.
        price_list: [{"nmId": 12345, "price": 1990, "discount": 15}, ...]
        Лимит: 1000 позиций за раз.
        """
        chunks = [price_list[i:i+1000] for i in range(0, len(price_list), 1000)]
        results = []
        for chunk in chunks:
            result = self._post(self.BASE_PRICES,
                                "/public/api/v1/prices", "prices", chunk)
            results.append(result)
            time.sleep(1)
        return {"chunks_sent": len(chunks), "results": results}

    # ──────────────────────────────────────────────────────────
    # БЛОК 5 — Карточки товаров
    # ──────────────────────────────────────────────────────────

    def get_cards(self, limit: int = 100, cursor: dict = None) -> dict:
        """Карточки товаров: nmID, vendorCode, название, категория."""
        payload = {
            "settings": {
                "cursor": cursor or {"limit": limit},
                "filter": {"withPhoto": -1},
            }
        }
        return self._post(self.BASE_CONTENT,
                          "/content/v2/get/cards/list", "content", payload)

    def get_all_cards(self) -> list[dict]:
        """Получить все карточки с автопагинацией."""
        all_cards = []
        cursor = {"limit": 100}
        while True:
            data = self.get_cards(limit=100, cursor=cursor)
            cards = data.get("cards", [])
            if not cards:
                break
            all_cards.extend(cards)
            next_cursor = data.get("cursor", {})
            if next_cursor.get("total", 0) == 0:
                break
            cursor = {"limit": 100,
                      "updatedAt": next_cursor.get("updatedAt"),
                      "nmID": next_cursor.get("nmID")}
            time.sleep(0.5)
        return all_cards

    # ──────────────────────────────────────────────────────────
    # БЛОК 6 — Аналитика карточек (воронка)
    # ──────────────────────────────────────────────────────────

    def get_nm_report(self, nm_ids: list[int],
                      date_from: str, date_to: str = None,
                      aggregate_by: str = "week") -> dict:
        """
        Воронка по nmID: просмотры → корзина → заказы → выкуп.
        aggregate_by: 'day' | 'week'
        """
        if date_to is None:
            date_to = datetime.now().strftime("%Y-%m-%d")
        payload = {
            "nmIDs": nm_ids[:20],  # лимит WB — 20 nmID за запрос
            "period": {"begin": date_from, "end": date_to},
            "page": 1,
            "aggregationLevel": aggregate_by,
        }
        return self._post(self.BASE_ANALYTIC,
                          "/api/v2/nm-report/detail", "analytics", payload)

    def get_all_nm_reports(self, nm_ids: list[int],
                           date_from: str, date_to: str = None) -> list[dict]:
        """Воронка для всех nmID с разбивкой по 20 штук."""
        all_data = []
        for i in range(0, len(nm_ids), 20):
            chunk = nm_ids[i:i+20]
            try:
                data = self.get_nm_report(chunk, date_from, date_to)
                cards = data.get("data", {}).get("cards", [])
                all_data.extend(cards)
            except WBAPIError as e:
                logger.error(f"Ошибка воронки для nmID {chunk}: {e}")
            time.sleep(1)
        return all_data

    # ──────────────────────────────────────────────────────────
    # БЛОК 7 — Реклама
    # ──────────────────────────────────────────────────────────

    def get_ad_campaigns(self) -> list[dict]:
        """Список всех рекламных кампаний.
        Ответ: {"adverts": [{"type": N, "status": N, "advert_list": [{advertId, changeTime}, ...]}]}
        Возвращаем плоский список из advert_list всех групп.
        """
        data = self._get(self.BASE_ADS, "/adv/v1/promotion/count", "ads")
        if not isinstance(data, dict):
            return []
        campaigns = []
        for group in data.get("adverts", []):
            campaigns.extend(group.get("advert_list", []))
        return campaigns

    def get_ad_stats(self, nm_ids: list[int],
                     date_from: str, date_to: str = None) -> list[dict]:
        """
        Расходы по рекламным кампаниям через /adv/v1/upd.
        Сопоставление кампания→nmID: числовой префикс campName до первого '_'.
        Возвращает [{"nmId": int, "spend": float}].
        """
        if date_to is None:
            date_to = datetime.now().strftime("%Y-%m-%d")
        # upd принимает даты без времени
        date_from_d = date_from[:10]
        date_to_d = date_to[:10]

        try:
            data = self._get(self.BASE_ADS, "/adv/v1/upd", "ads",
                             {"from": date_from_d, "to": date_to_d})
        except WBAPIError as e:
            logger.error(f"Ошибка /adv/v1/upd: {e}")
            return []

        if not isinstance(data, list):
            return []

        nm_id_set = set(nm_ids)
        spend_by_nm: dict[int, float] = {}
        for item in data:
            camp_name = item.get("campName", "")
            prefix = camp_name.split("_", 1)[0]
            if prefix.isdigit():
                nm_id = int(prefix)
                if nm_id in nm_id_set:
                    spend_by_nm[nm_id] = spend_by_nm.get(nm_id, 0.0) + float(item.get("updSum", 0) or 0)

        return [{"nmId": nm_id, "spend": spend} for nm_id, spend in spend_by_nm.items()]
