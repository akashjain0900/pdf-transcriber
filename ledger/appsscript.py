"""
Ledger — the Apps Script backend, as source to paste into Google Sheets.

Kept here as a Python string so the setup instructions and the code the user
pastes can never drift apart: the web UI serves this from the running app.

This is a rewrite of the original per-page version. The change that matters:
it writes a RANGE at a time instead of one row per request.

The old version called `appendRow` once per page. Across a corpus of this size
that is hundreds of thousands of HTTP round-trips to script.google.com, it
burns Apps Script's own daily execution quota, and — worse — `appendRow` is not
idempotent, so any retry silently duplicated a row. Here each chunk is written
with `setValues` at a range addressed by page number, which means writing the
same chunk twice produces exactly the same sheet.

BUMP SCRIPT_VERSION whenever you edit this. The UI shows the deployed version
next to the expected one, so a stale deployment is visible rather than being
diagnosed later from odd sheet contents.
"""

SCRIPT_VERSION = "4"

APPS_SCRIPT_CODE = r"""/**
 * Ledger — Old Book Transcriber, Apps Script backend (v4).
 *
 * SETUP
 * 1. Create (or open) the Google Sheet you want transcriptions written into.
 * 2. Extensions -> Apps Script. Delete any placeholder code and paste this in.
 * 3. Optional but recommended: set SHARED_SECRET below to any string, and put
 *    the same string in Ledger's Sheets settings. Without it, anyone who
 *    learns your Web App URL can write to your sheet.
 * 4. Deploy -> New deployment -> type "Web app".
 *      Execute as:      Me
 *      Who has access:  Anyone
 *    Deploy, authorise it, and copy the Web App URL.
 * 5. Paste that URL into Ledger -> Sheets & Settings, and click Test.
 *
 * If you edit this script later, use Deploy -> Manage deployments -> edit
 * (pencil) -> New version. Otherwise the live URL keeps running the old code.
 */

var SCRIPT_VERSION = "4";
var SHARED_SECRET = "";   // e.g. "vk5-ledger-2026" — leave "" to skip the check.
var INDEX_TAB = "Index";

/**
 * Columns written for every page. Order must match Ledger's publisher.
 *
 * A Google Sheets cell holds at most 50,000 characters, and some pages exceed
 * that. Such a page is spread across the four Transcription columns, split at
 * line boundaries, so rejoining it is just =C2&D2&E2&F2. Almost every page uses
 * only the first, leaving the rest blank.
 *
 * The overflow goes sideways rather than onto extra rows on purpose: each page
 * is written to the row matching its page number, which is what makes
 * republishing overwrite rather than duplicate.
 */
var HEADERS = [
  "PDF Page",
  "Type",
  "Transcription",
  "Transcription 2",
  "Transcription 3",
  "Transcription 4",
  "Footnotes",
  "Note",
  "Status"
];

function doPost(e) {
  var lock = LockService.getScriptLock();
  var haveLock = lock.tryLock(30000);
  try {
    var data = JSON.parse(e.postData.contents);

    if (SHARED_SECRET && data.secret !== SHARED_SECRET) {
      return jsonOut({ ok: false, error: "Invalid secret." });
    }

    switch (data.action) {
      case "ping":
        return jsonOut({ ok: true, version: SCRIPT_VERSION });
      case "syncChunk":
        return jsonOut(syncChunk(data));
      case "getBookStatus":
        return jsonOut(getBookStatus(data));
      default:
        return jsonOut({ ok: false, error: "Unknown action: " + data.action });
    }
  } catch (err) {
    return jsonOut({
      ok: false,
      error: String(err && err.message ? err.message : err)
    });
  } finally {
    if (haveLock) lock.releaseLock();
  }
}

function jsonOut(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

/** Sheet tab titles cannot contain : \ / ? * [ ] and must be <= 100 chars. */
function sanitizeTitle(name) {
  var clean = String(name || "Book").replace(/[:\\\/\?\*\[\]]/g, "").trim();
  if (!clean) clean = "Book";
  if (clean.length > 95) clean = clean.substring(0, 95).trim();
  return clean;
}

function getOrCreateIndexSheet(ss) {
  var sheet = ss.getSheetByName(INDEX_TAB);
  if (!sheet) {
    sheet = ss.insertSheet(INDEX_TAB, 0);
    sheet.appendRow(["Book", "Pages written", "Total pages", "Machine", "Updated", "Tab"]);
    sheet.setFrozenRows(1);
  }
  return sheet;
}

/**
 * Finds this book's tab, creating it with a header row if needed.
 * Ledger sends a stable `tab` name so the same book always lands in the same
 * tab even if two book titles sanitise to the same string.
 */
function getOrCreateBookSheet(ss, tabName) {
  var title = sanitizeTitle(tabName);
  var sheet = ss.getSheetByName(title);
  if (sheet) return { sheet: sheet, title: title };

  sheet = ss.insertSheet(title);
  sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]);
  sheet.setFrozenRows(1);
  sheet.setColumnWidth(3, 520);   // Transcription
  // The continuation columns are narrow: they are nearly always empty, and
  // wide empty columns just push the useful ones off screen.
  sheet.setColumnWidth(4, 90);    // Transcription 2
  sheet.setColumnWidth(5, 90);    // Transcription 3
  sheet.setColumnWidth(6, 90);    // Transcription 4
  sheet.setColumnWidth(7, 260);   // Footnotes
  return { sheet: sheet, title: title };
}

/**
 * Writes one chunk of pages.
 *
 * Rows are addressed by page number (row = pageNumber + 1, allowing for the
 * header), so re-sending a chunk overwrites the same cells rather than
 * appending duplicates. That makes every publish safely repeatable.
 */
function syncChunk(data) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var rows = data.rows || [];

  if (!rows.length) return { ok: true, written: 0 };

  var info = getOrCreateBookSheet(ss, data.tab || data.book);
  var sheet = info.sheet;

  // Rows arrive sorted by page number; the chunk is contiguous.
  var firstPage = Number(rows[0][0]);
  var targetRow = firstPage + 1;

  // Grow the sheet if this chunk lands past the current last row.
  var neededRows = targetRow + rows.length - 1;
  if (sheet.getMaxRows() < neededRows) {
    sheet.insertRowsAfter(sheet.getMaxRows(), neededRows - sheet.getMaxRows());
  }

  sheet
    .getRange(targetRow, 1, rows.length, HEADERS.length)
    .setValues(rows);

  // Only the final chunk updates the index, so a multi-chunk publish does not
  // rewrite the same index row over and over.
  if (data.isFinalChunk) {
    updateIndex(ss, {
      book: data.book,
      tab: info.title,
      sheetId: sheet.getSheetId(),
      written: Number(data.totalWritten || 0),
      totalPages: Number(data.totalPages || 0),
      machine: String(data.machine || "")
    });
  }

  return { ok: true, written: rows.length, tab: info.title };
}

function findIndexRow(indexSheet, bookName) {
  var last = indexSheet.getLastRow();
  if (last < 2) return -1;
  var values = indexSheet.getRange(2, 1, last - 1, 1).getValues();
  for (var i = 0; i < values.length; i++) {
    if (values[i][0] === bookName) return i + 2;
  }
  return -1;
}

function updateIndex(ss, info) {
  var indexSheet = getOrCreateIndexSheet(ss);
  var link = '=HYPERLINK("#gid=' + info.sheetId + '","Open")';
  var values = [
    info.book,
    info.written,
    info.totalPages,
    info.machine,
    new Date(),
    link
  ];

  var row = findIndexRow(indexSheet, info.book);
  if (row === -1) {
    indexSheet.appendRow(values);
  } else {
    indexSheet.getRange(row, 1, 1, values.length).setValues([values]);
  }
}

/**
 * Read-only: reports which pages of a book already have content in the sheet.
 * Ledger uses this to verify a publish landed, without writing anything.
 */
function getBookStatus(data) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var title = sanitizeTitle(data.tab || data.book);
  var sheet = ss.getSheetByName(title);

  if (!sheet) return { ok: true, exists: false, pages: [] };

  var last = sheet.getLastRow();
  if (last < 2) return { ok: true, exists: true, pages: [] };

  var values = sheet.getRange(2, 1, last - 1, HEADERS.length).getValues();
  var pages = [];
  for (var i = 0; i < values.length; i++) {
    var pageNumber = values[i][0];
    if (pageNumber === "" || pageNumber === null) continue;
    pages.push({
      pageNumber: Number(pageNumber),
      hasText: String(values[i][2] || "").length > 0
    });
  }
  return { ok: true, exists: true, tab: title, pages: pages };
}
"""
