/**
 * What every toast says to assistive technologies - `yarn test:unit`.
 *
 * The Snackbar's Close button holds nothing but an icon: without a label of
 * its own it is announced in English ("Close"), the library's default. Radix
 * also names the toast region and prefixes every announcement, in English
 * unless told otherwise. All three are translated, which needs the toasts to
 * render inside the translations.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
const TOAST = read("./toast.tsx");
const MAIN = read("../../main.tsx");

const LABELS = ["Close notification", "Notification", "Notifications ({hotkey})"];

test("the Close button of every toast is named, and the action has its alternative", () => {
  const snackbar = TOAST.match(/<Snackbar\b[\s\S]*?\/>/)?.[0] ?? "";
  assert.match(snackbar, /closeLabel=\{t_i18n\("Close notification"\)\}/);
  // Without it the library renders the action outside `Toast.Action`.
  assert.match(snackbar, /actionAltText=\{t\.action\?\.label\}/);
});

test("the toast region and each announcement are named in the user's language", () => {
  assert.match(TOAST, /<SnackbarProvider\b[^>]*\blabel=\{t_i18n\("Notification"\)\}/);
  // Radix replaces `{hotkey}` with the key that focuses the region (F8).
  assert.match(
    TOAST,
    /<SnackbarViewport\s+label=\{t_i18n\("Notifications \(\{hotkey\}\)", \{ hotkey: "\{hotkey\}" \}\)\}/,
  );
});

test("the toasts render inside the translations, or t_i18n would have no locale", () => {
  const intl = MAIN.indexOf("<AppIntlProvider>");
  const toasts = MAIN.indexOf("<ToastProvider>");
  assert.ok(intl !== -1 && toasts !== -1);
  assert.ok(intl < toasts, "ToastProvider must be mounted inside AppIntlProvider");
  assert.ok(MAIN.indexOf("</ToastProvider>") < MAIN.indexOf("</AppIntlProvider>"));
});

test("every label is translated in every catalog", () => {
  const catalogs = Object.fromEntries(
    ["en", "fr", "es", "ja"].map((lang) => [lang, JSON.parse(read(`../../i18n/lang/${lang}.json`)) as Record<string, string>]),
  );
  for (const label of LABELS) {
    assert.equal(catalogs.en[label], label);
    for (const lang of ["fr", "es", "ja"]) {
      assert.ok(catalogs[lang][label], `${lang}.json misses ${JSON.stringify(label)}`);
    }
  }
  assert.notEqual(catalogs.es["Close notification"], "Close notification");
  assert.notEqual(catalogs.fr["Close notification"], "Close notification");
  assert.notEqual(catalogs.ja["Close notification"], "Close notification");
  for (const lang of ["fr", "es", "ja"]) {
    assert.match(catalogs[lang]["Notifications ({hotkey})"], /\{hotkey\}/, `${lang} drops the hotkey`);
  }
});
