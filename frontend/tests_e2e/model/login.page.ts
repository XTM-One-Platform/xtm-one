import type { Page } from '@playwright/test';

export class LoginPage {
  constructor(private readonly page: Page) {}

  async goto(): Promise<void> {
    await this.page.goto('/login');
    await this.page.getByPlaceholder('user@mail.com').waitFor({ state: 'visible' });
  }

  async login(
    email = process.env.E2E_USER_EMAIL,
    password = process.env.E2E_USER_PASSWORD,
  ): Promise<void> {
    await this.submit(email, password);
    await this.acceptTermsOfUseIfShown();
    await this.page.getByRole('link', { name: 'Home', exact: true }).waitFor({ state: 'visible' });
  }

  /** Fill in and send the sign-in form, without waiting for what it opens. */
  async submit(
    email = process.env.E2E_USER_EMAIL,
    password = process.env.E2E_USER_PASSWORD,
  ): Promise<void> {
    if (!email || !password) {
      throw new Error(
        'E2E_USER_EMAIL and E2E_USER_PASSWORD must be set. ' +
          'Copy frontend/.env.e2e.example to frontend/.env.e2e and fill in your credentials.',
      );
    }
    await this.page.getByPlaceholder('user@mail.com').fill(email);
    await this.page.getByPlaceholder('••••••••').fill(password);
    await this.page.getByRole('button', { name: 'Sign In' }).click();
  }

  /**
   * Clear the Terms of Use consent gate if it is showing.
   *
   * The gate replaces the entire authenticated app until the user accepts, so
   * every spec's app shell is unreachable until this runs. It is active
   * whenever the deployment has an Agentic AI licence — which CI does — and
   * absent on Community Edition or with `TERMS_OF_USE_ENABLED=false`, hence
   * the tolerant probe rather than an unconditional wait.
   *
   * Also a no-op on a re-run against a database where this account already
   * accepted the current `TERMS_OF_USE_VERSION`; bumping that version makes
   * the gate reappear and this clears it again.
   *
   * `TermsGate` renders a skeleton until `/auth/me` answers, then either the
   * consent screen or the app shell - never both - so whichever appears first
   * is the answer. The terms are accepted once for the whole platform, so
   * most sign-ins never see the gate and must not wait out its timeout.
   */
  private async acceptTermsOfUseIfShown(): Promise<void> {
    const accept = this.page.getByRole('button', { name: 'Accept and continue' });
    const appShell = this.page.getByRole('link', { name: 'Home', exact: true });
    try {
      await accept.or(appShell).first().waitFor({ state: 'visible', timeout: 15_000 });
    } catch {
      return; // Neither yet: the caller's own wait for the shell reports it.
    }
    if (!(await accept.isVisible())) return; // Gate not shown — nothing to accept.
    // The button stays disabled until consent is explicitly given, so this
    // order matters: toggling first is what makes the click land.
    //
    // The consent control is the shared `Switch` (Radix), which exposes
    // `role="switch"` — NOT `role="checkbox"` — and renders a `<button>`, so
    // Playwright's `.check()` does not apply to it. `click()` is the toggle.
    await this.page.getByRole('switch').click();
    await accept.click();
  }

  async logout(): Promise<void> {
    await this.page.getByTestId('user-menu-trigger').click();
    await this.page.getByTestId('user-menu-sign-out').click();
    await this.page.waitForURL('**/login**');
  }
}
