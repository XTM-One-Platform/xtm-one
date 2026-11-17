/**
 * Hardcoded E2E test credentials and config.
 * Override via environment variables in CI or local .env.e2e.
 */

export const BASE_URL = process.env.E2E_BASE_URL ?? 'http://localhost:3100';

export const ADMIN_EMAIL = process.env.E2E_USER_EMAIL;
export const ADMIN_PASSWORD = process.env.E2E_USER_PASSWORD ;

/**
 * The address of the non-admin user a spec creates for itself and deletes in
 * its `afterAll`. One per spec and per worker slot (`testInfo.parallelIndex`,
 * unique among the workers running at once in every project of the run and
 * reused by a replacement worker, so a leftover from an aborted run is still
 * cleared): each spec clears the address before creating it, so a shared one
 * would let two specs running in parallel delete each other's account. Two
 * `playwright test` runs started side by side against one stack still share
 * each slot.
 *
 * `spec` must already be a lowercase slug, short enough to keep the address
 * within the 254 characters `EmailStr` accepts: the backend refuses an address
 * `EmailStr` rejects and stores the rest lowercased, while the leftover lookup
 * compares addresses exactly. Anything else throws rather than being rewritten,
 * so the address is the label verbatim and no two labels share an account.
 * The 254-character total is the only length limit a label can hit:
 * email-validator applies its 64-character local-part limit only in strict
 * mode, which `EmailStr` does not use, and the domain part is fixed.
 */
export function regularUserEmail(spec: string, parallelIndex: number): string {
  if (typeof spec !== 'string' || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(spec)) {
    throw new Error(`regularUserEmail: ${JSON.stringify(spec)} is not a lowercase slug (letters, digits, single hyphens)`);
  }
  if (!Number.isSafeInteger(parallelIndex) || parallelIndex < 0) {
    throw new Error(`regularUserEmail: ${parallelIndex} is not a worker slot (testInfo.parallelIndex)`);
  }
  const email = `e2e-regular-${spec}-w${parallelIndex}@e2e-example.com`;
  if (email.length > 254) {
    throw new Error(`regularUserEmail: "${spec}" makes a ${email.length}-character address (EmailStr accepts 254)`);
  }
  return email;
}
export const REGULAR_USER_PASSWORD = 'E2eRegularPwd!7';

/**
 * The address every spec shared before they were given one each. Swept where
 * it is created, so a run against a persistent database collects it.
 */
export const LEGACY_REGULAR_USER_EMAIL = 'e2e-regular@e2e-example.com';
