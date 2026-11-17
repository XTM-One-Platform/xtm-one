import { test as setup } from '@playwright/test';
import { LoginPage } from './model/login.page';
import { ok, withAdmin } from './helpers';

const authFile = 'tests_e2e/.auth/user.json';

setup('authenticate', async ({ page }) => {
  // The first-login organization setup proposal (#9173) is a modal dialog
  // over the app shell for the first administrator of a fresh XTM One
  // database: answer it once over the API so no spec signs in behind it.
  // Every /api/v1 call but the terms routes answers 403 until the Terms of Use
  // are accepted, so they are accepted here first (the login page then sees
  // no gate, as on any re-run).
  await withAdmin(async (apiCtx, headers) => {
    const terms = await (await ok(await apiCtx.get('/api/v1/auth/terms', { headers }), 'reading the Terms of Use state')).json();
    if (terms.required && !terms.accepted) {
      await ok(
        await apiCtx.post('/api/v1/auth/terms/accept', { headers, data: { version: terms.version } }),
        `accepting Terms of Use ${terms.version}`,
      );
    }
    await ok(
      await apiCtx.post('/api/v1/organization-context/setup-prompt', { headers, data: { outcome: 'dismissed' } }),
      'answering the organization setup proposal',
    );
  });
  const loginPage = new LoginPage(page);
  await loginPage.goto();
  await loginPage.login();
  await page.context().storageState({ path: authFile });
});
