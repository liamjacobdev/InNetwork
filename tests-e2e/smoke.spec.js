// End-to-end smoke (T2.3): search -> results render -> open detail drawer -> save
// favorite -> switch to Saved tab -> export CSV. The backend is fully mocked, so this
// exercises the real frontend wiring without touching NPPES/geocoding.
import { test, expect } from '@playwright/test';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const PAGE = pathToFileURL(join(process.cwd(), 'innetwork.html')).href;

const MEDICARE_PLAN = {
  id: 'medicare',
  label: 'Medicare (Original)',
  category: 'medicare',
  payer: 'medicare',
  confidence: 'verified',
  kind: 'government',
};
const PLANS = { plans: [MEDICARE_PLAN], categories: [{ id: 'medicare', label: 'Medicare', plans: [MEDICARE_PLAN] }] };
const SEARCH = {
  count: 1,
  total: 1,
  truncated: false,
  pool_capped: false,
  plans: [MEDICARE_PLAN],
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
      insurance: { medicare: { value: true, confidence: 'verified', source: 'medicare' } },
      lat: 30.77,
      lng: -86.58,
    },
  ],
};

test('search → drawer → save → Saved tab → export CSV', async ({ page }) => {
  // Catch-all first so the specific routes (registered after) take precedence.
  await page.route('**/api/**', (r) => r.fulfill({ json: {} }));
  await page.route('**/healthz', (r) => r.fulfill({ json: { ok: true } })); // backend "reachable"
  await page.route('**/api/insurance/plans*', (r) => r.fulfill({ json: PLANS }));
  await page.route('**/api/providers/search**', (r) => r.fulfill({ json: SEARCH }));

  await page.goto(PAGE);

  // Search
  await page.fill('#zip-input', '32536');
  await page.click('#search-btn');

  // Results render
  const card = page.locator('#results-list .provider-card').first();
  await expect(card).toBeVisible();
  await expect(card).toContainText('Jane Doe');

  // Open the detail drawer
  await card.click();
  await expect(page.locator('#detail-drawer')).toHaveClass(/open/);

  // Save the favorite from the drawer, then close it (the scrim overlays the tabs)
  await page.locator('#detail-drawer [data-action="toggle-fav"]').first().click();
  await page.keyboard.press('Escape');
  await expect(page.locator('#detail-drawer')).not.toHaveClass(/open/);

  // Switch to the Saved tab — the favorite is listed there
  await page.click('#tab-favorites');
  await expect(page.locator('#results-list')).toContainText('Jane Doe');

  // Export CSV fires a download
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.locator('#export-wrap [data-action="export-csv"]').click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/\.csv$/);
});
