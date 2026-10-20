document.addEventListener('DOMContentLoaded', () => {
  document.querySelector('#add-url')?.addEventListener('click', () => {
    const fields = document.querySelector('#url-fields');
    if (fields.querySelectorAll('input').length >= 20) return;
    const row = document.createElement('div');
    row.className = 'url-row';
    row.innerHTML = '<label class="grow">Product URL<input type="url" name="urls" placeholder="https://…" maxlength="2048"></label><button type="button" class="text-button" aria-label="Remove URL">×</button>';
    row.querySelector('button').addEventListener('click', () => row.remove());
    fields.append(row);
    row.querySelector('input').focus();
  });
  document.querySelectorAll('form[data-busy]').forEach(form => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button[type="submit"]');
      button.disabled = true;
      button.textContent = form.dataset.busy || 'Starting discovery…';
    });
  });
});
// Delegated so chips inside HTMX-swapped content work too.
document.addEventListener('click', e => {
  const chip = e.target.closest('[data-fill]');
  if (!chip) return;
  chip.closest('form').querySelector('input[name="price"]').value = chip.dataset.fill;
});
// Destructive forms ask first; without JavaScript they submit directly.
document.addEventListener('submit', event => {
  const message = event.target.dataset?.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});
document.addEventListener('htmx:responseError', event => {
  const target = event.detail.target;
  if (target) target.textContent = 'Request failed. Reload the page and try again.';
});
// Filters left blank stay out of the request, so the pushed URL reads like the page's own links.
document.addEventListener('htmx:configRequest', event => {
  const data = event.detail.formData;
  if (event.detail.elt.id !== 'outlet-filters' || !data) return;
  [...new Set(data.keys())].filter(key => data.getAll(key).every(value => value === '')).forEach(key => data.delete(key));
});
// The Filters dropdown closes on a click elsewhere or Escape, like a menu.
document.addEventListener('click', event => {
  document.querySelectorAll('details.filters[open]').forEach(menu => { if (!menu.contains(event.target)) menu.open = false; });
});
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') document.querySelectorAll('details.filters[open]').forEach(menu => { menu.open = false; });
});
// Name alerts field: comma adds like Enter, Backspace on an empty field removes the last tag,
// and a link to #alerts opens the panel with the cursor in the field.
document.addEventListener('keydown', event => {
  const input = event.target.closest?.('.tag-field input');
  if (!input) return;
  if (event.key === ',' ) {
    event.preventDefault();
    if (input.value.trim()) input.form.requestSubmit();
  } else if (event.key === 'Backspace' && !input.value) {
    const chips = input.closest('.tag-field').querySelectorAll('.tag-chip button');
    if (chips.length) chips[chips.length - 1].click();
  }
});
document.addEventListener('click', event => {
  const field = event.target.closest('.tag-field');
  if (field && !event.target.closest('a, button, input')) field.querySelector('input').focus();
});
document.addEventListener('DOMContentLoaded', () => {
  const target = location.hash && document.querySelector(`details${location.hash}`);
  if (!target) return;
  target.open = true;
  // After the browser has settled on the #fragment, which would otherwise keep the focus.
  requestAnimationFrame(() => target.querySelector('.tag-field input')?.focus());
});
