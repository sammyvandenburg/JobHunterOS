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

  // Async toggles (favorite / save) — update every control for the same job on the page.
  document.querySelectorAll('form[data-async]').forEach(function (form) {
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var btn = form.querySelector('button');
      btn.classList.add('is-loading');
      fetch(form.action, { method: 'POST', body: new FormData(form), headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' }, credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          btn.classList.remove('is-loading');
          if (data.error) { toast(data.error, false); return; }
          var job = data.job_id, kind = data.kind, on = data.on;
          document.querySelectorAll('form[data-async][data-job="' + job + '"][data-kind="' + kind + '"]').forEach(function (f) {
            var b = f.querySelector('button');
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
            b.setAttribute('aria-label', (on ? 'Remove from ' : 'Add to ') + (kind === 'favorite' ? 'favorites' : 'saved'));
            b.title = b.getAttribute('aria-label');
            f.action = f.action.replace(/\/(un)?(favorite|save)$/, '/' + (on ? '' : 'un') + kind);
          });
          var key = kind === 'favorite' ? 'favorites' : 'saved';
          if (typeof data.count === 'number') {
            document.querySelectorAll('[data-count="' + key + '"]').forEach(function (c) { c.textContent = data.count; c.classList.toggle('zero', data.count === 0); });
          }
          toast(data.message || 'Done');
        })
        .catch(function () { btn.classList.remove('is-loading'); toast('Could not save that. Try again.', false); });
    });
  });

  // Notes: save state feedback + autosave of edits after a pause.
  document.querySelectorAll('form[data-note-form]').forEach(function (form) {
    var ta = form.querySelector('textarea'), state = form.querySelector('.save-state'), timer;
    if (!ta) return;
    function save(silent) {
      var body = ta.value.trim();
      if (!body) return;
      if (state) { state.textContent = 'Saving…'; state.classList.remove('ok'); }
      fetch(form.action, { method: 'POST', body: new FormData(form), headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' }, credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.error) { if (state) state.textContent = data.error; return; }
          if (state) { state.textContent = 'Saved ' + (data.when || ''); state.classList.add('ok'); }
          if (data.note_id && form.dataset.noteForm === 'new') {
            // A new note became a real note: reload so it appears in the list with its own editor.
            window.location.href = window.location.pathname + '?msg=' + encodeURIComponent('Note saved.') + '#notes';
          }
        })
        .catch(function () { if (state) state.textContent = 'Not saved. Check your connection.'; });
    }
    if (form.dataset.noteForm === 'edit') {
      ta.addEventListener('input', function () { clearTimeout(timer); if (state) state.textContent = 'Editing…'; timer = setTimeout(function () { save(true); }, 1200); });
    }
    form.addEventListener('submit', function (ev) { ev.preventDefault(); clearTimeout(timer); save(false); });
  });
})();
