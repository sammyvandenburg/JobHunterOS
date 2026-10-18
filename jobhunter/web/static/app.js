// Progressive enhancement only. Everything works without JavaScript.
(function () {
  var csrf = document.querySelector('meta[name="csrf"]');
  csrf = csrf ? csrf.getAttribute('content') : '';

  function toast(msg, ok) {
    var el = document.getElementById('toast');
    if (!el) { el = document.createElement('div'); el.id = 'toast'; el.className = 'toast'; el.setAttribute('role', 'status'); document.body.appendChild(el); }
    el.textContent = msg; el.style.borderColor = ok === false ? '#ff5d5d' : '';
    el.classList.add('show');
    clearTimeout(el._t); el._t = setTimeout(function () { el.classList.remove('show'); }, 2200);
  }

  // Confirmations
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    if (form && form.dataset && form.dataset.confirm && !window.confirm(form.dataset.confirm)) { ev.preventDefault(); return; }
    var btn = form.querySelector('button[type=submit], button:not([type])');
    if (btn && !form.dataset.async) { btn.classList.add('is-loading'); btn.setAttribute('aria-busy', 'true'); }
  });

  // Sidebar on small screens
  var side = document.querySelector('.sidebar');
  document.querySelectorAll('[data-toggle-sidebar]').forEach(function (b) {
    b.addEventListener('click', function () { side.classList.toggle('open'); b.setAttribute('aria-expanded', side.classList.contains('open')); });
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && side) side.classList.remove('open'); });

  // Async toggles (favorite / save). One request at a time per job+kind; the server's answer is the truth.
  var inflight = {};
  document.querySelectorAll('form[data-async]').forEach(function (form) {
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var key = form.dataset.job + ':' + form.dataset.kind;
      if (inflight[key]) return;
      inflight[key] = true;
      var btn = form.querySelector('button');
      btn.classList.add('is-loading'); btn.disabled = true;
      fetch(form.action, { method: 'POST', body: new FormData(form), headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' }, credentials: 'same-origin' })
        .then(function (r) { if (!r.ok && r.status !== 400 && r.status !== 403) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(function (data) {
          if (data.error) { toast(data.error, false); return; }
          var job = data.job_id, kind = data.kind, on = data.on;
          document.querySelectorAll('form[data-async][data-job="' + job + '"][data-kind="' + kind + '"]').forEach(function (f) {
            var b = f.querySelector('button');
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
            b.setAttribute('aria-label', (on ? 'Remove from ' : 'Add to ') + (kind === 'favorite' ? 'favorites' : 'saved'));
            b.title = b.getAttribute('aria-label');
            // The next click must do the opposite of what just happened.
            f.action = f.action.replace(/\/(un)?(favorite|save)$/, '/' + (on ? 'un' : '') + kind);
          });
          var counterKey = kind === 'favorite' ? 'favorites' : 'saved';
          if (typeof data.count === 'number') {
            document.querySelectorAll('[data-count="' + counterKey + '"]').forEach(function (c) { c.textContent = data.count; c.classList.toggle('zero', data.count === 0); });
          }
          toast(data.message || 'Done');
        })
        .catch(function () { toast('Could not save that. Try again.', false); })
        .then(function () { inflight[key] = false; btn.classList.remove('is-loading'); btn.disabled = false; });
    });
  });

  // Notes: one save at a time, in order; a failed save never says "Saved"; leaving with unsaved text warns.
  var dirtyForms = 0;
  function setDirty(form, on) {
    if (!!form._dirty === on) return;
    form._dirty = on; dirtyForms += on ? 1 : -1;
  }
  window.addEventListener('beforeunload', function (e) {
    if (dirtyForms > 0) { e.preventDefault(); e.returnValue = ''; }
  });
  document.querySelectorAll('form[data-note-form]').forEach(function (form) {
    var ta = form.querySelector('textarea'), state = form.querySelector('.save-state'), timer, saving = false, queued = false, lastSaved = ta ? ta.value : '';
    if (!ta) return;
    function show(text, ok) { if (state) { state.textContent = text; state.classList.toggle('ok', !!ok); state.classList.toggle('bad', ok === false); } }
    function save(fromSubmit) {
      var body = ta.value.trim();
      if (!body) { if (fromSubmit) show('Write something first.', false); return; }
      if (body === lastSaved && form.dataset.noteForm === 'edit') { setDirty(form, false); show('Saved', true); return; }
      if (saving) { queued = true; return; }
      saving = true; show('Saving…');
      var sent = body;
      fetch(form.action, { method: 'POST', body: new FormData(form), headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' }, credentials: 'same-origin' })
        .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, data: data }; }); })
        .then(function (res) {
          if (!res.ok || res.data.error) { show(res.data.error || 'Not saved. Try again.', false); return; }
          lastSaved = sent;
          if (ta.value.trim() === sent) setDirty(form, false);
          show('Saved ' + (res.data.when || ''), true);
          if (res.data.note_id && form.dataset.noteForm === 'new') {
            setDirty(form, false);
            window.location.href = window.location.pathname + '?msg=' + encodeURIComponent('Note saved.') + '#notes';
          }
        })
        .catch(function () { show('Not saved. Check your connection and try again.', false); })
        .then(function () { saving = false; if (queued) { queued = false; save(false); } });
    }
    ta.addEventListener('input', function () {
      setDirty(form, ta.value.trim() !== lastSaved);
      if (form.dataset.noteForm === 'edit') { clearTimeout(timer); show('Editing…'); timer = setTimeout(function () { save(false); }, 1200); }
    });
    form.addEventListener('submit', function (ev) { ev.preventDefault(); clearTimeout(timer); save(true); });
  });
})();
