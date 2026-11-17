/**
 * Unit tests for the mobile companion's rules - `yarn test:unit`.
 *
 * A phone is told by the device, never by the screen width; on a phone the app
 * offers Today, Inbox and Ask, and nothing else of the platform.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { companionTab, companionView, isPhoneDevice } from "./companion-routes.ts";

const UA = {
  iphone:
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
  androidPhone:
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36",
  androidTablet:
    "Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
  ipad: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
  firefoxAndroid: "Mozilla/5.0 (Android 14; Mobile; rv:131.0) Gecko/131.0 Firefox/131.0",
  desktopChrome:
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
};

test("a phone is told by the device: the browser's own hint, else the user agent", () => {
  assert.equal(isPhoneDevice({ clientHintMobile: true, userAgent: UA.desktopChrome }), true);
  assert.equal(isPhoneDevice({ clientHintMobile: undefined, userAgent: UA.iphone }), true);
  assert.equal(isPhoneDevice({ clientHintMobile: false, userAgent: UA.androidPhone }), true);
  assert.equal(isPhoneDevice({ clientHintMobile: undefined, userAgent: UA.firefoxAndroid }), true);
});

test("a tablet and a computer get the full platform, whatever their window width", () => {
  for (const userAgent of [UA.androidTablet, UA.ipad, UA.desktopChrome]) {
    assert.equal(isPhoneDevice({ clientHintMobile: false, userAgent }), false, userAgent);
    assert.equal(isPhoneDevice({ clientHintMobile: undefined, userAgent }), false, userAgent);
  }
});

test("the companion offers Today, Inbox and Ask (and where a share lands), nothing else", () => {
  assert.equal(companionView("/"), "today");
  assert.equal(companionView(""), "today");
  assert.equal(companionView("/chat"), "ask");
  assert.equal(companionView("/inbox"), "inbox");
  assert.equal(companionView("/share"), "share");
  assert.equal(companionTab("share"), "ask");
  for (const path of ["/agents", "/settings/general", "/dashboards", "/dashboard", "/ctem/scoping", "/chatter"]) {
    assert.equal(companionView(path), null, path);
  }
  assert.equal(companionTab("ask"), "ask");
});
