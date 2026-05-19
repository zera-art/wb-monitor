"""
Google Docs writer — рекомендации к поставке WB.
Требует google-api-python-client и прав на Docs + Drive у service account.
"""

import logging
from datetime import datetime

from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

DOC_TITLE = "Рекомендации к поставке WB"

PRIORITY_ORDER = {"🔴 СРОЧНО": 0, "🟡 ПЛАНОВАЯ": 1, "🟢 ЗАПАС": 2}


def _build_creds(credentials_path: str) -> Credentials:
    return Credentials.from_service_account_file(credentials_path, scopes=SCOPES)


def _find_or_create_doc(drive_svc, doc_title: str, existing_id: str = None) -> str:
    """Находит документ по ID или имени; если нет — создаёт новый. Возвращает doc_id."""
    if existing_id:
        try:
            drive_svc.files().get(fileId=existing_id, fields="id").execute()
            return existing_id
        except Exception:
            logger.info(f"Doc ID {existing_id} недействителен, ищем по имени...")

    # Поиск по имени
    results = drive_svc.files().list(
        q=f"name='{doc_title}' and mimeType='application/vnd.google-apps.document' and trashed=false",
        fields="files(id,name)",
        pageSize=1,
    ).execute()
    files = results.get("files", [])
    if files:
        logger.info(f"Найден документ '{doc_title}': {files[0]['id']}")
        return files[0]["id"]

    # Создать новый
    file_meta = {
        "name": doc_title,
        "mimeType": "application/vnd.google-apps.document",
    }
    doc = drive_svc.files().create(body=file_meta, fields="id").execute()
    doc_id = doc["id"]
    logger.info(f"Создан новый документ '{doc_title}': {doc_id}")
    return doc_id


def _clear_doc(docs_svc, doc_id: str):
    """Очищает всё содержимое документа."""
    doc = docs_svc.documents().get(documentId=doc_id).execute()
    content = doc.get("body", {}).get("content", [])
    if not content:
        return
    end_index = content[-1].get("endIndex", 1)
    if end_index <= 1:
        return
    docs_svc.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{
            "deleteContentRange": {
                "range": {"startIndex": 1, "endIndex": end_index - 1}
            }
        }]},
    ).execute()


