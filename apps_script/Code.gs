/**
 * PGR — автоматичні рілси.
 * Вставляється в Apps Script таблиці «контент-план» (Розширення → Apps Script).
 *
 * Властивості скрипта (Налаштування проєкту → Властивості скрипта):
 *   GITHUB_TOKEN     — fine-grained токен GitHub з доступом Actions: Read and write до репозиторію reel_inst
 *   DRIVE_FOLDER_ID  — ID папки на Google Drive, куди класти готові рілси
 */

const CFG = {
  OWNER: 'Solomiiachumak',
  REPO: 'reel_inst',
  WORKFLOW: 'render.yml',
  SHEET: 'Рілси авто',
  TIMEOUT_MIN: 30, // якщо за цей час рілса немає — статус «помилка»
};

// Колонки вкладки (1 = A)
const COL = { DATE: 1, TEXT: 2, KEYWORDS: 3, STATUS: 4, LINK: 5, PEXELS_ID: 6, AUTHOR: 7, STARTED: 8, NOTE: 9 };
const HEADERS = ['дата публікації', 'текст на екрані', 'ключові слова для відео (англ.)', 'статус',
  'готовий рілс', 'Pexels ID', 'автор відео', 'запущено', 'коментар'];

const STATUS = {
  QUEUE: 'в чергу',
  OTHER: 'інше відео',
  RENDERING: 'рендериться',
  DONE: 'готово',
  ERROR: 'помилка',
};

function onOpen() {
  SpreadsheetApp.getUi().createMenu('Рілси')
    .addItem('Згенерувати («в чергу» та «інше відео»)', 'generateReels')
    .addItem('Забрати готові рілси', 'collectReels')
    .addSeparator()
    .addItem('Створити вкладку «Рілси авто»', 'setupSheet')
    .addItem('Увімкнути автозбір кожні 5 хв', 'installTrigger')
    .addToUi();
}

function setupSheet() {
  const ss = SpreadsheetApp.getActive();
  let sh = ss.getSheetByName(CFG.SHEET);
  if (!sh) sh = ss.insertSheet(CFG.SHEET);
  sh.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]).setFontWeight('bold');
  sh.setFrozenRows(1);
  sh.setColumnWidth(COL.TEXT, 320);
  sh.setColumnWidth(COL.KEYWORDS, 220);
  sh.getRange(2, COL.TEXT, 500, 1).setWrap(true);
  const rule = SpreadsheetApp.newDataValidation()
    .requireValueInList(Object.values(STATUS), true).build();
  sh.getRange(2, COL.STATUS, 500, 1).setDataValidation(rule);
}

function installTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'collectReels')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('collectReels').timeBased().everyMinutes(5).create();
  SpreadsheetApp.getActive().toast('Автозбір готових рілсів увімкнено');
}

// ---------- Запуск рендеру ----------
function generateReels() {
  const sh = sheet_();
  const data = sh.getDataRange().getValues();
  const used = usedIds_(data);
  let started = 0;

  for (let i = 1; i < data.length; i++) {
    const row = i + 1;
    const status = String(data[i][COL.STATUS - 1]).trim();
    if (status !== STATUS.QUEUE && status !== STATUS.OTHER) continue;

    const text = String(data[i][COL.TEXT - 1]).trim();
    const keywords = String(data[i][COL.KEYWORDS - 1]).trim();
    if (!text) { setNote_(sh, row, STATUS.ERROR, 'порожній текст'); continue; }

    const exclude = used.slice();
    const own = String(data[i][COL.PEXELS_ID - 1]).trim();
    if (own) exclude.push(own);

    const res = github_('post', `/actions/workflows/${CFG.WORKFLOW}/dispatches`, {
      ref: 'main',
      inputs: {
        row: String(row),
        text: text,
        keywords: keywords || 'nature film',
        exclude: exclude.join(','),
      },
    });

    if (res.getResponseCode() === 204) {
      sh.getRange(row, COL.STATUS).setValue(STATUS.RENDERING);
      sh.getRange(row, COL.STARTED).setValue(new Date());
      sh.getRange(row, COL.NOTE).setValue('');
      started++;
    } else {
      setNote_(sh, row, STATUS.ERROR, 'GitHub: ' + res.getResponseCode() + ' ' + res.getContentText().slice(0, 200));
    }
  }
  SpreadsheetApp.getActive().toast(`Запущено рендер: ${started}. Готові рілси з'являться за ~3–5 хв.`);
}

