/**
 * Unit tests for the delete confirmation's wait on the sharing preview -
 * `yarn test:unit`. The dialog opens at once; the preview only adds
 * who loses access, and never holds the delete back: a failure or a slow
 * answer leaves the page's own text.
 */

import { mock, test } from "node:test";
import assert from "node:assert/strict";

import { DELETE_PREVIEW_WAIT_MS, settleWithin } from "./preview-wait.ts";

test("a preview that answers in time gives its answer", async () => {
  assert.deepEqual(await settleWithin(Promise.resolve(["item"]), DELETE_PREVIEW_WAIT_MS, []), ["item"]);
});

test("a preview that fails gives the fallback", async () => {
  assert.deepEqual(await settleWithin(Promise.reject(new Error("boom")), DELETE_PREVIEW_WAIT_MS, []), []);
});

test("a preview slower than the bound gives the fallback, and its late answer is dropped", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    let answer: (items: string[]) => void = () => {};
    const slow = new Promise<string[]>((resolve) => {
      answer = resolve;
    });
    const settled = settleWithin(slow, DELETE_PREVIEW_WAIT_MS, [] as string[]);
    mock.timers.tick(DELETE_PREVIEW_WAIT_MS);
    assert.deepEqual(await settled, []);
    answer(["late"]);
    assert.deepEqual(await settled, []);
  } finally {
    mock.timers.reset();
  }
});

test("an answer just before the bound still counts", async () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    let answer: (items: string[]) => void = () => {};
    const slow = new Promise<string[]>((resolve) => {
      answer = resolve;
    });
    const settled = settleWithin(slow, DELETE_PREVIEW_WAIT_MS, [] as string[]);
    mock.timers.tick(DELETE_PREVIEW_WAIT_MS - 1);
    answer(["item"]);
    assert.deepEqual(await settled, ["item"]);
  } finally {
    mock.timers.reset();
  }
});

test("the bound stays short enough never to feel like a blocked delete", () => {
  assert.ok(DELETE_PREVIEW_WAIT_MS > 0 && DELETE_PREVIEW_WAIT_MS <= 5_000);
});
