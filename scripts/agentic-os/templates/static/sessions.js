/* Issue #269 — session history list, scrubbable replay, compare, bookmarks.
   Pure DOM construction (no innerHTML) so untrusted event/session text can
   never be parsed as markup. */
(function (global) {
  function el(tag, txt, cls) {
    var e = document.createElement(tag);
    if (txt != null) e.textContent = txt;
    if (cls) e.className = cls;
    return e;
  }
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

  function fmtDuration(started, finished) {
    var a = Date.parse(started), b = finished ? Date.parse(finished) : Date.now();
    if (isNaN(a) || isNaN(b)) return '–';
    var sec = Math.max(0, Math.round((b - a) / 1000));
    var m = Math.floor(sec / 60), s = sec % 60;
    return m + 'm ' + s + 's';
  }

  // ---- list ----
  function startList() {
    var tbody = document.querySelector('#sessions-table tbody');
    var modeSel = document.getElementById('sessions-filter-mode');
    var statusSel = document.getElementById('sessions-filter-status');
    var prev = document.getElementById('sessions-prev');
    var next = document.getElementById('sessions-next');
    var pageLabel = document.getElementById('sessions-page');
    var pageSize = 25;
    var offset = 0;

    function load() {
      var qs = '?limit=' + pageSize + '&offset=' + offset;
      if (modeSel.value) qs += '&mode=' + encodeURIComponent(modeSel.value);
      if (statusSel.value) qs += '&status=' + encodeURIComponent(statusSel.value);
      fetch('/api/sessions' + qs).then(function (r) { return r.json(); }).then(function (data) {
        var rows = data.sessions || [];
        clear(tbody);
        if (!rows.length) {
          var tr = el('tr'); var td = el('td', '(no sessions)', 'placeholder');
          td.colSpan = 8; tr.appendChild(td); tbody.appendChild(tr);
        }
        rows.forEach(function (s) {
          var tr = el('tr');
          var link = el('a', s.bookmark || s.id);
          link.href = '/sessions/' + encodeURIComponent(s.id);
          var idCell = el('td'); idCell.appendChild(link);
          tr.appendChild(idCell);
          tr.appendChild(el('td', s.started_at || '–'));
          tr.appendChild(el('td', fmtDuration(s.started_at, s.finished_at)));
          tr.appendChild(el('td', s.mode || '–'));
          tr.appendChild(el('td', String(s.work_items_processed != null ? s.work_items_processed : 0)));
          tr.appendChild(el('td', String(s.blocks != null ? s.blocks : 0)));
          tr.appendChild(el('td', String(s.failures != null ? s.failures : 0)));
          tr.appendChild(el('td', s.status || '–'));
          tbody.appendChild(tr);
        });
        prev.disabled = offset === 0;
        next.disabled = rows.length < pageSize;
        pageLabel.textContent = 'page ' + (offset / pageSize + 1);
      }).catch(function () {});
    }
    modeSel.addEventListener('change', function () { offset = 0; load(); });
    statusSel.addEventListener('change', function () { offset = 0; load(); });
    prev.addEventListener('click', function () { offset = Math.max(0, offset - pageSize); load(); });
    next.addEventListener('click', function () { offset += pageSize; load(); });
    load();
  }

  // ---- detail + replay ----
  function startDetail() {
    var parts = global.location.pathname.split('/');
    var sessionId = decodeURIComponent(parts[parts.length - 1] || '');
    var summary = document.getElementById('session-summary');
    var timeline = document.getElementById('replay-timeline');
    var scrubber = document.getElementById('replay-scrubber');
    var position = document.getElementById('replay-position');
    var jumpBlocker = document.getElementById('replay-jump-blocker');
    var jumpFailure = document.getElementById('replay-jump-failure');
    var bookmarkInput = document.getElementById('session-bookmark-input');
    var bookmarkSave = document.getElementById('session-bookmark-save');
    var bookmarkStatus = document.getElementById('session-bookmark-status');
    var allEvents = [];

    function addMeta(dl, k, v) {
      dl.appendChild(el('dt', k));
      dl.appendChild(el('dd', v == null ? '–' : String(v)));
    }

    fetch('/api/sessions/' + encodeURIComponent(sessionId)).then(function (r) {
      if (!r.ok) throw new Error('not found');
      return r.json();
    }).then(function (data) {
      var s = data.session;
      clear(summary);
      addMeta(summary, 'session', s.id);
      addMeta(summary, 'status', s.status);
      addMeta(summary, 'mode', s.mode);
      addMeta(summary, 'started', s.started_at);
      addMeta(summary, 'finished', s.finished_at);
      addMeta(summary, 'duration', fmtDuration(s.started_at, s.finished_at));
      addMeta(summary, 'work items', s.work_items_processed);
      addMeta(summary, 'blocks', s.blocks);
      addMeta(summary, 'failures', s.failures);
      if (s.summary_path) {
        summary.appendChild(el('dt', 'handoff doc'));
        var dd = el('dd');
        var a = el('a', 'session-summary.md');
        a.href = '/files/' + s.summary_path;
        a.target = '_blank';
        a.rel = 'noopener';
        dd.appendChild(a);
        summary.appendChild(dd);
      }
      if (s.bookmark) bookmarkInput.value = s.bookmark;
      loadEvents(s);
    }).catch(function () {
      clear(summary); summary.appendChild(el('dd', 'session not found'));
    });

    function loadEvents(s) {
      var qs = '?limit=500';
      if (s.started_at) qs += '&from=' + encodeURIComponent(s.started_at);
      if (s.finished_at) qs += '&to=' + encodeURIComponent(s.finished_at);
      fetch('/api/events/history' + qs).then(function (r) { return r.json(); }).then(function (data) {
        // history returns newest-first; replay wants oldest-first.
        allEvents = (data.events || []).slice().reverse();
        scrubber.max = String(allEvents.length);
        scrubber.value = String(allEvents.length);
        renderUpTo(allEvents.length);
      }).catch(function () {});
    }

    function renderUpTo(n) {
      clear(timeline);
      var slice = allEvents.slice(0, n);
      if (!slice.length) { timeline.appendChild(el('li', '(no events in window)', 'placeholder')); }
      slice.forEach(function (ev) {
        var li = el('li', (ev.ts || '') + ' · ' + ev.kind + ' [' + (ev.severity || 'info') + ']');
        li.className = 'step-' + (ev.severity === 'error' ? 'failed' : (ev.severity === 'warning' ? 'blocked' : 'done'));
        timeline.appendChild(li);
      });
      position.textContent = n >= allEvents.length ? 'end' : (n + '/' + allEvents.length);
    }

    scrubber.addEventListener('input', function () { renderUpTo(parseInt(scrubber.value, 10) || 0); });
    jumpBlocker.addEventListener('click', function () {
      var idx = allEvents.findIndex(function (e) { return e.severity === 'warning'; });
      if (idx >= 0) { scrubber.value = String(idx + 1); renderUpTo(idx + 1); }
    });
    jumpFailure.addEventListener('click', function () {
      var idx = allEvents.findIndex(function (e) { return e.severity === 'error'; });
      if (idx >= 0) { scrubber.value = String(idx + 1); renderUpTo(idx + 1); }
    });

    bookmarkSave.addEventListener('click', function () {
      bookmarkSave.disabled = true;
      fetch('/api/sessions/' + encodeURIComponent(sessionId) + '/bookmark', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: bookmarkInput.value })
      }).then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
        .then(function (res) {
          bookmarkStatus.textContent = res.ok ? 'saved' : ('failed: ' + (res.body.error || ''));
          bookmarkSave.disabled = false;
        }).catch(function () { bookmarkStatus.textContent = 'network error'; bookmarkSave.disabled = false; });
    });
  }

  // ---- compare ----
  function startCompare() {
    var params = new URLSearchParams(global.location.search);
    var a = params.get('a'), b = params.get('b');
    var tbody = document.querySelector('#compare-table tbody');
    document.getElementById('compare-a-head').textContent = a || 'A';
    document.getElementById('compare-b-head').textContent = b || 'B';
    if (!a || !b) {
      clear(tbody);
      var tr = el('tr'); var td = el('td', 'pass ?a=<id>&b=<id>', 'placeholder'); td.colSpan = 4;
      tr.appendChild(td); tbody.appendChild(tr); return;
    }
    fetch('/api/sessions/compare?a=' + encodeURIComponent(a) + '&b=' + encodeURIComponent(b))
      .then(function (r) { return r.json(); }).then(function (data) {
        clear(tbody);
        var fields = data.fields || {};
        Object.keys(fields).forEach(function (k) {
          var f = fields[k];
          var tr = el('tr');
          tr.appendChild(el('td', k));
          tr.appendChild(el('td', String(f.a)));
          tr.appendChild(el('td', String(f.b)));
          tr.appendChild(el('td', (f.delta > 0 ? '+' : '') + f.delta));
          tbody.appendChild(tr);
        });
      }).catch(function () {});
  }

  global.Sessions = { startList: startList, startDetail: startDetail, startCompare: startCompare };
})(window);