// ---------- Збір готових ----------
function collectReels() {
  const sh = sheet_();
  const data = sh.getDataRange().getValues();
  const pending = [];
  for (let i = 1; i < data.length; i++) {
    if (String(data[i][COL.STATUS - 1]).trim() === STATUS.RENDERING) {
      pending.push({ row: i + 1, started: data[i][COL.STARTED - 1] });
    }
  }
  if (!pending.length) return;

  const arts = JSON.parse(github_('get', '/actions/artifacts?per_page=100').getContentText()).artifacts || [];
  const folder = DriveApp.getFolderById(prop_('DRIVE_FOLDER_ID'));

  pending.forEach(p => {
    const startedAt = p.started instanceof Date ? p.started.getTime() : 0;
    const art = arts
      .filter(a => !a.expired && a.name.indexOf(`reel-row-${p.row}-`) === 0
        && new Date(a.created_at).getTime() >= startedAt - 60 * 1000)
      .sort((a, b) => new Date(b.created_at) - new Date(a.created_at))[0];

    if (!art) {
      if (startedAt && Date.now() - startedAt > CFG.TIMEOUT_MIN * 60 * 1000) {
        setNote_(sh, p.row, STATUS.ERROR, 'рендер не завершився — перевірте GitHub → Actions');
      }
      return;
    }

    const files = Utilities.unzip(downloadArtifact_(art.archive_download_url));
    const mp4 = files.find(f => f.getName().endsWith('.mp4'));
    const metaBlob = files.find(f => f.getName() === 'meta.json');
    const meta = metaBlob ? JSON.parse(metaBlob.getDataAsString('UTF-8')) : {};
    if (!mp4) { setNote_(sh, p.row, STATUS.ERROR, 'в архіві немає MP4'); return; }

    const stamp = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd_HHmm');
    const file = folder.createFile(mp4.setName(`reel_row${p.row}_${stamp}.mp4`));

    sh.getRange(p.row, COL.LINK).setValue(file.getUrl());
    sh.getRange(p.row, COL.PEXELS_ID).setValue(meta.pexels_id || '');
    sh.getRange(p.row, COL.AUTHOR).setValue(meta.author ? `${meta.author} ${meta.author_url || ''}`.trim() : '');
    sh.getRange(p.row, COL.STATUS).setValue(STATUS.DONE);
  });
}

// ---------- Допоміжне ----------
function sheet_() {
  const sh = SpreadsheetApp.getActive().getSheetByName(CFG.SHEET);
  if (!sh) throw new Error('Немає вкладки «' + CFG.SHEET + '». Меню Рілси → Створити вкладку.');
  return sh;
}

function usedIds_(data) {
  const ids = [];
  for (let i = 1; i < data.length; i++) {
    const id = String(data[i][COL.PEXELS_ID - 1]).trim();
    if (id) ids.push(id);
  }
  return ids;
}

function setNote_(sh, row, status, note) {
  sh.getRange(row, COL.STATUS).setValue(status);
  sh.getRange(row, COL.NOTE).setValue(note);
}

function prop_(name) {
  const v = PropertiesService.getScriptProperties().getProperty(name);
  if (!v) throw new Error('Не задано властивість скрипта ' + name);
  return v;
}

function github_(method, path, body) {
  const opts = {
    method: method,
    muteHttpExceptions: true,
    headers: {
      Authorization: 'Bearer ' + prop_('GITHUB_TOKEN'),
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
  };
  if (body) { opts.contentType = 'application/json'; opts.payload = JSON.stringify(body); }
  return UrlFetchApp.fetch(`https://api.github.com/repos/${CFG.OWNER}/${CFG.REPO}${path}`, opts);
}

// GitHub віддає архів через переадресацію; другий запит — без токена
function downloadArtifact_(url) {
  const first = UrlFetchApp.fetch(url, {
    followRedirects: false,
    muteHttpExceptions: true,
    headers: { Authorization: 'Bearer ' + prop_('GITHUB_TOKEN'), Accept: 'application/vnd.github+json' },
  });
  const headers = first.getAllHeaders();
  const location = headers.Location || headers.location;
  const resp = location ? UrlFetchApp.fetch(location) : first;
  return resp.getBlob().setContentType('application/zip');
}
