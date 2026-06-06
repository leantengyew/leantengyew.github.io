// FY2026 Australian Tax Constants
const FY = '2025–26';
const BRACKETS = [
  { min: 0,      max: 18200,   base: 0,     rate: 0     },
  { min: 18201,  max: 45000,   base: 0,     rate: 0.19  },
  { min: 45001,  max: 120000,  base: 5092,  rate: 0.325 },
  { min: 120001, max: 180000,  base: 29467, rate: 0.37  },
  { min: 180001, max: Infinity,base: 51667, rate: 0.45  },
];
const MEDICARE_RATE = 0.02;
const MEDICARE_SHADE_IN_THRESHOLD = 26000;  // FY26 approx
const MEDICARE_SHADE_IN_FAMILY = 43846;
const MLS_THRESHOLD = 93000;
const MLS_TIERS = [
  { threshold: 144001, rate: 0.015 },
  { threshold: 108001, rate: 0.0125 },
  { threshold: 93001,  rate: 0.01 },
];
const SUPER_CC_CAP = 30000;
const SUPER_NCC_CAP = 120000;
const SG_RATE = 0.115;
const TAX_FREE_THRESHOLD = 18200;

function calcIncomeTax(taxable) {
  if (taxable <= 0) return 0;
  for (const b of BRACKETS) {
    if (taxable <= b.max) {
      return b.base + (taxable - b.min + 1) * b.rate;
    }
  }
}

function calcLITO(taxable) {
  if (taxable <= 37500) return 700;
  if (taxable <= 45000) return Math.max(0, 700 - (taxable - 37500) * 0.05);
  if (taxable <= 66667) return Math.max(0, 325 - (taxable - 45000) * 0.015);
  return 0;
}

function calcMedicare(taxable, hasPrivateHealth, familySize) {
  let levy = 0;
  if (taxable > MEDICARE_SHADE_IN_THRESHOLD) {
    levy = taxable * MEDICARE_RATE;
  } else if (taxable > MEDICARE_SHADE_IN_THRESHOLD * 0.9) {
    levy = (taxable - MEDICARE_SHADE_IN_THRESHOLD * 0.9) * 0.1 * MEDICARE_RATE;
  }

  let mls = 0;
  if (!hasPrivateHealth && taxable > MLS_THRESHOLD) {
    for (const t of MLS_TIERS) {
      if (taxable >= t.threshold) { mls = taxable * t.rate; break; }
    }
  }

  return { levy, mls, total: levy + mls };
}

function fmtCurrency(n) {
  return '$' + Math.round(n).toLocaleString('en-AU');
}

function fmtPct(n) {
  return (n * 100).toFixed(1) + '%';
}

// State
const state = {
  salary: 0, otherIncome: 0, dividends: 0, dividendsFranked: 0,
  rentalIncome: 0, rentalExpenses: 0,
  capitalGains: 0, capitalGainsDiscount: false,
  workExpenses: 0, donations: 0, investmentExpenses: 0,
  selfEducation: 0, otherDeductions: 0,
  superBalance: 0, superCC: 0, employerSuper: 0,
  age: 0, hasPrivateHealth: false, familySize: 1,
  taxWithheld: 0,
};

// Tab switching
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(s => s.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'results') renderResults();
  });
});

// Bind all inputs
document.querySelectorAll('[data-key]').forEach(el => {
  el.addEventListener('input', () => {
    const k = el.dataset.key;
    if (el.type === 'checkbox') state[k] = el.checked;
    else state[k] = parseFloat(el.value) || 0;
  });
  el.addEventListener('change', () => {
    const k = el.dataset.key;
    if (el.type === 'checkbox') state[k] = el.checked;
    else if (el.type === 'select-one') state[k] = el.value === 'true' || el.value === 'yes';
    else state[k] = parseFloat(el.value) || 0;
  });
});

// PDF Upload
pdfjsLib.GlobalWorkerOptions.workerSrc =
  'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('pdfInput');
const preview  = document.getElementById('parsedPreview');

