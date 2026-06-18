// Issue #247 — Live Verification view.
// Streams reviewer + triager step.* events with parsed structured payload,
// a live decision log (GET /api/decisions), a verification queue with p50
// ETA, and an operator override drawer (write-gated).
(function (global) {
  'use strict';

  var VERIF_ROLES = { reviewer: true, triager: true };
  var MAX_ROWS = 100;

  var openSteps = {};        // step_id -> snapshot (reviewer/triager only)
  var timeline = [];         // reviewer/triager step.* events, newest first
  var durations = { reviewer: [], triager: [] }; // recent durations (ms)
  var oldestId = null;
  var overrideTarget = null;

  function el(id) { return document.getElementById(id); }
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

  function placeholderRow(node, text, colspan) {
    clear(node);
    if (colspan) {
      var tr = document.createElement('tr');
      var td = document.createElement('td');
      td.colSpan = colspan; td.className = 'placeholder'; td.textContent = text;
      tr.appendChild(td); node.appendChild(tr);
    } else {
      var li = document.createElement('li');
      li.className = 'placeholder'; li.textContent = text;
      node.appendChild(li);
    }
  }

  function chip(text, cls) {
    var s = document.createElement('span');
    s.className = 'chip ' + (cls || '');
    s.textContent = text;
    return s;
  }

  // Parse the free-text reviewer/triager `detail` for structured fields.
  function parseDetail(detail) {
    var out = {};
    (detail || '').split(/\r?\n/).forEach(function (line) {
      var m = line.match(/^\s*(verdict|reason|severity|priority|owasp|iso25010|findings)\s*:\s*(.+)$/i);
      if (m) out[m[1].toLowerCase()] = m[2].trim();
    });
    return out;
  }

  function fmtP50(role) {
    var arr = durations[role] || [];
    if (!arr.length) return '–';
    var sorted = arr.slice().sort(function (a, b) { return a - b; });
    var mid = sorted[Math.floor(sorted.length / 2)];
    var sec = Math.round(mid / 1000);
    return sec < 60 ? sec + 's' : Math.floor(sec / 60) + 'm' + (sec % 60) + 's';
  }

  function renderQueue() {
    var table = el('verif-queue');
    var tbody = table && table.querySelector('tbody');
    if (!tbody) return;
    var byWi = {};
    Object.keys(openSteps).forEach(function (sid) {
      var s = openSteps[sid];
      var wi = s.work_item_id || '(none)';
      byWi[wi] = byWi[wi] || { roles: {}, latest: s };
      byWi[wi].roles[s.role] = true;
      if (Date.parse(s.started_at) > Date.parse(byWi[wi].latest.started_at)) byWi[wi].latest = s;
    });
    var keys = Object.keys(byWi);
    if (!keys.length) { placeholderRow(tbody, '(nothing pending)', 4); return; }
    clear(tbody);
    keys.forEach(function (wi) {
      var entry = byWi[wi];
      var tr = document.createElement('tr');
      function td(text) { var c = document.createElement('td'); c.textContent = text == null ? '–' : text; return c; }
      if (wi !== '(none)') {
        var link = document.createElement('a');
        link.href = '/tasks/' + encodeURIComponent(wi);
        link.textContent = wi;
        var c0 = document.createElement('td'); c0.appendChild(link); tr.appendChild(c0);
      } else { tr.appendChild(td('(none)')); }
      tr.appendChild(td(Object.keys(entry.roles).join(', ')));
      tr.appendChild(td(entry.latest.role || '–'));
      tr.appendChild(td(fmtP50(entry.latest.role)));
      tbody.appendChild(tr);
    });
  }

  function renderTimeline() {
    var ol = el('verif-timeline');
    if (!ol) return;
    if (!timeline.length) { placeholderRow(ol, '(no reviewer / triager events)'); return; }
    clear(ol);
    timeline.slice(0, MAX_ROWS).forEach(function (ev) {
      var p = ev.payload || {};
      var li = document.createElement('li');
      li.className = 'step-row verif-row';
      var time = document.createElement('span');
      time.className = 'step-time';
      time.textContent = (ev.ts || '').replace('T', ' ').slice(0, 19);
      li.appendChild(time);
      li.appendChild(chip(p.role || '?', 'chip-role'));
      if (p.provider) li.appendChild(chip(p.provider, 'chip-provider'));
      if (p.work_item_id) {
        var link = document.createElement('a');
        link.href = '/tasks/' + encodeURIComponent(p.work_item_id);
        link.textContent = p.work_item_id;
        link.className = 'step-wi-inline';
        li.appendChild(link);
      }
      var parsed = parseDetail(p.detail);
      var fields = document.createElement('div');
      fields.className = 'verif-fields';
      if (p.role === 'reviewer') {
        if (parsed.verdict) fields.appendChild(chip('verdict: ' + parsed.verdict, 'chip-outcome chip-' + (parsed.verdict.toLowerCase().indexOf('approve') === 0 ? 'ok' : 'failed')));
        if (parsed.reason) { var r = document.createElement('span'); r.className = 'verif-reason'; r.textContent = parsed.reason; fields.appendChild(r); }
      } else {
        if (parsed.severity) fields.appendChild(chip('S: ' + parsed.severity, 'chip-phase'));
        if (parsed.priority) fields.appendChild(chip('P: ' + parsed.priority, 'chip-role'));
        if (parsed.owasp) fields.appendChild(chip('OWASP: ' + parsed.owasp, 'chip-provider'));
        if (parsed.iso25010) fields.appendChild(chip('ISO: ' + parsed.iso25010, 'chip-provider'));
      }
      if (ev.kind === 'step.end') fields.appendChild(chip(p.outcome || 'done', 'chip-outcome chip-' + (p.outcome || 'ok')));
      else fields.appendChild(chip('in_progress', 'chip-outcome chip-in_progress'));
      li.appendChild(fields);
      ol.appendChild(li);
    });
  }

  function ingestEvent(ev) {
    if (!ev || !ev.kind || ev.kind.indexOf('step.') !== 0) return;
    var p = ev.payload || {};
    if (!VERIF_ROLES[p.role]) return;
    if (ev.kind === 'step.start' && p.step_id) {
      openSteps[p.step_id] = {
        step: p.step_id, role: p.role, provider: p.provider,
        work_item_id: p.work_item_id, started_at: p.started_at || ev.ts
      };
    } else if (ev.kind === 'step.end' && p.step_id) {
      var s = openSteps[p.step_id];
      if (s) {
        var dur = Date.parse(p.ended_at || ev.ts) - Date.parse(s.started_at);
        if (!isNaN(dur) && dur >= 0 && durations[p.role]) {
          durations[p.role].push(dur);
          if (durations[p.role].length > 10) durations[p.role].shift();
        }
      }
      delete openSteps[p.step_id];
    }
    timeline.unshift(ev);
    if (timeline.length > MAX_ROWS * 2) timeline.length = MAX_ROWS * 2;
    if (oldestId === null || (ev.id && ev.id < oldestId)) oldestId = ev.id;
  }

  function renderDecisions() {
    var ul = el('verif-decisions');
    if (!ul) return;
    var filter = (el('verif-decision-filter') || {}).value || '';
    var url = '/api/decisions?limit=50';
    if (filter) url += '&actor=' + encodeURIComponent(filter);
    fetch(url).then(function (r) { return r.ok ? r.json() : { decisions: [] }; }).then(function (res) {
      var rows = res.decisions || [];
      if (!rows.length) { placeholderRow(ul, '(no decisions yet)'); return; }
      clear(ul);
      rows.forEach(function (d) {
        var li = document.createElement('li');
        li.className = 'decision-row';
        var isAuto = (d.actor || '').indexOf('-autopilot') >= 0;
        li.appendChild(chip(d.actor || d.decided_by, isAuto ? 'chip-role' : 'chip-provider'));
        var topic = document.createElement('span');
        topic.className = 'decision-topic';
        topic.textContent = d.topic;
        li.appendChild(topic);
        var rationale = document.createElement('span');
        rationale.className = 'decision-rationale';
        rationale.textContent = (d.rationale || '').slice(0, 120);
        li.appendChild(rationale);
        if (isAuto) {
          var btn = document.createElement('button');
          btn.type = 'button'; btn.className = 'btn-small';
          btn.textContent = 'Override';
          btn.dataset.decision = d.id;
          btn.dataset.topic = d.topic;
          btn.addEventListener('click', function () { openOverride(d); });
          li.appendChild(btn);
        }
        ul.appendChild(li);
      });
      applyWriteStateToOverrides();
    }).catch(function () {});
  }

  function applyWriteStateToOverrides() {
    fetch('/api/config').then(function (r) { return r.ok ? r.json() : {}; }).then(function (cfg) {
      var writesOn = !!((cfg && cfg.dashboard) || {}).enable_write_endpoints;
      document.querySelectorAll('#verif-decisions button[data-decision]').forEach(function (b) {
        b.disabled = !writesOn;
      });
      var submit = el('verif-override-submit');
      if (submit) submit.disabled = !writesOn || !overrideTarget;
    }).catch(function () {});
  }

  function openOverride(decision) {
    overrideTarget = decision;
    var drawer = el('verif-drawer');
    if (!drawer) return;
    drawer.hidden = false;
    var meta = el('verif-drawer-meta');
    clear(meta);
    [['decision', decision.id], ['actor', decision.actor], ['topic', decision.topic],
     ['rationale', decision.rationale]].forEach(function (pair) {
      if (pair[1] == null) return;
      var dt = document.createElement('dt'); dt.textContent = pair[0];
      var dd = document.createElement('dd'); dd.textContent = String(pair[1]);
      meta.appendChild(dt); meta.appendChild(dd);
    });
    applyWriteStateToOverrides();
  }

  function wireOverride() {
    var close = el('verif-drawer-close');
    if (close) close.addEventListener('click', function () {
      el('verif-drawer').hidden = true; overrideTarget = null;
    });
    var submit = el('verif-override-submit');
    if (submit) submit.addEventListener('click', function () {
      if (!overrideTarget) return;
      var note = (el('verif-override-note') || {}).value || '';
      var action = (el('verif-override-action') || {}).value || 'override';
      var statusEl = el('verif-override-status');
      if (!note.trim()) { if (statusEl) statusEl.textContent = 'note required'; return; }
      submit.disabled = true;
      if (statusEl) statusEl.textContent = 'submitting…';
      fetch('/api/decisions/' + encodeURIComponent(overrideTarget.id) + '/override', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note: note, action: action })
      }).then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
        .then(function (res) {
          if (statusEl) statusEl.textContent = res.ok ? 'recorded' : ('error: ' + (res.body.error || 'unknown'));
          if (res.ok) { renderDecisions(); }
        })
        .catch(function (exc) { if (statusEl) statusEl.textContent = 'error: ' + exc.message; submit.disabled = false; });
    });
    var filter = el('verif-decision-filter');
    if (filter) filter.addEventListener('change', renderDecisions);
  }

  function seedHistory() {
    fetch('/api/events/history?kind=step.*&limit=200').then(function (r) {
      return r.ok ? r.json() : { events: [] };
    }).then(function (res) {
      (res.events || []).forEach(function (ev) {
        var p = ev.payload || {};
        if (!VERIF_ROLES[p.role]) return;
        timeline.push(ev);
        if (oldestId === null || (ev.id && ev.id < oldestId)) oldestId = ev.id;
      });
      refreshAll();
    }).catch(function () {});
  }

  function refreshAll() {
    renderQueue();
    renderTimeline();
  }

  function startSSE() {
    if (typeof EventSource === 'undefined') return;
    var es = new EventSource('/api/events?kind=step.*');
    es.onmessage = function (e) {
      try { ingestEvent(JSON.parse(e.data)); } catch (_) { /* ignore */ }
    };
    es.onerror = function () { /* auto-reconnect */ };
  }

  function start() {
    wireOverride();
    seedHistory();
    renderDecisions();
    startSSE();
    setInterval(refreshAll, 2000);
    setInterval(renderDecisions, 5000);
  }

  global.Verifications = { start: start, parseDetail: parseDetail };
})(window);
