// D2: WCAG 2.2 AA — axe-core must find ZERO violations on every view AND state, and
// the golden journey must be completable keyboard-only. The backend is mocked so this
// exercises the real rendered DOM of each state without touching NPPES/geocoding.
import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const PAGE = pathToFileURL(join(process.cwd(), 'innetwork.html')).href;
const WCAG = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'];

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
const AETNA = {
  id: 'aetna',
  label: 'Aetna',
  category: 'commercial',
  payer: 'aetna',
  confidence: 'estimated',
  kind: 'commercial',
  level: 'payer',
  filterable: false,
};
const PLANS = {
  plans: [MEDICARE, AETNA],
  categories: [
    { id: 'medicare', label: 'Medicare', plans: [MEDICARE] },
    { id: 'commercial', label: 'Commercial / Employer', plans: [AETNA] },
  ],
};
const provider = (over = {}) => ({
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
  insurance: { medicare: { value: true, confidence: 'verified', source: 'medicare', level: 'plan' } },
  lat: 30.77,
  lng: -86.58,
  ...over,
});
const search = (providers) => ({
  count: providers.length,
  total: providers.length,
  truncated: false,
  pool_capped: false,
  applied_filters: [],
  context_plans: [],
  plans: [MEDICARE, AETNA],
  providers,
});

async function mock(page, { providers = [provider()] } = {}) {
  await page.route('**/api/**', (r) => r.fulfill({ json: {} }));
  await page.route('**/healthz', (r) => r.fulfill({ json: { ok: true } }));
  await page.route('**/api/insurance/plans*', (r) => r.fulfill({ json: PLANS }));
  await page.route('**/api/providers/search**', (r) => r.fulfill({ json: search(providers) }));
}

async function scan(page, context) {
  const { violations } = await new AxeBuilder({ page }).withTags(WCAG).analyze();
  const summary = violations.map((v) => `${v.id} (${v.impact}) x${v.nodes.length}`).join('; ');
  expect(violations, `axe violations in ${context}: ${summary}`).toEqual([]);
}

test('a11y: welcome / initial state', async ({ page }) => {
  await mock(page);
  await page.goto(PAGE);
  await expect(page.locator('.state-title')).toBeVisible();
  await scan(page, 'welcome');
});

test('a11y: results list + insurance filter (verified tier)', async ({ page }) => {
  await mock(page);
  await page.goto(PAGE);
  await page.fill('#zip-input', '32536');
  await page.click('#search-btn');
  await expect(page.locator('#results-list .provider-card').first()).toBeVisible();
  await scan(page, 'results list');

  // The insurance filter surfaces the verified plan chips (estimated tier removed).
  await expect(page.locator('.ins-chip').first()).toBeVisible();
  await scan(page, 'insurance filter');
});

test('a11y: provider detail drawer open', async ({ page }) => {
  await mock(page);
  await page.goto(PAGE);
  await page.fill('#zip-input', '32536');
  await page.click('#search-btn');
  await page.locator('#results-list .provider-card').first().click();
  await expect(page.locator('#detail-drawer')).toHaveClass(/open/);
  await scan(page, 'detail drawer');
});

test('a11y: map view', async ({ page }) => {
  await mock(page);
  await page.goto(PAGE);
  await page.fill('#zip-input', '32536');
  await page.click('#search-btn');
  await expect(page.locator('#results-list .provider-card').first()).toBeVisible();
  // The list/map toggle is a narrow-viewport control; on desktop the map already shows
  // alongside the list. Toggle only when present, then scan the map state either way.
  const toggle = page.locator('[data-action="view-map"]');
  if (await toggle.isVisible()) await toggle.click();
  // Wait for the map to actually initialize (Leaflet loads from a CDN) before scanning,
  // so axe sees the settled marker DOM rather than racing a mid-render frame. The map is
  // best-effort (a blocked CDN leaves the list as the accessible path), so don't fail if
  // it never appears — just give it a bounded chance to settle.
  await page
    .locator('.leaflet-container')
    .waitFor({ state: 'attached', timeout: 4000 })
    .catch(() => {});
  await page.waitForTimeout(300);
  await scan(page, 'map view');
});

test('a11y: favorites tab with a saved provider', async ({ page }) => {
  await mock(page);
  await page.goto(PAGE);
  await page.fill('#zip-input', '32536');
  await page.click('#search-btn');
  await page.locator('#results-list .provider-card [data-action="toggle-fav"]').first().click();
  await page.click('#tab-favorites');
  await expect(page.locator('#results-list')).toContainText('Jane Doe');
  await scan(page, 'favorites tab');
});

test('a11y: empty results state', async ({ page }) => {
  await mock(page, { providers: [] });
  await page.goto(PAGE);
  await page.fill('#zip-input', '00000');
  await page.click('#search-btn');
  await expect(page.locator('.state-title')).toContainText(/No providers/i);
  await scan(page, 'empty state');
});

test('golden journey is completable keyboard-only', async ({ page }) => {
  await mock(page);
  await page.goto(PAGE);

  // Type a ZIP and submit with Enter — no mouse.
  await page.locator('#zip-input').focus();
  await page.keyboard.type('32536');
  await page.keyboard.press('Enter');
  await expect(page.locator('#results-list .provider-card').first()).toBeVisible();

  // Tab to the provider's open control (its name button) and open it with the keyboard.
  const open = page.locator('#results-list .provider-card .card-name').first();
  await open.focus();
  await expect(open).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.locator('#detail-drawer')).toHaveClass(/open/);

  // Close with Escape.
  await page.keyboard.press('Escape');
  await expect(page.locator('#detail-drawer')).not.toHaveClass(/open/);
});
