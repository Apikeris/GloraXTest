'use strict'
const root = document.getElementById('quiz')
const base = `/api/attempts/${root.dataset.attemptId}`
const statusEl = document.getElementById('connection-status')
const retry = document.getElementById('retry')
const options = document.getElementById('options')
let endAt = 0,
  busy = false,
  pending = null,
  timer = null
const csrf = document.querySelector('meta[name="csrf-token"]').content

function disable() {
  options.querySelectorAll('button').forEach((button) => (button.disabled = true))
}

function problem() {
  statusEl.textContent = 'Нет ответа от сервера.'
  retry.hidden = false
  disable()
  busy = false
}

async function load() {
  if (busy) return
  busy = true
  retry.hidden = true
  const sent = performance.now()
  try {
    const response = await fetch(`${base}/current`, { cache: 'no-store' })
    if (!response.ok) throw new Error()
    const question = await response.json()
    if (question.status !== 'in_progress') {
      location.assign(question.result_url)
      return
    }
    endAt =
      performance.now() +
      Math.max(0, Date.parse(question.deadline) - Date.parse(question.server_now)) -
      (performance.now() - sent)
    document.getElementById('question-text').textContent = question.text
    document.getElementById('progress-text').textContent =
      `Вопрос ${question.number} из ${question.total}`
    document.getElementById('progress').value = question.number - 1
    options.replaceChildren()
    question.options.forEach((option) => {
      const button = document.createElement('button')
      button.className = 'option-btn'
      button.textContent = option.text
      button.addEventListener('click', () => submit(question.id, option.id))
      options.appendChild(button)
    })
    statusEl.textContent = 'Выберите один ответ'
    busy = false
    clearInterval(timer)
    timer = setInterval(tick, 100)
    tick()
  } catch {
    problem()
  }
}

async function submit(questionId, optionId) {
  if (busy) return
  busy = true
  disable()
  pending = { question_id: questionId, option_id: optionId }
  statusEl.textContent = 'Сохраняем ответ…'
  try {
    const response = await fetch(`${base}/answer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
      body: JSON.stringify(pending),
    })
    const data = await response.json()
    if (!response.ok && !data.expired) throw new Error()
    pending = null
    statusEl.textContent = data.accepted ? 'Ответ сохранён' : 'Время истекло'
    busy = false
    await load()
  } catch {
    problem()
  }
}

function tick() {
  const left = Math.max(0, Math.ceil((endAt - performance.now()) / 1000))
  document.getElementById('timer').textContent = `${left} с`
  if (left === 0 && !busy) {
    clearInterval(timer)
    disable()
    if (!pending) load()
  }
}
retry.addEventListener('click', () => {
  if (pending) submit(pending.question_id, pending.option_id)
  else load()
})
load()
