"""
Дополняет уже сохранённый HTML-отчёт элементами шаблона report-ui v2.1
(оглавление, инциденты, режим проверки, печать, скачивание TSV, скрытие PASS-ступеней),
не трогая табличные данные.

Usage:
  python upgrade_static_html_report_v21.py input.html [output.html]
"""
from __future__ import annotations

import re
import sys
from html import escape
from pathlib import Path

REPORT_UI_VERSION = "v2.1"
REQ_WARN = 5.0
REQ_FAIL = 10.0

V21_CSS_BLOCK = r"""
        .uc-metric-table th:nth-child(n+3):nth-child(-n+16),
        .uc-metric-table td:nth-child(n+3):nth-child(-n+16),
        .uc-metric-table th:nth-child(n+18):nth-child(-n+20),
        .uc-metric-table td:nth-child(n+18):nth-child(-n+20) {
            text-align: right;
        }
        .uc-metric-table th:nth-child(1), .uc-metric-table td:nth-child(1),
        .uc-metric-table th:nth-child(2), .uc-metric-table td:nth-child(2),
        .uc-metric-table th:nth-child(17), .uc-metric-table td:nth-child(17),
        .uc-metric-table th:nth-child(21), .uc-metric-table td:nth-child(21) {
            text-align: left;
        }
        .report-toc {
            margin: 16px 0 20px 0;
            padding: 12px 16px;
            background: #f5f7fa;
            border-radius: 6px;
            border: 1px solid #dde3ea;
        }
        .report-toc ul { margin: 8px 0 0 1.2em; }
        .report-toc h2 { font-size: 1.1rem; margin: 0 0 8px 0; color: #333; }
        .report-check-mode {
            margin: 12px 0;
            padding: 10px 14px;
            background: #e8f4fd;
            border-left: 3px solid #1976d2;
            border-radius: 4px;
        }
        .report-incidents {
            margin: 12px 0 20px 0;
            padding: 12px 16px;
            background: #fff5f5;
            border-left: 3px solid #c62828;
            border-radius: 4px;
        }
        .report-incidents-title { font-size: 1.05rem; margin: 0 0 8px 0; color: #b71c1c; }
        .report-incidents-list { margin: 0 0 0 1.2em; }
        .report-incidents-empty { margin: 0; color: #555; }
        .report-toolbar { margin: 12px 0 16px 0; }
        .report-toolbar-btn {
            font-size: 0.9em;
            padding: 6px 12px;
            cursor: pointer;
            background: #5d4037;
            color: #fff;
            border: none;
            border-radius: 4px;
        }
        .report-toolbar-btn:hover { background: #4e342e; }
        .report-toolbar-btn[aria-pressed="true"] { background: #33691e; }
        .download-table-btn, .download-all-uc-btn {
            font-size: 0.9em;
            padding: 6px 12px;
            margin-left: 8px;
            margin-bottom: 8px;
            cursor: pointer;
            background: #00695c;
            color: #fff;
            border: none;
            border-radius: 4px;
        }
        .download-table-btn:hover, .download-all-uc-btn:hover { background: #004d40; }
        .download-table-btn:disabled, .download-all-uc-btn:disabled { opacity: 0.7; cursor: wait; }
        body.report-hide-pass-uc tr.row-pass { display: none; }
        .report-footer { margin-top: 32px; padding-top: 12px; border-top: 1px solid #ccc; color: #666; font-size: 0.85em; }
        @media print {
            html { color-scheme: light !important; }
            html[data-theme="dark"] body {
                background: #fff !important;
                color: #000 !important;
            }
            html[data-theme="dark"] h1, html[data-theme="dark"] h2, html[data-theme="dark"] h3,
            html[data-theme="dark"] h4, html[data-theme="dark"] h5, html[data-theme="dark"] p,
            html[data-theme="dark"] li, html[data-theme="dark"] td, html[data-theme="dark"] th {
                color: #000 !important;
            }
            html[data-theme="dark"] table, html[data-theme="dark"] th, html[data-theme="dark"] td {
                border-color: #999 !important;
            }
            html[data-theme="dark"] th { background: #eee !important; }
            .no-print, .theme-toggle-wrap, .copy-table-btn, .copy-all-uc-btn,
            .download-table-btn, .download-all-uc-btn, .report-toolbar {
                display: none !important;
            }
            h1 { padding-right: 0 !important; }
            .report-toc, .report-incidents { break-inside: avoid; page-break-inside: avoid; }
            .summary-all-tg-section { break-inside: avoid; page-break-inside: avoid; }
            hr { break-before: page; page-break-before: always; }
        }
        html[data-theme="dark"] .download-table-btn, html[data-theme="dark"] .download-all-uc-btn {
            background: #00897b;
        }
        html[data-theme="dark"] .download-table-btn:hover, html[data-theme="dark"] .download-all-uc-btn:hover {
            background: #00695c;
        }
        html[data-theme="dark"] .report-toc {
            background: #252830;
            border-color: #3f4450;
        }
        html[data-theme="dark"] .report-toc h2 { color: #e4e4e7; }
        html[data-theme="dark"] .report-check-mode {
            background: #1e2a3a;
            border-left-color: #42a5f5;
            color: #e8e8ed;
        }
        html[data-theme="dark"] .report-incidents {
            background: #3d2020;
            border-left-color: #ef5350;
        }
        html[data-theme="dark"] .report-incidents-title { color: #ffab91; }
        html[data-theme="dark"] .report-incidents-empty { color: #b0b0b8; }
        html[data-theme="dark"] .report-toolbar-btn { background: #6d4c41; }
        html[data-theme="dark"] .report-toolbar-btn:hover { background: #5d4037; }
        html[data-theme="dark"] .report-footer { border-top-color: #4a5160; color: #a8a8b0; }
"""

