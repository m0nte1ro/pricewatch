document.addEventListener('DOMContentLoaded', () => {
  const canvas = document.querySelector('#price-chart');
  if (!canvas) return;
  const message = document.querySelector('#chart-message');
  const seriesSelect = document.querySelector('#series-select');
  const rangeSelect = document.querySelector('#range-select');
  const colors = ['#a2eac8', '#a6afff', '#edb986', '#e794ab', '#79c7e5', '#ddd17d'];
  let chart;
  let series = [];
  let controller;
  function draw() {
    chart?.destroy();
    const selected = series.filter((s, i) => seriesSelect.value === 'all' || String(i) === seriesSelect.value);
    message.textContent = selected.some(s => s.data.length) ? 'Each seller and condition has its own series. Click a legend entry to hide it.' : 'No observations in this range yet.';
    chart = new Chart(canvas, {
      type: 'line',
      data: {datasets: selected.map((s, i) => ({
        label: `${s.label} (${s.currency})`,
        data: s.data.map(p => ({x: new Date(p.x).getTime(), y: p.y})),
        borderColor: colors[i % colors.length], backgroundColor: colors[i % colors.length],
        borderWidth: 2, pointRadius: s.data.length < 20 ? 3 : 0, tension: 0, spanGaps: false,
      }))},
      options: {animation: false, responsive: true, maintainAspectRatio: false, parsing: false,
        interaction: {mode: 'nearest', intersect: false},
        plugins: {legend: {labels: {color: '#aeb6c4', usePointStyle: true, boxWidth: 8}},
          tooltip: {callbacks: {title: items => new Date(items[0].parsed.x).toLocaleString()}}},
        scales: {x: {type: 'linear', grid: {color: '#252c36'}, ticks: {color: '#8994a5', maxTicksLimit: 7, callback: v => new Date(v).toLocaleDateString(undefined, {month: 'short', day: 'numeric'})}},
          y: {grid: {color: '#252c36'}, ticks: {color: '#8994a5'}, title: {display: true, text: 'Price · currency in legend', color: '#8994a5'}}},
      },
    });
  }
  async function load() {
    controller?.abort();
    controller = new AbortController();
    try {
      const response = await fetch(`${canvas.dataset.url}?days=${rangeSelect.value}`, {signal: controller.signal});
      if (!response.ok) throw new Error('History request failed');
      series = (await response.json()).series;
      const previous = seriesSelect.value;
      seriesSelect.replaceChildren(new Option('All listings', 'all'));
      series.forEach((s, i) => seriesSelect.add(new Option(s.label, String(i))));
      seriesSelect.value = previous;
      if (!seriesSelect.value) seriesSelect.value = 'all';
      draw();
    } catch (error) {
      if (error.name !== 'AbortError') message.textContent = 'Could not load the chart. Refresh to try again.';
    }
  }
  rangeSelect.addEventListener('change', load);
  seriesSelect.addEventListener('change', draw);
  load();
});
