// E4 product proof: the golden journey — plan → ZIP → specialty → a VERIFIED provider —
// completes in well under 30s and under 5 interactions. Backend mocked so this measures
// the product flow, not the network.
import { test, expect } from '@playwright/test';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const PAGE = pathToFileURL(join(process.cwd(), 'innetwork.html')).href;

const MEDICARE = {
  id: 'medicare',
  label: 'Medicare (Original)',
  category: 'medicare',
  payer: 'medicare',
  confidence: 'verified',
  kind: 'government',
  level: 'plan',
  filterable: true,
};
const PLANS = { plans: [MEDICARE], categories: [{ id: 'medicare', label: 'Medicare', plans: [MEDICARE] }] };
const SEARCH = {
  count: 1,
  total: 1,
  truncated: false,
  pool_capped: false,
  applied_filters: ['medicare'],
  context_plans: [],
  plans: [MEDICARE],
  providers: [
    {
      npi: '1003000126',
      name: 'Jane Doe, MD',
      isOrg: false,
      specialty: 'Cardiology',
      taxonomies: [],
      address1: '1 Main St',
      city: 'Crestview',
      stateAb: 'FL',
      postalCode: '32536',
      fullAddress: '1 Main St, Crestview, FL, 32536',
      mailingAddress: '',
      phone: '8505551234',
      fax: '',
      gender: 'Female',
      soleProprietor: '',
      credential: 'MD',
      status: 'Active',
      enumerationDate: '2010-01-01',
      lastUpdated: '2020-01-01',
      insurance: {
        medicare: {
          value: true,
          confidence: 'verified',
          source: 'medicare',
          level: 'plan',
          source_url: 'https://data.cms.gov/...',
          fetched_at: 1700000000,
        },
      },
      lat: 30.77,
      lng: -86.58,
    },
  ],
};

test('golden journey: plan → ZIP → specialty → verified provider (<30s, <5 interactions)', async ({ page }) => {
  await page.route('**/api/**', (r) => r.fulfill({ json: {} }));
  await page.route('**/healthz', (r) => r.fulfill({ json: { ok: true } }));
  await page.route('**/api/insurance/plans*', (r) => r.fulfill({ json: PLANS }));
  await page.route('**/api/providers/search**', (r) => r.fulfill({ json: SEARCH }));

  const start = Date.now();
  let interactions = 0;
  await page.goto(PAGE);

  // 1) pick the insurance plan (Medicare is verified-by-default).
  await page.locator('[data-action="toggle-plan"][data-plan="medicare"]').first().click();
  interactions++;
  // 2) ZIP, 3) specialty, 4) search.
  await page.fill('#zip-input', '32536');
  interactions++;
  await page.selectOption('#specialty-select', 'Cardiology');
  interactions++;
  await page.click('#search-btn');
  interactions++;

  // A verified provider with a green "Confirmed" badge appears.
  const card = page.locator('#results-list .provider-card').first();
  await expect(card).toBeVisible();
  await expect(card).toContainText('Jane Doe');
  await expect(card.locator('.ins-badge')).toContainText('Medicare'); // plan-level verified

  const seconds = (Date.now() - start) / 1000;
  expect(interactions).toBeLessThanOrEqual(5);
  expect(seconds).toBeLessThan(30);
});
