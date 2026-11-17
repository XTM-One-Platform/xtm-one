/**
 * Browser language detection — mirrors OpenCTI's `BrowserLanguage.js`.
 *
 * Walks the browser language hints (preferred-languages list first, then the
 * various legacy single-value properties) and returns the first entry that
 * matches a supported platform locale. Falls back to English.
 */

export const LANGUAGE_AUTO = "auto";
export const DEFAULT_LANG = "en-us";

/** Concrete UI locales the platform ships translations for.
 *  Keep in sync with `SUPPORTED_LANGUAGES` in `backend/app/core/utils.py`. */
const SUPPORTED_LOCALES = ["en-us", "fr-fr", "es-es", "ja-jp"] as const;

export type PlatformLang = (typeof SUPPORTED_LOCALES)[number];

/** Match a raw browser language tag (e.g. "fr", "fr-FR", "es-419") against a
 *  supported locale — exact match first, then primary-subtag match. */
function matchLocale(tag: string): PlatformLang | null {
  const lower = tag.toLowerCase();
  const exact = SUPPORTED_LOCALES.find((l) => l === lower);
  if (exact) return exact;
  const primary = lower.split("-")[0];
  return SUPPORTED_LOCALES.find((l) => l.split("-")[0] === primary) ?? null;
}

/** Detect the preferred supported locale from the browser, or English. */
function detectBrowserLanguage(): PlatformLang {
  const nav = window.navigator as Navigator & {
    browserLanguage?: string;
    userLanguage?: string;
    systemLanguage?: string;
  };
  const candidates: string[] = [
    ...(nav.languages ?? []),
    nav.language,
    nav.browserLanguage ?? "",
    nav.userLanguage ?? "",
    nav.systemLanguage ?? "",
  ].filter(Boolean);
  for (const candidate of candidates) {
    const matched = matchLocale(candidate);
    if (matched) return matched;
  }
  return DEFAULT_LANG;
}

/**
 * Resolve the effective UI locale — identical semantics to OpenCTI `Root.tsx`:
 *
 *   user.language (if not "auto")
 *     -> platform_language (if not "auto")
 *       -> detected browser language
 *         -> English
 */
export function resolveLocale(
  userLanguage: string | null | undefined,
  platformLanguage: string | null | undefined,
): PlatformLang {
  const platformLang =
    platformLanguage && platformLanguage !== LANGUAGE_AUTO
      ? platformLanguage
      : detectBrowserLanguage();
  const lang =
    userLanguage && userLanguage !== LANGUAGE_AUTO ? userLanguage : platformLang;
  return (SUPPORTED_LOCALES as readonly string[]).includes(lang)
    ? (lang as PlatformLang)
    : DEFAULT_LANG;
}
