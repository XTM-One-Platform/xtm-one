/**
 * The flaky list, the Markdown block and the GitHub annotation over the
 * Playwright JSON report - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { annotation, collectFlaky, formatFlakyReport } from "./e2e-flaky.mjs";

const report = {
  suites: [
    {
      title: "auth",
      suites: [
        {
          title: "login flow",
          specs: [
            {
              file: "auth/login.spec.ts",
              line: 12,
              title: "signs in with an expired session",
              tests: [
                {
                  projectName: "chromium",
                  status: "flaky",
                  results: [
                    {
                      status: "failed",
                      retry: 0,
                      duration: 1500,
                      error: { message: "Timed out waiting for the welcome banner\n    at auth/login.spec.ts:12" },
                    },
                    { status: "passed", retry: 1, duration: 3000 },
                  ],
                },
              ],
            },
            {
              file: "auth/login.spec.ts",
              line: 40,
              title: "signs in normally",
              tests: [
                {
                  projectName: "chromium",
                  status: "expected",
                  results: [{ status: "passed", retry: 0, duration: 900 }],
                },
              ],
            },
            {
              file: "auth/login.spec.ts",
              line: 77,
              title: "rejects a bad password",
              tests: [
                {
                  projectName: "chromium",
                  status: "unexpected",
                  results: [
                    { status: "failed", retry: 0, duration: 400, error: { message: "boom" } },
                    { status: "failed", retry: 1, duration: 500, error: { message: "boom again" } },
                  ],
                },
              ],
            },
          ],
        },
      ],
    },
    {
      title: "chat/chat-turn.spec.ts",
      specs: [
        {
          file: "chat/chat-turn.spec.ts",
          line: 3,
          title: "streams an answer",
          tests: [
            {
              projectName: "firefox",
              status: "skipped",
              results: [{ status: "skipped", retry: 0, duration: 10 }],
            },
          ],
        },
      ],
    },
  ],
};

test("only the flaky test is collected, with its suite chain, attempts, first error line and lost seconds", () => {
  const flaky = collectFlaky(report);
  assert.equal(flaky.length, 1);
  assert.deepEqual(flaky[0], {
    file: "auth/login.spec.ts",
    line: 12,
    title: "auth › login flow › signs in with an expired session",
    project: "chromium",
    attempts: 2,
    firstError: "Timed out waiting for the welcome banner",
    wastedSeconds: 1.5,
  });
});

test("an enclosing suite titled like its spec file drops out of the chain, and a timed-out attempt still leaves an error", () => {
  const flaky = collectFlaky({
    title: "chat/chat-turn.spec.ts",
    suites: [
      {
        title: "sending",
        specs: [
          {
            file: "chat/chat-turn.spec.ts",
            line: 8,
            title: "retries a failed send",
            tests: [
              {
                projectName: "chromium",
                status: "flaky",
                results: [
                  {
                    status: "timedOut",
                    retry: 0,
                    duration: 2500,
                    error: { message: "Test timeout of 30000ms exceeded" },
                  },
                  { status: "passed", retry: 1, duration: 800 },
                ],
              },
            ],
          },
        ],
      },
    ],
  });
  assert.equal(flaky.length, 1);
  assert.equal(flaky[0].title, "sending › retries a failed send");
  assert.equal(flaky[0].firstError, "Test timeout of 30000ms exceeded");
  assert.equal(flaky[0].wastedSeconds, 2.5);
});

test("an empty report collects nothing and renders an empty string", () => {
  assert.deepEqual(collectFlaky({}), []);
  assert.deepEqual(collectFlaky({ suites: [] }), []);
  assert.equal(formatFlakyReport([]), "");
});

test("the Markdown block has the heading, one row per entry, and escapes `|` in the error", () => {
  const markdown = formatFlakyReport([
    {
      file: "auth/login.spec.ts",
      line: 12,
      title: "auth › login flow › signs in with an expired session",
      project: "chromium",
      attempts: 2,
      firstError: "expected a || b to be a | b",
      wastedSeconds: 1.5,
    },
  ]);
  assert.equal(
    markdown,
    [
      "### Flaky tests (passed only on retry): 1",
      "",
      "| Test | Attempts | Lost | First error |",
      "| --- | --- | --- | --- |",
      "| `auth/login.spec.ts:12` auth › login flow › signs in with an expired session | 2 | 1.5 s | expected a \\|\\| b to be a \\| b |",
    ].join("\n"),
  );
});

test("the annotation points at the spec, pluralises the retry count and escapes the message", () => {
  assert.equal(
    annotation({ file: "auth/login.spec.ts", line: 12, title: "auth › login flow › signs in", attempts: 2 }),
    "::warning file=frontend/tests_e2e/auth/login.spec.ts,line=12,title=Flaky E2E test::auth › login flow › signs in passed only after 1 retry",
  );
  assert.equal(
    annotation({ file: "auth/login.spec.ts", line: 12, title: "auth › login flow › signs in", attempts: 3 }),
    "::warning file=frontend/tests_e2e/auth/login.spec.ts,line=12,title=Flaky E2E test::auth › login flow › signs in passed only after 2 retries",
  );
  assert.equal(
    annotation({ file: "pipes.spec.ts", line: 5, title: "100% sure\nand\rdone", attempts: 2 }),
    "::warning file=frontend/tests_e2e/pipes.spec.ts,line=5,title=Flaky E2E test::100%25 sure%0Aand%0Ddone passed only after 1 retry",
  );
});
