// Issue #246 — Live Orchestration view.
// Phase machine graph + active loops + step timeline + subprocess tail drawer.
// Subscribes to the SSE stream filtered by `?kind=step.*`; paginates history
// through GET /api/events/history?before=<id>&kind=step.*.
(function (global) {
  'use strict';

  var PHASES = ['analyze', 'design', 'implement', 'review', 'triage', 'generate', 'gate', 'run', 'report'];
  var MAX_TIMELINE = 200;

  var openSteps = {};   // step_id -> snapshot
  var timeline = [];    // step.* events, most recent first
  var oldestId = null;
  var phaseFilter = null;
  var autoscroll = true;

  function el(id) { return document.getElementById(id); }

  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }

  function placeholder(node, text, colspan) {
    clear(node);
    if (colspan) {
      var tr = document.createElement('tr');
      var td = document.createElement('td');
      td.colSpan = colspan;
      td.className = 'placeholder';
      td.textContent = text;
      tr.appendChild(td);
      node.appendChild(tr);
    } else {
      var li = document.createElement('li');
      li.className = 'placeholder';
      li.textContent = text;
      node.appendChild(li);
    }
  }

  function svgEl(tag, attrs) {
    var node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    return node;
  }

  function buildPhaseMachine() {
    var svg = el('phase-machine');
    if (!svg) return;
    clear(svg);
    var nodeW = 96, nodeH = 40, gap = 12, y = 60;
    PHASES.forEach(function (phase, i) {
      var x = i * (nodeW + gap) + 8;
      if (i > 0) {
        var prevX = (i - 1) * (nodeW + gap) + 8 + nodeW;
        svg.appendChild(svgEl('line', { x1: prevX, y1: y + nodeH / 2, x2: x, y2: y + nodeH / 2, class: 'pm-edge' }));
      }
      var g = svgEl('g', { class: 'pm-node', 'data-phase': phase, id: 'pm-' + phase });
      g.appendChild(svgEl('rect', { x: x, y: y, width: nodeW, height: nodeH, rx: 6, class: 'pm-rect' }));
      var label = svgEl('text', { x: x + nodeW / 2, y: y + 17, class: 'pm-label' });
      label.textContent = phase;
      g.appendChild(label);
      var counters = svgEl('text', { x: x + nodeW / 2, y: y + 32, class: 'pm-counters', id: 'pm-counters-' + phase });
      counters.textContent = '0 / 0 / 0';
      g.appendChild(counters);
      g.addEventListener('click', function () { togglePhaseFilter(phase); });
      svg.appendChild(g);
    });
  }

  function togglePhaseFilter(phase) {
    phaseFilter = (phaseFilter === phase) ? null : phase;
    var clearBtn = el('phase-filter-clear');
    if (clearBtn) clearBtn.hidden = !phaseFilter;
    PHASES.forEach(function (p) {
      var node = el('pm-' + p);
      if (node) node.classList.toggle('pm-filtered', !!phaseFilter && p !== phaseFilter);
    });
    renderTimeline();
  }

  function recomputePhaseHighlights() {
    var counts = {};
    PHASES.forEach(function (p) { counts[p] = { active: 0, ok: 0, fail: 0 }; });
    Object.keys(openSteps).forEach(function (sid) {
      var s = openSteps[sid];
      if (counts[s.phase]) counts[s.phase].active += 1;
    });
    var cutoff = Date.now() - 5 * 60 * 1000;
    timeline.forEach(function (ev) {
      var p = ev.payload && ev.payload.phase;
      if (!counts[p]) return;
      if (ev.kind !== 'step.end') return;
      var ts = Date.parse(ev.ts);
      if (isNaN(ts) || ts < cutoff) return;
      if (ev.payload.outcome === 'ok') counts[p].ok += 1;
      else counts[p].fail += 1;
    });
    PHASES.forEach(function (p) {
      var c = counts[p];
      var counterEl = el('pm-counters-' + p);
      if (counterEl) counterEl.textContent = c.active + ' / ' + c.ok + ' / ' + c.fail;
      var node = el('pm-' + p);
      if (node) node.classList.toggle('pm-active', c.active > 0);
    });
  }

  function fmtElapsed(startIso) {
    var start = Date.parse(startIso);
    if (isNaN(start)) return '–';
    var sec = Math.max(0, Math.round((Date.now() - start) / 1000));
    if (sec < 60) return sec + 's';
    var m = Math.floor(sec / 60), s = sec % 60;
    return m + 'm' + (s < 10 ? '0' : '') + s + 's';
  }

  function renderActiveLoops() {
    var table = el('active-loops');
    var tbody = table && table.querySelector('tbody');
    if (!tbody) return;
    var byWi = {};
    Object.keys(openSteps).forEach(function (sid) {
      var s = openSteps[sid];
      var wi = s.work_item_id || '(none)';
      if (!byWi[wi] || Date.parse(s.started_at) > Date.parse(byWi[wi].started_at)) byWi[wi] = s;
    });
    var keys = Object.keys(byWi);
    if (!keys.length) { placeholder(tbody, '(no active loops)', 7); return; }
    clear(tbody);
    keys.forEach(function (wi) {
      var s = byWi[wi];
      var tr = document.createElement('tr');
      function td(text) { var c = document.createElement('td'); c.textContent = text == null ? '–' : text; return c; }
      if (wi !== '(none)') {
        var link = document.createElement('a');
        link.href = '/tasks/' + encodeURIComponent(wi);
        link.textContent = wi;
        var c0 = document.createElement('td'); c0.appendChild(link); tr.appendChild(c0);
      } else {
        tr.appendChild(td('(none)'));
      }
      tr.appendChild(td(s.phase));
      tr.appendChild(td(s.role || '–'));
      tr.appendChild(td(s.skill || '–'));
      tr.appendChild(td(s.provider || '–'));
      tr.appendChild(td(fmtElapsed(s.started_at)));
      var actionTd = document.createElement('td');
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn-small';
      btn.textContent = 'Cancel';
      btn.disabled = true;
      btn.dataset.wi = wi;
      actionTd.appendChild(btn);
      tr.appendChild(actionTd);
      tbody.appendChild(tr);
    });
    applyWriteStateToActions();
  }

  function applyWriteStateToActions() {
    fetch('/api/config').then(function (r) { return r.ok ? r.json() : {}; }).then(function (cfg) {
      var writesOn = !!((cfg && cfg.dashboard) || {}).enable_write_endpoints;
      var btns = document.querySelectorAll('#active-loops button[data-wi]');
      btns.forEach(function (b) { b.disabled = !writesOn; });
    }).catch(function () {});
  }

  function chip(text, cls) {
    var s = document.createElement('span');
    s.className = 'chip ' + (cls || '');
    s.textContent = text;
    return s;
  }

  function renderTimeline() {
    var ol = el('step-timeline');
    if (!ol) return;
    var rows = timeline.filter(function (ev) {
      if (!phaseFilter) return true;
      return ev.payload && ev.payload.phase === phaseFilter;
    });
    if (!rows.length) { placeholder(ol, '(no step events)'); return; }
    clear(ol);
    rows.slice(0, MAX_TIMELINE).forEach(function (ev) {
      var p = ev.payload || {};
      var li = document.createElement('li');
      li.className = 'step-row';
      var time = document.createElement('span');
      time.className = 'step-time';
      time.textContent = (ev.ts || '').replace('T', ' ').slice(0, 19);
      li.appendChild(time);
      li.appendChild(chip(p.phase || '?', 'chip-phase'));
      if (p.role) li.appendChild(chip(p.role, 'chip-role'));
      if (p.provider) li.appendChild(chip(p.provider, 'chip-provider'));
      if (p.skill) {
        var sk = document.createElement('span');
        sk.className = 'step-skill';
        sk.textContent = p.skill;
        li.appendChild(sk);
      }
      if (p.work_item_id) {
        var link = document.createElement('a');
        link.href = '/tasks/' + encodeURIComponent(p.work_item_id);
        link.textContent = p.work_item_id;
        link.className = 'step-wi';
        li.appendChild(link);
      }
      var outcome = ev.kind === 'step.end' ? (p.outcome || 'done') : 'in_progress';
      li.appendChild(chip(outcome, 'chip-outcome chip-' + outcome));
      li.addEventListener('click', function () { openDrawer(ev); });
      ol.appendChild(li);
    });
  }

  function ingestEvent(ev) {
    if (!ev || !ev.kind || ev.kind.indexOf('step.') !== 0) return;
    var p = ev.payload || {};
    if (ev.kind === 'step.start' && p.step_id) {
      openSteps[p.step_id] = {
        step: p.step_id, phase: p.phase, role: p.role, skill: p.skill,
        provider: p.provider, work_item_id: p.work_item_id,
        started_at: p.started_at || ev.ts, log_ref: p.log_ref
      };
    } else if (ev.kind === 'step.end' && p.step_id) {
      delete openSteps[p.step_id];
    }
    if (ev.kind === 'step.start' || ev.kind === 'step.end') {
      timeline.unshift(ev);
      if (timeline.length > MAX_TIMELINE * 2) timeline.length = MAX_TIMELINE * 2;
      if (oldestId === null || (ev.id && ev.id < oldestId)) oldestId = ev.id;
    }
  }

  function refreshAll() {
    recomputePhaseHighlights();
    renderActiveLoops();
    renderTimeline();
  }

  function openDrawer(ev) {
    var drawer = el('step-drawer');
    if (!drawer) return;
    var p = ev.payload || {};
    drawer.hidden = false;
    el('step-drawer-title').textContent = (p.kind || 'step') + ' — ' + (p.phase || '');
    var meta = el('step-drawer-meta');
    clear(meta);
    [['step_id', p.step_id], ['actor', ev.actor], ['role', p.role], ['provider', p.provider],
     ['skill', p.skill], ['work_item', p.work_item_id], ['candidate', p.candidate_id],
     ['outcome', p.outcome], ['detail', p.detail]].forEach(function (pair) {
      if (pair[1] == null || pair[1] === '') return;
      var dt = document.createElement('dt'); dt.textContent = pair[0];
      var dd = document.createElement('dd'); dd.textContent = String(pair[1]);
      meta.appendChild(dt); meta.appendChild(dd);
    });
    var logPath = p.log_ref || null;
    el('step-drawer-logpath').textContent = logPath || '(no log_ref)';
    var logBody = el('step-drawer-log');
    if (logPath) streamLog(logPath, logBody);
    else logBody.textContent = '(no log_ref for this step)';
    renderTranscript(p);
  }

  // Issue #270 — when a step row maps to a model invocation, show its
  // structured reasoning transcript (thinking / tool calls / text).
  function renderTranscript(p) {
    var section = el('step-drawer-transcript');
    var list = el('step-drawer-transcript-list');
    if (!section || !list) return;
    section.hidden = true;
    clear(list);
    var detail = p.detail || '';
    var m = detail.match(/invocation=([A-Za-z0-9_-]+)/);
    if (!m) return;
    fetch('/api/transcripts/' + encodeURIComponent(m[1])).then(function (r) {
      return r.ok ? r.json() : { chunks: [] };
    }).then(function (data) {
      var chunks = data.chunks || [];
      if (!chunks.length) return;
      clear(list);
      chunks.forEach(function (c) {
        var li = document.createElement('li');
        li.className = 'transcript-chunk transcript-' + (c.kind || 'text');
        var label = document.createElement('span');
        label.className = 'transcript-kind';
        label.textContent = c.kind;
        var body = document.createElement('pre');
        body.className = 'transcript-payload';
        body.textContent = c.payload;
        li.appendChild(label);
        li.appendChild(body);
        list.appendChild(li);
      });
      section.hidden = false;
    }).catch(function () {});
  }

  var logTimer = null;
  function streamLog(logPath, logBody) {
    if (logTimer) { clearInterval(logTimer); logTimer = null; }
    function poll() {
      fetch('/files/' + logPath).then(function (r) { return r.ok ? r.text() : ''; }).then(function (text) {
        logBody.textContent = text || '(empty)';
        if (autoscroll) logBody.scrollTop = logBody.scrollHeight;
      }).catch(function () {});
    }
    poll();
    logTimer = setInterval(poll, 1000);
  }

  function loadMore() {
    var url = '/api/events/history?kind=step.*&limit=200';
    if (oldestId) url += '&before=' + encodeURIComponent(oldestId);
    fetch(url).then(function (r) { return r.ok ? r.json() : { events: [] }; }).then(function (res) {
      (res.events || []).forEach(function (ev) {
        if (ev.kind === 'step.start' || ev.kind === 'step.end') {
          timeline.push(ev);
          if (oldestId === null || (ev.id && ev.id < oldestId)) oldestId = ev.id;
        }
      });
      refreshAll();
    }).catch(function () {});
  }

  function wireDrawer() {
    var close = el('step-drawer-close');
    if (close) close.addEventListener('click', function () {
      el('step-drawer').hidden = true;
      if (logTimer) { clearInterval(logTimer); logTimer = null; }
    });
    var copy = el('step-drawer-copy');
    if (copy) copy.addEventListener('click', function () {
      var path = el('step-drawer-logpath').textContent;
      if (navigator.clipboard && path) navigator.clipboard.writeText(path).catch(function () {});
    });
    var pause = el('step-drawer-autoscroll');
    if (pause) pause.addEventListener('click', function () {
      autoscroll = !autoscroll;
      pause.setAttribute('aria-pressed', String(autoscroll));
      pause.textContent = autoscroll ? 'Pause autoscroll' : 'Resume autoscroll';
    });
    var clearBtn = el('phase-filter-clear');
    if (clearBtn) clearBtn.addEventListener('click', function () { togglePhaseFilter(phaseFilter); });
    var more = el('step-load-more');
    if (more) more.addEventListener('click', loadMore);
  }

  function startSSE() {
    if (typeof EventSource === 'undefined') return;
    var es = new EventSource('/api/events?kind=step.*');
    es.onmessage = function (e) {
      try { ingestEvent(JSON.parse(e.data)); } catch (_) { /* ignore malformed */ }
    };
    es.onerror = function () { /* browser auto-reconnects */ };
  }

  function start() {
    buildPhaseMachine();
    wireDrawer();
    loadMore();
    startSSE();
    setInterval(refreshAll, 2000);
  }

  global.Orchestration = { start: start };
})(window);
