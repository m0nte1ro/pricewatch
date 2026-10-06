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
      button.textContent = 'Starting discovery…';
    });
  });
});
document.addEventListener('htmx:responseError', event => {
  const target = event.detail.target;
  if (target) target.textContent = 'Request failed. Reload the page and try again.';
});