dropzone.addEventListener('click', () => fileInput.click());
dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('drag-over'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('drag-over'));
dropzone.addEventListener('drop', e => {
  e.preventDefault();
  dropzone.classList.remove('drag-over');
  handleFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', () => handleFile(fileInput.files[0]));

async function handleFile(file) {
  if (!file) return;
  if (file.type !== 'application/pdf') {
    alert('Please upload a PDF file.');
    return;
  }

  const reader = new FileReader();
  reader.onload = async (e) => {
    try {
      const pdf = await pdfjsLib.getDocument({ data: e.target.result }).promise;
      let fullText = '';
      for (let i = 1; i <= pdf.numPages; i++) {
        const page = await pdf.getPage(i);
        const content = await page.getTextContent();
        fullText += content.items.map(x => x.str).join(' ') + '\n';
      }
      parsePdfText(fullText);
    } catch (err) {
      alert('Could not read PDF. It may be scanned/image-based — please enter values manually.');
    }
  };
  reader.readAsArrayBuffer(file);
}

function extractNum(text, ...patterns) {
  for (const pattern of patterns) {
    const m = text.match(pattern);
    if (m) return parseFloat(m[1].replace(/,/g, '')) || 0;
  }
  return null;
}

function parsePdfText(text) {
  const found = [];
  const set = (key, val, label) => {
    if (val !== null && val > 0) {
      setField(key, val);
      found.push(`${label}: ${fmtCurrency(val)}`);
    }
  };

  set('salary',      extractNum(text, /gross\s+(?:income|salary|wages)[:\s]+\$?([\d,]+)/i, /total\s+tax\s+withheld[:\s]+\$?([\d,]+)/i, /gross\s+payment[:\s]+\$?([\d,]+)/i), 'Gross Income/Salary');
  set('taxWithheld', extractNum(text, /tax\s+withheld[:\s]+\$?([\d,]+)/i, /amount\s+withheld[:\s]+\$?([\d,]+)/i), 'Tax Withheld');
  set('dividends',   extractNum(text, /(?:total\s+)?dividends?[:\s]+\$?([\d,]+)/i, /unfranked\s+amount[:\s]+\$?([\d,]+)/i), 'Dividends');
  set('dividendsFranked', extractNum(text, /franked\s+amount[:\s]+\$?([\d,]+)/i, /franking\s+credit[:\s]+\$?([\d,]+)/i), 'Franking Credits');
  set('rentalIncome', extractNum(text, /(?:gross\s+)?rental\s+income[:\s]+\$?([\d,]+)/i), 'Rental Income');
  set('rentalExpenses', extractNum(text, /total\s+rental\s+(?:expenses?|deductions?)[:\s]+\$?([\d,]+)/i), 'Rental Expenses');

  if (found.length > 0) {
    preview.classList.add('show');
    preview.innerHTML = `<h4>Auto-detected from PDF (please verify)</h4><ul>${found.map(f => `<li>${f}</li>`).join('')}</ul>`;
  } else {
    preview.classList.add('show');
    preview.innerHTML = `<h4>PDF uploaded</h4><p>No values were auto-detected. The document may be scanned or in an unsupported format. Please enter values manually.</p>`;
  }
}

function setField(key, val) {
  const el = document.querySelector(`[data-key="${key}"]`);
  if (el) { el.value = val; state[key] = val; }
}

// Render Results
function renderResults() {
  const s = state;

  const grossIncome = s.salary + s.otherIncome + s.dividends + s.dividendsFranked
    + Math.max(0, s.rentalIncome - s.rentalExpenses)
    + (s.capitalGains * (s.capitalGainsDiscount ? 0.5 : 1));

  const totalDeductions = s.workExpenses + s.donations + s.investmentExpenses
    + s.selfEducation + s.otherDeductions;

  const taxableIncome = Math.max(0, grossIncome - totalDeductions);

  const grossTax = calcIncomeTax(taxableIncome);
  const lito     = calcLITO(taxableIncome);
  const { levy, mls } = calcMedicare(taxableIncome, s.hasPrivateHealth, s.familySize);

  const totalTax = Math.max(0, grossTax - lito + levy + mls - s.dividendsFranked);
  const refund   = s.taxWithheld - totalTax;
  const effectiveRate = taxableIncome > 0 ? totalTax / taxableIncome : 0;

  // Current bracket
  const bracket = BRACKETS.find(b => taxableIncome <= b.max) || BRACKETS[BRACKETS.length - 1];

  document.getElementById('res-taxable').textContent = fmtCurrency(taxableIncome);
  document.getElementById('res-tax').textContent     = fmtCurrency(totalTax);
  document.getElementById('res-refund').textContent  = fmtCurrency(Math.abs(refund));
  document.getElementById('res-refund').className    = 'value ' + (refund >= 0 ? 'green' : 'red');
  document.getElementById('res-refund-label').textContent = refund >= 0 ? 'Estimated Refund' : 'Amount Owing';
  document.getElementById('res-rate').textContent    = fmtPct(effectiveRate);

  // Breakdown table
  document.getElementById('breakdown').innerHTML = `
    <tr><td>Gross Income</td><td>${fmtCurrency(grossIncome)}</td></tr>
    <tr><td>Less: Deductions</td><td>−${fmtCurrency(totalDeductions)}</td></tr>
    <tr class="highlight"><td>Taxable Income</td><td>${fmtCurrency(taxableIncome)}</td></tr>
    <tr><td>Income Tax</td><td>${fmtCurrency(grossTax)}</td></tr>
    <tr><td>Less: LITO</td><td>−${fmtCurrency(lito)}</td></tr>
    <tr><td>Less: Franking Credits</td><td>−${fmtCurrency(s.dividendsFranked)}</td></tr>
    <tr><td>Medicare Levy</td><td>${fmtCurrency(levy)}</td></tr>
    ${mls > 0 ? `<tr><td>Medicare Levy Surcharge</td><td>${fmtCurrency(mls)}</td></tr>` : ''}
    <tr class="highlight"><td>Total Tax Payable</td><td>${fmtCurrency(totalTax)}</td></tr>
    <tr><td>Tax Withheld (PAYG)</td><td>${fmtCurrency(s.taxWithheld)}</td></tr>
    <tr class="highlight"><td>${refund >= 0 ? 'Refund' : 'Amount Owing'}</td><td style="color:${refund >= 0 ? '#276749' : '#c53030'}">${fmtCurrency(Math.abs(refund))}</td></tr>
  `;

  // Bracket table highlight
  document.querySelectorAll('.bracket-row').forEach(row => {
    row.classList.toggle('highlight', row.dataset.min == bracket.min);
  });

  // Advice
  const adviceEl = document.getElementById('advice-list');
  const tips = generateAdvice(s, taxableIncome, bracket, totalTax, mls);
  if (tips.length === 0) {
    adviceEl.innerHTML = `<div class="empty-state"><div class="icon">✅</div><p>You appear to be well optimised. Enter more details for tailored advice.</p></div>`;
  } else {
    adviceEl.innerHTML = tips.map(t => `
      <li class="${t.priority}">
        <div class="priority">${t.priority.toUpperCase()} PRIORITY</div>
        <div class="title">${t.title}</div>
        <div class="desc">${t.desc}</div>
        ${t.saving ? `<div class="saving">Potential saving: ${t.saving}</div>` : ''}
      </li>
    `).join('');
  }
}

function generateAdvice(s, taxable, bracket, totalTax, mls) {
  const tips = [];
  const rate = bracket.rate;

  // 1. Super concessional contributions
  const totalCC = s.employerSuper + s.superCC;
  const ccRoom  = SUPER_CC_CAP - totalCC;
  if (ccRoom > 1000 && taxable > 18200) {
    const saving = Math.min(ccRoom, taxable - 18200) * rate;
    tips.push({
      priority: 'high',
      title: 'Salary Sacrifice to Super',
      desc: `You have ${fmtCurrency(ccRoom)} remaining in your concessional (pre-tax) super cap for FY26. Salary sacrificing into super is taxed at only 15% instead of your marginal rate of ${fmtPct(rate)}. Arrange this with your employer before 30 June 2026.`,
      saving: fmtCurrency(saving),
    });
  }

  // 2. Medicare Levy Surcharge
  if (mls > 0) {
    tips.push({
      priority: 'high',
      title: 'Avoid Medicare Levy Surcharge with Private Health Cover',
      desc: `You're paying ${fmtCurrency(mls)} in Medicare Levy Surcharge because your income exceeds $93,000 and you have no private hospital cover. A basic hospital policy (~$1,200/year) can eliminate this surcharge. Compare policies at privatehealth.gov.au.`,
      saving: fmtCurrency(mls),
    });
  }

  // 3. Prepay deductible expenses
  if (taxable > 45000) {
    tips.push({
      priority: 'high',
      title: 'Prepay Deductible Expenses Before 30 June',
      desc: `Prepay up to 12 months of deductible expenses before 30 June 2026 to bring forward deductions into FY26. This includes income protection insurance premiums, investment loan interest, and subscriptions. Effective at your ${fmtPct(rate)} marginal rate.`,
      saving: null,
    });
  }

  // 4. Work from home deductions
  if (s.workExpenses < 500 && s.salary > 0) {
    tips.push({
      priority: 'medium',
      title: 'Claim Work-Related Deductions',
      desc: `The ATO's revised fixed rate method allows 67 cents/hour for working from home (covering phone, internet, electricity). Keep a log of your WFH hours. Also review: union fees, professional memberships, tools, uniforms, and work-related travel.`,
      saving: null,
    });
  }

  // 5. Charitable donations
  if (s.donations === 0) {
    tips.push({
      priority: 'low',
      title: 'Charitable Giving to DGR Organisations',
      desc: `Donations of $2 or more to Deductible Gift Recipients (DGRs) are fully tax deductible. Consider timing large donations in FY26 if you're in a higher bracket. Bunching 2 years of donations into one year can increase the tax benefit.`,
      saving: null,
    });
  }

  // 6. Capital gains timing
  if (s.capitalGains > 0 && !s.capitalGainsDiscount) {
    tips.push({
      priority: 'high',
      title: 'Hold Assets >12 Months for 50% CGT Discount',
      desc: `Assets held for more than 12 months qualify for a 50% CGT discount. If you are close to the 12-month mark on an investment, consider deferring the sale until after the anniversary date to halve your capital gain.`,
      saving: fmtCurrency(s.capitalGains * 0.5 * rate),
    });
  }

  // 7. Negative gearing
  if (s.rentalIncome > 0 && s.rentalExpenses > s.rentalIncome) {
    const loss = s.rentalExpenses - s.rentalIncome;
    tips.push({
      priority: 'low',
      title: 'Negatively Geared Property — Maximise Deductions',
      desc: `Your rental property shows a net loss of ${fmtCurrency(loss)}, which is offsetting your other income (negative gearing). Ensure you claim ALL allowable expenses: depreciation, borrowing expenses, rates, repairs, agent fees, and interest. Consider a Quantity Surveyor report to maximise depreciation.`,
      saving: fmtCurrency(loss * rate),
    });
  }

  // 8. Catch-up super contributions
  if (s.superBalance < 500000 && ccRoom > 0 && s.age > 0) {
    tips.push({
      priority: 'medium',
      title: 'Catch-Up Concessional Super Contributions',
      desc: `If your total super balance is below $500,000, you can carry forward unused concessional cap amounts from FY19 onwards. Check your myGov account for unused cap history and make a personal deductible contribution via your fund.`,
      saving: null,
    });
  }

  // 9. Spouse super splitting / co-contribution
  if (s.salary > 0 && taxable < 43445) {
    tips.push({
      priority: 'medium',
      title: 'Government Super Co-Contribution',
      desc: `If your total income is below $43,445 and you make a personal (after-tax) super contribution, the government will match it up to $500. For every $1 you contribute, the government adds up to 50 cents. Maximum co-contribution of $500 applies when income is below $28,445.`,
      saving: '$500',
    });
  }

  // 10. Income protection
  if (s.salary > 60000 && s.otherDeductions < 1000) {
    tips.push({
      priority: 'medium',
      title: 'Income Protection Insurance',
      desc: `Premiums for income protection insurance (covering loss of income due to illness or injury) are fully tax deductible when held outside super. At your marginal rate of ${fmtPct(rate)}, this effectively reduces the after-tax cost significantly.`,
      saving: null,
    });
  }

  return tips;
}

// CSV Upload
const csvInput = document.getElementById('csvInput');
csvInput.addEventListener('change', () => {
  const file = csvInput.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (e) => {
    const lines = e.target.result.split('\n').filter(l => l.trim());
    let income = 0, deductions = 0;
    lines.slice(1).forEach(line => {
      const parts = line.split(',');
      const type = (parts[0] || '').toLowerCase();
      const amount = parseFloat((parts[1] || '').replace(/[^0-9.-]/g, '')) || 0;
      if (type.includes('income') || type.includes('salary')) income += amount;
      if (type.includes('deduct') || type.includes('expense')) deductions += amount;
    });
    if (income > 0) setField('salary', income);
    if (deductions > 0) setField('workExpenses', deductions);
    alert(`CSV imported: Income ${fmtCurrency(income)}, Deductions ${fmtCurrency(deductions)}`);
  };
  reader.readAsText(file);
});
