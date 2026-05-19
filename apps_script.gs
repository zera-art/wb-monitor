/**
 * Google Apps Script — триггер на чекбокс "Согласовано" в листе ОЧЕРЕДЬ ИЗМЕНЕНИЙ
 * Установить как триггер onEdit: Расширения → Apps Script → Триггеры → onEdit
 */

var SHEET_NAME = "📋 ОЧЕРЕДЬ ИЗМЕНЕНИЙ";
var APPROVE_COL = 9; // 1-based: колонка I ("Согласовано")

var GITHUB_OWNER = "zera-art";
var GITHUB_REPO  = "wb-monitor";
var WORKFLOW_ID  = "daily_update.yml";
var BRANCH       = "master";

/**
 * Триггер на редактирование ячейки.
 * Срабатывает когда чекбокс "Согласовано" становится TRUE.
 */
function onEdit(e) {
  var sheet = e.source.getActiveSheet();
  if (sheet.getName() !== SHEET_NAME) return;

  var range = e.range;
  // Только колонка "Согласовано" (APPROVE_COL, 1-based)
  if (range.getColumn() !== APPROVE_COL) return;
  // Только если значение стало TRUE
  if (e.value !== "TRUE") return;

  triggerGithubActions();
}

/**
 * Запускает GitHub Actions workflow через API.
 * GITHUB_TOKEN должен быть добавлен в Script Properties (не хранить в коде).
 */
function triggerGithubActions() {
  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty("GITHUB_TOKEN");

  if (!token) {
    SpreadsheetApp.getUi().alert(
      "❌ Ошибка: GITHUB_TOKEN не найден в Script Properties.\n" +
      "Перейдите: Расширения → Apps Script → Настройки проекта → Свойства скрипта"
    );
    return;
  }

  var url = "https://api.github.com/repos/" + GITHUB_OWNER + "/" + GITHUB_REPO +
            "/actions/workflows/" + WORKFLOW_ID + "/dispatches";

  var options = {
    method: "post",
    headers: {
      "Authorization": "token " + token,
      "Accept": "application/vnd.github.v3+json",
      "Content-Type": "application/json"
    },
    payload: JSON.stringify({ ref: BRANCH }),
    muteHttpExceptions: true
  };

  try {
    var response = UrlFetchApp.fetch(url, options);
    var code = response.getResponseCode();

    if (code === 204) {
      SpreadsheetApp.getUi().alert(
        "⏳ Цены отправляются на WB. Таблица обновится через 5-7 минут"
      );
    } else {
      SpreadsheetApp.getUi().alert(
        "❌ Ошибка запуска GitHub Actions. Код: " + code + "\n" + response.getContentText()
      );
    }
  } catch (err) {
    SpreadsheetApp.getUi().alert("❌ Ошибка: " + err.toString());
  }
}

/*
 * ═══════════════════════════════════════════════════════════
 * КАК УСТАНОВИТЬ
 * ═══════════════════════════════════════════════════════════
 *
 * 1. Открыть Google Sheets → Расширения → Apps Script
 *
 * 2. Вставить весь код из этого файла в редактор (заменить содержимое)
 *
 * 3. Добавить GITHUB_TOKEN в Script Properties:
 *    - В редакторе Apps Script: Настройки проекта (шестерёнка слева)
 *    - Раздел "Свойства скрипта" → Добавить свойство
 *    - Имя: GITHUB_TOKEN
 *    - Значение: ваш Personal Access Token с правом workflow
 *    - Создать токен: GitHub → Settings → Developer settings →
 *      Personal access tokens (classic) → Generate new token
 *      Права: repo, workflow
 *
 * 4. Сохранить скрипт (Ctrl+S)
 *
 * 5. Установить триггер:
 *    - Левая панель → Триггеры (значок часов)
 *    - Добавить триггер:
 *      Функция: onEdit
 *      Тип: onEdit (редактирование таблицы)
 *    - Разрешить доступ при запросе Google
 *
 * Готово! Теперь при постановке ✓ в колонку "Согласовано"
 * автоматически запустится GitHub Actions workflow.
 * ═══════════════════════════════════════════════════════════
 */
