/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The locale a chart writes its numbers in: the app's active
 * language, the one `AppIntlProvider` resolves for the person, so the viewer,
 * the static export page and the PDF write `29,5 M` for the same person.
 *
 * It is read from the i18n context in the render that uses it: a language
 * switch re-renders the chart with the new formatting at once, and the build
 * `chart.tsx` settles moves with it. `<html lang>` is set from the same locale
 * only in an effect AFTER that render, and setting it triggers no render, so
 * it is never read while a provider is mounted. Outside one, the page's
 * declared language, then the browser's, then English.
 */
import { useContext } from "react";
import { IntlContext, type IntlShape } from "react-intl";

/** The rule: the active locale when there is one, else the page's, the browser's, English. */
export function chartLocale(active?: string | null): string {
  if (active) return active;
  if (typeof document !== "undefined" && document.documentElement.lang) return document.documentElement.lang;
  if (typeof navigator !== "undefined" && navigator.language) return navigator.language;
  return "en";
}

/** The chart locale of the current render (the only reader of the rule in a component). */
export function useChartLocale(): string {
  // Typed as always present, `undefined` outside an `IntlProvider`.
  const intl = useContext(IntlContext) as IntlShape | undefined;
  return chartLocale(intl?.locale);
}
