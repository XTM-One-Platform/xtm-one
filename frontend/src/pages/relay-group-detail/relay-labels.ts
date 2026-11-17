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
 * What the relay pages say about a relay's error codes, platforms and request
 * sources. The codes mirror `RELAY_ERROR_MESSAGES` in
 * `backend/app/services/relay_transport.py`; an unknown code is shown as is.
 */
import { withBasePath } from "@/lib/base-path";
import { RUNNER_PLATFORM_CONFIG } from "@/lib/constants";
import type { Relay, RelayRequestLog } from "@/types";

const DOCKER_LOGO = withBasePath("/logos/docker.svg");

/**
 * The brand mark of where a relay runs: an install target of the enroll
 * dialog, or `relayPlatformKey()` of an enrolled relay. The OS marks are the
 * XTM One Desktop page's.
 */
export const RELAY_LOGOS: Record<string, string> = {
  linux: RUNNER_PLATFORM_CONFIG.linux.logo,
  macos: RUNNER_PLATFORM_CONFIG.macos.logo,
  windows: RUNNER_PLATFORM_CONFIG.windows.logo,
  docker: DOCKER_LOGO,
  kubernetes: withBasePath("/logos/kubernetes.svg"),
  container: DOCKER_LOGO,
};

/**
 * Why a relayed request failed, and what to do about it.
 * @i18n-keys
 */
const RELAY_ERROR_LABELS = {
  destination_denied: "The relay does not allow this destination. Add it to the relay's local list (and to the group's, when the group has one).",
  dns_failed: "The relay could not resolve the host name. Check the name from the relay's machine.",
  connect_failed: "The relay could not connect to the destination. Check the port and the firewall between them.",
  tls_failed: "The TLS handshake failed. Add the internal CA to the relay's ca_bundle, or list the host in insecure_tls_destinations.",
  timeout: "The request timed out between the relay and the destination.",
  too_large: "The request or the response is larger than a relay carries.",
  no_relay_online: "No relay of this group is online. Its requests fail until one connects.",
  relay_disconnected: "The relay disconnected before the request finished.",
  protocol_error: "The relay answered something the platform could not read. Update the relay.",
} as const;

export function relayErrorLabel(code: string | null | undefined): string | null {
  if (!code) return null;
  return (RELAY_ERROR_LABELS as Record<string, string>)[code] ?? null;
}

/**
 * The operating system a relay runs on.
 * @i18n-keys
 */
const RELAY_PLATFORM_LABELS = {
  linux: "Linux",
  macos: "macOS",
  windows: "Windows",
  container: "Container",
} as const;

/** Where an enrolled relay runs: a relay in a container enrolls as `linux` and says so in its host facts. */
export function relayPlatformKey(relay: Pick<Relay, "platform" | "host_facts">): string {
  return relay.host_facts?.container === true ? "container" : relay.platform;
}

export function relayPlatformLabel(platform: string | null | undefined): string {
  if (!platform) return "";
  return (RELAY_PLATFORM_LABELS as Record<string, string>)[platform] ?? platform;
}

/**
 * Where a relayed request came from.
 * @i18n-keys
 */
const RELAY_SOURCE_LABELS = {
  integration: "Integration",
  mcp_server: "MCP server",
  flow: "Flow",
  test: "Test",
} as const;

export function relaySourceLabel(source: string | null | undefined): string {
  if (!source) return "";
  return (RELAY_SOURCE_LABELS as Record<string, string>)[source] ?? source;
}

/**
 * The outcome filter of the Activity tab.
 * @i18n-keys
 */
export const RELAY_OUTCOME_LABELS = { ok: "Succeeded", error: "Failed" } as const;
export type RelayOutcome = keyof typeof RELAY_OUTCOME_LABELS;

/** A relayed request failed: an error code, or a 5xx answer from the destination. */
export function relayRequestFailed(entry: Pick<RelayRequestLog, "error_code" | "status_code">): boolean {
  return !!entry.error_code || (entry.status_code ?? 0) >= 500;
}

/** The API's outcome: one value selected narrows the log, none or both is every request. */
export function relayOutcomeParam(selected: readonly string[]): RelayOutcome | undefined {
  const known = selected.filter((value): value is RelayOutcome => value in RELAY_OUTCOME_LABELS);
  return known.length === 1 ? known[0] : undefined;
}

/** A byte count as a person reads it. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 1024) return `${Math.max(0, Math.round(bytes || 0))} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}
