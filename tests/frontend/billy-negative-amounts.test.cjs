const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const { test } = require('node:test')
const { JSDOM } = require('jsdom')

const frontend = path.join(__dirname, '../../custom_components/bill_tracker/frontend')
function source(name) {
  return fs.readFileSync(path.join(frontend, name), 'utf8')
    .replace(/^import '[^']+'\n/gm, '')
    .replace(/^import\s*\{[\s\S]*?\}\s*from\s*'[^']+'\s*/gm, '')
    .replace(/^export /gm, '')
}

function fixture(t, language = 'en') {
  const dom = new JSDOM('<!doctype html><body></body>', {
    runScripts: 'outside-only', url: 'https://ha.example/billy?view=bills',
  })
  t.after(() => dom.window.close())
  dom.window.console.info = () => {}
  dom.window.confirm = () => true
  dom.window.eval(source('billy-extra-i18n.js') + '\n' + source('billy-panel.js'))
  const calls = []
  const hass = {
    language, user: { id: 'test-user', is_admin: true }, config: { currency: 'EUR' },
    async callWS(message) { calls.push(message); return {} },
  }
  const category = { id: 'power', name: 'Power', enabled: true, color: '#03a9f4', interval_months: 1 }
  const bill = (id, amount) => ({
    id, category_id: 'power', category: 'Power', amount, house_id: 'home',
    paid_year: 2026, paid_month: 10, year: 2026, month: 10,
    period_start_year: 2026, period_start_month: 10, period_end_year: 2026, period_end_month: 10,
    due_date: null, payment_date: null, paid: false, reimbursement_status: 'none',
    created_at: '2026-10-01', split: [],
  })
  const panel = dom.window.document.createElement('billy-bills')
  panel._data = {
    categories: [category], payers: [], pending_imports: [],
    houses: [{ id: 'home', name: 'Home' }], default_house_id: 'home',
    expenses: [bill('regular', 80), bill('credit', -25.5)],
  }
  panel.hass = hass
  panel._render()
  const flush = () => new Promise(resolve => setImmediate(resolve))
  return { dom, panel, calls, flush }
}

test('credit bills are listed with their minus sign and a credit style', t => {
  const f = fixture(t)
  const amounts = [...f.panel.shadowRoot.querySelectorAll('.bill-amount')]
  const credit = amounts.find(node => node.textContent.includes('25.50'))
  const regular = amounts.find(node => node.textContent.includes('80.00'))
  assert.match(credit.textContent, /[-−]/)
  assert.ok(credit.classList.contains('credit'))
  assert.ok(!regular.classList.contains('credit'))
})

test('the bill form allows negative amounts and explains them', t => {
  const f = fixture(t)
  f.panel._openBill()
  const input = f.panel.shadowRoot.querySelector('#bill-form input[name="amount"]')
  assert.equal(input.getAttribute('min'), null)
  assert.equal(input.closest('label').querySelector('.field-help').textContent,
    'Use a negative amount for a credit note or refund.')
})

test('a negative amount is sent to the backend', async t => {
  const f = fixture(t)
  f.panel._openBill()
  const form = f.panel.shadowRoot.querySelector('#bill-form')
  form.querySelector('input[name="amount"]').value = '-25.5'
  form.dispatchEvent(new f.dom.window.Event('submit', { cancelable: true }))
  await f.flush()
  const add = f.calls.find(call => call.type === 'bill_tracker/add')
  assert.ok(add, 'bill_tracker/add was called')
  assert.equal(add.amount, -25.5)
})

for (const language of ['it', 'es', 'fr', 'de', 'pt']) {
  test(`the amount hint is translated (${language})`, t => {
    const f = fixture(t, language)
    f.panel._openBill()
    const help = f.panel.shadowRoot.querySelector('#bill-form input[name="amount"]')
      .closest('label').querySelector('.field-help').textContent
    assert.ok(help && help !== 'amountHelp')
    assert.notEqual(help, 'Use a negative amount for a credit note or refund.')
  })
}

