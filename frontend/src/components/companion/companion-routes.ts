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
 * The mobile companion: what a phone shows of XTM One.
 *
 * On a phone (`isPhoneDevice`) XTM One is not the whole platform squeezed onto a small screen; it
 * is a companion to it, with three tabs: Today (the platform's home
 * dashboard), Inbox (their notifications, approvals answered in place) and Ask
 * (the assistant). Every other page of
 * the platform is built for a computer, so on a phone it says so instead of
 * being drawn half-usable (`CompanionShell`).
 *
 * Pure module: `companion-routes.test.ts` runs it on `node --test`.
 */

/** What the browser says about the device it runs on. */
export interface DeviceSignals {
  /** `navigator.userAgentData.mobile` (User-Agent Client Hints, Chromium
   *  browsers): the browser's own answer, `undefined` where it is not exposed
   *  (Safari, Firefox). */
  clientHintMobile: boolean | undefined;
  userAgent: string;
}

// The user agent tokens of a phone: an iPhone or iPod, an Android PHONE
// (Android tablets leave out "Mobile"), and the other phone platforms.
const PHONE_UA_RE = /iPhone|iPod|Android.+Mobile|Windows Phone|IEMobile|BlackBerry|BB10|webOS|Opera Mini|Mobile.+Firefox/i;

/**
 * Whether the device is a phone: then the app is the companion.
 *
 * Decided by the DEVICE, never by the width of the screen: a phone held in
 * landscape, or one whose browser reports a desktop-class viewport, is still a
 * phone, and a narrow desktop window is still a computer. Either signal is
 * enough: the browser's own Client Hint, or, where it is not exposed or not
 * set (Safari, Firefox, an embedded browser), the user agent's phone tokens.
 * On a real phone the two agree. A tablet is not a phone: it gets the full
 * platform.
 */
export function isPhoneDevice({ clientHintMobile, userAgent }: DeviceSignals): boolean {
  return clientHintMobile === true || PHONE_UA_RE.test(userAgent);
}

/** A screen of the companion. */
export type CompanionView = "today" | "inbox" | "ask" | "share";

/** The companion screen `pathname` (router-relative, without the base path)
 *  names, or `null` for a page the companion does not offer. */
export function companionView(pathname: string): CompanionView | null {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
  if (path === "" || path === "/") return "today";
  if (path === "/chat") return "ask";
  if (path === "/inbox") return "inbox";
  if (path === "/share") return "share";
  return null;
}

/** The tab a companion screen belongs to (a share lands in Ask). */
export function companionTab(view: CompanionView): "today" | "inbox" | "ask" {
  return view === "share" ? "ask" : view;
}