V21_SCRIPT = r"""
<script>
(function () {
  var THEME_KEY = 'loadProfileReportTheme';
  function applyThemeFromStorage() {
    try {
      if (localStorage.getItem(THEME_KEY) === 'dark') {
        document.documentElement.setAttribute('data-theme', 'dark');
      } else {
        document.documentElement.removeAttribute('data-theme');
      }
    } catch (e) {}
    syncThemeToggle();
  }
  function syncThemeToggle() {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    var dark = document.documentElement.getAttribute('data-theme') === 'dark';
    btn.textContent = dark ? 'Светлая тема' : 'Тёмная тема';
    btn.setAttribute('aria-pressed', dark ? 'true' : 'false');
  }
  function toggleTheme() {
    var dark = document.documentElement.getAttribute('data-theme') === 'dark';
    if (dark) {
      document.documentElement.removeAttribute('data-theme');
      try { localStorage.removeItem(THEME_KEY); } catch (e) {}
    } else {
      document.documentElement.setAttribute('data-theme', 'dark');
      try { localStorage.setItem(THEME_KEY, 'dark'); } catch (e) {}
    }
    syncThemeToggle();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', applyThemeFromStorage);
  } else {
    applyThemeFromStorage();
  }
  document.addEventListener('DOMContentLoaded', function () {
    var tbtn = document.getElementById('theme-toggle');
    if (tbtn) tbtn.addEventListener('click', toggleTheme);
  });
})();
(function () {
  function normalizeCell(t) {
    return t.replace(/\r?\n/g, ' ').replace(/\t/g, ' ').replace(/\s+/g, ' ').trim();
  }
  function tableToTSV(table) {
    var rows = [];
    for (var r = 0; r < table.rows.length; r++) {
      var cells = [];
      for (var c = 0; c < table.rows[r].cells.length; c++) {
        cells.push(normalizeCell(table.rows[r].cells[c].innerText));
      }
      rows.push(cells.join('\t'));
    }
    return rows.join('\r\n');
  }
  function safeFilePart(s) {
    if (!s) return 'report';
    return String(s).replace(/[^a-zA-Z0-9._-]+/g, '_').replace(/^_|_$/g, '') || 'report';
  }
  function downloadText(filename, text) {
    var blob = new Blob([text], { type: 'text/tab-separated-values;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }
  function copyPlainText(text, btn, originalLabel) {
    function doneOk() {
      btn.textContent = 'Скопировано';
      btn.disabled = true;
      setTimeout(function () {
        btn.textContent = originalLabel;
        btn.disabled = false;
      }, 2000);
    }
    function fallback() {
      window.prompt('Копирование в буфер недоступно. Выделите и Ctrl+C:', text);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(doneOk).catch(fallback);
    } else {
      fallback();
    }
  }
  document.addEventListener('DOMContentLoaded', function () {
    var runId = safeFilePart(document.body.getAttribute('data-test-run'));
    document.querySelectorAll('.table-copy-wrap').forEach(function (wrap) {
      var btn = wrap.querySelector('.copy-table-btn');
      var dlb = wrap.querySelector('.download-table-btn');
      var table = wrap.querySelector('table');
      if (!btn || !table) return;
      var singleLabel = btn.textContent;
      btn.addEventListener('click', function () {
        copyPlainText(tableToTSV(table), btn, singleLabel);
      });
      if (dlb) {
        var dlLabel = dlb.textContent;
        dlb.addEventListener('click', function () {
          downloadText(runId + '_table.tsv', tableToTSV(table));
          dlb.textContent = 'Скачано';
          setTimeout(function () { dlb.textContent = dlLabel; }, 1500);
        });
      }
    });
    document.querySelectorAll('.copy-all-uc-btn').forEach(function (btn) {
      var allLabel = btn.textContent;
      btn.addEventListener('click', function () {
        var region = btn.closest('.uc-tables-region');
        if (!region) return;
        var wraps = region.querySelectorAll('.table-copy-wrap.uc-table-wrap');
        var parts = [];
        var bl = region.getAttribute('data-block-label');
        if (bl) parts.push('# ' + bl);
        wraps.forEach(function (w) {
          var el = w.previousElementSibling;
          var ucTitle = '';
          if (el && el.tagName === 'H3') ucTitle = normalizeCell(el.innerText);
          if (ucTitle) parts.push('# ' + ucTitle);
          var tbl = w.querySelector('table');
          if (tbl) parts.push(tableToTSV(tbl));
          parts.push('');
        });
        var text = parts.join('\r\n').replace(/\r\n+$/, '');
        copyPlainText(text, btn, allLabel);
      });
    });
    document.querySelectorAll('.download-all-uc-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var region = btn.closest('.uc-tables-region');
        if (!region) return;
        var wraps = region.querySelectorAll('.table-copy-wrap.uc-table-wrap');
        var parts = [];
        var bl = region.getAttribute('data-block-label');
        var blSafe = bl ? safeFilePart(bl) : 'uc_block';
        if (bl) parts.push('# ' + bl);
        wraps.forEach(function (w) {
          var el = w.previousElementSibling;
          var ucTitle = '';
          if (el && el.tagName === 'H3') ucTitle = normalizeCell(el.innerText);
          if (ucTitle) parts.push('# ' + ucTitle);
          var tbl = w.querySelector('table');
          if (tbl) parts.push(tableToTSV(tbl));
          parts.push('');
        });
        var text = parts.join('\r\n').replace(/\r\n+$/, '');
        downloadText(runId + '_' + blSafe + '_all_uc.tsv', text);
      });
    });
    var hidePassBtn = document.getElementById('toggle-hide-pass-rows');
    if (hidePassBtn) {
      hidePassBtn.addEventListener('click', function () {
        var on = document.body.classList.toggle('report-hide-pass-uc');
        hidePassBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
        hidePassBtn.textContent = on ? 'Показать PASS-ступени' : 'Скрыть PASS-ступени';
      });
    }
  });
})();
</script>
"""


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def _extract_test_run(html: str) -> str:
    m = re.search(
        r"<p><strong>Test Run ID:</strong>\s*([^<]+)</p>",
        html,
    )
    return (m.group(1).strip() if m else "report").replace('"', "")


