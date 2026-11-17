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

import type { AuthResponse, PlatformConfig, User } from "./types";

const FRONTEND_TOKEN_KEY = "copilot_token";
const ADDIN_TOKEN_KEY = "filigran_copilot_token";
const BASE_URL_KEY = "filigran_copilot_url";

const MAX_RETRIES = 3;
const RETRY_BASE_MS = 500;
const REQUEST_TIMEOUT_MS = 30_000;

// The platform serving this page under /office/, as origin + base path (empty
// at the root, "/xtm-one" under BASE_PATH); null on a dev server.
function hostedBase(): string | null {
  const { origin, pathname } = window.location;
  const at = pathname.search(/\/office(\/|$)/);
  return at === -1 ? null : origin + pathname.slice(0, at);
}

function isSameOrigin(): boolean {
  return hostedBase() !== null;
}

// ---------------------------------------------------------------------------
// Token & base-URL persistence
// ---------------------------------------------------------------------------

export function getToken(): string | null {
  if (isSameOrigin()) {
    const frontendToken = localStorage.getItem(FRONTEND_TOKEN_KEY);
    if (frontendToken) return frontendToken;
  }
  return (
    localStorage.getItem(ADDIN_TOKEN_KEY) ||
    localStorage.getItem(FRONTEND_TOKEN_KEY)
  );
}

function setToken(token: string) {
  localStorage.setItem(ADDIN_TOKEN_KEY, token);
  if (isSameOrigin()) {
    localStorage.setItem(FRONTEND_TOKEN_KEY, token);
  }
}

export function clearToken() {
  localStorage.removeItem(ADDIN_TOKEN_KEY);
}

export function getBaseUrl(): string {
  const stored = localStorage.getItem(BASE_URL_KEY);
  if (stored) return stored;
  // Without a stored URL: the platform that serves the page, else the page's
  // own origin, never a fixed instance.
  return hostedBase() ?? window.location.origin;
}

export function setBaseUrl(url: string) {
  const normalized = url.replace(/\/+$/, "");
  localStorage.setItem(BASE_URL_KEY, normalized);
}

// ---------------------------------------------------------------------------
// Retry helper – retries on 5xx / network errors with exponential backoff
// ---------------------------------------------------------------------------

function isRetryable(status: number): boolean {
  return status >= 500 && status < 600;
}

async function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

// ---------------------------------------------------------------------------
// Core request function
// ---------------------------------------------------------------------------

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const token = getToken();
  if (!token) throw new Error("Not authenticated");

  let lastError: Error | null = null;

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

    try {
      const res = await fetch(`${getBaseUrl()}/api/v1${path}`, {
        method,
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });

      clearTimeout(timeout);

      if (res.status === 401) {
        clearToken();
        throw new Error("Session expired");
      }

      if (isRetryable(res.status) && attempt < MAX_RETRIES) {
        lastError = new Error(`HTTP ${res.status}`);
        await sleep(RETRY_BASE_MS * 2 ** attempt);
        continue;
      }

      if (!res.ok) {
        const text = await res.text().catch(() => "Unknown error");
        throw new Error(`HTTP ${res.status}: ${text}`);
      }

      return res.json();
    } catch (err: unknown) {
      clearTimeout(timeout);

      if (err instanceof DOMException && err.name === "AbortError") {
        lastError = new Error("Request timed out");
      } else if (err instanceof TypeError) {
        lastError = new Error(`Network error: ${err.message}`);
      } else if (err instanceof Error) {
        if (
          err.message === "Session expired" ||
          err.message.startsWith("HTTP ")
        ) {
          throw err;
        }
        lastError = err;
      } else {
        throw err;
      }

      if (attempt < MAX_RETRIES) {
        await sleep(RETRY_BASE_MS * 2 ** attempt);
        continue;
      }
    }
  }

  throw lastError ?? new Error("Request failed after retries");
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

/**
 * Fetch the public platform configuration (no auth required).
 *
 * The Office add-in renders product branding ("Filigran Copilot" vs
 * "XTM One" vs admin-customised `platform_name`) before the user has
 * signed in, so this matches the unauthenticated `/api/v1/platform/config`
 * endpoint the main frontend uses for the same purpose.  If
 * ``baseUrlOverride`` is omitted the persisted/derived
 * :func:`getBaseUrl` value is used.
 */
export async function getPlatformConfig(
  baseUrlOverride?: string,
): Promise<PlatformConfig> {
  const base = baseUrlOverride ?? getBaseUrl();
  const res = await fetch(`${base}/api/v1/platform/config`);
  if (!res.ok) throw new Error(`Platform config failed (${res.status})`);
  return res.json();
}

export async function login(
  email: string,
  password: string,
  baseUrl: string,
): Promise<AuthResponse> {
  setBaseUrl(baseUrl);
  const res = await fetch(`${getBaseUrl()}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });

  if (!res.ok) {
    if (res.status === 401) throw new Error("Invalid credentials");
    throw new Error(`Login failed (${res.status})`);
  }

  const data: AuthResponse = await res.json();
  // A two-factor account gets a 200 with no `access_token` — the
  // session only exists after a code is verified, and this add-in has no code
  // step yet. Say so instead of storing `undefined` and leaving the user with
  // a signed-in-looking pane that 401s on every call.
  if (data.mfa_required || !data.access_token) {
    throw new Error(
      "This account uses two-factor authentication, which this add-in does not support yet. Sign in with an API key instead.",
    );
  }
  setToken(data.access_token);
  return data;
}

export async function getMe(): Promise<User> {
  return request("GET", "/auth/me");
}
