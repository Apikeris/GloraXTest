document.querySelectorAll('[name="project_id"]').forEach((radio) =>
  radio.addEventListener('change', () => {
    document.getElementById('expected-count').value = radio.dataset.count
    document.getElementById('selection-info').textContent =
      `В тесте ${radio.dataset.count} вопросов · 20 секунд на каждый`
    document.getElementById('start-btn').disabled = false
  }),
)
