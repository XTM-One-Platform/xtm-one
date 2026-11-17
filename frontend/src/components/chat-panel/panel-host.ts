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
 * How XTM One hosts the `@filigran/chatbot` panel: the session token, the
 * platform's language (its tag and its translations), accent, font and theme,
 * the reply's working files and the header's agent link.
 */
import { useCallback, useMemo, useSyncExternalStore } from "react";
import { useIntl } from "react-intl";
import type { ChatAttachment, ChatMessage, ChatPanelTheme, Translate, XtmAgent } from "@filigran/chatbot";
import { FLOW_ENTRY_PREFIX, GENERAL_ASSISTANT_ENTRY } from "@/components/chat-panel/endpoints";
import { getToken } from "@/lib/api";
import { revisionKey } from "@/pages/chat/file-revisions";
import { withBasePath } from "@/lib/base-path";

/**
 * The design system's tokens for the panel, as XTM One's own chat page
 * drew them: the page's background, its text, the composer a layer above the
 * page, a user's message on a tint of the accent with the page's text, answers
 * at the body size and code in IBM Plex Mono. The tokens follow the colour mode
 * and the administrator's background, so one value serves both modes and every
 * branding; each names its layer, as an unlayered alias is the page's layer 0.
 * Corners and the composer's focus are the design system's too: its two radii,
 * the composer drawn as its text field (`rounded-sm`, the input fill
 * `--bg-input-default`, no border at rest and `border-input-hover` under the
 * pointer) and focused with its `ring-focus` colour, so the panel sets no
 * corner, fill, border or ring of its own. Its notices and states take the
 * design system's feedback text tones (`tertiary`: orange-800 / red-700 /
 * green-800 in light, the 200 / 300 steps in dark), the pair that keeps 4.5:1
 * on the panel's surfaces in both modes, never the panel's amber or red.
 * Its menus (the agent switcher) are the design system's menu: the panel on
 * `--bg-elevation-highlight`, a step off the row hover, and the keyboard-active
 * row marked by a 2px `--border-input-hover` track, so the row reads in both
 * modes (xtm-one #10910, UX retest of #10911, F1).
 */
export const PANEL_THEME: ChatPanelTheme = {
  surface: "var(--bg-elevation-default-layer-0)",
  surfaceRaised: "var(--bg-elevation-default-layer-1)",
  surfaceSubtle: "var(--bg-elevation-default-layer-1)",
  surfaceHover: "var(--bg-elevation-hover-layer-0)",
  menuSurface: "var(--bg-elevation-highlight)",
  menuActiveBorder: "var(--border-input-hover)",
  inputSurface: "var(--bg-input-default)",
  radiusSm: "var(--radius-sm)",
  radiusMd: "var(--radius-md)",
  inputRadius: "var(--radius-sm)",
  inputBorderHover: "var(--border-input-hover)",
  focusRing: "var(--color-filigran-brand-primary)",
  warning: "var(--color-feedback-warning-tertiary)",
  danger: "var(--color-feedback-error-tertiary)",
  success: "var(--color-feedback-success-tertiary)",
  headerBackground: "none",
  text: "var(--text-default-primary)",
  textMuted: "var(--text-default-secondary)",
  textSecondary: "var(--text-default-secondary)",
  border: "var(--border-elevation-subtle-soft-layer-0-transparency-15)",
  textSize: "var(--text-4)",
  userBackground: "color-mix(in oklab, var(--color-accent) 10%, transparent)",
  userText: "var(--text-default-primary)",
  userRadius: "var(--radius-sm)",
  userPadding: "0.75rem 1rem",
  userTextSize: "var(--text-content-base)",
  fontMono: "var(--font-content-code)",
  codeSize: "0.8125rem",
  codeSurface: "var(--bg-elevation-default-layer-1)",
};

/** The session token, as the web chat routes take it. */
export function useChatPanelHeaders(): Record<string, string> | undefined {
  const token = getToken();
  return useMemo(() => (token ? { Authorization: `Bearer ${token}` } : undefined), [token]);
}

/**
 * The platform's language as the BCP 47 tag the panel takes (`fr-fr` →
 * `fr-FR`): the panel speaks it from its own catalog and writes its times in it.
 */
export function panelLocale(locale: string): string {
  try {
    return Intl.getCanonicalLocales(locale)[0] ?? locale;
  } catch {
    return locale;
  }
}

/**
 * The platform's translations as the panel takes them (`t`): a key the
 * platform's catalog holds comes back translated, any other comes back
 * unchanged, so the panel falls back to its own catalog in `locale`. The
 * panel fills `{name}` placeholders itself, so a template is returned as
 * written, never formatted.
 */
export function panelTranslate(messages: Readonly<Record<string, unknown>>, format: (key: string) => string): Translate {
  return (key) => {
    const message = messages[key];
    if (typeof message !== "string") return key;
    return /[{}]/.test(message) ? message : format(key);
  };
}

