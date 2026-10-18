// Small progressive enhancements only; everything works without JavaScript.
document.addEventListener('submit', function (ev) {
  var form = ev.target;
  if (form && form.dataset && form.dataset.confirm) {
    if (!window.confirm(form.dataset.confirm)) { ev.preventDefault(); }
  }
});
