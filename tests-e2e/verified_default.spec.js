// Verified tier only. The insurance filter surfaces ONLY plans InNetwork can confirm from
// a real source; the estimated tier was removed — an unverifiable catalog payer never
// appears as a filter chip, and there is no "Include estimated" mode toggle.
import { test, expect } from '@playwright/test';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const PAGE = pathToFileURL(join(process.cwd(), 'innetwork.html')).href;

const MED = {
  id: 'medicare',
  label: 'Medicare (Original)',
  category: 'medicare',
  payer: 'medicare',
  confidence: 'verified',
  kind: 'government',
};
const AETNA = {
  id: 'aetna',
  label: 'Aetna',
  category: 'commercial',
  payer: 'aetna',
  confidence: 'estimated',
  kind: 'commercial',
};
const PLANS = {
  plans: [MED, AETNA],
  categories: [
    { id: 'medicare', label: 'Medicare', plans: [MED] },
    { id: 'commercial', label: 'Commercial', plans: [AETNA] },
  ],
};

test('the filter offers only verified plans; estimated payers never appear', async ({ page }) => {
  await page.route('**/api/**', (r) => r.fulfill({ json: {} }));
  await page.route('**/healthz', (r) => r.fulfill({ json: { ok: true } }));
  await page.route('**/api/insurance/plans*', (r) => r.fulfill({ json: PLANS }));
  await page.goto(PAGE);

  // Verified Medicare is offered; the estimated Aetna is not — with no way to reveal it.
  await expect(page.locator('#insurance-filter .ins-chip[data-plan="medicare"]')).toBeVisible();
  await expect(page.locator('#insurance-filter .ins-chip[data-plan="aetna"]')).toHaveCount(0);
  await expect(page.locator('#insurance-filter .conf-dot.estimated')).toHaveCount(0);
  await expect(page.locator('#insurance-filter .ins-chip')).toHaveCount(1);
  // The estimated-tier mode toggle is gone entirely.
  await expect(page.locator('#insurance-filter [data-action="ins-mode"]')).toHaveCount(0);
});
