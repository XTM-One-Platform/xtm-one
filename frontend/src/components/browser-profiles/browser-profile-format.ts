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

/** Labels and pure helpers of the browser profile screens. */
import type { BrowserProfileApp, BrowserProfileLoginConfig, BrowserSessionStatus } from "@/types";

/** The English label of a browser session status: the last probe's answer, never the cookies'. @i18n-keys */
export const SESSION_STATUS_LABELS = {
  none: "Not signed in",
  unverified: "Saved, not checked",
  active: "Signed in",
  expired: "Session expired (lands on login page)",
  awaiting_mfa: "Waiting for MFA",
  signing_in: "Signing in",
  failed: "Sign-in failed",
} as const satisfies Record<BrowserSessionStatus, string>;

/** The badge each session status wears. */
export const SESSION_VARIANTS: Record<BrowserSessionStatus, "success" | "cyan" | "warning" | "destructive" | "outline"> = {
  none: "outline",
  unverified: "outline",
  active: "success",
  expired: "warning",
  awaiting_mfa: "cyan",
  signing_in: "cyan",
  failed: "destructive",
};

/** How often a profile's saved session is refreshed, by minutes. @i18n-keys */
export const KEEPALIVE_LABELS = {
  "0": "Never",
  "30": "Every 30 minutes",
  "60": "Every hour",
  "120": "Every 2 hours",
  "240": "Every 4 hours",
  "480": "Every 8 hours",
  "720": "Every 12 hours",
  "1440": "Every day",
} as const;

/** A comma or newline separated host list as the profile stores it. */
export function parseHosts(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((entry) => entry.trim().toLowerCase())
    .filter(Boolean);
}

/** The form's application rows as the profile stores them: a row without a URL left out, names and URLs trimmed. */
export function appsFrom(rows: BrowserProfileApp[]): BrowserProfileApp[] {
  return rows
    .map((row) => ({ name: row.name.trim(), url: row.url.trim() }))
    .filter((row) => row.url);
}

/** Whether an application row's URL would be refused on save: the rule of the backend's clean_apps (http(s), a host, no credentials, at most 2000 characters). A blank URL is no problem: the row is left out. */
export function appUrlInvalid(url: string): boolean {
  const text = url.trim();
  if (!text) return false;
  if (text.length > 2000) return true;
  try {
    const parsed = new URL(text);
    return !(parsed.protocol === "http:" || parsed.protocol === "https:") || !parsed.hostname || Boolean(parsed.username || parsed.password);
  } catch {
    return true;
  }
}

/** The label an application wears on the profile card: its name, else its host. */
export function appLabel(app: BrowserProfileApp): string {
  if (app.name.trim()) return app.name.trim();
  try {
    return new URL(app.url).hostname;
  } catch {
    return app.url;
  }
}

/** The applications on one line of the card: the first two labels, then how many more. */
export function appsSummary(apps: BrowserProfileApp[]): string {
  const labels = apps.map(appLabel);
  const shown = labels.slice(0, 2).join(", ");
  return labels.length > 2 ? `${shown}, +${labels.length - 2}` : shown;
}

/** The sign-in setting listing the storage keys the application keeps its session in. */
export const KEEP_STORAGE_KEYS = "keep_storage_keys";

/** A comma or newline separated list of storage keys, each once: a key keeps its case. */
function parseStorageKeys(text: string): string[] {
  const keys = text
    .split(/[\n,]/)
    .map((entry) => entry.trim())
    .filter(Boolean);
  return [...new Set(keys)];
}

/** A profile's sign-in settings as the form's fields hold them: the storage keys as one comma-separated text. */
export function loginConfigTexts(config: BrowserProfileLoginConfig | null | undefined): Record<string, string> {
  return Object.fromEntries(
    Object.entries(config ?? {}).map(([key, value]) => [key, Array.isArray(value) ? value.join(", ") : value]),
  );
}

/** The form's sign-in settings as the profile stores them: blank ones left out, the storage keys a list. */
export function loginConfigFrom(texts: Record<string, string>): BrowserProfileLoginConfig {
  const config: BrowserProfileLoginConfig = {};
  for (const [key, text] of Object.entries(texts)) {
    if (key === KEEP_STORAGE_KEYS) {
      const keys = parseStorageKeys(text);
      if (keys.length) config[key] = keys;
    } else if (text.trim()) {
      config[key] = text;
    }
  }
  return config;
}
