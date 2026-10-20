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
  // Alerts & pages sit beside the list on wide screens; on narrow ones they fold away.
  const manage = document.querySelector('details.manage');
  if (manage && window.matchMedia('(max-width: 1150px)').matches) manage.open = false;
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
