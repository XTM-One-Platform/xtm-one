/**
 * Unit tests for how a device installs the companion - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { INSTALL_BANNER_SNOOZE_MS, installBannerSnoozed, installMethod, isIosSafari } from "./install-app.ts";

const IPHONE_SAFARI =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1";
const IPHONE_CHROME =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/128.0 Mobile/15E148 Safari/604.1";
const IPAD_SAFARI =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15";
const ANDROID =
  "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36";

test("the installed app offers nothing; a browser prompt wins over the iOS explanation", () => {
  const base = { standalone: false, hasPrompt: false, userAgent: ANDROID, maxTouchPoints: 5 };
  assert.equal(installMethod({ ...base, standalone: true, hasPrompt: true }), "installed");
  assert.equal(installMethod({ ...base, hasPrompt: true }), "prompt");
  assert.equal(installMethod(base), "unavailable");
  assert.equal(installMethod({ ...base, userAgent: IPHONE_SAFARI }), "ios");
});

test("only Safari on iOS and iPadOS installs from the Share menu", () => {
  assert.equal(isIosSafari(IPHONE_SAFARI, 5), true);
  assert.equal(isIosSafari(IPAD_SAFARI, 5), true);
  // The same user agent on a Mac with no touch screen is a Mac.
  assert.equal(isIosSafari(IPAD_SAFARI, 0), false);
  assert.equal(isIosSafari(IPHONE_CHROME, 5), false);
  assert.equal(isIosSafari(ANDROID, 5), false);
});

test("a dismissed banner stays away for a month", () => {
  const now = 1_800_000_000_000;
  assert.equal(installBannerSnoozed(null, now), false);
  assert.equal(installBannerSnoozed(now - 1000, now), true);
  assert.equal(installBannerSnoozed(now - INSTALL_BANNER_SNOOZE_MS, now), false);
  assert.equal(installBannerSnoozed(Number.NaN, now), false);
});

test("a linked phone is named by its device and browser", async () => {
  const { deviceName, devicePlatform } = await import("./install-app.ts");
  assert.equal(deviceName(IPHONE_SAFARI), "iPhone · Safari");
  assert.equal(deviceName(IPHONE_CHROME), "iPhone · Chrome");
  assert.equal(deviceName(ANDROID), "Android · Chrome");
  assert.equal(deviceName("Mozilla/5.0 (Android 14; Mobile; rv:131.0) Gecko/131.0 Firefox/131.0"), "Android · Firefox");
  assert.equal(devicePlatform(IPHONE_SAFARI), "ios");
  assert.equal(devicePlatform(ANDROID), "android");
  assert.equal(devicePlatform("Mozilla/5.0 (Windows NT 10.0)"), "other");
});
