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
 * Whether a relay group can carry a resource's requests to its service,
 * read off the destination patterns the screen already has.
 *
 * Two lists decide: the group's (`allowed_destinations`, checked by the
 * platform on the host as written - `destination_allowed` in
 * `backend/app/services/relays.py`, mirrored exactly by `groupAllows`) and each
 * relay's own (`declared_destinations`, checked on the relay AFTER DNS, so a
 * host name against an IP or CIDR pattern cannot be decided here:
 * `relayMayAllow` answers `true` for it). A screen warns only when the answer
 * is certain.
 *
 * Patterns: `*`, `host`, `*.domain`, an IP, a CIDR, each with an optional
 * `:port` (an IPv6 address or CIDR carries its port in brackets).
 */

export interface Destination {
  host: string;
  port: number;
}

/** The host and port a URL reaches, `null` for anything but http(s). */
export function parseDestination(url: string | null | undefined): Destination | null {
  if (!url) return null;
  let parsed: URL;
  try {
    parsed = new URL(url.trim());
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
  if (!host) return null;
  const port = parsed.port ? Number(parsed.port) : parsed.protocol === "https:" ? 443 : 80;
  return { host, port };
}

// The settings an integration names its service's address in, most common first.
const SERVICE_URL_KEYS = ["base_url", "url", "instance_url", "server_url", "api_url"];

/** The service URL an integration's settings name, if any. */
export function serviceUrlOf(extra: Record<string, unknown> | null | undefined): string | null {
  for (const key of SERVICE_URL_KEYS) {
    const value = extra?.[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

interface Pattern {
  anyHost?: boolean;
  /** IPv4 network as [address, prefix length]. */
  v4?: [number, number];
  /** An IPv6 address or network: kept as text, never matched here. */
  v6?: true;
  suffix?: string;
  host?: string;
  port: number | null;
}

function ipv4(text: string): number | null {
  const parts = text.split(".");
  if (parts.length !== 4) return null;
  let value = 0;
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) return null;
    const n = Number(part);
    if (n > 255) return null;
    value = value * 256 + n;
  }
  return value;
}

function splitPort(text: string): [string, number | null] {
  if (text.startsWith("[")) {
    const end = text.indexOf("]");
    if (end < 0) return [text, null];
    const rest = text.slice(end + 1);
    const port = rest.startsWith(":") && /^\d+$/.test(rest.slice(1)) ? Number(rest.slice(1)) : null;
    return [text.slice(1, end), port];
  }
  const colons = text.split(":").length - 1;
  if (colons === 1) {
    const [head, port] = text.split(":");
    return [head, /^\d+$/.test(port) ? Number(port) : null];
  }
  return [text, null];
}

function parsePattern(raw: string): Pattern | null {
  const [host, port] = splitPort(raw.trim().toLowerCase());
  if (!host) return null;
  if (host === "*") return { anyHost: true, port };
  if (host.includes(":")) return { v6: true, port };
  const [address, prefixText] = host.split("/");
  const v4 = ipv4(address);
  if (v4 !== null) {
    const prefix = prefixText === undefined ? 32 : Number(prefixText);
    if (!Number.isInteger(prefix) || prefix < 0 || prefix > 32) return null;
    return { v4: [v4, prefix], port };
  }
  if (prefixText !== undefined) return null;
  if (host.startsWith("*.")) return { suffix: host.slice(2), port };
  if (host.includes("*")) return null;
  return { host, port };
}

function inV4(network: [number, number], address: number): boolean {
  const [base, prefix] = network;
  if (prefix === 0) return true;
  const size = 2 ** (32 - prefix);
  return Math.floor(address / size) === Math.floor(base / size);
}

function hostIsIp(host: string): boolean {
  return host.includes(":") || ipv4(host) !== null;
}

/** The platform's check of the group's list: `null` = no restriction. */
export function groupAllows(patterns: string[] | null | undefined, dest: Destination): boolean {
  if (patterns === null || patterns === undefined) return true;
  const address = ipv4(dest.host);
  for (const raw of patterns) {
    const pattern = parsePattern(raw);
    if (!pattern) continue;
    if (pattern.port !== null && pattern.port !== dest.port) continue;
    if (pattern.anyHost) return true;
    // An IPv6 pattern or host: the platform decides, the browser does not try.
    if (pattern.v6 || dest.host.includes(":")) return true;
    if (pattern.v4) {
      if (address !== null && inV4(pattern.v4, address)) return true;
      continue;
    }
    if (pattern.suffix !== undefined) {
      if (dest.host.endsWith(`.${pattern.suffix}`)) return true;
      continue;
    }
    if (dest.host === pattern.host) return true;
  }
  return false;
}

/**
 * Whether a relay's own list could admit the destination: `false` only when
 * no pattern can match, whatever the host resolves to.
 */
export function relayMayAllow(patterns: string[] | null | undefined, dest: Destination): boolean {
  if (!patterns) return false;
  const address = ipv4(dest.host);
  for (const raw of patterns) {
    const pattern = parsePattern(raw);
    if (!pattern) continue;
    if (pattern.port !== null && pattern.port !== dest.port) continue;
    if (pattern.anyHost || pattern.v6 || dest.host.includes(":")) return true;
    if (pattern.v4) {
      // A name is checked on the addresses it resolves to, on the relay.
      if (address === null || inV4(pattern.v4, address)) return true;
      continue;
    }
    if (hostIsIp(dest.host)) continue;
    if (pattern.suffix !== undefined ? dest.host.endsWith(`.${pattern.suffix}`) : dest.host === pattern.host) {
      return true;
    }
  }
  return false;
}
