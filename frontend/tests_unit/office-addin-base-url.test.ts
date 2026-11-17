/**
 * Which platform the Office add-in calls, `apps/microsoft-office/src/lib/api.ts` -
 * `yarn test:unit`. A stored URL wins; otherwise the platform that
 * serves the page under /office/, its base path included; otherwise the page's
 * own origin. Never a fixed instance.
 */
import { afterEach, test } from "node:test";
import assert from "node:assert/strict";

import { getBaseUrl } from "../../apps/microsoft-office/src/lib/api.ts";

const store = new Map<string, string>();

function at(url: string) {
  const { origin, pathname } = new URL(url);
  Object.defineProperty(globalThis, "window", {
    value: { location: { origin, pathname } },
    configurable: true,
  });
  Object.defineProperty(globalThis, "localStorage", {
    value: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => store.set(key, value),
      removeItem: (key: string) => store.delete(key),
    },
    configurable: true,
  });
}

afterEach(() => store.clear());

test("the add-in served at the root calls its own platform", () => {
  at("https://xtm.example.com/office/taskpane.html");
  assert.equal(getBaseUrl(), "https://xtm.example.com");
  at("https://xtm.example.com/office");
  assert.equal(getBaseUrl(), "https://xtm.example.com");
});

test("under a base path, the add-in keeps it", () => {
  at("https://host.example.com/xtm-one/office/taskpane.html");
  assert.equal(getBaseUrl(), "https://host.example.com/xtm-one");
});

test("a path that only starts like /office is not the add-in's", () => {
  at("https://host.example.com/officers/office-hours");
  assert.equal(getBaseUrl(), "https://host.example.com");
});

test("on a dev server, the page's own origin, never a fixed instance", () => {
  at("https://localhost:3100/");
  assert.equal(getBaseUrl(), "https://localhost:3100");
});

test("a stored URL wins", () => {
  at("https://xtm.example.com/office/taskpane.html");
  store.set("filigran_copilot_url", "https://other.example.com");
  assert.equal(getBaseUrl(), "https://other.example.com");
});