def _make_requests(recommendations: list[dict]) -> list[dict]:
    """
    Строит список requests для Google Docs batchUpdate.
    Документ сначала очищается, затем заполняется текстом и форматированием.
    """
    now_msk = datetime.now().strftime("%d.%m.%Y %H:%M")

    # Группировка по кластерам
    clusters: dict[str, list[dict]] = {}
    for r in recommendations:
        cl = r.get("cluster", "Прочее")
        clusters.setdefault(cl, []).append(r)

    # Сводка
    prio_count = {"🔴": 0, "🟡": 0, "🟢": 0}
    abc_count  = {"A": 0, "B": 0}
    for r in recommendations:
        for emoji in prio_count:
            if r.get("priority", "").startswith(emoji):
                prio_count[emoji] += 1
        abc = r.get("abc", "")
        if abc in abc_count:
            abc_count[abc] += 1

    lines: list[tuple[str, str]] = []  # (text, style) где style: "title"|"h2"|"body"|"bold_body"

    lines.append(("РЕКОМЕНДАЦИИ К ПОСТАВКЕ WB\n", "title"))
    lines.append((f"Обновлено: {now_msk} МСК\n", "subtitle"))
    lines.append((f"Горизонт: 28 дней | Сезонность учтена\n\n", "subtitle"))

    lines.append((
        f"Итого SKU: {len(recommendations)} | "
        f"🔴 Срочно: {prio_count['🔴']} | "
        f"🟡 Плановая: {prio_count['🟡']} | "
        f"🟢 Запас: {prio_count['🟢']}\n",
        "bold_body"
    ))
    cluster_summary = " | ".join(f"{cl}: {len(recs)}" for cl, recs in sorted(clusters.items()))
    lines.append((f"{cluster_summary}\n", "body"))
    lines.append((
        f"Категория A: {abc_count['A']} SKU | Категория B: {abc_count['B']} SKU\n\n",
        "body"
    ))

    col_headers = (
        "Приоритет | Артикул | Баркод | Название | Категория | ABC | "
        "Остаток | Продажи/день | Потребность 28д | Рекомендовать (шт) | Дней до нуля\n"
    )

    for cluster, recs in sorted(clusters.items()):
        lines.append((f"\n📦 {cluster.upper()} — {len(recs)} SKU\n", "h2"))
        lines.append((col_headers, "bold_body"))
        for r in recs:
            row_text = (
                f"{r.get('priority',''):<12} | "
                f"{r.get('vendor_code', r.get('nm_id','')):<10} | "
                f"{r.get('barcode',''):<15} | "
                f"{r.get('name','')[:30]:<30} | "
                f"{r.get('category','')[:20]:<20} | "
                f"{r.get('abc',''):<3} | "
                f"{r.get('stock',0):<8} | "
                f"{r.get('sales_per_day',0):<13.2f} | "
                f"{r.get('needed_28d',0):<16} | "
                f"{r.get('recommended_qty',0):<19} | "
                f"{r.get('days_to_zero',0):.1f}\n"
            )
            lines.append((row_text, "body"))

    # Вставляем весь текст одним запросом, затем форматируем заголовки
    full_text = "".join(text for text, _ in lines)

    requests = []

    # Вставить текст в начало документа (index 1)
    requests.append({
        "insertText": {"location": {"index": 1}, "text": full_text}
    })

    # Форматирование заголовка (первая строка — жирный крупный шрифт)
    title_len = len(lines[0][0])
    requests.append({
        "updateTextStyle": {
            "range": {"startIndex": 1, "endIndex": 1 + title_len},
            "textStyle": {"bold": True, "fontSize": {"magnitude": 16, "unit": "PT"}},
            "fields": "bold,fontSize",
        }
    })

    # Форматирование H2 разделов (кластеры)
    cursor = 1
    for text, style in lines:
        if style == "h2":
            requests.append({
                "updateTextStyle": {
                    "range": {
                        "startIndex": cursor,
                        "endIndex": cursor + len(text),
                    },
                    "textStyle": {"bold": True, "fontSize": {"magnitude": 13, "unit": "PT"}},
                    "fields": "bold,fontSize",
                }
            })
        elif style == "bold_body":
            requests.append({
                "updateTextStyle": {
                    "range": {
                        "startIndex": cursor,
                        "endIndex": cursor + len(text),
                    },
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            })
        cursor += len(text)

    return requests


def create_or_update_supply_doc(
    recommendations: list[dict],
    credentials_path: str,
    doc_id: str = None,
) -> str:
    """
    Создаёт или обновляет документ 'Рекомендации к поставке WB' в Google Drive.
    Возвращает URL документа.
    """
    try:
        from googleapiclient.discovery import build
    except ImportError:
        logger.error("google-api-python-client не установлен. "
                     "Добавьте google-api-python-client в requirements.txt")
        return ""

    creds = _build_creds(credentials_path)
    drive_svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    docs_svc  = build("docs",  "v1", credentials=creds, cache_discovery=False)

    actual_id = _find_or_create_doc(drive_svc, DOC_TITLE, doc_id)

    # Очистить документ
    _clear_doc(docs_svc, actual_id)

    # Заполнить
    if recommendations:
        requests = _make_requests(recommendations)
        docs_svc.documents().batchUpdate(
            documentId=actual_id,
            body={"requests": requests},
        ).execute()
    else:
        docs_svc.documents().batchUpdate(
            documentId=actual_id,
            body={"requests": [{
                "insertText": {
                    "location": {"index": 1},
                    "text": "Нет рекомендаций к поставке на сегодня.\n",
                }
            }]},
        ).execute()

    doc_url = f"https://docs.google.com/document/d/{actual_id}/edit"
    logger.info(f"Документ обновлён: {doc_url}")
    return doc_url
