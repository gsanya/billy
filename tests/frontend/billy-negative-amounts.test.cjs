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
