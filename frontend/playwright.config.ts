import path from 'node:path';
import { defineConfig, devices } from '@playwright/test';
import dotenv from 'dotenv';
import { E2E_DIR, shardMatch } from './tests_e2e/shards';

dotenv.config({ path: '.env.e2e', override: false });

// `E2E_SHARD=<index>/<total>` runs one CI job's share of each project's spec
// files (tests_e2e/shards.ts); unset, every project runs every file.
const coreShard = shardMatch(E2E_DIR, ['a2a', 'evaluation', 'opencti']);
const a2aShard = shardMatch(path.join(E2E_DIR, 'a2a'));

/**
 * See https://playwright.dev/docs/test-configuration.
 *
 * One run covers both suites. The A2A projects need a deployment configured
 * for A2A — accelerated clocks, its own origin trusted, a scripted model
 * seeded — and `a2a-setup` fails the run when it is absent rather than
 * skipping, so reach for `--project=core` against an ordinary dev stack.
 * The `e2e` job in .github/workflows/e2e.yml is where that configuration lives.
 */
export default defineConfig({
  testDir: './tests_e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  // One: each shard's runner also carries its own stack on 4 vCPUs, and a
  // second browser flakes the rendering and timing specs badly enough that the
  // retries cost more wall clock than the parallelism saves. Shards, not
  // workers, are where this suite's parallelism comes from.
  workers: process.env.CI ? 1 : undefined,
  // The JSON report is what the nightly run measures the shard weights from (backend/scripts/shard_weights.py).
  reporter: process.env.CI
    ? [['list'], ['html', { open: 'never' }], ['json', { outputFile: 'test-results/timings.json' }]]
    : [['list'], ['html', { open: 'never' }]],

  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:3100',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    headless: true,
    viewport: { width: 1920, height: 1080 },
    locale: 'en-US',
    timezoneId: 'UTC',
  },

  expect: { timeout: 15_000 },
  timeout: 60_000,

  projects: [
    {
      name: 'setup',
      // `testDir` is ./tests_e2e and this matches recursively, so without the
      // exclusion the A2A suite's own setup runs here too, and fails on a
      // deployment that carries none of the A2A configuration it checks for.
      testMatch: /.*\.setup\.ts/,
      testIgnore: /a2a\//,
    },
    {
      name: 'core',
      // On the PROJECT, not the top level: a project-level `testIgnore`
      // replaces the top-level one rather than merging with it, so a top-level
      // entry here would be silently overridden.
      testIgnore: [/opencti\/.*\.spec\.ts/, /a2a\//, /evaluation\//],
      ...(coreShard ? { testMatch: coreShard } : {}),
      use: {
        ...devices['Desktop Chrome'],
        storageState: 'tests_e2e/.auth/user.json',
      },
      dependencies: ['setup'],
    },
    {
      // Evaluation runs only in copilot or dev mode with `EVALUATION` on,
      // so these specs need a deployment of their own: the `evaluation` job of
      // .github/workflows/e2e.yml. Against any other stack, reach for `--project=core`.
      name: 'evaluation',
      testDir: './tests_e2e/evaluation',
      use: {
        ...devices['Desktop Chrome'],
        storageState: 'tests_e2e/.auth/user.json',
      },
      dependencies: ['setup'],
    },
    {
      name: 'a2a-setup',
      testDir: './tests_e2e/a2a',
      testMatch: /a2a\.setup\.ts/,
      // Ordered: the terms gate is cleared before the sign-in that meets it.
      fullyParallel: false,
      workers: 1,
      timeout: 180_000,
      retries: 0,
    },
    {
      name: 'a2a',
      testDir: './tests_e2e/a2a',
      testIgnore: /a2a\.setup\.ts/,
      ...(a2aShard ? { testMatch: a2aShard } : {}),
      // Every spec shares one scripted model, one database and one set of
      // per-partner rate limits, and several assert on deployment-global state.
      // Two files at once and they arbitrate with each other, not the product.
      fullyParallel: false,
      workers: 1,
      // A retry that passes hides exactly the timing bug the spec is there to
      // catch. A flake here is a finding.
      retries: 0,
      // A delegate handover waits on the worker's reconciler, and a
      // floor-interval branch waits out a sweep even on the accelerated clock.
      timeout: 180_000,
      use: {
        ...devices['Desktop Chrome'],
        storageState: 'tests_e2e/.auth/a2a.json',
        // `SELF.origin` in tests_e2e/a2a/instance.ts: specs navigate by
        // relative path, so this must match the origin their protocol calls use.
        baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:4000',
        // These projects never retry, so `on-first-retry` would capture nothing.
        trace: 'retain-on-failure',
      },
      dependencies: ['a2a-setup'],
    },
  ],
});
