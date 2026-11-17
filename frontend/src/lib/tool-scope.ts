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
 * Keys of the integration tool allow-lists (`integration_allowed_tools` on
 * agents and assignments).
 *
 * A key normally names a provider: every instance of a provider exposes the
 * same tools, so one list scopes them all. Every OpenAPI integration, though,
 * shares the `openapi` provider while exposing its own API. Its tools are
 * scoped under a per-integration key, `openapi@<integration id>`, so scoping
 * one API never hides or reveals another. The runtime
 * (`filter_integration_tools`) applies a provider list and a per-integration
 * list together: a tool must pass both.
 *
 * No `@/` imports: `yarn test:unit` loads this module directly.
 */

/** Providers whose every integration is a different API with its own tools. */
const PER_INTEGRATION_API_PROVIDERS: readonly string[] = ["openapi"];

/** Mirrors the backend's `INSTANCE_SCOPE_SEP`. */
const INSTANCE_SCOPE_SEP = "@";

type ToolAllowMap = Record<string, string[] | null>;

interface ScopedIntegration {
  id: string;
  provider: string;
}

/** Whether each integration of *provider* is its own API (OpenAPI). */
export function isPerIntegrationApiProvider(provider: string): boolean {
  return PER_INTEGRATION_API_PROVIDERS.includes(provider);
}

/** The allow-list key that the tool-scope editor reads and writes for *integration*. */
export function toolScopeKey(integration: ScopedIntegration): string {
  return isPerIntegrationApiProvider(integration.provider)
    ? `${integration.provider}${INSTANCE_SCOPE_SEP}${integration.id}`
    : integration.provider;
}

/**
 * The tool names the runtime lets through for *integration*, or null when
 * nothing restricts it: its provider list and its per-integration list,
 * intersected.
 */
export function effectiveAllowedTools(
  allowed: ToolAllowMap | null | undefined,
  integration: ScopedIntegration,
): string[] | null {
  const lists = [
    allowed?.[integration.provider],
    allowed?.[`${integration.provider}${INSTANCE_SCOPE_SEP}${integration.id}`],
  ].filter((list): list is string[] => Array.isArray(list));
  if (lists.length === 0) return null;
  return lists.reduce((kept, list) => kept.filter((name) => list.includes(name)));
}

/**
 * Whether an allow-list entry still applies: its provider is bound and, for a
 * per-integration key, that integration is bound.
 */
export function isToolScopeKeyActive(
  key: string,
  activeProviders: ReadonlySet<string>,
  boundIntegrationIds: ReadonlySet<string>,
): boolean {
  const at = key.indexOf(INSTANCE_SCOPE_SEP);
  if (at < 0) return activeProviders.has(key);
  return activeProviders.has(key.slice(0, at)) && boundIntegrationIds.has(key.slice(at + 1));
}