def _extract_cluster_n(html: str) -> int | None:
    m = re.search(r"Сводка кластера\s*\([^)]*N\s*=\s*(\d+)", html)
    return int(m.group(1)) if m else None


def _check_mode_line(html: str, test_run: str) -> str:
    n = _extract_cluster_n(html)
    if n is not None and "fallback" in html:
        return (
            f"Кластерный fallback: в jmeter нет разреза runner для этого test_run; "
            f"N = {n} из measurement «jmeter_runner_meta»; запросы к jmeter без фильтра test_run; "
            f"пер-под таблицы недоступны."
        )
    if re.search(r"Под \(runner\)", html):
        return (
            "Несколько подов (тег runner в jmeter для этого test_run): проверка по каждому поду и сводка кластера."
        )
    return (
        "Один источник: метрики jmeter с фильтром по test_run; целевой и фактический RPS — для одного экземпляра профиля."
    )


def _collect_incidents(html: str) -> str:
    items: list[str] = []
    pos = 0
    while True:
        m = re.search(
            r"<h3(?:\s+id=\"report-uc-\d+\")?>\s*([\w_]+)\s*-\s*"
            r'<span class="status-(FAIL|PARTIAL)">',
            html[pos:],
        )
        if not m:
            break
        tg = m.group(1).strip()
        st = m.group(2)
        start = pos + m.end()
        nxt = re.search(r"<h3\s", html[start:])
        block = html[start : start + nxt.start()] if nxt else html[start : start + 120000]
        trm = re.search(r'<tr class="row-(fail|partial)">', block, re.I)
        stage = "?"
        dev = "—"
        if trm:
            tend = block.find("</tr>", trm.start())
            tr_html = block[trm.start() : tend if tend != -1 else trm.start()]
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr_html, re.DOTALL)
            if len(tds) >= 7:
                stage = _strip_tags(tds[0])
                dev = _strip_tags(tds[6])
        items.append(
            f"<li><code>{escape(tg)}</code>, ступень {escape(stage)}: "
            f'<span class="status-{st}">{st}</span>, '
            f"отклонение ALL % — <strong>{escape(dev)}</strong></li>"
        )
        pos = start + 1
    if not items:
        return '<p class="report-incidents-empty">Нет ступеней со статусом FAIL или PARTIAL по RPS.</p>'
    return '<ul class="report-incidents-list">\n' + "\n".join(items) + "\n</ul>"