function dashboardWithDebt(t, items, amount) {
  const dom = new JSDOM('<!doctype html><body></body>', { runScripts: 'outside-only', url: 'https://ha.example/billy' })
  t.after(() => dom.window.close())
  dom.window.console.info = () => {}
  dom.window.confirm = () => true
  dom.window.eval(source('billy-extra-i18n.js') + '\n' + source('billy-panel.js'))
  const calls = []
  const dashboard = dom.window.document.createElement('billy-dashboard')
  dashboard._hass = { language: 'en', user: { is_admin: true }, config: { currency: 'EUR' },
    async callWS(message) { calls.push(message); return {} } }
  dashboard._data = {
    currency: 'EUR', payers: [{ id: 'b', name: 'Orsi' }, { id: 'a', name: 'Sanya' }],
    debts: [{ from_payer_id: 'b', from_name: 'Orsi', to_payer_id: 'a', to_name: 'Sanya', amount, items }],
  }
  dashboard.shadowRoot.innerHTML = '<div class="modal" id="reimbursement-modal" hidden><div class="modal-backdrop"></div><div class="modal-card" id="reimbursement-modal-card"></div></div>'
  dashboard._openReimbursementDetails({ dataset: { from: 'b', to: 'a' } })
  const card = dashboard.shadowRoot.getElementById('reimbursement-modal-card')
  return { dom, dashboard, card, calls }
}

const DEBT_ITEMS = [
  { kind: 'expense', id: 'bill', label: 'Electricity', amount: 11045 },
  { kind: 'expense', id: 'internet', label: 'Telekom', amount: 4942.5 },
  { kind: 'expense', id: 'credit', label: 'Electricity', amount: -9122 },
]

test('reimbursement details list credits as negative lines and add up to the debt', t => {
  const f = dashboardWithDebt(t, DEBT_ITEMS, 6865.5)
  const amounts = [...f.card.querySelectorAll('.reimbursement-detail-row b')].map(node => node.textContent)
  assert.equal(amounts.length, 3)
  assert.match(amounts[2], /[-−]/)
  assert.match(f.card.querySelector('#reimbursement-selected-total').textContent, /6,865\.50/)
  assert.equal(f.card.querySelector('#reimbursement-confirm-selected').disabled, false)
})

test('a selection that is zero, negative or above the debt cannot be confirmed', async t => {
  const f = dashboardWithDebt(t, DEBT_ITEMS, 6865.5)
  const boxes = [...f.card.querySelectorAll('[data-reimbursement-item]')]
  const confirm = f.card.querySelector('#reimbursement-confirm-selected')
  const toggle = (index, checked) => {
    boxes[index].checked = checked
    boxes[index].dispatchEvent(new f.dom.window.Event('change'))
  }
  toggle(0, false); toggle(1, false)          // only the credit left: negative
  assert.equal(confirm.disabled, true)
  toggle(0, true); toggle(2, false)           // 11045 > 6865.5 owed
  assert.equal(confirm.disabled, true)
  toggle(0, false); toggle(1, true)           // 4942.5 is fine
  assert.equal(confirm.disabled, false)
  confirm.click()
  await new Promise(resolve => setImmediate(resolve))
  const sent = f.calls.find(call => call.type === 'bill_tracker/settlement/add')
  assert.equal(sent.amount, 4942.5)
  assert.equal(JSON.stringify(sent.line_items), JSON.stringify([{ kind: 'expense', id: 'internet' }]))
})

test('a manually completed credit is shown as paid back by the payer', t => {
  const dom = new JSDOM('<!doctype html><body></body>', { runScripts: 'outside-only', url: 'https://ha.example/billy' })
  t.after(() => dom.window.close())
  dom.window.eval(source('billy-reimbursement-history.js'))
  const history = dom.window.document.createElement('billy-reimbursement-history')
  history._hass = { language: 'en', config: { currency: 'EUR' } }
  history._data = {
    payers: [{ id: 'a', name: 'Sanya' }, { id: 'b', name: 'Orsi' }],
    expenses: [{ id: 'credit', amount: -100, payer_id: 'a', payer: 'Sanya', reimbursement_manual_done: true,
      split: [{ payer_id: 'a', percentage: 50, name: 'Sanya' }, { payer_id: 'b', percentage: 50, name: 'Orsi' }] }],
    recurring_occurrences: [], settlements: [],
  }
  const [row] = history._manualRows()
  assert.equal(row.from_payer_id, 'a')
  assert.equal(row.to_payer_id, 'b')
  assert.equal(row.amount, 50)
})