/** {@link panelTranslate} over the platform's catalog in the active language. */
export function usePanelTranslate(): Translate {
  const intl = useIntl();
  const format = useCallback((key: string) => intl.formatMessage({ id: key, defaultMessage: key }), [intl]);
  return useMemo(() => panelTranslate(intl.messages, format), [intl.messages, format]);
}

/** A computed colour as `#rrggbb`, the form the panel takes its accent in. */
function toHex(color: string): string | undefined {
  const value = color.trim();
  if (/^#[0-9a-f]{6}$/i.test(value)) return value;
  if (/^#[0-9a-f]{3}$/i.test(value)) return `#${[...value.slice(1)].map((c) => c + c).join("")}`;
  const rgb = value.match(/^rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)/i);
  if (!rgb) return undefined;
  return `#${rgb.slice(1, 4).map((n) => Number(n).toString(16).padStart(2, "0")).join("")}`;
}

/** The page's `--color-accent` as `#rrggbb`. */
function readAccent(): string | undefined {
  const probe = document.createElement("span");
  probe.style.color = "var(--color-accent)";
  probe.style.display = "none";
  document.body.appendChild(probe);
  const accent = toHex(getComputedStyle(probe).color);
  probe.remove();
  return accent;
}

// The accent last read, while a panel follows it: a read lays out a probe, so
// it happens when a panel subscribes and when the page may have changed, never
// on each render (`useSyncExternalStore` asks for the snapshot every time).
let accentSnapshot: { value: string | undefined } | null = null;
let accentFollowers = 0;

function accentNow(): string | undefined {
  accentSnapshot ??= { value: readAccent() };
  return accentSnapshot.value;
}

/**
 * Calls `onChange` whenever the page's accent may have changed: the platform
 * mode and colour mode on `<html>`, and the administrator's branding, a
 * `<style>` the platform config adds to `<head>` once it has loaded and
 * rewrites when the colours change.
 */
function subscribeToAccent(onChange: () => void): () => void {
  accentFollowers += 1;
  accentSnapshot = { value: readAccent() };
  const observer = new MutationObserver(() => {
    accentSnapshot = { value: readAccent() };
    onChange();
  });
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "data-mode", "class", "style"] });
  observer.observe(document.head, { childList: true, subtree: true, characterData: true });
  return () => {
    observer.disconnect();
    accentFollowers -= 1;
    // Nobody follows the page any more: the next panel reads it afresh.
    if (accentFollowers === 0) accentSnapshot = null;
  };
}

/**
 * The platform's accent and body font, read from the theme so the panel follows
 * the brand and the colour mode: the panel takes a hex accent and a font name,
 * not the design system's tokens. The accent is read again whenever the page's
 * changes, as the page's own controls follow `--color-accent`: a value read once
 * at mount was the default whenever the administrator's branding loaded after it.
 */
export function usePlatformLook(): { accentColor: string | undefined; fontFamily: string | undefined } {
  const accentColor = useSyncExternalStore(subscribeToAccent, accentNow, () => undefined);
  const fontFamily = useMemo(() => getComputedStyle(document.body).fontFamily || undefined, []);
  return useMemo(() => ({ accentColor, fontFamily }), [accentColor, fontFamily]);
}

const FILE_MARKER = /\[\[FILE:([0-9a-f-]+)\]\]/gi;

/**
 * The files of a reply its text does not name that go behind its one
 * "Working files" line (`collapseUnnamedFiles`), as `partitionReplyFiles`
 * folds them on the old page: a working file (a scratch file, a picture a tool
 * made to check the work, #6878); a file the same reply wrote again under the
 * same name and type, listed after it; and, once the reply names some of its
 * files, every one it leaves unnamed. The package asks only of unnamed files.
 */
export function isWorkingFile(attachment: ChatAttachment, message: Pick<ChatMessage, "content" | "attachments">): boolean {
  if (attachment.fileTag === "working_file") return true;
  const files = message.attachments ?? [];
  const own = new Set(files.map((f) => f.fileId.toLowerCase()));
  for (const [, id] of message.content.matchAll(FILE_MARKER)) {
    if (own.has(id.toLowerCase())) return true;
  }
  const key = revisionKey(panelFile(attachment));
  // By id: the package may hand over a copy of the reply's attachment.
  const later = files.slice(files.findIndex((f) => f.fileId === attachment.fileId) + 1);
  return later.some((f) => revisionKey(panelFile(f)) === key);
}

const panelFile = (f: ChatAttachment) => ({ filename: f.filename, content_type: f.contentType ?? null, type: f.type ?? "" });

/**
 * The page the header's agent name links to (`agentHref`): the agent's own,
 * under the platform's base path. None for a picker entry that is not an
 * agent: the General Assistant, a chat-flow.
 */
export function agentPageHref(agent: Pick<XtmAgent, "id">): string | undefined {
  if (agent.id === GENERAL_ASSISTANT_ENTRY || agent.id.startsWith(FLOW_ENTRY_PREFIX)) return undefined;
  return withBasePath(`/agents/${encodeURIComponent(agent.id)}`);
}
