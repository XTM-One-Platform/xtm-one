# CI license

The end-to-end suites (`e2e.yml`, `relay-e2e.yml`) run with an Enterprise Edition license, since the agentic features are EE. `.github/actions/ci-license` puts it in `ENTERPRISE_LICENSE` for the job's later steps.

## Contract

- **`.github/ci-license-url` holds a URL**: the license is the public `ci`-type certificate at that URL. It is not a secret: `backend/app/enterprise/licensing.py` ends a `ci` license 45 minutes after the platform's creation (`min(platform created_at + 45 min, notBefore + 1 year)`, no grace period, as OpenCTI's `licensing.ts`; pinned by `backend/tests/test_licensing_ci_expiration.py`). A fork's pull request gets neither the repository secrets nor the repository variables, but it reads this committed file, so it runs the same suite. A URL that does not answer with a PEM fails the step; it never falls back silently.
- **The file holds no URL**: the license is the secret `E2E_ENTERPRISE_LICENSE`. A fork's pull request then has none and runs as Community Edition.
- An E2E job must stay under 45 minutes from the start of the platform. The runs take 11 to 16 minutes, build included.
- The certificate works one year at most: its deadline is `notBefore + 365 days` (the platform ignores the `notAfter` of a `ci` license), computed by `.github/scripts/ci-license-deadline.sh` (tests: `ci-license-deadline.test.sh`). The "CI license" step fails with that date from that day on (the platform ends the license at notBefore's time of day, partway through it), and warns from 30 days before it.
- Deployments never use it: the private repository that publishes images and runs AWX uses real licenses.

## What the public certificate gives away

Anyone can download it and start an Enterprise Edition platform with it. The 45 minutes are counted from `platform_settings.created_at`, a column of the operator's own database: whoever resets it keeps Enterprise Edition on that installation, existing data included, until the certificate's own end (`notBefore + 1 year`). Without a creation date to count from (no platform settings row yet), a `ci` license is not granted at all, as in OpenCTI's `xtm-one-license.ts`. OpenCTI's `licensing.ts` computes the cap from the same `settings.created_at` and ignores the certificate's `notAfter` too, so this is the accepted trade of the OpenCTI model, not a hole of this repository. What bounds it is `notBefore + 365 days`: renewing the certificate every year ends it for anyone who kept the old file. `notAfter` does not shorten it; a shorter life means a `notBefore` in the past.

## Renewal

Every Monday, `ci-license-expiry.yml` reads the deadline of the certificate at the URL of `.github/ci-license-url` (nothing to check while the file holds none). From 30 days before it, it keeps one open issue, `chore(licensing): renew the CI license before <date>`, and comments on it each week; it closes the issue at the first check that finds a certificate valid for 30 days or more.

To renew, the license owner issues a new `ci` certificate (step 1 below) and replaces the file at the same URL. Nothing changes in this repository: the next run downloads the new one. Run the workflow by hand (Actions > CI license expiry > Run workflow) to close the issue at once.

## Activation

Until step 3, nothing changes: the workflows keep using the secret.

The URL is a committed file, not a repository variable: GitHub passes no variable to the workflow of a fork's pull request either, so a variable would leave the fork on the Community Edition, the case this exists for.

1. **License owner**: issue a `ci`-type XTM One Enterprise Edition certificate (`license_type` `ci`, bound to no platform: `license_global`) (`notBefore` today, or earlier for a shorter life: the license ends one year after it, see above) and upload it to the public JFrog repository `ci-utils`, for example `xtm-one-license/xtm_one_ci_licence.crt`. Check it from a shell with no credential: `curl -fsSL <url> | openssl x509 -noout -subject -dates`.
2. **Local check** (optional): `ENTERPRISE_LICENSE="$(curl -fsSL <url>)"` in `.env`, start the platform, open the license page of the Settings: it reads `ci` and expires 45 minutes after the first start.
3. **Anyone with write access**: put the URL on its own line in `.github/ci-license-url` and merge that one-line pull request.
4. Re-run the E2E and the relay E2E on a pull request of the repository: both are green, and the log of the "CI license" step says `the public certificate at <url>`. Open a pull request from a fork (or the next outside one) and check the same.
5. **Repository admin**: delete the secret `E2E_ENTERPRISE_LICENSE`, then remove the `fallback` input in both workflows and in the action (a follow-up commit).

To go back, empty the file (one more commit): the secret is used again, if it still exists.
