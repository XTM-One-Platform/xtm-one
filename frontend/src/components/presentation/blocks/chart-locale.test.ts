/**
 * Pinned here: the locale a chart writes its numbers in. A language
 * switch must reach the chart in the render the new locale arrives in:
 * `AppIntlProvider` sets `<html lang>` only in an effect after that render,
 * and setting it re-renders nothing, so a chart reading the attribute kept the
 * old formatting (and its settle build never moved).
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { IntlProvider } from "react-intl";

import { chartLocale, useChartLocale } from "./chart-locale.ts";

const VALUE = 29500.5;
const written = (locale: string): string => new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(VALUE);

/** What a chart reads and writes in one render: the locale, then a value in it. */
function Probe() {
  const locale = useChartLocale();
  return createElement("output", null, `${locale}|${written(locale)}`);
}

const render = (locale?: string): string =>
  renderToString(
    locale ? createElement(IntlProvider, { locale, messages: {} }, createElement(Probe)) : createElement(Probe),
  );

/** Runs `body` with `<html lang>` reading `lang`, as the page would. */
function withPageLang(lang: string, body: () => void): void {
  const scope = globalThis as { document?: unknown };
  const previous = scope.document;
  scope.document = { documentElement: { lang } };
  try {
    body();
  } finally {
    scope.document = previous;
  }
}

test("a chart writes in the provider's locale in the render it arrives in, whatever <html lang> still says", () => {
  withPageLang("en", () => {
    assert.ok(render("fr-fr").includes(`fr-fr|${written("fr-fr")}`));
    // The next language switch re-renders the chart in that language too.
    assert.ok(render("ja-jp").includes(`ja-jp|${written("ja-jp")}`));
  });
  // The switch changes the formatting itself, not only the name.
  assert.notEqual(written("fr-fr"), written("en-us"));
});

test("a chart rendered without a provider falls back to the page's declared language", () => {
  withPageLang("de", () => {
    assert.ok(render().includes(`de|${written("de")}`));
  });
});

test("the rule: the active locale first, then the page, the browser, English", () => {
  withPageLang("de", () => {
    assert.equal(chartLocale("es-es"), "es-es");
    assert.equal(chartLocale(null), "de");
  });
  withPageLang("", () => {
    const browser = typeof navigator !== "undefined" && navigator.language ? navigator.language : "en";
    assert.equal(chartLocale(), browser);
  });
});