def _inject_h3_ids(html: str) -> str:
    n = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal n
        if m.group(0).startswith("<h3 id="):
            return m.group(0)
        n += 1
        return f'<h3 id="report-uc-{n}">'

    return re.sub(r"<h3>", repl, html)


def _build_toc(html: str) -> str:
    lines = [
        '    <nav class="report-toc" aria-label="Оглавление по Use Case (Thread Group)">',
        '    <h2 id="report-toc-heading">Оглавление</h2>',
        "    <ul>",
    ]
    for m in re.finditer(
        r'<h3 id="(report-uc-\d+)">(.+?)</h3>',
        html,
        re.DOTALL,
    ):
        aid = m.group(1)
        label = _strip_tags(m.group(2))
        lines.append(f'      <li><a href="#{aid}">{escape(label)}</a></li>')
    lines.append(
        '      <li><a href="#summary-all-tg">Сводная статистика по всем Thread Groups</a></li>'
    )
    lines.extend(["    </ul>", "    </nav>"])
    return "\n".join(lines) + "\n"


def upgrade_html(html: str) -> str:
    if "report-toc-heading" in html and "toggle-hide-pass-rows" in html:
        return html

    test_run = _extract_test_run(html)
    check_mode = _check_mode_line(html, test_run)

    if "Пороги статуса «запросов»" not in html:
        html = html.replace(
            "    <p><strong>Порог отклонения RPS (эта проверка):</strong> 10%</p>\n"
            "    <p style=\"font-size:0.95em; color:#444;\"><strong>Оценка старта теста:</strong>",
            "    <p><strong>Порог отклонения RPS (эта проверка):</strong> 10%</p>\n"
            f"    <p style=\"font-size:0.95em; color:#444;\"><strong>Пороги статуса «запросов» (информ.):</strong> "
            f"WARN если отклонение по количеству &gt; {REQ_WARN:g}%, FAIL если &gt; {REQ_FAIL:g}%.</p>\n"
            "    <p style=\"font-size:0.95em; color:#444;\"><strong>Оценка старта теста:</strong>",
            1,
        )

    html = re.sub(r"<body\s*>", f'<body data-test-run="{escape(test_run)}">', html, count=1)

    if "</style>" in html and V21_CSS_BLOCK.strip() not in html:
        html = html.replace("    </style>", V21_CSS_BLOCK + "\n    </style>", 1)

    html = _inject_h3_ids(html)

    incidents_body = _collect_incidents(html)
    toc = _build_toc(html)

    toolbar = """    <div class="report-toolbar no-print">
    <button type="button" id="toggle-hide-pass-rows" class="report-toolbar-btn" aria-pressed="false" title="Скрыть строки ступеней со статусом PASS в таблицах UC и в сводной по всем TG">Скрыть PASS-ступени</button>
    </div>
"""

    check_block = (
        '    <p class="report-check-mode"><strong>Режим проверки:</strong> '
        f"{escape(check_mode)}</p>\n"
    )
    incidents_block = (
        '    <div class="report-incidents">\n'
        '    <h2 class="report-incidents-title">Инциденты (FAIL / PARTIAL по RPS)</h2>\n'
        f"{incidents_body}\n"
        "    </div>\n"
    )

    insert = check_block + incidents_block + toolbar + toc

    html = re.sub(
        r"(<p><strong>Общий статус:</strong> <span class=\"status-[^\"]+\">[^<]+</span></p>\n)",
        r"\1\n" + insert,
        html,
        count=1,
    )

    html = html.replace(
        '<h2>Сводка кластера',
        '<h2 class="report-cluster-heading">Сводка кластера',
        1,
    )

    html = html.replace(
        '    <button type="button" class="copy-all-uc-btn" title="Все таблицы UC в этом блоке (TSV). Нижняя сводная «по всем TG» не копируется.">Скопировать все UC</button>\n    </div>',
        '    <button type="button" class="copy-all-uc-btn" title="Все таблицы UC в этом блоке (TSV). Нижняя сводная «по всем TG» не копируется.">Скопировать все UC</button>\n'
        '    <button type="button" class="download-all-uc-btn" title="Скачать все таблицы UC этого блока одним TSV-файлом">Скачать все UC (TSV)</button>\n    </div>',
        1,
    )

    _btn = (
        '    <button type="button" class="copy-table-btn" title="Копировать таблицу: значения через табуляцию — вставка в Excel / таблицы">Скопировать таблицу</button>\n'
        '    <button type="button" class="download-table-btn" title="Скачать таблицу как TSV-файл">Скачать TSV</button>\n'
    )
    html = html.replace(
        '    <button type="button" class="copy-table-btn" title="Копировать таблицу: значения через табуляцию — вставка в Excel / таблицы">Скопировать таблицу</button>\n'
        "    <table>\n        <colgroup class=\"uc-profile-cols\">",
        _btn + "    <table class=\"uc-metric-table\">\n        <colgroup class=\"uc-profile-cols\">",
    )

    html = html.replace(
        '<div style="background-color: #e8f5e9; padding: 20px; margin: 20px 0; border-left: 5px solid #4CAF50; border-radius: 5px;">\n'
        '        <h2 style="margin-top: 0; color: #2e7d32;">Сводная статистика по всем Thread Groups</h2>',
        '<div class="summary-all-tg-section" style="background-color: #e8f5e9; padding: 20px; margin: 20px 0; border-left: 5px solid #4CAF50; border-radius: 5px;">\n'
        '        <h2 id="summary-all-tg" style="margin-top: 0; color: #2e7d32;">Сводная статистика по всем Thread Groups</h2>',
        1,
    )

    html = html.replace(
        '    <button type="button" class="copy-table-btn" title="Копировать таблицу: значения через табуляцию — вставка в Excel / таблицы">Скопировать таблицу</button>\n'
        '    <table style="box-shadow: 0 2px 4px rgba(0,0,0,0.1);">',
        _btn + '    <table class="uc-metric-table" style="box-shadow: 0 2px 4px rgba(0,0,0,0.1);">',
        1,
    )

    req_title = (
        f"Статус по количеству запросов (информационный): PASS ≤ {REQ_WARN:g}%, "
        f"WARN ≤ {REQ_FAIL:g}%, FAIL > {REQ_FAIL:g}%"
    )
    _needle = "\u043d\u043d\u044b\u0439): PASS"
    _old_titles = {
        m.group(0)
        for m in re.finditer(r'title="([^"]+)"', html)
        if _needle in m.group(1)
    }
    for _o in _old_titles:
        html = html.replace(_o, f'title="{req_title}"')

    _li_prefix = (
        "\u0438\u043d\u0444\u043e\u0440\u043c\u0430\u0446\u0438\u043e\u043d\u043d\u044b\u0439 "
        "\u0438\u043d\u0434\u0438\u043a\u0430\u0442\u043e\u0440 \u043f\u043e "
        "\u043e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u0438\u044e "
        "\u043a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u0430 "
        "\u0437\u0430\u043f\u0440\u043e\u0441\u043e\u0432 "
    )
    m_li = re.search(re.escape(_li_prefix) + r"\(.+?\)", html)
    if m_li:
        html = html.replace(
            m_li.group(0),
            f"{_li_prefix}(PASS ≤ {REQ_WARN:g}%, WARN ≤ {REQ_FAIL:g}%, FAIL > {REQ_FAIL:g}%)",
            1,
        )

    body_end = html.rfind("</body>")
    script_start = html.rfind("<script>", 0, body_end)
    if script_start != -1:
        script_end = html.find("</script>", script_start)
        if script_end != -1:
            script_end += len("</script>")
            footer = f'    <footer class="report-footer"><small>report-ui: {REPORT_UI_VERSION}</small></footer>\n'
            block = footer + V21_SCRIPT.strip() + "\n"
            if '<footer class="report-footer"' not in html:
                html = html[:script_start] + block + html[script_end:]

    return html


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(2)
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_name(src.stem + "_report_ui_v21.html")
    text = src.read_text(encoding="utf-8")
    out = upgrade_html(text)
    dst.write_text(out, encoding="utf-8")
    print(f"[OK] Записано: {dst}")


if __name__ == "__main__":
    main()
