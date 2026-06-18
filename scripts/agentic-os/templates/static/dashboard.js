(function (global) {
  function el(tag, txt, opts) {
    var e = document.createElement(tag);
    if (txt != null) e.textContent = String(txt);
    if (opts && opts.className) e.className = opts.className;
    return e;
  }

  function clearChildren(n) {
    while (n.firstChild) n.removeChild(n.firstChild);
  }

  // Issue #115 — inline error banner; replaces window.alert() so the
  // operator stays in flow and screen readers see a live region update.
  function showInlineError(container, message) {
    if (!container) return;
    var banner = container.querySelector('.inline-error');
    if (!banner) {
      banner = document.createElement('div');
      banner.className = 'inline-error';
      banner.setAttribute('role', 'alert');
      banner.setAttribute('aria-live', 'assertive');
      container.insertBefore(banner, container.firstChild);
    }
    banner.textContent = message;
  }

  function safeText(id, value) {
    var node = document.getElementById(id);
    if (node) node.textContent = value == null ? '' : String(value);
  }

  function setBadge(state) {
    var badge = document.getElementById('runtime-badge');
    if (!badge) return;
    badge.textContent = state;
    badge.className = 'badge badge-' +
      (state === 'ready' ? 'ok' : state === 'degraded' ? 'warn' : 'err');
  }

  function setBadgeUnreachable() {
    var badge = document.getElementById('runtime-badge');
    if (!badge) return;
    badge.textContent = 'unreachable';
    badge.className = 'badge badge-err';
  }

  function getTaskIdFromPath() {
    var parts = location.pathname.split('/').filter(function (p) { return p.length; });
    if (parts.length < 2 || parts[0] !== 'tasks') return null;
    return decodeURIComponent(parts[1]);
  }

  function fmtJson(value) {
    try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
  }

  function fmtLocalTs(value) {
    if (value == null || value === '') return '';
    var s = String(value);
    var d = new Date(s);
    if (isNaN(d.getTime())) return s;
    function pad(n) { return n < 10 ? '0' + n : '' + n; }
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
      + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  // ---- runtime ----
  function refreshRuntime() {
    return fetch('/api/status').then(function (r) {
      if (!r.ok) throw new Error('status ' + r.status);
      return r.json();
    }).then(function (s) {
      setBadge(s.runtime);
      // Issue #191 — the Runtime card reflects the operator work-item
      // queue (`work_items`), not the internal scheduler `tasks` table.
      // We fall back to the legacy `tasks` shape for compatibility with
      // older API payloads.
      var queueSrc = s.work_items || s.tasks || {};
      safeText('t-queued', queueSrc.queued != null ? queueSrc.queued : '0');
      safeText('t-running', queueSrc.running != null ? queueSrc.running : '0');
      safeText('t-failed', queueSrc.failed != null ? queueSrc.failed : '0');
      if (s.bugs) {
        safeText('b-open', s.bugs.open);
        safeText('b-known', s.bugs.known);
      }
      safeText('bl-open', (s.blockers_open || []).length);

      var lTb = document.querySelector('#leases tbody');
      if (lTb) {
        clearChildren(lTb);
        var leases = s.leases || [];
        if (!leases.length) {
          var emptyTr = document.createElement('tr');
          var emptyTd = el('td', '(no active leases)', { className: 'placeholder' });
          emptyTd.colSpan = 4;
          emptyTr.appendChild(emptyTd);
          lTb.appendChild(emptyTr);
        }
        leases.forEach(function (l) {
          var tr = document.createElement('tr');
          tr.appendChild(el('td', l.owner));
          tr.appendChild(el('td', l.pid));
          tr.appendChild(el('td', l.host));
          tr.appendChild(el('td', l.expires_at));
          lTb.appendChild(tr);
        });
      }

      var lastRun = document.getElementById('last-run');
      if (lastRun) lastRun.textContent = s.last_run ? fmtJson(s.last_run) : '(none)';
    }).catch(function () {
      setBadgeUnreachable();
    });
  }

  function startRuntimePolling() {
    refreshRuntime();
    setInterval(refreshRuntime, 2500);
  }

  // ---- config ----
  function _renderSourceList(elId, sources) {
    var el = document.getElementById(elId);
    if (!el) return;
    clearChildren(el);
    if (!Array.isArray(sources) || sources.length === 0) {
      var li = document.createElement('li');
      li.className = 'placeholder';
      li.textContent = '(none configured)';
      el.appendChild(li);
      return;
    }
    sources.forEach(function (src) {
      var li = document.createElement('li');
      var kindBadge = document.createElement('span');
      kindBadge.className = 'src-kind src-' + (src.type || 'unknown');
      kindBadge.textContent = src.type || '?';
      li.appendChild(kindBadge);
      var code = document.createElement('code');
      code.textContent = src.value || '';
      li.appendChild(code);
      el.appendChild(li);
    });
  }

  function _renderRunner(elId, runner) {
    var el = document.getElementById(elId);
    if (!el) return;
    if (!runner) {
      el.textContent = '(default)';
      el.className = 'runner-chip runner-default';
      return;
    }
    el.textContent = runner;
    el.className = 'runner-chip runner-' + runner.replace(/[^a-z0-9-]/gi, '-');
  }

  function _renderCredentials(elId, creds) {
    var el = document.getElementById(elId);
    if (!el) return;
    if (!creds || creds.ref_type === 'none' || creds.ref_type == null) {
      el.textContent = '(none)';
      el.className = 'creds-chip creds-none';
      return;
    }
    el.textContent = creds.ref_type + ' • ' + (creds.value || '');
    el.className = 'creds-chip creds-' + creds.ref_type;
  }

  function loadConfig() {
    return fetch('/api/config').then(function (r) {
      if (!r.ok) throw new Error('config ' + r.status);
      return r.json();
    }).then(function (cfg) {
      safeText('cfg-source', cfg.source || '<missing>');
      var sut = cfg.sut || {};
      safeText('cfg-sut-kind', sut.kind || '(unset)');
      safeText('cfg-sut-root', sut.root || '');
      safeText('cfg-tests-dir', sut.tests_dir || 'tests');
      safeText('cfg-compose', sut.compose_file || '');
      safeText('cfg-test-runner', sut.test_runner || '');
      var hc = sut.healthcheck || {};
      var hcText = (hc.command || []).join(' ');
      safeText('cfg-healthcheck', hcText || '(none)');
      safeText('cfg-base-url', sut.base_url || '(unset)');
      safeText('cfg-api-base-url', sut.api_base_url || '(unset)');
      safeText('cfg-ui-url', sut.ui_url || '(unset)');
      var tests = sut.tests || {};
      _renderRunner('cfg-runner-api', (tests.api || {}).runner);
      _renderRunner('cfg-runner-ui', (tests.ui || {}).runner);
      _renderSourceList('cfg-openapi-sources', (sut.openapi || {}).sources);
      _renderSourceList('cfg-docs-sources', (sut.docs || {}).sources);
      _renderCredentials('cfg-credentials', sut.credentials);
      var dash = cfg.dashboard || {};
      safeText('cfg-dashboard-host', (dash.host || '127.0.0.1') + ':' + (dash.port || 8765));
      var gateEl = document.getElementById('cfg-write-gate');
      if (gateEl) {
        gateEl.textContent = dash.enable_write_endpoints ? 'enabled' : 'disabled';
        gateEl.className = 'write-chip write-' + (dash.enable_write_endpoints ? 'on' : 'off');
      }
    }).catch(function () {
      safeText('cfg-source', '(unreachable)');
    });
  }

  // ---- patches ----
  function _patchStateChip(state) {
    var span = document.createElement('span');
    span.className = 'chip chip-' + state;
    span.textContent = state;
    return span;
  }

  function refreshPatchesGlobal() {
    var tbody = document.querySelector('#patches-table tbody');
    if (!tbody) return Promise.resolve();
    return fetch('/api/patches').then(function (r) {
      if (!r.ok) throw new Error('patches ' + r.status);
      return r.json();
    }).then(function (payload) {
      var counts = { waiting: 0, rejected: 0, abandoned: 0, approved: 0 };
      var rows = payload.patches || [];
      clearChildren(tbody);
      if (!rows.length) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 4;
        td.className = 'placeholder';
        td.textContent = '(no patches yet)';
        tr.appendChild(td);
        tbody.appendChild(tr);
      } else {
        rows.forEach(function (p) {
          counts[p.state] = (counts[p.state] || 0) + 1;
          var tr = document.createElement('tr');
          var tdTask = document.createElement('td');
          var a = document.createElement('a');
          a.href = '/tasks/' + encodeURIComponent(p.work_item_id);
          a.appendChild(el('code', p.work_item_id));
          tdTask.appendChild(a);
          tr.appendChild(tdTask);
          var tdPatch = document.createElement('td');
          tdPatch.appendChild(el('code', p.patch_path));
          tr.appendChild(tdPatch);
          var tdState = document.createElement('td');
          tdState.appendChild(_patchStateChip(p.state));
          tr.appendChild(tdState);
          tr.appendChild(el('td', p.patch_created || ''));
          tbody.appendChild(tr);
        });
      }
      safeText('ps-waiting', counts.waiting || 0);
      safeText('ps-rejected', counts.rejected || 0);
      safeText('ps-abandoned', counts.abandoned || 0);
      safeText('ps-approved', counts.approved || 0);
    }).catch(function () {});
  }

  function startPatchPolling() {
    refreshPatchesGlobal();
    setInterval(refreshPatchesGlobal, 4000);
  }

  function _currentTaskId() {
    var m = window.location.pathname.match(/^\/tasks\/([^/]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  }

  function refreshTaskPatches() {
    var tbody = document.querySelector('#task-patches tbody');
    var taskId = _currentTaskId();
    if (!tbody || !taskId) return Promise.resolve();
    return Promise.all([
      fetch('/api/patches/' + encodeURIComponent(taskId)).then(function (r) {
        return r.ok ? r.json() : { patches: [] };
      }),
      fetch('/api/config').then(function (r) {
        return r.ok ? r.json() : { dashboard: {} };
      }),
    ]).then(function (parts) {
      var rows = (parts[0].patches || []);
      var writesOn = !!(parts[1].dashboard && parts[1].dashboard.enable_write_endpoints);
      clearChildren(tbody);
      if (!rows.length) {
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.colSpan = 4;
        td.className = 'placeholder';
        td.textContent = '(no patches yet)';
        tr.appendChild(td);
        tbody.appendChild(tr);
        return;
      }
      rows.forEach(function (p) {
        var tr = document.createElement('tr');
        tr.appendChild((function () {
          var td = document.createElement('td');
          td.appendChild(el('code', p.patch_path));
          return td;
        })());
        var tdState = document.createElement('td');
        tdState.appendChild(_patchStateChip(p.state));
        tr.appendChild(tdState);
        tr.appendChild(el('td', p.patch_created || ''));
        var tdAction = document.createElement('td');
        if (p.state === 'approved_pending_apply') {
          var applyBtn = document.createElement('button');
          applyBtn.type = 'button';
          applyBtn.className = 'btn-small';
          applyBtn.textContent = 'Apply patch';
          applyBtn.disabled = !writesOn;
          if (!writesOn) applyBtn.title = 'dashboard.enable_write_endpoints=false';
          applyBtn.addEventListener('click', function () {
            applyBtn.disabled = true;
            applyBtn.textContent = 'Applying...';
            fetch('/api/tasks/' + encodeURIComponent(taskId) + '/apply-patch', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: '{}'
            }).then(function (r) {
              return r.json().catch(function () { return {}; }).then(function (payload) {
                if (!r.ok) throw new Error(payload.message || payload.error || ('HTTP ' + r.status));
                return payload;
              });
            }).then(function () {
              refreshTaskPatches();
              loadTaskDetail();
            }).catch(function (err) {
              applyBtn.textContent = 'Apply failed';
              applyBtn.title = err && err.message ? err.message : 'apply failed';
            });
          });
          tdAction.appendChild(applyBtn);
        } else if (p.state === 'waiting' || p.state === 'rejected') {
          var btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'btn-danger btn-small';
          btn.textContent = 'Abandon';
          btn.disabled = !writesOn;
          if (!writesOn) btn.title = 'dashboard.enable_write_endpoints=false';
          btn.addEventListener('click', function () { _openAbandonDialog(taskId, p.patch_path); });
          tdAction.appendChild(btn);
        } else {
          var span = document.createElement('span');
          span.className = 'muted';
          span.textContent = '—';
          tdAction.appendChild(span);
        }
        tr.appendChild(tdAction);
        tbody.appendChild(tr);
      });
    }).catch(function () {});
  }

  function startTaskPatchPolling() {
    refreshTaskPatches();
    setInterval(refreshTaskPatches, 4000);
  }

  function _openAbandonDialog(taskId, patchPath) {
    var dlg = document.getElementById('abandon-dialog');
    if (!dlg) return;
    var label = document.getElementById('abandon-patch-label');
    var reason = document.getElementById('abandon-reason');
    var cancel = document.getElementById('abandon-cancel');
    var confirm = document.getElementById('abandon-confirm');
    if (label) label.textContent = patchPath;
    if (reason) reason.value = '';
    if (cancel) cancel.onclick = function () { dlg.close(); };
    if (confirm) confirm.onclick = function (ev) {
      ev.preventDefault();
      var text = (reason && reason.value || '').trim();
      if (text.length < 6) {
        reason.focus();
        return;
      }
      fetch('/api/tasks/' + encodeURIComponent(taskId) + '/abandon-patch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ patch_path: patchPath, reason: text }),
      }).then(function (r) {
        if (!r.ok) return r.json().then(function (e) { throw new Error(e.message || 'abandon failed'); });
        dlg.close();
        refreshTaskPatches();
      }).catch(function (err) {
        // Issue #115 — no window.alert; surface the error inline so
        // the operator stays in the modal and can correct the input.
        showInlineError(dlg, err && err.message ? err.message : 'abandon failed');
      });
    };
    if (typeof dlg.showModal === 'function') dlg.showModal();
    else dlg.setAttribute('open', '');
  }

  // ---- tasks list ----
  function refreshTasksList() {
    var tb = document.querySelector('#work-items tbody');
    if (!tb) return Promise.resolve();
    return Promise.all([
      fetch('/api/tasks').then(function (r) {
        if (!r.ok) throw new Error('tasks ' + r.status);
        return r.json();
      }),
      fetch('/api/config').then(function (r) {
        return r.ok ? r.json() : { dashboard: {} };
      }).catch(function () { return { dashboard: {} }; })
    ]).then(function (parts) {
      var payload = parts[0] || {};
      var cfg = parts[1] || {};
      var rows = payload.tasks || [];
      var orphans = payload.orphans || 0;
      _queueState.rows = rows;
      _queueState.orphans = orphans;
      _queueState.writeEnabled = !!(cfg.dashboard && cfg.dashboard.enable_write_endpoints);
      _queueState.lastFetchOk = true;
      var orphanBadge = document.getElementById('orphan-count');
      if (orphanBadge) {
        if (orphans > 0) {
          orphanBadge.hidden = false;
          orphanBadge.className = 'badge badge-err';
          orphanBadge.textContent = orphans + ' missing spec';
        } else {
          orphanBadge.hidden = true;
        }
      }
      var pruneBtn = document.getElementById('prune-orphans-btn');
      if (pruneBtn) {
        pruneBtn.hidden = orphans === 0;
      }
      _renderQueueViewMode();
    }).catch(function () {
      _queueState.lastFetchOk = false;
      clearChildren(tb);
      var tr = document.createElement('tr');
      var td = el('td', 'unreachable', { className: 'badge-err' });
      td.colSpan = 8;
      tr.appendChild(td);
      tb.appendChild(tr);
    });
  }

  function wirePruneOrphans() {
    var btn = document.getElementById('prune-orphans-btn');
    var msg = document.getElementById('prune-msg');
    if (!btn) return;
    btn.addEventListener('click', function () {
      if (!confirm('Drop all task rows whose spec file is missing on disk? '
                   + 'This cannot be undone.')) {
        return;
      }
      btn.disabled = true;
      if (msg) {
        msg.hidden = false;
        msg.className = 'msg muted';
        msg.textContent = 'Pruning…';
      }
      fetch('/api/tasks/prune', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (payload) {
          if (!r.ok) {
            throw new Error(payload.message || payload.error || ('HTTP ' + r.status));
          }
          return payload;
        });
      }).then(function (payload) {
        if (msg) {
          msg.className = 'msg ok';
          msg.textContent = 'Pruned ' + (payload.count || 0) + ' orphan task(s).';
        }
        return refreshTasksList();
      }).catch(function (e) {
        if (msg) {
          msg.className = 'msg err';
          msg.textContent = 'Prune failed: ' + e.message;
        }
      }).then(function () {
        btn.disabled = false;
      });
    });
  }

  // Issue #198 — queue filter/search/group state. Cached server payload
  // so filter changes re-render without an extra round trip.
  var _queueState = { rows: [], orphans: 0, lastFetchOk: true };

  function _queueFilters() {
    var q = (document.getElementById('queue-search') || {}).value || '';
    var st = (document.getElementById('queue-filter-status') || {}).value || '';
    var pr = (document.getElementById('queue-filter-priority') || {}).value || '';
    var view = (document.getElementById('queue-view-mode') || {}).value || 'table';
    return { q: q.trim().toLowerCase(), status: st, priority: pr, view: view };
  }

  function _filterRows(rows, f) {
    return rows.filter(function (t) {
      if (f.status && t.status !== f.status) return false;
      if (f.priority && t.priority !== f.priority) return false;
      if (f.q) {
        var hay = ((t.id || '') + ' ' + (t.title || '') + ' ' + (t.spec_path || '')).toLowerCase();
        if (hay.indexOf(f.q) === -1) return false;
      }
      return true;
    });
  }

  function _laneFor(t) {
    var debt = t.candidate_debt || {};
    var pending = Number(debt.needs_operator_decision || 0);
    if (t.status === 'blocked') return 'blocked';
    if (t.status === 'failed') return 'failed';
    if (t.status === 'running') return 'running';
    if (pending > 0) return 'needs-action';
    if (t.status === 'queued') return 'needs-action';
    if (t.status === 'done') return 'done';
    return 'running';
  }

  function _renderQueueLanes(rows) {
    var container = document.getElementById('queue-lanes-view');
    if (!container) return;
    var lanes = [
      { id: 'needs-action', label: 'Needs action' },
      { id: 'running',      label: 'Running' },
      { id: 'blocked',      label: 'Blocked' },
      { id: 'failed',       label: 'Failed' },
      { id: 'done',         label: 'Done' }
    ];
    var grouped = {};
    lanes.forEach(function (l) { grouped[l.id] = []; });
    rows.forEach(function (t) {
      var lane = _laneFor(t);
      (grouped[lane] = grouped[lane] || []).push(t);
    });
    while (container.firstChild) container.removeChild(container.firstChild);
    lanes.forEach(function (lane) {
      var col = document.createElement('div');
      col.className = 'queue-lane';
      col.dataset.lane = lane.id;
      var h3 = document.createElement('h3');
      h3.textContent = lane.label;
      var count = document.createElement('span');
      count.className = 'lane-count';
      count.textContent = String((grouped[lane.id] || []).length);
      h3.appendChild(count);
      col.appendChild(h3);
      var items = grouped[lane.id] || [];
      if (!items.length) {
        var empty = document.createElement('div');
        empty.className = 'muted small';
        empty.textContent = '—';
        col.appendChild(empty);
      }
      items.forEach(function (t) {
        var card = document.createElement('a');
        card.className = 'queue-card';
        card.href = '/tasks/' + encodeURIComponent(t.id);
        var title = document.createElement('div');
        title.className = 'queue-title';
        title.textContent = t.title || t.id;
        card.appendChild(title);
        var meta = document.createElement('div');
        meta.className = 'queue-meta';
        var debt = t.candidate_debt || {};
        var bits = [t.priority || '?', t.status || '?', t.id];
        if ((debt.total || 0) > 0) {
          bits.push((debt.generate_now || 0) + '/' + (debt.total || 0) + ' approved');
        }
        meta.textContent = bits.join(' · ');
        card.appendChild(meta);
        if ((debt.total || 0) > 0) {
          var prog = document.createElement('div');
          prog.className = 'queue-progress';
          var fill = document.createElement('span');
          var pct = Math.round(((debt.generate_now || 0) / (debt.total || 1)) * 100);
          fill.style.width = pct + '%';
          prog.appendChild(fill);
          card.appendChild(prog);
        }
        col.appendChild(card);
      });
      container.appendChild(col);
    });
  }

  function _renderQueueViewMode() {
    var f = _queueFilters();
    var tableView = document.getElementById('queue-table-view');
    var lanesView = document.getElementById('queue-lanes-view');
    if (tableView && lanesView) {
      tableView.hidden = f.view !== 'table';
      lanesView.hidden = f.view !== 'lanes';
    }
    var rows = _filterRows(_queueState.rows, f);
    if (f.view === 'lanes') {
      _renderQueueLanes(rows);
    } else {
      _renderQueueTable(rows);
    }
  }

  function _renderQueueTable(rows) {
    var tb = document.querySelector('#work-items tbody');
    if (!tb) return;
    clearChildren(tb);
    if (!rows.length) {
      var tr = document.createElement('tr');
      var td = el('td', '', { className: 'placeholder' });
      td.textContent = '(no tasks match the current filter)';
      td.colSpan = 8;
      tr.appendChild(td);
      tb.appendChild(tr);
      return;
    }
    var writeEnabled = !!_queueState.writeEnabled;
    rows.forEach(function (t) {
      var tr = document.createElement('tr');
      if (t.spec_missing) tr.className = 'row-orphan';
      var tdId = document.createElement('td');
      var a = document.createElement('a');
      a.href = '/tasks/' + encodeURIComponent(t.id);
      a.appendChild(el('code', t.id));
      tdId.appendChild(a);
      tr.appendChild(tdId);
      var tdStatus = document.createElement('td');
      var b = document.createElement('span');
      b.className = 'badge badge-state-' + (t.status || 'queued');
      b.textContent = t.status;
      tdStatus.appendChild(b);
      var debt = t.candidate_debt || {};
      var pending = Number(debt.needs_operator_decision || 0);
      if (t.done_with_pending_decisions || (t.status === 'done' && pending > 0)) {
        var warn = document.createElement('span');
        warn.className = 'badge badge-warn';
        warn.style.marginLeft = '6px';
        warn.textContent = pending + ' pending decisions';
        warn.title = 'done with pending test decisions — '
          + pending + ' of ' + (debt.total || 0)
          + ' planned candidates still need an operator decision';
        tdStatus.appendChild(warn);
      }
      tr.appendChild(tdStatus);
      tr.appendChild(el('td', t.priority));
      tr.appendChild(el('td', t.title));
      var tdSpec = document.createElement('td');
      tdSpec.appendChild(el('code', t.spec_path || ''));
      if (t.spec_missing) {
        var missingBadge = document.createElement('span');
        missingBadge.className = 'badge badge-err';
        missingBadge.style.marginLeft = '6px';
        missingBadge.textContent = 'MISSING';
        missingBadge.title = 'spec file not found on disk; prune to drop this orphan row';
        tdSpec.appendChild(missingBadge);
      }
      tr.appendChild(tdSpec);
      var lastLabel = t.last_artifact_kind
        ? (t.last_artifact_kind + ' — ' + (t.last_artifact_path || ''))
        : '(none yet)';
      tr.appendChild(el('td', lastLabel));
      tr.appendChild(el('td', fmtLocalTs(t.created_at)));
      // Issue #224 — per-row delete action.
      var tdAction = document.createElement('td');
      var delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.className = 'btn btn-danger';
      delBtn.textContent = 'Delete';
      delBtn.disabled = !writeEnabled;
      if (!writeEnabled) {
        delBtn.title = 'dashboard.enable_write_endpoints=false';
      }
      delBtn.addEventListener('click', function () {
        deleteTaskFromList(t);
      });
      tdAction.appendChild(delBtn);
      tr.appendChild(tdAction);
      tb.appendChild(tr);
    });
  }

  // Issue #224 — delete a single work item from the dashboard.
  function deleteTaskFromList(task) {
    if (!task || !task.id) return;
    var host = document.querySelector('main') || document.body;
    var confirmed = window.confirm(
      'Delete task ' + task.id + ' (' + (task.title || '') + ')?\n\n'
      + 'This removes the work item and its runtime artifacts. '
      + 'The spec markdown file on disk is left in place.'
    );
    if (!confirmed) return;
    fetch('/api/tasks/' + encodeURIComponent(task.id), {
      method: 'DELETE',
      headers: { 'Accept': 'application/json' }
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) {
          showInlineError(
            host,
            'Delete failed: '
              + (data.message || data.error || ('HTTP ' + r.status))
          );
          return;
        }
        refreshTasksList();
      });
    }).catch(function (err) {
      showInlineError(
        host,
        'Delete failed: ' + (err && err.message ? err.message : String(err))
      );
    });
  }

  function _wireQueueControls() {
    ['queue-search', 'queue-filter-status', 'queue-filter-priority', 'queue-view-mode'].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el || el.dataset.wired === '1') return;
      el.dataset.wired = '1';
      el.addEventListener('input', _renderQueueViewMode);
      el.addEventListener('change', _renderQueueViewMode);
    });
  }

  function startTasksListPolling() {
    wirePruneOrphans();
    _wireQueueControls();
    refreshTasksList();
    setInterval(refreshTasksList, 5000);
  }

  // ---- task form ----
  function wireTaskForm() {
    var form = document.getElementById('task-form');
    if (!form) return;
    var button = document.getElementById('task-submit');
    var msg = document.getElementById('task-msg');
    var warn = document.getElementById('write-gate-warning');

    function applyFormGate() {
      return fetch('/api/config').then(function (r) {
        return r.ok ? r.json() : null;
      }).then(function (cfg) {
        if (!cfg || !warn) return;
        var dash = cfg.dashboard || {};
        var enabled = !!dash.enable_write_endpoints;
        var fullMode = !!dash.full_mode;
        var autonomyOn = !!dash.autonomy_unlocks_writes;
        if (enabled) {
          warn.className = 'msg ok';
          if (autonomyOn) {
            warn.textContent = 'writes unlocked by full autonomy session';
          } else if (fullMode) {
            warn.textContent = 'writes unlocked by serve --full';
          } else {
            warn.textContent = 'dashboard.enable_write_endpoints = true';
          }
        } else {
          warn.className = 'msg err';
          warn.textContent =
            'writes disabled — set dashboard.enable_write_endpoints=true, '
            + 'restart with serve --full, or start full autonomy';
        }
      }).catch(function () {});
    }

    applyFormGate();
    setInterval(applyFormGate, 4000);

    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      button.disabled = true;
      msg.className = 'msg muted';
      msg.textContent = 'Creating…';
      var fd = new FormData(form);
      var payload = {};
      fd.forEach(function (value, key) {
        if (typeof value === 'string' && value.trim().length) payload[key] = value;
      });
      fetch('/api/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          if (!r.ok) {
            var detail = body.message || body.error || ('HTTP ' + r.status);
            throw new Error(detail);
          }
          return body;
        });
      }).then(function (body) {
        msg.className = 'msg ok';
        msg.textContent = 'created ' + body.work_item.id;
        form.reset();
        setTimeout(function () {
          location.href = '/tasks/' + encodeURIComponent(body.work_item.id);
        }, 500);
      }).catch(function (e) {
        msg.className = 'msg err';
        msg.textContent = e.message;
      }).then(function () {
        button.disabled = false;
      });
    });
  }

  // ---- task detail ----
  function loadTaskDetail() {
    var id = getTaskIdFromPath();
    if (!id) return Promise.resolve();
    safeText('task-id', id);
    var headers = { 'Accept': 'application/json' };
    var detailPromise = fetch('/api/tasks/' + encodeURIComponent(id), { headers: headers })
      .then(function (r) {
        if (r.status === 404) throw new Error('not_found');
        if (!r.ok) throw new Error('http ' + r.status);
        return r.json();
      });
    var specPromise = fetch('/api/tasks/' + encodeURIComponent(id) + '/spec', { headers: headers })
      .then(function (r) {
        if (!r.ok) return null;
        return r.json();
      })
      .catch(function () { return null; });

    return Promise.all([detailPromise, specPromise]).then(function (parts) {
      var detail = parts[0];
      var spec = parts[1];
      var meta = document.getElementById('meta');
      clearChildren(meta);
      var w = detail.work_item || {};
      ['id', 'status', 'priority', 'sut_root', 'spec_path', 'created_at', 'updated_at']
        .forEach(function (k) {
          var dt = el('dt', k);
          var dd;
          if (k === 'status') {
            dd = document.createElement('dd');
            var b = document.createElement('span');
            b.className = 'badge badge-state-' + (w.status || 'queued');
            b.textContent = w.status || '–';
            dd.appendChild(b);
          } else {
            dd = el('dd', w[k] == null ? '' : String(w[k]));
          }
          meta.appendChild(dt);
          meta.appendChild(dd);
        });
      renderTaskStatusBadge('task-status-badge', w.status);
      // Issue #330 — surface the idempotent-no-op signal as a "covered"
      // banner so the operator does not stare at an unchanged status badge
      // after `implement-tests` finds every requested surface already in
      // the coverage ledger.
      renderCoverageState(detail.coverage_state);
      // Issue #192 — surface candidate debt next to the task title badge
      // and as a meta row so the operator sees the pending decisions
      // even when the task is `done`.
      renderCandidateDebt(detail);
      renderTimeline(detail.artifacts || [], w.status);
      renderArtifactGroups(detail.artifacts || []);
      renderTaskCandidates(id);
      var specEl = document.getElementById('spec');
      if (specEl) {
        specEl.textContent = spec && spec.markdown ? spec.markdown : '(spec not available)';
      }
      renderPatchAndGate(detail.artifacts || []);
      renderGeneratedTests(detail.generated_tests || []);
      wireActionButtons(id);
    }).catch(function (err) {
      var meta = document.getElementById('meta');
      if (meta) {
        clearChildren(meta);
        meta.appendChild(el('dt', 'error'));
        meta.appendChild(el('dd', err.message));
      }
    });
  }

  function startTaskDetailPolling() {
    loadTaskDetail();
    setInterval(loadTaskDetail, 5000);
    // Issue #225 — generated tests review wiring.
    wireGeneratedTestsReview();
  }

  // ---- generated tests review (issue #225) -------------------------
  // PR #226 codex review — polling fully clears the list, which would
  // discard unsaved textarea edits. Track open/dirty editors so the
  // 15s refresh skips while a file is being edited; user can still
  // force a reload with the "Refresh" button.
  var _generatedTestsOpen = new Set();
  var _generatedTestsDirty = new Set();
  function _hasUnsavedGeneratedTestEdits() {
    return _generatedTestsDirty.size > 0;
  }
  function wireGeneratedTestsReview() {
    var refreshBtn = document.getElementById('generated-tests-refresh');
    if (!refreshBtn || refreshBtn.dataset.wired === '1') return;
    refreshBtn.dataset.wired = '1';
    refreshBtn.addEventListener('click', function () {
      // Manual refresh is operator-initiated; warn before nuking edits.
      if (_hasUnsavedGeneratedTestEdits()
          && !window.confirm(
            'Discard unsaved test-file edits and reload from disk?'
          )) {
        return;
      }
      _generatedTestsDirty.clear();
      refreshGeneratedTests({ force: true });
    });
    refreshGeneratedTests({ force: true });
    setInterval(function () {
      // Auto-refresh only when nothing is being edited so the operator
      // never loses keystrokes between polls.
      if (_hasUnsavedGeneratedTestEdits()) return;
      refreshGeneratedTests({});
    }, 15000);
  }

  function _taskIdFromPath() {
    var m = window.location.pathname.match(/^\/tasks\/([^\/?#]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  }

  function refreshGeneratedTests(opts) {
    var taskId = _taskIdFromPath();
    var listHost = document.getElementById('generated-tests-list');
    var msg = document.getElementById('generated-tests-msg');
    var countEl = document.getElementById('generated-tests-count');
    if (!taskId || !listHost) return;
    // Bail out automatic refreshes if the operator has unsaved edits.
    // `opts.force` lets the explicit Refresh button bypass this guard.
    if (!opts || !opts.force) {
      if (_hasUnsavedGeneratedTestEdits()) return;
    }
    Promise.all([
      fetch('/api/tasks/' + encodeURIComponent(taskId) + '/generated-tests')
        .then(function (r) { return r.ok ? r.json() : { items: [] }; })
        .catch(function () { return { items: [] }; }),
      fetch('/api/config').then(function (r) {
        return r.ok ? r.json() : { dashboard: {} };
      }).catch(function () { return { dashboard: {} }; })
    ]).then(function (parts) {
      var items = (parts[0] && parts[0].items) || [];
      var writeEnabled = !!(parts[1] && parts[1].dashboard
        && parts[1].dashboard.enable_write_endpoints);
      clearChildren(listHost);
      if (countEl) countEl.textContent = items.length + ' files';
      if (!items.length) {
        if (msg) {
          msg.className = 'msg muted';
          msg.textContent = 'No generated tests yet for this task. Run '
            + '"Generate tests" first.';
        }
        return;
      }
      if (msg) {
        msg.className = 'msg muted';
        msg.textContent = 'Edits save directly to the path shown; the test '
          + 'runner picks them up on the next "Run tests".';
      }
      items.forEach(function (it) {
        listHost.appendChild(_buildGeneratedTestRow(taskId, it, writeEnabled));
      });
    });
  }

  function _buildGeneratedTestRow(taskId, item, writeEnabled) {
    var li = document.createElement('li');
    li.className = 'generated-test-item';
    var head = document.createElement('div');
    head.className = 'generated-test-head';
    var code = document.createElement('code');
    code.textContent = item.relative_path;
    head.appendChild(code);
    if (!item.exists) {
      var badge = document.createElement('span');
      badge.className = 'badge badge-err';
      badge.style.marginLeft = '8px';
      badge.textContent = 'missing on disk';
      badge.title = 'Apply the patch first (review-gate --apply-patch) or '
        + 'restore the file before editing.';
      head.appendChild(badge);
    }
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = item.exists ? 'Preview' : 'Inspect path';
    btn.className = 'btn-small';
    btn.style.marginLeft = 'auto';
    head.appendChild(btn);
    li.appendChild(head);

    var bodyHost = document.createElement('div');
    bodyHost.className = 'generated-test-body';
    bodyHost.hidden = true;
    li.appendChild(bodyHost);

    var loaded = false;
    btn.addEventListener('click', function () {
      if (!bodyHost.hidden) {
        bodyHost.hidden = true;
        btn.textContent = item.exists ? 'Preview' : 'Inspect path';
        return;
      }
      bodyHost.hidden = false;
      btn.textContent = 'Hide';
      if (loaded) return;
      _loadGeneratedTestBody(taskId, item, bodyHost, writeEnabled);
      loaded = true;
    });
    return li;
  }

  function _loadGeneratedTestBody(taskId, item, host, writeEnabled) {
    clearChildren(host);
    var loadingMsg = el('p', 'loading…', { className: 'muted' });
    host.appendChild(loadingMsg);
    fetch('/api/tasks/' + encodeURIComponent(taskId)
      + '/generated-tests/' + item.relative_path
    ).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        return { ok: r.ok, status: r.status, data: data };
      });
    }).then(function (resp) {
      clearChildren(host);
      if (!resp.ok) {
        var err = el('p', '', { className: 'msg err' });
        err.textContent = 'Load failed: '
          + (resp.data.message || resp.data.error || ('HTTP ' + resp.status));
        host.appendChild(err);
        return;
      }
      var preview = document.createElement('pre');
      preview.className = 'generated-test-preview';
      preview.textContent = resp.data.content || '';
      host.appendChild(preview);
      var editor = document.createElement('textarea');
      editor.className = 'generated-test-editor';
      editor.rows = 16;
      editor.value = resp.data.content || '';
      editor.disabled = !writeEnabled;
      var baseline = editor.value;
      editor.addEventListener('input', function () {
        if (editor.value !== baseline) {
          _generatedTestsDirty.add(item.relative_path);
        } else {
          _generatedTestsDirty.delete(item.relative_path);
        }
      });
      host.appendChild(editor);
      var actions = document.createElement('div');
      actions.className = 'generated-test-actions';
      var saveBtn = document.createElement('button');
      saveBtn.type = 'button';
      saveBtn.textContent = 'Save changes';
      saveBtn.className = 'btn';
      saveBtn.disabled = !writeEnabled;
      if (!writeEnabled) {
        saveBtn.title = 'dashboard.enable_write_endpoints=false';
      }
      saveBtn.addEventListener('click', function () {
        _saveGeneratedTest(taskId, item, editor.value, host, function () {
          baseline = editor.value;
          _generatedTestsDirty.delete(item.relative_path);
        });
      });
      actions.appendChild(saveBtn);
      host.appendChild(actions);
    }).catch(function (err) {
      clearChildren(host);
      var fail = el('p', '', { className: 'msg err' });
      fail.textContent = 'Load failed: '
        + (err && err.message ? err.message : String(err));
      host.appendChild(fail);
    });
  }

  function _saveGeneratedTest(taskId, item, content, host, onSuccess) {
    var status = document.createElement('p');
    status.className = 'msg muted';
    status.textContent = 'saving…';
    host.appendChild(status);
    fetch('/api/tasks/' + encodeURIComponent(taskId)
      + '/generated-tests/' + item.relative_path, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: content })
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        return { ok: r.ok, status: r.status, data: data };
      });
    }).then(function (resp) {
      if (!resp.ok) {
        status.className = 'msg err';
        status.textContent = 'Save failed: '
          + (resp.data.message || resp.data.error || ('HTTP ' + resp.status));
        return;
      }
      status.className = 'msg ok';
      status.textContent = 'Saved (' + (resp.data.size || 0) + ' bytes).';
      if (typeof onSuccess === 'function') onSuccess();
    }).catch(function (err) {
      status.className = 'msg err';
      status.textContent = 'Save failed: '
        + (err && err.message ? err.message : String(err));
    });
  }

  var TIMELINE_STEP_TO_KINDS = {
    'analyze':          ['analysis', 'sut_map'],
    'plan':             ['test_plan'],
    'implement-tests':  ['patch'],
    'review-gate':      ['gate'],
    'apply-patch':      ['apply'],
    'run-tests':        ['run', 'report', 'evidence'],
    'final-gate':       []  // final-gate registers another 'gate' artifact; tracked via task status.
  };

  function renderTaskStatusBadge(id, status) {
    var n = document.getElementById(id);
    if (!n) return;
    n.textContent = '';
    var b = document.createElement('span');
    b.className = 'badge badge-state-' + (status || 'queued');
    b.textContent = status || '?';
    n.appendChild(b);
  }

  // Issue #330 — render the idempotent-no-op signal. The backend (PR for
  // #330) returns a `coverage_state` payload only when the latest
  // generation event for this work item was `work_item.implement_idempotent_noop`
  // (no newer `work_item.patch_generated` has superseded it), so any value
  // here means "all requested surfaces already covered". A falsy value
  // hides the banner; the autonomy flow is unchanged (the backend never
  // mutates work-item status to surface this).
  function renderCoverageState(state) {
    var section = document.getElementById('section-coverage-state');
    if (!section) return;
    if (!state || state.state !== 'covered') {
      section.hidden = true;
      return;
    }
    section.hidden = false;
    var textEl = document.getElementById('coverage-state-text');
    var skipped = Array.isArray(state.skipped_surfaces)
      ? state.skipped_surfaces : [];
    if (textEl) {
      var when = state.ts ? ' (last noop at ' + state.ts + ')' : '';
      textEl.textContent = skipped.length
        ? ('All ' + skipped.length + ' requested surfaces already covered '
           + 'by existing specs — nothing to generate' + when + '.')
        : ('Latest implement-tests run found every requested surface '
           + 'already covered' + when + '.');
    }
    var countEl = document.getElementById('coverage-state-skipped-count');
    if (countEl) countEl.textContent = String(skipped.length);
    var listEl = document.getElementById('coverage-state-skipped');
    if (listEl) {
      clearChildren(listEl);
      if (!skipped.length) {
        var li = document.createElement('li');
        li.className = 'placeholder';
        li.textContent = '(none)';
        listEl.appendChild(li);
        return;
      }
      skipped.forEach(function (s) {
        var li = document.createElement('li');
        var kind = s && s.surface_kind ? String(s.surface_kind) : '?';
        var key = s && s.surface_key ? String(s.surface_key) : '?';
        var ak = s && s.assertion_kind ? String(s.assertion_kind) : '?';
        var label = document.createElement('code');
        label.textContent = kind + ' · ' + key + ' · ' + ak;
        li.appendChild(label);
        listEl.appendChild(li);
      });
    }
  }

  // Issue #192 — render the candidate plan summary so operators see
  // pending operator decisions even on `done` tasks. We append a warning
  // chip next to the task title status badge and a meta row with the
  // full breakdown (total/approved/generated/rejected/needs-decision).
  function renderCandidateDebt(detail) {
    var badgeHost = document.getElementById('task-status-badge');
    var meta = document.getElementById('meta');
    if (!detail || (!badgeHost && !meta)) return;
    var debt = detail.candidate_debt || {};
    var w = detail.work_item || {};
    var pending = Number(debt.needs_operator_decision || 0);
    var total = Number(debt.total || 0);
    var warn = detail.done_with_pending_decisions
      || (w.status === 'done' && pending > 0);

    if (badgeHost) {
      // Remove any stale warning chip from a previous render.
      var prior = badgeHost.querySelector('.candidate-debt-warn');
      if (prior) prior.remove();
      if (warn) {
        var chip = document.createElement('span');
        chip.className = 'badge badge-warn candidate-debt-warn';
        chip.style.marginLeft = '8px';
        chip.textContent = 'done with ' + pending + ' pending decisions';
        chip.title = pending + ' of ' + total
          + ' planned candidates still need an operator decision';
        badgeHost.appendChild(chip);
      }
    }

    if (meta && total > 0) {
      var dt = document.createElement('dt');
      dt.textContent = 'plan summary';
      var dd = document.createElement('dd');
      dd.textContent = 'total ' + total
        + ' · approved ' + (debt.generate_now || 0)
        + ' · needs decision ' + pending
        + ' · not testable ' + (debt.not_testable || 0)
        + ' · blocked ' + (debt.blocked_missing_docs || 0);
      if (warn) {
        dd.style.color = 'var(--warn, #b45309)';
        dd.style.fontWeight = '600';
      }
      meta.appendChild(dt);
      meta.appendChild(dd);
    }
  }

  function renderTimeline(artifacts, status) {
    var ol = document.getElementById('task-timeline');
    if (!ol) return;
    var present = {};
    (artifacts || []).forEach(function (a) { present[a.kind] = true; });
    Array.prototype.forEach.call(ol.children, function (li) {
      var step = li.getAttribute('data-step');
      var kinds = TIMELINE_STEP_TO_KINDS[step] || [];
      var done = kinds.length && kinds.some(function (k) { return present[k]; });
      li.classList.remove('step-done', 'step-active', 'step-failed', 'step-blocked');
      if (done) li.classList.add('step-done');
      if (step === 'final-gate' && status === 'done') li.classList.add('step-done');
      if (status === 'failed' && !done) li.classList.remove('step-done');
    });
    if (status === 'blocked') {
      var blocked = ol.querySelector('.step-done + li:not(.step-done)') || ol.lastElementChild;
      if (blocked) blocked.classList.add('step-blocked');
    } else if (status === 'failed') {
      var failed = ol.querySelector('.step-done + li:not(.step-done)') || ol.lastElementChild;
      if (failed) failed.classList.add('step-failed');
    } else {
      var active = ol.querySelector('.step-done + li:not(.step-done)');
      if (active) active.classList.add('step-active');
    }
  }

  var GROUP_ORDER = ['spec', 'sut_map', 'analysis', 'test_plan', 'patch', 'gate', 'run', 'report', 'evidence', 'bug'];
  var GROUP_LABELS = {
    'spec': 'Spec', 'sut_map': 'SUT map', 'analysis': 'Analysis',
    'test_plan': 'Test plan', 'patch': 'Patches', 'gate': 'Gates',
    'run': 'Run manifests', 'report': 'Reports', 'evidence': 'Evidence',
    'bug': 'Bugs'
  };

  function renderArtifactGroups(artifacts) {
    var host = document.getElementById('artifact-groups');
    if (!host) return;
    clearChildren(host);
    if (!artifacts.length) {
      var ph = document.createElement('p');
      ph.className = 'placeholder';
      ph.textContent = '(no artifacts yet)';
      host.appendChild(ph);
      return;
    }
    var grouped = {};
    artifacts.forEach(function (a) {
      (grouped[a.kind] = grouped[a.kind] || []).push(a);
    });
    GROUP_ORDER.forEach(function (kind) {
      var items = grouped[kind];
      if (!items || !items.length) return;
      var section = document.createElement('div');
      section.className = 'card';
      var h = document.createElement('h3');
      h.textContent = GROUP_LABELS[kind] || kind;
      section.appendChild(h);
      var ul = document.createElement('ul');
      items.forEach(function (a) {
        var li = document.createElement('li');
        if (kind === 'spec') {
          li.appendChild(el('code', a.path));
        } else {
          li.appendChild(artifactAnchor(a.path));
        }
        li.appendChild(document.createTextNode(' — ' + fmtLocalTs(a.created_at)));
        ul.appendChild(li);
      });
      section.appendChild(ul);
      host.appendChild(section);
    });
  }

  function renderPatchAndGate(artifacts) {
    var patchEl = document.getElementById('patch-state');
    var gateEl = document.getElementById('gate-state');
    if (!patchEl && !gateEl) return;
    var latestPatch = null;
    var latestGate = null;
    artifacts.forEach(function (a) {
      if (a.kind === 'patch') {
        if (!latestPatch || a.created_at > latestPatch.created_at) latestPatch = a;
      } else if (a.kind === 'gate') {
        if (!latestGate || a.created_at > latestGate.created_at) latestGate = a;
      }
    });
    if (patchEl) {
      if (latestPatch) {
        patchEl.textContent = '';
        patchEl.appendChild(artifactAnchor(latestPatch.path));
        patchEl.appendChild(document.createTextNode(' — ' + fmtLocalTs(latestPatch.created_at)));
      } else {
        patchEl.textContent = '(none)';
      }
    }
    if (gateEl) {
      if (latestGate) {
        gateEl.textContent = '';
        gateEl.appendChild(artifactAnchor(latestGate.path));
        gateEl.appendChild(document.createTextNode(' — ' + fmtLocalTs(latestGate.created_at)));
      } else {
        gateEl.textContent = '(none)';
      }
    }
  }

  function renderGeneratedTests(generated) {
    var summary = document.getElementById('generated-specs-summary');
    var list = document.getElementById('generated-specs-list');
    var rows = Array.isArray(generated) ? generated : [];
    if (summary) {
      summary.textContent = rows.length
        ? rows.length + ' executable spec file(s)'
        : '(none yet)';
    }
    if (!list) return;
    clearChildren(list);
    if (!rows.length) {
      var empty = document.createElement('li');
      empty.className = 'placeholder';
      empty.textContent = '(no generated specs yet)';
      list.appendChild(empty);
      return;
    }
    rows.forEach(function (item) {
      var li = document.createElement('li');
      li.appendChild(el('code', item.relative_path || '(unknown path)'));
      var meta = [];
      if (item.candidate_id) meta.push('candidate ' + item.candidate_id);
      if (item.runner) meta.push(item.runner);
      if (item.manifest_path) meta.push('manifest ' + item.manifest_path);
      if (meta.length) {
        li.appendChild(el('span', ' — ' + meta.join(' · '), { className: 'muted' }));
      }
      list.appendChild(li);
    });
  }

  var ACTION_DEFS = [
    { kind: 'analyze',         id: 'action-analyze' },
    { kind: 'plan',            id: 'action-plan' },
    { kind: 'implement-tests', id: 'action-implement-tests' },
    { kind: 'review-gate',     id: 'action-review-gate' },
    { kind: 'apply-patch',     id: 'action-apply-patch' },
    { kind: 'run-tests',       id: 'action-run-tests' },
    { kind: 'final-gate',      id: 'action-final-gate' }
  ];

  // Selection state for bulk actions (issue #138). Reset every render
  // so a stale candidate id from a previous render cannot leak into
  // the next bulk POST after the table is refreshed.
  var candidateSelection = new Set();
  var candidateRowState = {};  // candidate_id -> { item, assertionInput }

  function _runnable(item) {
    return item
      && item.decision !== 'generate_now'
      && item.decision !== 'not_testable'
      && (item.test_type === 'api' || item.test_type === 'ui');
  }

  // Issue #221 — accessibility/security candidates are not generated
  // automatically, but the operator still needs to edit their expected
  // assertion and pick a decision per row. Bulk approve and checkbox
  // selection stay restricted to runnable candidates because the server
  // approve-all path skips the rest.
  function _editable(item) {
    return item
      && item.decision !== 'generate_now'
      && item.decision !== 'not_testable';
  }

  // Issue #222 — UI candidates without a discoverable target_page need a
  // structured decision form (visible text / role+name / URL contains)
  // instead of a single free-form assertion.
  function _needsStructuredUiAssertion(item) {
    if (!item || item.test_type !== 'ui') return false;
    var page = item.target_page;
    if (page == null) return true;
    var trimmed = String(page).trim();
    return trimmed === '' || trimmed === '/';
  }

  function _composeUiAssertion(visibleText, roleName, urlContains) {
    var parts = [];
    if (visibleText) parts.push('visible text "' + visibleText + '"');
    if (roleName) parts.push('role/name "' + roleName + '"');
    if (urlContains) parts.push('URL contains "' + urlContains + '"');
    return parts.join(' AND ');
  }

  function _readBulkDefaults() {
    var get = function (id) {
      var node = document.getElementById(id);
      return node && node.value ? node.value.trim() : '';
    };
    return {
      expected_assertion: get('bulk-assertion'),
      required_test_data: get('bulk-data'),
      cleanup_strategy: get('bulk-cleanup')
    };
  }

  function _updateBulkCount() {
    var count = candidateSelection.size;
    var label = document.getElementById('bulk-count');
    if (label) label.textContent = count + ' selected';
    var approveSel = document.getElementById('approve-selected-candidates');
    var rejectSel = document.getElementById('reject-selected-candidates');
    var enabled = count > 0;
    if (approveSel) {
      approveSel.disabled = !enabled || approveSel.dataset.writeEnabled !== '1';
    }
    if (rejectSel) {
      rejectSel.disabled = !enabled || rejectSel.dataset.writeEnabled !== '1';
    }
  }

  function _setSummary(payload) {
    var details = document.getElementById('candidate-summary');
    var list = document.getElementById('candidate-summary-list');
    if (!details || !list) return;
    clearChildren(list);
    var outcomes = (payload && payload.outcomes) || [];
    if (!outcomes.length) {
      details.hidden = true;
      return;
    }
    details.hidden = false;
    details.open = (payload && payload.failed) ? true : false;
    outcomes.forEach(function (o) {
      var li = document.createElement('li');
      li.className = 'outcome outcome-' + (o.status || 'unknown');
      var idSpan = el('strong', (o.candidate_id || '?') + ': ');
      var statusSpan = el('span', o.status || '?', { className: 'badge badge-outcome-' + (o.status || 'unknown') });
      li.appendChild(idSpan);
      li.appendChild(statusSpan);
      if (o.reason) {
        li.appendChild(el('span', ' — ' + o.reason, { className: 'muted' }));
      }
      list.appendChild(li);
    });
  }

  function renderTaskCandidates(taskId) {
    var tbody = document.querySelector('#task-candidates tbody');
    var msg = document.getElementById('candidate-msg');
    if (!tbody) return;
    Promise.all([
      fetch('/api/tasks/' + encodeURIComponent(taskId) + '/candidates').then(function (r) {
        return r.ok ? r.json() : { items: [], error: 'plan_missing' };
      }),
      fetch('/api/config').then(function (r) { return r.ok ? r.json() : { dashboard: {} }; })
    ]).then(function (parts) {
      var payload = parts[0] || {};
      var cfg = parts[1] || {};
      var writeEnabled = !!(cfg.dashboard && cfg.dashboard.enable_write_endpoints);
      var items = payload.items || [];
      var approveAll = document.getElementById('approve-all-candidates');
      clearChildren(tbody);

      // Reset selection on every render — candidate ids may have
      // shifted after approve, so re-deriving from the table is safer
      // than carrying stale ids forward.
      candidateSelection.clear();
      candidateRowState = {};
      var selectAll = document.getElementById('select-all-candidates');
      if (selectAll) {
        selectAll.checked = false;
        selectAll.disabled = !writeEnabled || !items.some(_runnable);
      }
      ['approve-selected-candidates', 'reject-selected-candidates'].forEach(function (id) {
        var node = document.getElementById(id);
        if (node) {
          node.dataset.writeEnabled = writeEnabled ? '1' : '0';
        }
      });

      if (msg) {
        msg.className = 'msg muted';
        if (items.length) {
          var approvedApi = items.filter(function (i) {
            return i.decision === 'generate_now' && i.test_type === 'api';
          }).length;
          var approvedUi = items.filter(function (i) {
            return i.decision === 'generate_now' && i.test_type === 'ui';
          }).length;
          var skipped = items.filter(function (i) {
            return i.decision === 'not_testable' || i.decision === 'blocked_missing_docs';
          }).length;
          var pending = items.filter(function (i) {
            return i.decision === 'needs_operator_decision';
          }).length;
          msg.textContent = 'Generate tests creates executable files from approved API/UI candidates: '
            + approvedApi + ' api, ' + approvedUi + ' ui, '
            + pending + ' pending, ' + skipped + ' skipped.';
        } else {
          msg.textContent = 'Run analysis and plan to load candidates.';
        }
      }
      if (approveAll) {
        approveAll.disabled = !writeEnabled || !items.some(_runnable);
        approveAll.onclick = function () { approveAllCandidates(taskId, approveAll); };
      }
      var approveSel = document.getElementById('approve-selected-candidates');
      if (approveSel) {
        approveSel.onclick = function () { applyBulkDecision(taskId, 'approve'); };
      }
      var rejectSel = document.getElementById('reject-selected-candidates');
      if (rejectSel) {
        rejectSel.onclick = function () { applyBulkDecision(taskId, 'reject'); };
      }
      if (selectAll) {
        selectAll.onchange = function () {
          var toggle = !!selectAll.checked;
          var boxes = tbody.querySelectorAll('input.candidate-checkbox');
          for (var i = 0; i < boxes.length; i += 1) {
            var box = boxes[i];
            if (box.disabled) continue;
            box.checked = toggle;
            var cid = box.dataset.candidateId;
            if (toggle) candidateSelection.add(cid); else candidateSelection.delete(cid);
          }
          _updateBulkCount();
        };
      }

      if (!items.length) {
        var empty = document.createElement('tr');
        var td = el('td', '(no candidates yet)', { className: 'placeholder' });
        td.colSpan = 6;
        empty.appendChild(td);
        tbody.appendChild(empty);
        _updateBulkCount();
        return;
      }

      items.forEach(function (item) {
        var tr = document.createElement('tr');
        tr.dataset.candidateId = item.candidate_id || '';

        // --- selection checkbox -----------------------------------
        var selTd = document.createElement('td');
        var box = document.createElement('input');
        box.type = 'checkbox';
        box.className = 'candidate-checkbox';
        box.dataset.candidateId = item.candidate_id || '';
        box.disabled = !writeEnabled || !_runnable(item);
        box.setAttribute('aria-label', 'Select ' + (item.candidate_id || 'candidate'));
        box.addEventListener('change', function () {
          if (box.checked) candidateSelection.add(item.candidate_id);
          else candidateSelection.delete(item.candidate_id);
          _updateBulkCount();
        });
        selTd.appendChild(box);
        tr.appendChild(selTd);

        // --- identifier + decision + type --------------------------
        tr.appendChild(el('td', item.candidate_id || '?'));
        var decision = document.createElement('td');
        var badge = el('span', item.decision || '?', {
          className: 'badge badge-state-' + (item.decision || 'queued')
        });
        decision.appendChild(badge);
        tr.appendChild(decision);
        tr.appendChild(el('td', item.test_type || '?'));

        // --- editable assertion -----------------------------------
        // Issue #221: accessibility/security rows must accept operator
        // input even though they are not runnable. Issue #222: UI rows
        // without a target_page get a structured form (visible text,
        // role/name, URL contains) instead of one free-form textarea.
        var assertionTd = document.createElement('td');
        var area;
        var structured = null;
        var editingAllowed = writeEnabled && _editable(item);
        if (_needsStructuredUiAssertion(item)) {
          structured = document.createElement('div');
          structured.className = 'candidate-structured-ui';
          var mkField = function (key, label, placeholder) {
            var wrap = document.createElement('label');
            wrap.className = 'candidate-structured-field';
            wrap.appendChild(el('span', label, { className: 'candidate-structured-label' }));
            var input = document.createElement('input');
            input.type = 'text';
            input.className = 'candidate-structured-input';
            input.dataset.field = key;
            input.placeholder = placeholder;
            input.disabled = !editingAllowed;
            wrap.appendChild(input);
            structured.appendChild(wrap);
            return input;
          };
          structured.visibleText = mkField('visible_text', 'Visible text', 'e.g. "Sign in"');
          structured.roleName = mkField('role_name', 'Role / name', 'e.g. button "Submit"');
          structured.urlContains = mkField('url_contains', 'URL contains', 'e.g. /post/');
          // Mirror back-compat: stash a hidden textarea so per-row Approve
          // can submit a composed expected_assertion for legacy consumers.
          area = document.createElement('textarea');
          area.style.display = 'none';
          structured.appendChild(area);
          assertionTd.appendChild(structured);
        } else {
          area = document.createElement('textarea');
          area.className = 'candidate-edit';
          area.rows = 2;
          area.value = item.expected_assertion || '';
          area.disabled = !editingAllowed;
          area.placeholder = item.expected_assertion ? '' : '(none — add an assertion)';
          assertionTd.appendChild(area);
        }
        tr.appendChild(assertionTd);

        // --- per-row actions ---------------------------------------
        var actions = document.createElement('td');
        [
          ['approve', 'Approve'],
          ['needs-decision', 'Needs decision'],
          ['reject', 'Reject']
        ].forEach(function (pair) {
          var btn = document.createElement('button');
          btn.type = 'button';
          btn.textContent = pair[1];
          btn.disabled = !writeEnabled;
          btn.addEventListener('click', function () {
            // Pass the (possibly-edited) assertion back to the server
            // so per-row Approve respects inline edits (issue #138).
            var assertion;
            if (structured) {
              var vt = structured.visibleText.value.trim();
              var rn = structured.roleName.value.trim();
              var uc = structured.urlContains.value.trim();
              if (pair[0] === 'approve' && !(vt || rn || uc)) {
                showInlineError(
                  assertionTd,
                  'Provide at least one assertion (visible text, role/name, or URL contains) before approving.'
                );
                return;
              }
              assertion = _composeUiAssertion(vt, rn, uc) || item.expected_assertion || '';
            } else {
              assertion = area.value;
            }
            var edited = Object.assign({}, item, { expected_assertion: assertion });
            updateCandidateDecision(taskId, edited, pair[0]);
          });
          actions.appendChild(btn);
        });
        tr.appendChild(actions);
        tbody.appendChild(tr);

        candidateRowState[item.candidate_id] = {
          item: item,
          assertionInput: area,
          structured: structured
        };
      });

      _updateBulkCount();
    }).catch(function () {
      if (msg) {
        msg.className = 'msg err';
        msg.textContent = 'candidate load failed';
      }
    });
  }

  function applyBulkDecision(taskId, action) {
    var ids = Array.from(candidateSelection);
    if (!ids.length) return;
    var msg = document.getElementById('candidate-msg');
    var defaults = _readBulkDefaults();
    if (msg) {
      msg.className = 'msg muted';
      msg.textContent = action + ' selected (' + ids.length + '): running...';
    }
    // Bulk defaults take precedence over the in-memory edit when set,
    // so an operator can paste a single assertion once and override
    // every selected row in one click.
    //
    // The fan-out runs SEQUENTIALLY. Since issue #161 the server-side
    // endpoint serializes the TEST-PLAN.json read-modify-write under a
    // per-task `file_lock`, so parallel POSTs no longer corrupt state.
    // We still chain client-side because:
    //   - the operator gets deterministic per-row outcome ordering in
    //     the UI message bar,
    //   - a single failure aborts the rest, matching the "stop on first
    //     red" UX expected by reviewers,
    //   - it bounds load on the dashboard's single-thread HTTP server
    //     when a plan has dozens of rows selected.
    var postOne = function (cid) {
      var rowState = candidateRowState[cid] || {};
      var item = rowState.item || { candidate_id: cid };
      // Issue #222 — for UI rows with a structured form, the composed
      // assertion takes precedence over the bulk default unless the
      // operator pasted a freeform override.
      var structuredAssertion = '';
      if (rowState.structured) {
        structuredAssertion = _composeUiAssertion(
          rowState.structured.visibleText.value.trim(),
          rowState.structured.roleName.value.trim(),
          rowState.structured.urlContains.value.trim()
        );
      }
      var assertion = defaults.expected_assertion
        || structuredAssertion
        || (rowState.assertionInput ? rowState.assertionInput.value : (item.expected_assertion || ''));
      var body = {
        expected_assertion: assertion || null,
        required_test_data: defaults.required_test_data || item.required_test_data || null,
        cleanup_strategy: defaults.cleanup_strategy || item.cleanup_strategy || null,
        target_page: item.target_page || null,
        reason: 'dashboard bulk ' + action
      };
      return fetch(
        '/api/tasks/' + encodeURIComponent(taskId) +
        '/candidates/' + encodeURIComponent(cid) +
        '/' + action,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        }
      ).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (data) {
          return {
            candidate_id: cid,
            status: r.ok ? 'approved' : 'failed',
            reason: r.ok ? undefined : (data.message || data.error || ('HTTP ' + r.status))
          };
        });
      }).catch(function (err) {
        return { candidate_id: cid, status: 'failed', reason: err.message };
      });
    };

    var outcomes = [];
    var chain = Promise.resolve();
    ids.forEach(function (cid) {
      chain = chain.then(function () {
        return postOne(cid).then(function (outcome) { outcomes.push(outcome); });
      });
    });
    chain.then(function () {
      var approved = outcomes.filter(function (o) { return o.status === 'approved'; }).length;
      var failed = outcomes.filter(function (o) { return o.status === 'failed'; }).length;
      if (msg) {
        msg.className = failed ? 'msg err' : 'msg ok';
        msg.textContent = 'bulk ' + action + ': approved ' + approved + ', failed ' + failed;
      }
      _setSummary({ outcomes: outcomes, approved: approved, failed: failed });
      renderTaskCandidates(taskId);
    });
  }

  function approveAllCandidates(taskId, btn) {
    var msg = document.getElementById('candidate-msg');
    if (msg) {
      msg.className = 'msg muted';
      msg.textContent = 'approve all runnable: running...';
    }
    if (btn) btn.disabled = true;
    fetch(
      '/api/tasks/' + encodeURIComponent(taskId) + '/candidates/approve-all',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}'
      }
    ).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (payload) {
        if (!r.ok) throw new Error(payload.message || payload.error || ('HTTP ' + r.status));
        return payload;
      });
    }).then(function (payload) {
      if (msg) {
        msg.className = payload.failed ? 'msg err' : 'msg ok';
        msg.textContent = 'approve all runnable: approved ' + (payload.approved || 0)
          + ', skipped ' + (payload.skipped || 0)
          + ', failed ' + (payload.failed || 0);
      }
      _setSummary(payload);
      renderTaskCandidates(taskId);
    }).catch(function (err) {
      if (msg) {
        msg.className = 'msg err';
        msg.textContent = 'approve all runnable failed: ' + err.message;
      }
      renderTaskCandidates(taskId);
    });
  }

  function updateCandidateDecision(taskId, item, action) {
    var msg = document.getElementById('candidate-msg');
    if (msg) {
      msg.className = 'msg muted';
      msg.textContent = action + ' ' + item.candidate_id + ': running...';
    }
    var body = {
      expected_assertion: item.expected_assertion || null,
      required_test_data: item.required_test_data || null,
      cleanup_strategy: item.cleanup_strategy || null,
      target_page: item.target_page || null,
      reason: 'dashboard decision'
    };
    fetch(
      '/api/tasks/' + encodeURIComponent(taskId) +
      '/candidates/' + encodeURIComponent(item.candidate_id) +
      '/' + action,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      }
    ).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (payload) {
        if (!r.ok) throw new Error(payload.message || payload.error || ('HTTP ' + r.status));
        return payload;
      });
    }).then(function () {
      if (msg) {
        msg.className = 'msg ok';
        msg.textContent = item.candidate_id + ' updated';
      }
      renderTaskCandidates(taskId);
    }).catch(function (err) {
      if (msg) {
        msg.className = 'msg err';
        msg.textContent = item.candidate_id + ' failed: ' + err.message;
      }
    });
  }

  function wireActionButtons(taskId) {
    var buttons = ACTION_DEFS.map(function (def) {
      return { def: def, el: document.getElementById(def.id) };
    }).filter(function (b) { return b.el != null; });
    if (!buttons.length) return;
    if (buttons[0].el.dataset.wired === '1') return;
    var warn = document.getElementById('actions-write-warning');
    var msg = document.getElementById('actions-msg');

    buttons.forEach(function (b) { b.el.dataset.wired = '1'; });

    function applyWriteState() {
      // Issue #194 — combine the global write gate (config) with the
      // per-task prerequisite gate (/gating) so a button is enabled
      // only when BOTH allow it. The reason string from /gating becomes
      // the button tooltip so an operator can see why it is blocked
      // without opening DevTools.
      return Promise.all([
        fetch('/api/config').then(function (r) { return r.ok ? r.json() : null; }),
        fetch('/api/tasks/' + encodeURIComponent(taskId) + '/gating').then(function (r) {
          return r.ok ? r.json() : null;
        })
      ]).then(function (parts) {
        var cfg = parts[0];
        var gating = parts[1];
        if (!cfg) return;
        var dash = cfg.dashboard || {};
        var writesOn = !!dash.enable_write_endpoints;
        var fullMode = !!dash.full_mode;
        var autonomyOn = !!dash.autonomy_unlocks_writes;
        if (warn) {
          if (writesOn) {
            warn.className = 'msg ok';
            if (autonomyOn) {
              warn.textContent = 'writes unlocked by full autonomy session';
            } else if (fullMode) {
              warn.textContent = 'writes unlocked by serve --full';
            } else {
              warn.textContent = 'dashboard.enable_write_endpoints = true';
            }
          } else {
            warn.className = 'msg err';
            warn.textContent =
              'writes disabled — set dashboard.enable_write_endpoints=true, '
              + 'restart with serve --full, or start full autonomy';
          }
        }
        var actions = (gating && gating.actions) || {};
        buttons.forEach(function (b) {
          // Preserve in-flight disable: invoke() marks the button as busy so
          // the poll cannot re-enable it mid-request and let the user fire a
          // duplicate POST against a long-running action.
          if (b.el.dataset.inFlight === '1') return;
          var entry = actions[b.def.kind] || { enabled: true, reason: '' };
          var allowed = writesOn && entry.enabled;
          b.el.disabled = !allowed;
          if (!writesOn) {
            b.el.title = 'dashboard writes disabled';
          } else if (!entry.enabled) {
            b.el.title = entry.reason || 'prerequisites not met for this step';
          } else {
            b.el.removeAttribute('title');
          }
        });
      }).catch(function () {});
    }

    applyWriteState();
    setInterval(applyWriteState, 4000);

    function invoke(kind, btn) {
      msg.className = 'msg muted';
      msg.textContent = kind + ': running…';
      btn.disabled = true;
      btn.dataset.inFlight = '1';
      var body = (kind === 'review-gate' || kind === 'apply-patch' ||
                  kind === 'run-tests' || kind === 'final-gate')
        ? '{}' : null;
      var init = {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
      };
      if (body != null) init.body = body;
      fetch('/api/tasks/' + encodeURIComponent(taskId) + '/' + kind, init)
        .then(function (r) {
          return r.json().catch(function () { return {}; }).then(function (payload) {
            if (!r.ok) {
              throw new Error(payload.message || payload.error || ('HTTP ' + r.status));
            }
            return payload;
          });
        })
        .then(function (payload) {
          renderActionResult(kind, payload);
          loadTaskDetail();
        })
        .catch(function (e) {
          msg.className = 'msg err';
          msg.textContent = kind + ' failed: ' + e.message;
        })
        .then(function () {
          delete btn.dataset.inFlight;
          // Re-evaluate disabled from live config; do not blindly enable, in
          // case writes were revoked while the action was running.
          applyWriteState();
        });
    }

    buttons.forEach(function (b) {
      b.el.addEventListener('click', function () { invoke(b.def.kind, b.el); });
    });
  }

  function renderActionResult(kind, payload) {
    var msg = document.getElementById('actions-msg');
    var outcome = document.getElementById('last-action-outcome');
    if (typeof payload.exit_code === 'number') {
      var code = payload.exit_code;
      var failure = payload.failure_kind;
      var cls = 'badge ' + (code === 0 ? 'badge-ok' : code === 1 ? 'badge-warn' : 'badge-err');
      var label = code === 0 ? 'green'
        : code === 1 ? (failure === 'product' ? 'product/test failure' : 'rejected')
        : code === 130 ? 'cancelled' : 'infra failure';
      if (outcome) {
        outcome.textContent = '';
        var span = document.createElement('span');
        span.className = cls;
        span.textContent = kind + ' — exit ' + code + ' — ' + label;
        outcome.appendChild(span);
      }
      setText('last-action-exit', String(code));
      setText('last-action-failure', failure || '(none)');
      setText('last-action-run', payload.run_id || '–');
      renderArtifactLink('last-action-manifest', payload.manifest_path);
      renderArtifactLink('last-action-reports',
        payload.reports_path ? (payload.reports_path + '/summary.md') : null,
        payload.reports_path);
      renderBugsList('last-action-bugs', payload.bugs_opened);
      if (msg) {
        msg.className = code === 0 ? 'msg ok' : code === 1 ? 'msg muted' : 'msg err';
        msg.textContent = kind + ' done — exit ' + code +
          (failure ? ' (' + failure + ')' : '');
      }
    } else {
      // analyze/plan/implement-tests payloads return {status, artifacts:[...]}
      var artifacts = (payload.artifacts || []).map(function (a) {
        return a.kind + ':' + a.path;
      }).join(', ');
      if (msg) {
        msg.className = 'msg ok';
        msg.textContent = kind + ' ok — ' + (artifacts || '(no artifacts)');
      }
      if (outcome) {
        outcome.textContent = '';
        var ok = document.createElement('span');
        ok.className = 'badge badge-ok';
        ok.textContent = kind + ' — ' + (payload.status || 'ok');
        outcome.appendChild(ok);
      }
    }
  }

  function setText(id, value) {
    var n = document.getElementById(id);
    if (n) n.textContent = value;
  }

  function renderBugsList(id, bugs) {
    var dd = document.getElementById(id);
    if (!dd) return;
    dd.textContent = '';
    if (!bugs || !bugs.length) {
      dd.textContent = '(none)';
      return;
    }
    bugs.forEach(function (b, idx) {
      if (idx) dd.appendChild(document.createTextNode(', '));
      var link = artifactAnchor('bugs/' + b);
      dd.appendChild(link);
    });
  }

  function artifactAnchor(p) {
    var a = document.createElement('a');
    a.href = '/files/' + p.split('/').map(encodeURIComponent).join('/');
    a.target = '_blank';
    a.rel = 'noopener';
    a.appendChild(el('code', p));
    return a;
  }

  function renderArtifactLink(id, fetchPath, labelOverride) {
    var dd = document.getElementById(id);
    if (!dd) return;
    dd.textContent = '';
    if (!fetchPath) {
      dd.textContent = '(none)';
      return;
    }
    var a = document.createElement('a');
    a.href = '/files/' + fetchPath.split('/').map(encodeURIComponent).join('/');
    a.target = '_blank';
    a.rel = 'noopener';
    a.appendChild(el('code', labelOverride || fetchPath));
    dd.appendChild(a);
  }

  // ---- events SSE ----
  function startEvents(opts) {
    opts = opts || {};
    var boxId = opts.boxId || 'events';
    var modeId = opts.modeId || 'ev-mode';
    var taskFilter = opts.taskId || null;
    var box = document.getElementById(boxId);
    if (!box) return;
    if (!global.EventSource) return;
    try {
      var es = new EventSource('/api/events');
      safeText(modeId, '(stream)');
      es.onmessage = function (ev) {
        try {
          var j = JSON.parse(ev.data);
          if (taskFilter) {
            var payloadTask = j.payload && (j.payload.work_item_id || j.payload.task_id);
            if (j.task_id !== taskFilter && payloadTask !== taskFilter) return;
          }
          var line = fmtLocalTs(j.ts) + '  [' + j.severity + ']  ' + j.kind + '  ' +
            (j.payload ? JSON.stringify(j.payload) : '');
          var div = el('div', line, { className: 'ev-' + (j.severity || 'info') });
          box.insertBefore(div, box.firstChild);
          while (box.childNodes.length > 200) box.removeChild(box.lastChild);
        } catch (_) { /* ignore */ }
      };
      es.onerror = function () { /* browser auto-reconnects */ };
    } catch (e) { /* SSE not available */ }
  }

  function startTaskEvents() {
    var id = getTaskIdFromPath();
    if (!id) return;
    startEvents({ boxId: 'task-events', modeId: 'task-ev-mode', taskId: id });
  }

  // ---- active task summary (dashboard index) ----
  var ACTION_LABELS = {
    queued: 'Run "Analyze SUT"',
    analyzing: 'Run "Create test plan"',
    planned: 'Run "Generate tests"',
    implementing: 'Run "Review gate"',
    reviewing: 'Run "Apply patch"',
    running: 'Run "Run tests"',
    bug_adjudication: 'Adjudicate open bugs',
    blocked: 'Resolve blockers, then retry',
    failed: 'Inspect last run + retry',
    done: 'Run "Final gate"'
  };

  function refreshActiveTask() {
    var card = document.getElementById('active-task-card');
    if (!card) return Promise.resolve();
    return fetch('/api/tasks').then(function (r) {
      if (!r.ok) throw new Error('tasks ' + r.status);
      return r.json();
    }).then(function (payload) {
      var rows = (payload.tasks || []).slice();
      // pick the first non-final task; otherwise the most recent.
      var active = null;
      var priority = ['analyzing', 'planning', 'planned', 'implementing',
                      'reviewing', 'running', 'blocked', 'failed',
                      'bug_adjudication', 'queued', 'done'];
      for (var i = 0; i < priority.length && !active; i++) {
        active = rows.find(function (r) { return r.status === priority[i]; });
      }
      if (!active) active = rows[0] || null;
      if (!active) {
        safeText('active-task-title', '');
        safeText('active-task-id', '–');
        safeText('active-task-status', '–');
        safeText('active-task-priority', '–');
        safeText('active-task-last', '–');
        renderNextAction(null, 'no tasks yet — create one to begin');
        return;
      }
      var title = document.getElementById('active-task-title');
      if (title) {
        title.textContent = '';
        var link = document.createElement('a');
        link.href = '/tasks/' + encodeURIComponent(active.id);
        link.textContent = active.title;
        title.appendChild(link);
      }
      safeText('active-task-id', active.id);
      renderStatusBadge('active-task-status', active.status);
      safeText('active-task-priority', active.priority);
      safeText('active-task-last',
        active.last_artifact_kind
          ? active.last_artifact_kind + ' — ' + (active.last_artifact_path || '')
          : '(none yet)');
      var nextLabel = ACTION_LABELS[active.status] || 'Open task detail';
      renderNextAction(active.id, nextLabel);
    }).catch(function () { /* swallow */ });
  }

  function renderStatusBadge(id, status) {
    var n = document.getElementById(id);
    if (!n) return;
    n.textContent = '';
    var b = document.createElement('span');
    b.className = 'badge badge-state-' + status;
    b.textContent = status;
    n.appendChild(b);
  }

  function renderNextAction(taskId, label) {
    var dest = document.getElementById('active-task-next');
    if (!dest) return;
    dest.textContent = '';
    if (!taskId) {
      var span = document.createElement('span');
      span.className = 'muted';
      span.textContent = label;
      dest.appendChild(span);
      return;
    }
    var hint = document.createElement('span');
    hint.className = 'muted';
    hint.textContent = 'Next:';
    dest.appendChild(hint);
    var link = document.createElement('a');
    link.href = '/tasks/' + encodeURIComponent(taskId);
    link.className = 'btn';
    link.style.padding = '6px 12px';
    link.style.fontSize = '13px';
    link.textContent = label + ' →';
    dest.appendChild(link);
  }

  function startActiveTaskPolling() {
    refreshActiveTask();
    setInterval(refreshActiveTask, 4000);
  }

  // ---- Phase 2 — full-auto indicator + suggestions + agents + skills ----
  function startFullAutoIndicator() {
    function refresh() {
      fetch('/api/config').then(function (r) { return r.ok ? r.json() : null; })
        .then(function (cfg) {
          var el = document.getElementById('full-auto-indicator');
          if (!el || !cfg) return;
          var on = !!(cfg.dashboard && cfg.dashboard.full_mode);
          el.setAttribute('data-state', on ? 'on' : 'off');
        }).catch(function () {});
    }
    refresh();
    setInterval(refresh, 6000);
  }

  function startSuggestionsPolling() {
    function refresh() {
      var list = document.getElementById('suggestions-list');
      if (!list) return;
      fetch('/api/suggestions').then(function (r) {
        return r.ok ? r.json() : { suggestions: [] };
      }).then(function (data) {
        var items = data.suggestions || [];
        clearChildren(list);
        if (!items.length) {
          var li = document.createElement('li');
          li.className = 'placeholder';
          li.textContent = '(no actionable suggestions right now)';
          list.appendChild(li);
          return;
        }
        items.forEach(function (s) {
          var li = document.createElement('li');
          li.className = 'suggestion suggestion-' + (s.priority || 'P2').toLowerCase();
          var p = document.createElement('span');
          p.className = 'chip chip-' + (s.kind || 'note');
          p.textContent = s.priority + ' · ' + s.kind;
          li.appendChild(p);
          var msg = document.createElement('span');
          msg.textContent = ' ' + s.message;
          li.appendChild(msg);
          list.appendChild(li);
        });
      }).catch(function () {});
    }
    refresh();
    setInterval(refresh, 8000);
  }

  function _formatAgentCommand(command) {
    return JSON.stringify(Array.isArray(command) ? command : [], null, 2);
  }

  function _parseAgentCommand(raw) {
    var parsed;
    try {
      parsed = JSON.parse(String(raw || '').trim());
    } catch (e) {
      throw new Error('command must be a JSON array, for example ["codex"]');
    }
    if (!Array.isArray(parsed) || !parsed.length || parsed.some(function (item) { return typeof item !== 'string' || !item.trim(); })) {
      throw new Error('command must be a non-empty JSON array of strings');
    }
    return parsed.map(function (item) { return item.trim(); });
  }

  function _providerSelect(current) {
    var select = document.createElement('select');
    ['claude', 'codex', 'antigravity', 'script'].forEach(function (provider) {
      var option = document.createElement('option');
      option.value = provider;
      option.textContent = provider;
      if (provider === current) option.selected = true;
      select.appendChild(option);
    });
    return select;
  }

  function startAgentsPolling() {
    var grid = document.getElementById('agents-grid');
    var reloadBtn = document.getElementById('agents-reload');
    var writeStatus = document.getElementById('agents-write-status');

    function refresh(force) {
      if (!grid) return;
      if (!force && grid.querySelector('form[data-dirty="true"]')) return;
      Promise.all([
        fetch('/api/agents').then(function (r) { return r.ok ? r.json() : { agents: [] }; }),
        fetch('/api/config').then(function (r) { return r.ok ? r.json() : { dashboard: {} }; }),
      ]).then(function (parts) {
        var agents = parts[0].agents || [];
        var writesOn = !!(parts[1].dashboard && parts[1].dashboard.enable_write_endpoints);
        if (writeStatus) {
          writeStatus.textContent = writesOn
            ? 'writes enabled'
            : 'read-only: start with --full or set dashboard.enable_write_endpoints=true';
          writeStatus.className = writesOn ? 'write-chip write-on' : 'write-chip write-off';
        }
        clearChildren(grid);
        if (!agents.length) {
          grid.appendChild(el('p', '(no agents configured)', { className: 'placeholder' }));
          return;
        }
        agents.forEach(function (a) {
          var card = document.createElement('div');
          card.className = 'card agent-card agent-' + a.role;
          var head = document.createElement('div');
          head.className = 'agent-card-head';
          var h = document.createElement('h3');
          h.textContent = a.role;
          head.appendChild(h);
          var badge = document.createElement('span');
          badge.className = 'chip chip-' + (a.binary_found ? 'approved' : 'rejected');
          badge.textContent = a.binary_found ? 'binary found' : 'not on PATH';
          head.appendChild(badge);
          card.appendChild(head);
          var dl = document.createElement('dl');
          dl.className = 'meta';
          [['command', (a.command || []).join(' ')], ['auto_fire', String(a.auto_fire)]].forEach(function (pair) {
            var dt = document.createElement('dt'); dt.textContent = pair[0];
            var dd = document.createElement('dd'); dd.textContent = pair[1];
            dl.appendChild(dt); dl.appendChild(dd);
          });
          card.appendChild(dl);

          var form = document.createElement('form');
          form.className = 'agent-form';
          form.dataset.role = a.role;
          form.dataset.dirty = 'false';

          var providerLabel = document.createElement('label');
          providerLabel.textContent = 'Provider';
          var providerInput = _providerSelect(a.provider || 'script');
          providerLabel.appendChild(providerInput);
          form.appendChild(providerLabel);

          var commandLabel = document.createElement('label');
          commandLabel.textContent = 'Command argv JSON';
          var commandInput = document.createElement('textarea');
          commandInput.rows = 3;
          commandInput.value = _formatAgentCommand(a.command || []);
          commandLabel.appendChild(commandInput);
          form.appendChild(commandLabel);

          var autoLabel = document.createElement('label');
          autoLabel.className = 'agent-check-row';
          var autoInput = document.createElement('input');
          autoInput.type = 'checkbox';
          autoInput.checked = !!a.auto_fire;
          autoLabel.appendChild(autoInput);
          autoLabel.appendChild(document.createTextNode(' auto fire'));
          form.appendChild(autoLabel);

          [providerInput, commandInput, autoInput].forEach(function (input) {
            input.disabled = !writesOn;
            if (!writesOn) input.title = 'dashboard.enable_write_endpoints=false';
            input.addEventListener('input', function () { form.dataset.dirty = 'true'; });
            input.addEventListener('change', function () { form.dataset.dirty = 'true'; });
          });

          var actions = document.createElement('div');
          actions.className = 'agent-actions';
          var saveBtn = document.createElement('button');
          saveBtn.type = 'submit';
          saveBtn.className = 'btn-small';
          saveBtn.textContent = 'Save';
          saveBtn.disabled = !writesOn;
          if (!writesOn) saveBtn.title = 'enable write endpoints to edit';
          actions.appendChild(saveBtn);

          var testBtn = document.createElement('button');
          testBtn.type = 'button';
          testBtn.className = 'btn-small';
          testBtn.textContent = 'Test connectivity';
          testBtn.disabled = !writesOn;
          if (!writesOn) testBtn.title = 'enable write endpoints to test';
          actions.appendChild(testBtn);

          var cardReloadBtn = document.createElement('button');
          cardReloadBtn.type = 'button';
          cardReloadBtn.className = 'btn-small btn-muted';
          cardReloadBtn.textContent = 'Reload';
          cardReloadBtn.addEventListener('click', function () { refresh(true); });
          actions.appendChild(cardReloadBtn);
          form.appendChild(actions);

          var result = document.createElement('pre');
          result.className = 'block';
          result.style.maxHeight = '120px';
          result.style.overflow = 'auto';
          result.hidden = true;

          form.addEventListener('submit', function (evt) {
            evt.preventDefault();
            result.hidden = false;
            var command;
            try {
              command = _parseAgentCommand(commandInput.value);
            } catch (e) {
              result.textContent = 'error: ' + e.message;
              return;
            }
            saveBtn.disabled = true;
            result.textContent = 'saving…';
            fetch('/api/agents/' + encodeURIComponent(a.role), {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                provider: providerInput.value,
                command: command,
                auto_fire: autoInput.checked
              })
            }).then(function (r) {
              return r.json().then(function (d) { return { ok: r.ok, body: d }; });
            }).then(function (res) {
              if (!res.ok) throw new Error(res.body.error || 'save failed');
              form.dataset.dirty = 'false';
              result.textContent = 'saved';
              refresh(true);
            }).catch(function (e) {
              result.textContent = 'error: ' + e.message;
              saveBtn.disabled = false;
            });
          });

          testBtn.addEventListener('click', function () {
            testBtn.disabled = true;
            result.hidden = false;
            result.textContent = 'testing…';
            fetch('/api/agents/' + encodeURIComponent(a.role) + '/test', { method: 'POST' })
              .then(function (r) { return r.json(); })
              .then(function (d) {
                result.textContent = JSON.stringify(d, null, 2);
                testBtn.disabled = false;
              }).catch(function (e) {
                result.textContent = 'error: ' + e;
                testBtn.disabled = false;
              });
          });
          card.appendChild(form);
          card.appendChild(result);
          grid.appendChild(card);
        });
      }).catch(function () {});
    }
    if (reloadBtn && !reloadBtn.dataset.wired) {
      reloadBtn.dataset.wired = 'true';
      reloadBtn.addEventListener('click', function () { refresh(true); });
    }
    refresh(true);
    setInterval(function () { refresh(false); }, 10000);
  }

  function startSkillsPolling() {
    function refresh() {
      var tbody = document.querySelector('#skills-table tbody');
      if (!tbody) return;
      Promise.all([
        fetch('/api/skills').then(function (r) { return r.ok ? r.json() : { skills: [] }; }),
        fetch('/api/config').then(function (r) { return r.ok ? r.json() : { dashboard: {} }; }),
      ]).then(function (parts) {
        var skills = parts[0].skills || [];
        var writesOn = !!(parts[1].dashboard && parts[1].dashboard.enable_write_endpoints);
        clearChildren(tbody);
        if (!skills.length) {
          var tr = document.createElement('tr');
          var td = document.createElement('td');
          td.colSpan = 8;
          td.className = 'placeholder';
          td.textContent = '(no skills installed; run init or copy files to skills/)';
          tr.appendChild(td);
          tbody.appendChild(tr);
          return;
        }
        skills.forEach(function (s) {
          var tr = document.createElement('tr');
          tr.appendChild(el('td', '', { children: [el('code', s.id)] }));
          tr.appendChild(el('td', s.name));
          tr.appendChild(el('td', s.description || ''));
          tr.appendChild(el('td', s.source));
          ['planner','implementer','reviewer','triager'].forEach(function (role) {
            var td = document.createElement('td');
            var cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.checked = (s.roles_enabled || []).indexOf(role) >= 0;
            cb.disabled = !writesOn;
            if (!writesOn) cb.title = 'enable write endpoints to toggle';
            cb.addEventListener('change', function () {
              var endpoint = '/api/skills/' + s.id + (cb.checked ? '/enable' : '/disable');
              fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ role: role }),
              }).catch(function () { cb.checked = !cb.checked; });
            });
            td.appendChild(cb);
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
      }).catch(function () {});
    }
    refresh();
    setInterval(refresh, 12000);
  }

  function wireSutMode() {
    var local = document.getElementById('sut-mode-local');
    var online = document.getElementById('sut-mode-online');
    var webEn = document.getElementById('sut-web-enabled');
    var webUrl = document.getElementById('sut-web-url');
    var apiEn = document.getElementById('sut-api-enabled');
    var apiUrl = document.getElementById('sut-api-url');
    var saveBtn = document.getElementById('sut-mode-save');
    var statusEl = document.getElementById('sut-mode-status');
    if (!local || !saveBtn) return;
    function loadCurrent() {
      fetch('/api/config').then(function (r) { return r.json(); }).then(function (cfg) {
        var sut = cfg.sut || {};
        var mode = sut.mode || 'local';
        if (mode === 'online') online.checked = true; else local.checked = true;
        var web = sut.web || {}; var api = sut.api || {};
        webEn.checked = web.enabled !== false;
        webUrl.value = web.url || sut.base_url || sut.ui_url || '';
        apiEn.checked = api.enabled !== false;
        apiUrl.value = api.url || sut.api_base_url || '';
      }).catch(function () {});
    }
    saveBtn.addEventListener('click', function () {
      var payload = {
        mode: online.checked ? 'online' : 'local',
        web: { enabled: webEn.checked, url: webUrl.value.trim() || null },
        api: { enabled: apiEn.checked, url: apiUrl.value.trim() || null }
      };
      statusEl.textContent = 'saving…';
      fetch('/api/sut/mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      }).then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
        .then(function (res) {
          if (res.ok) {
            statusEl.textContent = 'saved (' + res.body.mode + ')';
            if (typeof loadConfig === 'function') loadConfig();
          } else {
            statusEl.textContent = 'error: ' + (res.body.error || 'unknown');
          }
        })
        .catch(function (e) { statusEl.textContent = 'error: ' + e.message; });
    });
    loadCurrent();
  }

  function wireFullAutonomy() {
    var startBtn = document.getElementById('autonomy-start-btn');
    var pauseBtn = document.getElementById('autonomy-pause-btn');
    var resumeBtn = document.getElementById('autonomy-resume-btn');
    var stopBtn = document.getElementById('autonomy-stop-btn');
    var minutesEl = document.getElementById('autonomy-minutes');
    var timerEl = document.getElementById('autonomy-timer');
    var stateEl = document.getElementById('autonomy-state');
    var logEl = document.getElementById('autonomy-log');
    var warnEl = document.getElementById('autonomy-warning');
    var awaitingEl = document.getElementById('autonomy-awaiting');
    var blockedChip = document.getElementById('autonomy-blocked-chip');
    var budgetEl = document.getElementById('autonomy-budget');
    var providersEl = document.getElementById('autonomy-providers');
    var coverageEl = document.getElementById('autonomy-coverage');
    var sparkEl = document.getElementById('autonomy-sparklines');
    if (!startBtn) return;
    var stoppingRequested = false;
    var pausePending = false;
    var coverageFlagsOn = false;
    var lastBlockedCount = 0;
    var sparkBuckets = {};   // role -> array of {ts, count}
    function fmtSeconds(s) {
      if (s === null || s === undefined) return '';
      var m = Math.floor(s / 60), sec = s % 60;
      return m + 'm ' + sec + 's left';
    }
    // One-shot config read for coverage-gauge flags.
    fetch('/api/config').then(function (r) { return r.json(); }).then(function (cfg) {
      var a = (cfg && cfg.autonomy) || {};
      coverageFlagsOn = !!(a.coverage_floor || a.coverage_architect);
    }).catch(function () {});
    function refresh() {
      fetch('/api/autonomy/status').then(function (r) { return r.json(); }).then(function (data) {
        var sess = data.session;
        if (!sess) {
          stoppingRequested = false;
          pausePending = false;
          stateEl.textContent = 'idle';
          startBtn.disabled = false;
          if (pauseBtn) { pauseBtn.disabled = true; pauseBtn.hidden = false; }
          if (resumeBtn) { resumeBtn.disabled = true; resumeBtn.hidden = true; }
          stopBtn.disabled = true;
          timerEl.textContent = '';
          if (awaitingEl) { awaitingEl.hidden = true; awaitingEl.textContent = ''; }
          if (blockedChip) blockedChip.hidden = true;
          [budgetEl, providersEl, coverageEl, sparkEl].forEach(function (el) {
            if (el) { el.hidden = true; clearChildren(el); }
          });
          clearChildren(logEl);
          return;
        }
        var running = sess.status === 'running';
        var paused = sess.status === 'paused';
        if (!running && !paused) { stoppingRequested = false; pausePending = false; }
        if (paused) pausePending = false;
        stateEl.textContent = stoppingRequested ? 'stopping…' : sess.status;
        startBtn.disabled = running || paused || stoppingRequested;
        if (pauseBtn) {
          pauseBtn.hidden = paused;
          pauseBtn.disabled = !running || stoppingRequested || pausePending;
        }
        if (resumeBtn) {
          resumeBtn.hidden = !paused;
          resumeBtn.disabled = !paused || stoppingRequested;
        }
        stopBtn.disabled = (!running && !paused) || stoppingRequested;
        timerEl.textContent = running ? fmtSeconds(sess.seconds_left) : (paused ? 'paused' : '');
        renderAutonomyWidgets(sess);
        if (awaitingEl) {
          if (sess.paused_reason && running) {
            awaitingEl.hidden = false;
            awaitingEl.textContent = 'Blocked: ' + sess.paused_reason;
          } else if (sess.awaiting_task && running) {
            awaitingEl.hidden = false;
            awaitingEl.textContent = 'No work items in the queue — full autonomy is running exploratory discovery while it waits. Add a task to direct the run.';
          } else {
            awaitingEl.hidden = true;
            awaitingEl.textContent = '';
          }
        }
        clearChildren(logEl);
        (sess.events_log || []).slice(-25).reverse().forEach(function (e) {
          var li = document.createElement('li');
          li.className = e.ok ? 'autonomy-log-ok' : 'autonomy-log-err';
          li.textContent = fmtLocalTs(e.ts) + ' · ' + e.step + (e.detail ? ' — ' + e.detail : '');
          logEl.appendChild(li);
        });
      }).catch(function () {});
    }
    startBtn.addEventListener('click', function () {
      var mins = parseInt(minutesEl.value, 10) || 60;
      if (mins < 60) {
        warnEl.hidden = false;
        warnEl.textContent = 'Warning: <1h budget may not finish a full build + tests + reports cycle.';
      } else { warnEl.hidden = true; }
      if (!confirm('Start full autonomy for ' + mins + ' min? Some actions may require sudo; if so, the dashboard must be restarted as root for those to succeed.')) return;
      fetch('/api/autonomy/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ max_minutes: mins })
      }).then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
        .then(function (res) {
          if (!res.ok) {
            warnEl.hidden = false;
            warnEl.textContent = 'Start failed: ' + (res.body.error || JSON.stringify(res.body));
          }
          refresh();
        });
    });
    stopBtn.addEventListener('click', function () {
      if (stoppingRequested) return;
      stoppingRequested = true;
      stopBtn.disabled = true;
      startBtn.disabled = true;
      stateEl.textContent = 'stopping…';
      fetch('/api/autonomy/stop', { method: 'POST' })
        .then(function (r) {
          return r.json().then(function (b) { return { ok: r.ok, body: b }; },
                               function () { return { ok: r.ok, body: {} }; });
        })
        .then(function (res) {
          if (!res.ok) {
            stoppingRequested = false;
            warnEl.hidden = false;
            warnEl.textContent = 'Stop failed: ' + (res.body.error || JSON.stringify(res.body));
          }
          refresh();
        })
        .catch(function (err) {
          stoppingRequested = false;
          warnEl.hidden = false;
          warnEl.textContent = 'Stop failed: ' + (err && err.message ? err.message : 'network error');
          refresh();
        });
    });
    function sendControl(action, btn) {
      if (btn) btn.disabled = true;
      fetch('/api/autonomy/' + action, { method: 'POST' })
        .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; },
                                                   function () { return { ok: r.ok, body: {} }; }); })
        .then(function (res) {
          if (!res.ok) {
            warnEl.hidden = false;
            warnEl.textContent = action + ' failed: ' + (res.body.error || JSON.stringify(res.body));
          }
          refresh();
        })
        .catch(function (err) {
          warnEl.hidden = false;
          warnEl.textContent = action + ' failed: ' + (err && err.message ? err.message : 'network error');
          refresh();
        });
    }
    if (pauseBtn) pauseBtn.addEventListener('click', function () {
      pausePending = true; pauseBtn.disabled = true; sendControl('pause', pauseBtn);
    });
    if (resumeBtn) resumeBtn.addEventListener('click', function () {
      resumeBtn.disabled = true; sendControl('resume', resumeBtn);
    });

    // ---- widgets: budget bars, provider chips, blocked chip, coverage, sparkline ----
    function barNode(label, current, max, pct) {
      var row = document.createElement('div');
      row.className = 'budget-row';
      var name = document.createElement('span');
      name.className = 'budget-label';
      name.textContent = label;
      var track = document.createElement('div');
      track.className = 'budget-track';
      var fill = document.createElement('div');
      fill.className = 'budget-fill';
      var p = (typeof pct === 'number') ? Math.max(0, Math.min(100, pct)) : 0;
      fill.style.width = p + '%';
      if (typeof pct === 'number' && pct >= 80) fill.classList.add('budget-red');
      else if (typeof pct === 'number' && pct >= 60) fill.classList.add('budget-orange');
      track.appendChild(fill);
      var val = document.createElement('span');
      val.className = 'budget-val';
      val.textContent = (typeof pct === 'number')
        ? (current + (max ? '/' + max : '') + ' (' + pct + '%)')
        : (current + (max ? '/' + max : '') + ' (no limit)');
      row.appendChild(name); row.appendChild(track); row.appendChild(val);
      return row;
    }
    function renderBudget() {
      fetch('/api/budget/status').then(function (r) { return r.json(); }).then(function (b) {
        clearChildren(budgetEl);
        var s = b.session || {};
        budgetEl.appendChild(barNode('session tokens', s.tokens || 0, s.max_tokens, s.tokens_pct));
        if (s.max_usd) budgetEl.appendChild(barNode('session usd', s.cost_usd || 0, s.max_usd, s.usd_pct));
        (b.per_role || []).forEach(function (r) {
          budgetEl.appendChild(barNode(r.role, r.tokens, r.max_tokens, r.pct));
        });
        budgetEl.hidden = false;
      }).catch(function () {});
    }
    function renderProviders() {
      fetch('/api/providers/cooldowns').then(function (r) { return r.json(); }).then(function (data) {
        clearChildren(providersEl);
        var rows = data.cooldowns || [];
        var heading = document.createElement('span');
        heading.className = 'providers-label';
        heading.textContent = rows.length ? 'cold providers:' : 'providers: all ready';
        providersEl.appendChild(heading);
        rows.forEach(function (c) {
          var left = '';
          var until = Date.parse(c.cooldown_until);
          if (!isNaN(until)) {
            var mins = Math.max(0, Math.round((until - Date.now()) / 60000));
            left = ' (' + mins + 'm left)';
          }
          var chip = document.createElement('span');
          chip.className = 'provider-chip provider-cold';
          chip.textContent = c.role + ': ' + c.provider + ' cold' + left;
          chip.title = 'trigger: ' + (c.trigger || 'unknown');
          providersEl.appendChild(chip);
        });
        providersEl.hidden = false;
      }).catch(function () {});
    }
    function renderBlocked() {
      fetch('/api/tasks').then(function (r) { return r.json(); }).then(function (data) {
        var items = (data && data.work_items) || data || [];
        if (!Array.isArray(items)) items = [];
        var n = items.filter(function (w) {
          return (w.status === 'blocked' || w.phase === 'blocked');
        }).length;
        if (n > 0) {
          blockedChip.hidden = false;
          blockedChip.textContent = '⚠ ' + n + ' blocked';
          if (n > lastBlockedCount) {
            blockedChip.classList.add('chip-pulse');
            setTimeout(function () { blockedChip.classList.remove('chip-pulse'); }, 1500);
          }
        } else {
          blockedChip.hidden = true;
        }
        lastBlockedCount = n;
      }).catch(function () {});
    }
    function renderCoverage() {
      if (!coverageFlagsOn) { coverageEl.hidden = true; return; }
      fetch('/api/dashboard/overview').then(function (r) { return r.json(); }).then(function (ov) {
        clearChildren(coverageEl);
        var pct = (ov && typeof ov.coverage_pct === 'number') ? ov.coverage_pct : null;
        var label = document.createElement('span');
        label.className = 'coverage-label';
        label.textContent = 'coverage floor: ' + (pct === null ? 'n/a' : pct + '%');
        var donut = document.createElement('div');
        donut.className = 'coverage-donut';
        if (pct !== null) donut.style.setProperty('--cov', pct + '%');
        coverageEl.appendChild(donut);
        coverageEl.appendChild(label);
        coverageEl.hidden = false;
      }).catch(function () { coverageEl.hidden = true; });
    }
    function renderSparkline() {
      fetch('/api/events/history?limit=200').then(function (r) { return r.json(); }).then(function (data) {
        var events = (data && data.events) || data || [];
        if (!Array.isArray(events)) events = [];
        var now = Date.now();
        var perRole = {};
        events.forEach(function (ev) {
          if (ev.kind !== 'step.start') return;
          var p = ev.payload || {};
          var role = p.role || ev.actor || 'unknown';
          var ts = Date.parse(ev.ts);
          if (isNaN(ts) || now - ts > 60000) return;
          perRole[role] = perRole[role] || [];
          perRole[role].push(ts);
        });
        clearChildren(sparkEl);
        var roles = Object.keys(perRole);
        if (!roles.length) { sparkEl.hidden = true; return; }
        roles.forEach(function (role) {
          var row = document.createElement('div');
          row.className = 'spark-row';
          var name = document.createElement('span');
          name.className = 'spark-label';
          name.textContent = role;
          var bars = document.createElement('span');
          bars.className = 'spark-bars';
          // 12 buckets of 5s over the last 60s.
          var buckets = new Array(12).fill(0);
          perRole[role].forEach(function (ts) {
            var idx = Math.min(11, Math.floor((now - ts) / 5000));
            buckets[11 - idx] += 1;
          });
          var maxB = Math.max.apply(null, buckets.concat([1]));
          buckets.forEach(function (c) {
            var b = document.createElement('i');
            b.style.height = Math.round((c / maxB) * 100) + '%';
            bars.appendChild(b);
          });
          row.appendChild(name); row.appendChild(bars);
          sparkEl.appendChild(row);
        });
        sparkEl.hidden = false;
      }).catch(function () {});
    }
    function renderAutonomyWidgets(sess) {
      renderBudget();
      renderProviders();
      renderBlocked();
      renderCoverage();
      renderSparkline();
    }

    refresh();
    setInterval(refresh, 4000);
  }

  function renderHelpDoc() {
    var slot = document.getElementById('help-doc');
    var loading = document.getElementById('help-loading');
    if (!slot) return;
    fetch('/api/help').then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (payload) {
        if (!r.ok) throw new Error(payload.message || payload.error || ('HTTP ' + r.status));
        return payload;
      });
    }).then(function (payload) {
      if (loading) loading.remove();
      // /api/help returns HTML rendered by agentic_os.help_md.render from
      // docs/dashboard-help.md — the renderer HTML-escapes every source-text
      // token. Parse it through DOMParser (which never executes scripts or
      // loads subresources) and transplant the structural nodes; that gives
      // us a structurally-safe path even though the input is already trusted.
      var doc = new DOMParser().parseFromString(payload.html || '', 'text/html');
      var allowed = { H1:1, H2:1, H3:1, P:1, UL:1, OL:1, LI:1, PRE:1, CODE:1, STRONG:1, EM:1, A:1 };
      Array.from(doc.body.childNodes).forEach(function (node) {
        if (node.nodeType === 1 && !allowed[node.tagName]) return;
        slot.appendChild(document.importNode(node, true));
      });
      // Scrub anchors so only same-origin / fragment / docs/ links survive.
      slot.querySelectorAll('a').forEach(function (a) {
        var href = a.getAttribute('href') || '';
        if (href.startsWith('#') || href.startsWith('/') || href.startsWith('mailto:')) return;
        a.removeAttribute('href');
      });
      if (window.location.hash) {
        var target = document.getElementById(window.location.hash.slice(1));
        if (target) target.scrollIntoView({ block: 'start' });
      }
    }).catch(function (e) {
      if (loading) {
        loading.className = 'msg err';
        loading.textContent = 'Help failed to load: ' + e.message;
      }
    });
  }

  function wireInbox() {
    var fileInput = document.getElementById('inbox-file');
    var uploadBtn = document.getElementById('inbox-upload-btn');
    var ingestBtn = document.getElementById('inbox-ingest-btn');
    var synthesizeBtn = document.getElementById('inbox-synthesize-btn');
    var synthesizeTitle = document.getElementById('inbox-synthesize-title');
    var msg = document.getElementById('inbox-msg');
    var pendingList = document.getElementById('inbox-pending-list');
    var resultsList = document.getElementById('inbox-results-list');
    if (!fileInput || !uploadBtn || !ingestBtn || !synthesizeBtn) return;
    var writeEnabled = false;

    function setMsg(text, cls) {
      if (!msg) return;
      msg.textContent = text;
      msg.className = 'msg ' + (cls || 'muted');
    }

    function setWriteButtonsDisabled(disabled) {
      uploadBtn.disabled = disabled;
      ingestBtn.disabled = disabled;
      synthesizeBtn.disabled = disabled;
    }

    function applyWriteGate() {
      return fetch('/api/config').then(function (r) {
        return r.ok ? r.json() : null;
      }).then(function (cfg) {
        var enabled = !!(cfg && cfg.dashboard && cfg.dashboard.enable_write_endpoints);
        writeEnabled = enabled;
        setWriteButtonsDisabled(!enabled);
        if (!enabled) {
          setMsg(
            'writes disabled — set dashboard.enable_write_endpoints=true, restart with serve --full, or start full autonomy',
            'err'
          );
        } else if (msg && msg.textContent.indexOf('writes disabled') === 0) {
          setMsg('Ready.', 'ok');
        }
      }).catch(function () {});
    }

    function extractionBadgeLabel(status) {
      if (status === 'ok') return 'extract: OK';
      if (status === 'low') return 'extract: LOW';
      if (status === 'failed') return 'extract: FAILED';
      if (status === 'unsupported') return 'extract: UNSUPPORTED';
      return 'extract: ?';
    }

    function appendExtractionBadge(li, extraction) {
      if (!extraction || !extraction.status) return;
      var badge = document.createElement('span');
      badge.className = 'badge badge-extract-' + extraction.status;
      badge.textContent = extractionBadgeLabel(extraction.status);
      var tooltip = extraction.message;
      if (!tooltip) {
        if (extraction.status === 'ok' && extraction.density != null) {
          tooltip = 'Extractable text density ' + extraction.density + ' chars/page (' +
            (extraction.chars || 0) + ' chars over ' + (extraction.pages || 0) + ' page(s)).';
        } else if (extraction.status === 'ok') {
          tooltip = 'Extractable-text document.';
        }
      }
      if (tooltip) {
        badge.title = tooltip;
        badge.setAttribute('aria-label', tooltip);
      }
      li.appendChild(document.createTextNode(' '));
      li.appendChild(badge);
    }

    function renderPending(files) {
      if (!pendingList) return;
      clearChildren(pendingList);
      if (!files.length) {
        var li = document.createElement('li');
        li.className = 'placeholder';
        li.textContent = '(no files pending in ./inbox/ or ./pretask/)';
        pendingList.appendChild(li);
        return;
      }
      files.forEach(function (f) {
        var li = document.createElement('li');
        var extraction = f.extraction || null;
        if (extraction && extraction.status && extraction.status !== 'ok') {
          li.classList.add('inbox-pending-' + extraction.status);
        }
        var sizeLabel = f.size != null ? f.size + ' bytes' : 'unknown size';
        li.appendChild(document.createTextNode(f.path + ' — ' + sizeLabel));
        appendExtractionBadge(li, extraction);
        if (extraction && extraction.status && extraction.status !== 'ok' && extraction.message) {
          var hint = document.createElement('div');
          hint.className = 'inbox-extract-msg muted';
          hint.textContent = extraction.message;
          li.appendChild(hint);
        }
        pendingList.appendChild(li);
      });
    }

    function refreshPending() {
      return fetch('/api/inbox').then(function (r) {
        return r.ok ? r.json() : { files: [] };
      }).then(function (data) {
        renderPending(data.files || []);
      }).catch(function () {});
    }

    function appendTaskLink(li, workItemId) {
      if (!workItemId) return;
      var a = document.createElement('a');
      a.href = '/tasks/' + encodeURIComponent(workItemId);
      a.textContent = workItemId;
      li.appendChild(a);
    }

    function renderInboxResults(results) {
      if (!resultsList) return;
      clearChildren(resultsList);
      if (!results.length) {
        var empty = document.createElement('li');
        empty.className = 'placeholder';
        empty.textContent = '(no ingest results)';
        resultsList.appendChild(empty);
        return;
      }
      results.forEach(function (r) {
        var li = document.createElement('li');
        if (r.status === 'created') {
          li.className = 'autonomy-log-ok';
          li.appendChild(document.createTextNode(r.source + ' -> '));
          appendTaskLink(li, r.work_item_id);
          li.appendChild(document.createTextNode(' (' + (r.title || 'task') + ')'));
        } else {
          li.textContent = r.source + ' - ' + r.error;
          li.className = 'autonomy-log-err';
        }
        resultsList.appendChild(li);
      });
    }

    function readFileAsBase64(file) {
      return new Promise(function (resolve, reject) {
        var reader = new FileReader();
        reader.onload = function () {
          var result = String(reader.result || '');
          var idx = result.indexOf(',');
          resolve(idx >= 0 ? result.slice(idx + 1) : result);
        };
        reader.onerror = function () { reject(new Error('file read failed')); };
        reader.readAsDataURL(file);
      });
    }

    uploadBtn.addEventListener('click', function () {
      if (!writeEnabled) {
        applyWriteGate();
        return;
      }
      var file = fileInput.files && fileInput.files[0];
      if (!file) {
        setMsg('Pick a file first.', 'err');
        return;
      }
      uploadBtn.disabled = true;
      setMsg('Uploading ' + file.name + '…', 'muted');
      readFileAsBase64(file).then(function (b64) {
        return fetch('/api/inbox/upload', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: file.name, content_base64: b64 }),
        }).then(function (r) {
          return r.json().catch(function () { return {}; }).then(function (payload) {
            if (!r.ok) {
              throw new Error(payload.message || payload.error || ('HTTP ' + r.status));
            }
            return payload;
          });
        });
      }).then(function (payload) {
        setMsg('Uploaded to ' + payload.path + ' (' + payload.bytes + ' bytes).', 'ok');
        fileInput.value = '';
        return refreshPending();
      }).catch(function (e) {
        setMsg('Upload failed: ' + e.message, 'err');
      }).then(function () {
        uploadBtn.disabled = false;
      });
    });

    ingestBtn.addEventListener('click', function () {
      if (!writeEnabled) {
        applyWriteGate();
        return;
      }
      ingestBtn.disabled = true;
      setMsg('Ingesting…', 'muted');
      fetch('/api/inbox/ingest', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (payload) {
          if (!r.ok) {
            throw new Error(payload.message || payload.error || ('HTTP ' + r.status));
          }
          return payload;
        });
      }).then(function (payload) {
        var results = payload.results || [];
        setMsg(
          'Ingest done — created ' + (payload.created || 0)
          + ', failed ' + (payload.failed || 0) + '.',
          (payload.failed > 0 ? 'err' : 'ok')
        );
        renderInboxResults(results);
        return refreshPending();
      }).catch(function (e) {
        setMsg('Ingest failed: ' + e.message, 'err');
      }).then(function () {
        ingestBtn.disabled = !writeEnabled;
      });
    });

    synthesizeBtn.addEventListener('click', function () {
      if (!writeEnabled) {
        applyWriteGate();
        return;
      }
      synthesizeBtn.disabled = true;
      setMsg('Creating task from pending documents…', 'muted');
      fetch('/api/inbox/synthesize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: synthesizeTitle && synthesizeTitle.value ? synthesizeTitle.value : null
        }),
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (payload) {
          if (!r.ok) {
            throw new Error(payload.message || payload.error || ('HTTP ' + r.status));
          }
          return payload;
        });
      }).then(function (payload) {
        if (payload.status === 'empty') {
          setMsg('No pending documents.', 'muted');
          renderInboxResults([]);
        } else if (payload.status === 'created') {
          setMsg(
            'Task created from ' + (payload.source_count || 0)
            + ' document(s); failed ' + (payload.failed || 0) + '.',
            (payload.failed > 0 ? 'err' : 'ok')
          );
          renderInboxResults(payload.results || []);
        } else {
          setMsg('Task synthesis failed; failed ' + (payload.failed || 0) + ' document(s).', 'err');
          renderInboxResults(payload.results || []);
        }
        return refreshPending();
      }).catch(function (e) {
        setMsg('Task synthesis failed: ' + e.message, 'err');
      }).then(function () {
        synthesizeBtn.disabled = !writeEnabled;
      });
    });

    applyWriteGate();
    setInterval(applyWriteGate, 4000);
    refreshPending();
    setInterval(refreshPending, 5000);
  }

  function wireSupportBundle() {
    var btn = document.getElementById('support-bundle-build-btn');
    var msg = document.getElementById('support-bundle-msg');
    var resultsList = document.getElementById('support-bundle-results');
    if (!btn) return;
    var writeEnabled = false;

    function setMsg(text, cls) {
      if (!msg) return;
      msg.textContent = text;
      msg.className = 'msg ' + (cls || 'muted');
    }

    function applyWriteGate() {
      return fetch('/api/config').then(function (r) {
        return r.ok ? r.json() : null;
      }).then(function (cfg) {
        var enabled = !!(cfg && cfg.dashboard && cfg.dashboard.enable_write_endpoints);
        writeEnabled = enabled;
        btn.disabled = !enabled;
        if (!enabled) {
          setMsg(
            'writes disabled — set dashboard.enable_write_endpoints=true, restart with serve --full, or start full autonomy',
            'err'
          );
        } else if (msg && msg.textContent.indexOf('writes disabled') === 0) {
          setMsg('Ready.', 'ok');
        }
      }).catch(function () {});
    }

    function appendResult(payload) {
      if (!resultsList) return;
      var li = document.createElement('li');
      li.className = 'autonomy-log-ok';
      var link = document.createElement('a');
      link.href = payload.download_url || '#';
      link.textContent = payload.filename || payload.path || 'support bundle';
      link.setAttribute('download', payload.filename || '');
      li.appendChild(link);
      var meta = document.createElement('span');
      meta.className = 'muted';
      meta.textContent = ' — ' + (payload.bytes || 0) + ' bytes, ' +
        ((payload.manifest && payload.manifest.files && payload.manifest.files.length) || 0) +
        ' file(s)';
      li.appendChild(meta);
      resultsList.appendChild(li);
    }

    btn.addEventListener('click', function () {
      if (!writeEnabled) {
        applyWriteGate();
        return;
      }
      btn.disabled = true;
      setMsg('Building support bundle…', 'muted');
      fetch('/api/support-bundle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (payload) {
          if (!r.ok) {
            throw new Error(payload.message || payload.error || ('HTTP ' + r.status));
          }
          return payload;
        });
      }).then(function (payload) {
        setMsg(
          'Support bundle ready: ' + payload.path + ' (' + payload.bytes + ' bytes). ' +
          'Review the manifest before sending.',
          'ok'
        );
        appendResult(payload);
      }).catch(function (e) {
        setMsg('Support bundle failed: ' + e.message, 'err');
      }).then(function () {
        btn.disabled = !writeEnabled;
      });
    });

    applyWriteGate();
    setInterval(applyWriteGate, 4000);
  }

  // ===================================================================
  // Issues #193/#195/#196/#197/#199/#201 — Cockpit polling + rendering.
  // ===================================================================

  function _clearChildren(el) {
    if (!el) return;
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  function _setText(id, value) {
    var el = document.getElementById(id);
    if (el) el.textContent = value == null ? '–' : String(value);
  }

  function _setPill(id, state, label) {
    var el = document.getElementById(id);
    if (!el) return;
    el.dataset.state = state || 'idle';
    el.textContent = label || state || '';
  }

  function _outcomePalette(outcome) {
    switch (outcome) {
      case 'green':
      case 'success':
      case 'pass': return { color: 'var(--c-success)', label: outcome };
      case 'product': return { color: 'var(--c-danger)', label: 'product' };
      case 'infra': return { color: 'var(--c-infra)', label: 'infra' };
      case 'timeout': return { color: 'var(--c-warn)', label: 'timeout' };
      case 'known-bug':
      case 'known_bug': return { color: 'var(--c-known)', label: 'known-bug' };
      default: return { color: 'var(--c-muted)', label: outcome || 'unknown' };
    }
  }

  function _renderPreflight(payload) {
    var list = document.getElementById('preflight-checks');
    if (!list) return;
    _clearChildren(list);
    var checks = (payload && payload.checks) || [];
    if (!checks.length) {
      var empty = document.createElement('li');
      empty.className = 'placeholder';
      empty.textContent = 'no checks available';
      list.appendChild(empty);
    }
    checks.forEach(function (c) {
      var li = document.createElement('li');
      li.className = 'preflight-item';
      li.dataset.state = c.status || 'pass';
      var head = document.createElement('div');
      head.className = 'preflight-head';
      var pill = document.createElement('span');
      pill.className = 'status-pill';
      pill.dataset.state = c.status || 'pass';
      pill.textContent = c.status || 'pass';
      head.appendChild(pill);
      var name = document.createElement('strong');
      name.textContent = c.id || '';
      head.appendChild(name);
      li.appendChild(head);
      var msg = document.createElement('div');
      msg.className = 'preflight-msg';
      msg.textContent = c.message || '';
      li.appendChild(msg);
      if (c.actions && c.actions.length) {
        var ul = document.createElement('ul');
        ul.className = 'preflight-actions';
        c.actions.forEach(function (a) {
          var ai = document.createElement('li');
          ai.textContent = a;
          ul.appendChild(ai);
        });
        li.appendChild(ul);
      }
      list.appendChild(li);
    });
    if (payload && payload.ok === false) {
      _setPill('preflight-overall', 'fail', 'not ready');
    } else if (payload && payload.warn) {
      _setPill('preflight-overall', 'warn', 'warn');
    } else {
      _setPill('preflight-overall', 'pass', 'ready');
    }
  }

  function _renderCurrentProcess(cur) {
    var body = document.getElementById('process-body');
    if (!body) return;
    _clearChildren(body);
    if (!cur) {
      _setPill('process-pill', 'idle', 'idle');
      var idle = document.createElement('p');
      idle.className = 'muted small';
      idle.textContent = 'No action running. Start one from a task page.';
      body.appendChild(idle);
      return;
    }
    _setPill('process-pill', 'running', 'running');
    var wid = cur.work_item_id || '';
    var workflow = cur.workflow || cur.task_kind || 'run';
    var line1 = document.createElement('p');
    line1.className = 'process-headline';
    var strong = document.createElement('strong');
    strong.textContent = workflow;
    line1.appendChild(strong);
    if (wid) {
      line1.appendChild(document.createTextNode(' · ' + wid));
    }
    body.appendChild(line1);
    if (cur.started_at) {
      var line2 = document.createElement('p');
      line2.className = 'muted small';
      line2.textContent = 'started ' + cur.started_at;
      body.appendChild(line2);
    }
    if (cur.active_count > 1) {
      var more = document.createElement('p');
      more.className = 'muted small';
      more.textContent = '(+' + (cur.active_count - 1) + ' more active)';
      body.appendChild(more);
    }
    if (cur.log_path) {
      var link = document.createElement('a');
      link.className = 'btn-link';
      link.href = '/files/' + cur.log_path;
      link.textContent = 'Open log →';
      body.appendChild(link);
    }
  }

  function _renderNextAction(nxt) {
    var body = document.getElementById('next-action-body');
    if (!body) return;
    _clearChildren(body);
    if (!nxt) {
      _setPill('next-pill', 'idle', 'clear');
      var empty = document.createElement('p');
      empty.className = 'muted small';
      empty.textContent = 'No pending work — queue is clear.';
      body.appendChild(empty);
      return;
    }
    _setPill('next-pill', 'attention', 'attention');
    var title = document.createElement('p');
    title.className = 'next-action-title';
    var strong = document.createElement('strong');
    strong.textContent = nxt.action || 'Review';
    title.appendChild(strong);
    body.appendChild(title);
    var hint = document.createElement('p');
    hint.className = 'muted small';
    hint.textContent = nxt.hint || '';
    body.appendChild(hint);
    if (nxt.work_item_id) {
      var link = document.createElement('a');
      link.className = 'btn-link';
      link.href = '/tasks/' + encodeURIComponent(nxt.work_item_id);
      link.textContent = nxt.work_item_id + ' →';
      body.appendChild(link);
    }
  }

  function _renderLatestRunSummary(run) {
    var body = document.getElementById('run-summary-body');
    if (!body) return;
    _clearChildren(body);
    if (!run) {
      _setPill('run-verdict', 'idle', 'no runs');
      var empty = document.createElement('p');
      empty.className = 'muted small';
      empty.textContent = 'No runs yet.';
      body.appendChild(empty);
      return;
    }
    var exit = run.exit_code;
    var failureKind = run.failure_kind;
    var verdict = exit === 0 ? 'pass' :
                  failureKind === 'product' ? 'product' :
                  failureKind === 'infra' ? 'infra' :
                  failureKind === 'timeout' ? 'timeout' :
                  'unknown';
    _setPill('run-verdict', verdict, verdict);
    var line = document.createElement('p');
    line.className = 'run-summary-line';
    var strong = document.createElement('strong');
    strong.textContent = exit == null ? '—' : String(exit);
    line.appendChild(document.createTextNode('exit '));
    line.appendChild(strong);
    line.appendChild(document.createTextNode(' · ' + (failureKind || 'none')));
    body.appendChild(line);
    if (run.task_id) {
      var meta = document.createElement('p');
      meta.className = 'muted small';
      meta.textContent = 'task ' + run.task_id;
      body.appendChild(meta);
    }
    if (run.finished_at) {
      var when = document.createElement('p');
      when.className = 'muted small';
      when.textContent = 'finished ' + run.finished_at;
      body.appendChild(when);
    }
    if (run.manifest_path) {
      var link = document.createElement('a');
      link.className = 'btn-link';
      link.href = '/files/' + run.manifest_path;
      link.textContent = 'Open manifest →';
      body.appendChild(link);
    }
  }

  function _renderMetrics(overview) {
    var cand = overview.candidates || {};
    var gen = overview.generated_tests || {};
    var budget = overview.budget || {};
    _setText('m-planned', cand.total || 0);
    _setText(
      'm-planned-sub',
      (cand.generate_now || 0) + ' approved · ' +
      (cand.needs_operator_decision || 0) + ' pending · ' +
      (cand.not_testable || 0) + ' n/a'
    );
    _setText('m-generated', gen.total || 0);
    _setText(
      'm-generated-sub',
      (gen.api || 0) + ' api · ' + (gen.ui || 0) + ' ui · ' + (gen.other || 0) + ' other'
    );
    _setText('m-debt', cand.needs_operator_decision || 0);
    _setText(
      'm-debt-sub',
      (cand.needs_operator_decision || 0) > 0
        ? 'decisions awaiting operator'
        : 'all candidates decided'
    );
    var debtCard = document.querySelector('.metric-debt');
    if (debtCard) {
      debtCard.dataset.state = (cand.needs_operator_decision || 0) > 0 ? 'attention' : 'ok';
    }
    var totalTokens = budget.total_tokens || 0;
    var ratio = typeof budget.ratio === 'number' ? budget.ratio : 0;
    var pct = Math.round(ratio * 100);
    _setText('m-budget', totalTokens.toLocaleString ? totalTokens.toLocaleString() : totalTokens);
    _setText(
      'm-budget-sub',
      pct + '% · $' + Number(budget.cost_usd || 0).toFixed(4)
    );
    var budgetCard = document.querySelector('.metric-budget');
    if (budgetCard) {
      budgetCard.dataset.state = budget.state || 'ok';
    }
  }

  function _bar(svg, x, y, w, h, fill, title) {
    var rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('x', x);
    rect.setAttribute('y', y);
    rect.setAttribute('width', Math.max(1, w));
    rect.setAttribute('height', Math.max(1, h));
    rect.setAttribute('fill', fill);
    if (title) {
      var t = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      t.textContent = title;
      rect.appendChild(t);
    }
    svg.appendChild(rect);
    return rect;
  }

  function _emptyChart(container, label) {
    _clearChildren(container);
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 320 80');
    var t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    t.setAttribute('x', 10); t.setAttribute('y', 40);
    t.setAttribute('font-size', '11'); t.setAttribute('fill', 'currentColor');
    t.textContent = label;
    svg.appendChild(t);
    container.appendChild(svg);
  }

  function _renderHistoryChart(history) {
    var container = document.getElementById('chart-history');
    if (!container) return;
    if (!history || !history.length) {
      _emptyChart(container, 'no run history yet');
      _setText('m-runs', 0);
      _setText('m-runs-sub', '0 green · 0 product · 0 infra');
      return;
    }
    _clearChildren(container);
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 320 80');
    svg.setAttribute('preserveAspectRatio', 'none');
    var n = history.length;
    var slot = 320 / n;
    var counts = { green: 0, product: 0, infra: 0, timeout: 0, unknown: 0 };
    history.forEach(function (r, i) {
      var fill = _outcomePalette(r.outcome).color;
      counts[r.outcome] = (counts[r.outcome] || 0) + 1;
      var pad = Math.min(2, slot * 0.15);
      _bar(svg, i * slot + pad, 8, slot - pad * 2, 64, fill,
           r.outcome + ' · ' + (r.task_id || '') + (r.finished_at ? ' · ' + r.finished_at : ''));
    });
    container.appendChild(svg);
    _setText('m-runs', n);
    _setText(
      'm-runs-sub',
      (counts.green || 0) + ' green · ' +
      (counts.product || 0) + ' product · ' +
      (counts.infra || 0) + ' infra'
    );
  }

  function _renderFunnelChart(funnel) {
    var container = document.getElementById('chart-funnel');
    if (!container) return;
    var stages = [
      { key: 'planned', label: 'plan', color: 'var(--c-accent)' },
      { key: 'approved', label: 'ok', color: 'var(--c-success)' },
      { key: 'generated', label: 'gen', color: 'var(--c-info)' },
      { key: 'run_against_work_items', label: 'run', color: 'var(--c-warn)' }
    ];
    var max = 0;
    stages.forEach(function (s) {
      var v = funnel[s.key] || 0;
      if (v > max) max = v;
    });
    if (max === 0) { _emptyChart(container, 'no candidates yet'); return; }
    _clearChildren(container);
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 320 80');
    var slot = 320 / stages.length;
    stages.forEach(function (s, i) {
      var v = funnel[s.key] || 0;
      var h = Math.max(2, (v / max) * 56);
      _bar(svg, i * slot + 8, 72 - h, slot - 16, h, s.color, s.label + ': ' + v);
      var label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      label.setAttribute('x', i * slot + slot / 2);
      label.setAttribute('y', 78);
      label.setAttribute('text-anchor', 'middle');
      label.setAttribute('font-size', '9');
      label.setAttribute('fill', 'currentColor');
      label.textContent = s.label + ' ' + v;
      svg.appendChild(label);
    });
    container.appendChild(svg);
  }

  function _renderTrendChart(trend) {
    var container = document.getElementById('chart-trend');
    if (!container) return;
    var keys = ['green', 'product', 'infra', 'timeout', 'unknown'];
    var total = 0;
    keys.forEach(function (k) { total += trend[k] || 0; });
    if (total === 0) { _emptyChart(container, 'no runs yet'); return; }
    _clearChildren(container);
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 320 80');
    var x = 0;
    keys.forEach(function (k) {
      var v = trend[k] || 0;
      if (v === 0) return;
      var w = (v / total) * 320;
      _bar(svg, x, 20, w, 40, _outcomePalette(k).color, k + ': ' + v);
      var label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      label.setAttribute('x', x + w / 2);
      label.setAttribute('y', 70);
      label.setAttribute('text-anchor', 'middle');
      label.setAttribute('font-size', '9');
      label.setAttribute('fill', 'currentColor');
      label.textContent = k + ' ' + v;
      svg.appendChild(label);
      x += w;
    });
    container.appendChild(svg);
  }

  function _renderLastRunStructured(overview) {
    var run = overview.latest_run;
    if (!run) {
      _setPill('last-run-verdict', 'idle', 'no runs');
      _setText('last-run-meta', '');
      ['total','passed','failed','skipped','known-bug'].forEach(function (k) {
        _setText('lr-' + k, 'n/a');
      });
      return;
    }
    var exit = run.exit_code;
    var fk = run.failure_kind;
    var verdict = exit === 0 ? 'pass' :
                  fk === 'product' ? 'product' :
                  fk === 'infra' ? 'infra' :
                  fk === 'timeout' ? 'timeout' :
                  'unknown';
    _setPill('last-run-verdict', verdict, verdict);
    _setText(
      'last-run-meta',
      'exit ' + (exit == null ? '—' : exit) +
      ' · task ' + (run.task_id || '—') +
      (run.finished_at ? ' · ' + run.finished_at : '')
    );
    if (run.report_counts_available) {
      _setText('lr-total', run.total != null ? run.total : '0');
      _setText('lr-passed', run.passed != null ? run.passed : '0');
      _setText('lr-failed', run.failed != null ? run.failed : '0');
      _setText('lr-skipped', run.skipped != null ? run.skipped : '0');
      _setText('lr-known-bug', run.known_bug != null ? run.known_bug : '0');
    } else {
      _setText('lr-total', 'counts unavailable');
      _setText('lr-passed', 'n/a');
      _setText('lr-failed', 'n/a');
      _setText('lr-skipped', 'n/a');
      _setText('lr-known-bug', 'n/a');
    }
    var failures = Array.isArray(run.failures) ? run.failures : [];
    var box = document.getElementById('last-run-failures');
    var list = document.getElementById('last-run-failures-list');
    if (!box || !list) return;
    _clearChildren(list);
    if (!failures.length) { box.hidden = true; return; }
    box.hidden = false;
    failures.slice(0, 20).forEach(function (f) {
      var li = document.createElement('li');
      var tags = Array.isArray(f.tags) ? f.tags : [];
      var isKnown = tags.some(function (t) { return /known.?bug/i.test(t); });
      li.dataset.knownBug = isKnown ? 'true' : 'false';
      var name = document.createElement('strong');
      name.textContent = f.name || f.id || '(unnamed)';
      li.appendChild(name);
      if (isKnown) {
        var chip = document.createElement('span');
        chip.className = 'status-pill';
        chip.dataset.state = 'known-bug';
        chip.textContent = 'known-bug';
        li.appendChild(chip);
      }
      if (f.message) {
        var msg = document.createElement('div');
        msg.className = 'muted small';
        msg.textContent = String(f.message).slice(0, 200);
        li.appendChild(msg);
      }
      list.appendChild(li);
    });
  }

  function refreshCockpit() {
    Promise.all([
      fetch('/api/dashboard/overview').then(function (r) { return r.ok ? r.json() : {}; }),
      fetch('/api/dashboard/preflight').then(function (r) { return r.ok ? r.json() : {}; }),
      fetch('/api/dashboard/charts').then(function (r) { return r.ok ? r.json() : {}; })
    ]).then(function (results) {
      var overview = results[0] || {};
      var preflight = results[1] || {};
      var charts = results[2] || {};
      _renderPreflight(preflight);
      _renderCurrentProcess(overview.current_process);
      _renderNextAction(overview.next_action);
      _renderLatestRunSummary(overview.latest_run);
      _renderMetrics(overview);
      _renderHistoryChart(charts.run_history);
      _renderFunnelChart(charts.funnel || {});
      _renderTrendChart(charts.failure_trend || {});
      _renderLastRunStructured(overview);
    }).catch(function () { /* keep last frame */ });
  }

  function startCockpitPolling() {
    refreshCockpit();
    setInterval(refreshCockpit, 5000);
  }

  // Issue #247 — work-item Verification tab. Seeds reviewer/triager step.*
  // events for this work item from history and keeps them live via SSE.
  function wireWorkItemVerification() {
    var ol = document.getElementById('wi-verif-timeline');
    if (!ol) return;
    var m = window.location.pathname.match(/^\/tasks\/([^\/?#]+)/);
    var workItemId = m ? decodeURIComponent(m[1]) : null;
    if (!workItemId) return;
    var rows = [];

    function parseDetail(detail) {
      var out = {};
      (detail || '').split(/\r?\n/).forEach(function (line) {
        var mm = line.match(/^\s*(verdict|reason|severity|priority|owasp|iso25010)\s*:\s*(.+)$/i);
        if (mm) out[mm[1].toLowerCase()] = mm[2].trim();
      });
      return out;
    }

    function render() {
      while (ol.firstChild) ol.removeChild(ol.firstChild);
      if (!rows.length) {
        var ph = document.createElement('li');
        ph.className = 'placeholder';
        ph.textContent = '(no reviewer / triager events for this work item)';
        ol.appendChild(ph);
        return;
      }
      rows.slice(0, 60).forEach(function (ev) {
        var p = ev.payload || {};
        var li = document.createElement('li');
        li.className = 'step-row verif-row';
        var time = document.createElement('span');
        time.className = 'step-time';
        time.textContent = (ev.ts || '').replace('T', ' ').slice(0, 19);
        li.appendChild(time);
        var roleChip = document.createElement('span');
        roleChip.className = 'chip chip-role';
        roleChip.textContent = p.role || '?';
        li.appendChild(roleChip);
        var parsed = parseDetail(p.detail);
        Object.keys(parsed).forEach(function (k) {
          var c = document.createElement('span');
          c.className = 'chip chip-phase';
          c.textContent = k + ': ' + parsed[k];
          li.appendChild(c);
        });
        ol.appendChild(li);
      });
    }

    function ingest(ev) {
      var p = ev.payload || {};
      if (p.work_item_id !== workItemId) return;
      if (p.role !== 'reviewer' && p.role !== 'triager') return;
      rows.unshift(ev);
      if (rows.length > 120) rows.length = 120;
      render();
    }

    fetch('/api/events/history?kind=step.*&limit=200').then(function (r) {
      return r.ok ? r.json() : { events: [] };
    }).then(function (res) {
      (res.events || []).forEach(function (ev) {
        var p = ev.payload || {};
        if (p.work_item_id === workItemId && (p.role === 'reviewer' || p.role === 'triager')) rows.push(ev);
      });
      render();
    }).catch(function () {});

    if (global.EventSource) {
      var es = new EventSource('/api/events?kind=step.*');
      es.onmessage = function (e) { try { ingest(JSON.parse(e.data)); } catch (_) {} };
    }
  }

  // Issue #242 — work-item detail SUT branch surface. Reads the canonical
  // branch name and exposes "View diff" hitting GET /api/sut/git/diff.
  function wireWorkItemGit() {
    var stateEl = document.getElementById('wi-git-state');
    if (!stateEl) return;
    var m = window.location.pathname.match(/^\/tasks\/([^\/?#]+)/);
    var workItemId = m ? decodeURIComponent(m[1]) : null;
    if (!workItemId) return;
    var btn = document.getElementById('wi-git-view-diff');
    var branchEl = document.getElementById('wi-git-branch');
    var diffWrap = document.getElementById('wi-git-diff-wrap');
    var diffBody = document.getElementById('wi-git-diff-body');

    function refresh() {
      fetch('/api/sut/git/diff?work_item=' + encodeURIComponent(workItemId))
        .then(function (r) { return r.ok ? r.json() : {}; })
        .then(function (res) {
          if (res && res.branch && branchEl) branchEl.textContent = res.branch;
          if (stateEl) {
            if (res && res.ok) stateEl.textContent = 'present';
            else stateEl.textContent = (res && res.error) ? res.error : 'no branch';
          }
        })
        .catch(function () { if (stateEl) stateEl.textContent = '(unreachable)'; });
    }

    if (btn) {
      btn.addEventListener('click', function () {
        if (diffBody) diffBody.textContent = 'loading…';
        if (diffWrap) diffWrap.hidden = false;
        fetch('/api/sut/git/diff?work_item=' + encodeURIComponent(workItemId))
          .then(function (r) { return r.ok ? r.json() : {}; })
          .then(function (res) {
            if (diffBody) {
              if (res && res.ok) diffBody.textContent = res.diff || '(empty diff)';
              else diffBody.textContent = 'error: ' + ((res && res.error) || 'unknown');
            }
          })
          .catch(function (exc) { if (diffBody) diffBody.textContent = 'error: ' + exc.message; });
      });
    }
    refresh();
    setInterval(refresh, 10000);
  }

  // Issue #241 — Git integration widget. Polls /api/sut/git/status and
  // /api/config (for git.enabled). The "Run git ensure" button hits the
  // new POST /api/sut/git/ensure endpoint; it is disabled when the
  // dashboard runs in read-only mode (enable_write_endpoints=false).
  function wireGitIntegration() {
    var card = document.getElementById('git-int-state');
    if (!card) return;
    var btn = document.getElementById('git-int-ensure');
    var statusEl = document.getElementById('git-int-status');
    var modeEl = document.getElementById('git-int-mode');
    var lastFetchAt = '(never)';

    function setText(id, value) {
      var el = document.getElementById(id);
      if (el) el.textContent = value == null || value === '' ? '(unset)' : String(value);
    }

    function refresh() {
      var gitCfg = {};
      var writesOn = false;
      fetch('/api/config').then(function (r) { return r.ok ? r.json() : {}; }).then(function (cfg) {
        gitCfg = (cfg && cfg.git) || {};
        writesOn = !!((cfg && cfg.dashboard) || {}).enable_write_endpoints;
        if (modeEl) {
          if (gitCfg.enabled) modeEl.textContent = 'enabled';
          else modeEl.textContent = 'disabled';
        }
        return fetch('/api/sut/git/status');
      }).then(function (r) { return r.ok ? r.json() : {}; }).then(function (st) {
        if (st && st.skipped) {
          setText('git-int-state', '⚪ git binary missing');
          setText('git-int-initialized', '–');
          setText('git-int-remote', '–');
          setText('git-int-head', '–');
          if (btn) btn.disabled = true;
          return;
        }
        var enabled = !!gitCfg.enabled;
        setText('git-int-state', enabled ? '🟢 enabled' : '🔴 disabled');
        setText('git-int-initialized', st && st.initialized ? 'yes' : 'no');
        setText('git-int-remote', (st && st.remote_url) || (gitCfg.origin || 'not set'));
        setText('git-int-head', st && st.head_sha ? String(st.head_sha).slice(0, 7) : '–');
        setText('git-int-last-fetch', lastFetchAt);
        if (btn) btn.disabled = !writesOn || !enabled;
      }).catch(function () {
        setText('git-int-state', '(unreachable)');
      });
    }

    if (btn) {
      btn.addEventListener('click', function () {
        btn.disabled = true;
        if (statusEl) statusEl.textContent = 'running…';
        fetch('/api/sut/git/ensure', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
          .then(function (r) { return r.json().then(function (b) { return { ok: r.ok, body: b }; }); })
          .then(function (res) {
            if (statusEl) {
              statusEl.textContent = res.body.summary || (res.ok ? 'ok' : 'error');
            }
            var body = document.getElementById('git-int-ops-body');
            if (body) body.textContent = JSON.stringify(res.body.ops || [], null, 2);
            lastFetchAt = new Date().toISOString();
            refresh();
          })
          .catch(function (exc) {
            if (statusEl) statusEl.textContent = 'error: ' + exc.message;
            btn.disabled = false;
          });
      });
    }

    refresh();
    setInterval(refresh, 8000);
  }

  global.AgenticOS = {
    startRuntimePolling: startRuntimePolling,
    loadConfig: loadConfig,
    startTasksListPolling: startTasksListPolling,
    wireTaskForm: wireTaskForm,
    startTaskDetailPolling: startTaskDetailPolling,
    startEvents: startEvents,
    startTaskEvents: startTaskEvents,
    startActiveTaskPolling: startActiveTaskPolling,
    startPatchPolling: startPatchPolling,
    startTaskPatchPolling: startTaskPatchPolling,
    refreshPatchesGlobal: refreshPatchesGlobal,
    refreshTaskPatches: refreshTaskPatches,
    startFullAutoIndicator: startFullAutoIndicator,
    startSuggestionsPolling: startSuggestionsPolling,
    startAgentsPolling: startAgentsPolling,
    startSkillsPolling: startSkillsPolling,
    wireSutMode: wireSutMode,
    wireGitIntegration: wireGitIntegration,
    wireWorkItemGit: wireWorkItemGit,
    wireWorkItemVerification: wireWorkItemVerification,
    wireFullAutonomy: wireFullAutonomy,
    wireInbox: wireInbox,
    wireSupportBundle: wireSupportBundle,
    renderHelpDoc: renderHelpDoc,
    startCockpitPolling: startCockpitPolling,
    refreshCockpit: refreshCockpit
  };
})(window);
