/* eslint-disable react-refresh/only-export-components */
/**
 * i18n layer — mirrors OpenCTI's react-intl setup.
 *
 * Message ids ARE the English strings (OpenCTI convention): a missing
 * translation silently renders the English id, so untranslated surfaces
 * degrade gracefully and catalogs can grow incrementally.
 *
 * Locale resolution (identical to OpenCTI `Root.tsx`):
 *   user.language (if not "auto")
 *     -> PlatformSettings.platform_language (if not "auto")
 *       -> detected browser language
 *         -> English (en-us)
 */
import { useCallback, useEffect, type ReactNode } from "react";
import { IntlProvider, useIntl } from "react-intl";
import { getToken } from "@/lib/api";
import { useMeAboveRoutes } from "@/hooks/api/useAuthApi";
import { usePlatformConfig } from "@/hooks/usePlatformConfig";
import {
  DEFAULT_LANG,
  resolveLocale,
  type PlatformLang,
} from "./browser-language";
import messages_en from "./lang/en.json";
import messages_fr from "./lang/fr.json";
import messages_es from "./lang/es.json";
import messages_ja from "./lang/ja.json";

export { LANGUAGE_AUTO } from "./browser-language";

const MESSAGES: Record<PlatformLang, Record<string, string>> = {
  "en-us": messages_en,
  "fr-fr": messages_fr,
  "es-es": messages_es,
  "ja-jp": messages_ja,
};

/** Supported languages with native labels — mirrors OpenCTI `availableLanguage`. */
export const AVAILABLE_LANGUAGES: { value: PlatformLang; label: string; name: string }[] = [
  { value: "en-us", label: "English", name: "English" },
  { value: "es-es", label: "Español", name: "Spanish" },
  { value: "fr-fr", label: "Français", name: "French" },
  { value: "ja-jp", label: "日本語", name: "Japanese" },
];

/** Display label for a stored language value ("auto" handled by callers). */
export function languageLabel(value: string | null | undefined): string | null {
  return AVAILABLE_LANGUAGES.find((l) => l.value === value)?.label ?? null;
}

/**
 * Resolve the current effective locale from the platform config and the
 * cached `/auth/me` user (when authenticated). Pre-auth surfaces (login)
 * resolve platform language -> browser detection only.
 */
function useResolvedLocale(): PlatformLang {
  const platformConfig = usePlatformConfig();
  // Errors are non-fatal here — the provider must never take down the login
  // page, so locale resolution just degrades to platform/browser detection.
  const { data: user } = useMeAboveRoutes(!!getToken());
  return resolveLocale(user?.language, platformConfig?.platform_language);
}

export function AppIntlProvider({ children }: { children: ReactNode }) {
  const locale = useResolvedLocale();
  const messages = MESSAGES[locale] ?? MESSAGES[DEFAULT_LANG];

  // Keep <html lang> in sync for accessibility / font selection.
  useEffect(() => {
    document.documentElement.lang = locale.split("-")[0];
  }, [locale]);

  // NOTE: deliberately NOT keyed by locale — IntlProvider propagates new
  // locale/messages props through context, so a locale change re-renders
  // translated strings without unmounting the whole tree (a `key` would
  // wipe in-flight UI state, e.g. login form inputs, when the resolved
  // locale settles after /platform/config loads).
  return (
    <IntlProvider
      locale={locale}
      messages={messages}
      defaultLocale={DEFAULT_LANG}
      onError={(err) => {
        // English strings are the ids — a missing translation renders the
        // id itself, which is the desired fallback. Other errors (e.g. a
        // malformed ICU message in one catalog entry) are logged instead of
        // thrown so a single bad translation can never take down the app.
        if (err.code === "MISSING_TRANSLATION") return;
        console.error("[i18n]", err);
      }}
    >
      {children}
    </IntlProvider>
  );
}

/**
 * Formatter hook — same API shape as OpenCTI's `useFormatter()`.
 * `t_i18n("Some English string")` returns the translation for the active
 * locale, or the English string itself when untranslated.
 */
export function useFormatter() {
  const intl = useIntl();
  // Stable identity (per locale) so t_i18n can appear in hook dependency
  // arrays without re-triggering effects on every render.
  const t_i18n = useCallback(
    (id: string, values?: Record<string, string | number>): string => {
      // A label looked up by a value the server sent can be `undefined` at
      // runtime whatever its type says; formatjs throws on a missing id
      // before `onError` runs, which would take the whole app down.
      if (!id) {
        console.error("[i18n] t_i18n called without a message id");
        return "";
      }
      // `defaultMessage` is REQUIRED for the English-ids-are-messages scheme
      // to interpolate: without it, react-intl returns a catalog-missing id
      // as a raw string with NO ICU formatting, so "{count} commands"
      // renders literally for any key not (yet) in en.json. With it, the id
      // itself is parsed as the ICU message and values always interpolate.
      return intl.formatMessage({ id, defaultMessage: id }, values);
    },
    [intl],
  );
  // Text the server sends that is a catalog key for a built-in and the
  // person's own words for what they wrote (a pack's name, a step's label):
  // translated when the catalog holds it, shown as written otherwise. Never
  // parsed as an ICU message, so an apostrophe or a brace a person typed is
  // not taken for message syntax, and an empty text renders empty instead
  // of throwing.
  const t_text = useCallback(
    (text: string): string =>
      Object.prototype.hasOwnProperty.call(intl.messages, text) ? t_i18n(text) : text,
    [intl, t_i18n],
  );
  return { t_i18n, t_text, locale: intl.locale as PlatformLang, intl };
}
