/**
 * Tool names and descriptions the user sets on OpenAPI operations.
 *
 * Editing keeps the raw text (so typing is not trimmed away); the payload
 * sent to the backend is trimmed, and an operation with neither a name nor a
 * description falls back to its generated tool. Kept out of the component
 * files (Fast Refresh: component modules export components only).
 */
import type { OpenAPIOperation, OpenAPIToolOverride, OpenAPIToolOverrides } from "@/types";

/** *overrides* with *patch* applied to one operation (dropped when both fields are empty). */
export function withOverride(
  overrides: OpenAPIToolOverrides,
  key: string,
  patch: OpenAPIToolOverride,
): OpenAPIToolOverrides {
  const merged = { ...overrides[key], ...patch };
  const next = { ...overrides };
  if (!merged.name?.trim() && !merged.description?.trim()) delete next[key];
  else next[key] = merged;
  return next;
}

/** The overrides as the API takes them: trimmed, empty fields left out. */
export function overridesPayload(overrides: OpenAPIToolOverrides): OpenAPIToolOverrides {
  const out: OpenAPIToolOverrides = {};
  for (const [key, value] of Object.entries(overrides)) {
    const name = value.name?.trim();
    const description = value.description?.trim();
    if (name || description) out[key] = { ...(name ? { name } : {}), ...(description ? { description } : {}) };
  }
  return out;
}

/** The overrides a stored spec carries: wherever the tool differs from the generated one. */
export function overridesFromOperations(operations: OpenAPIOperation[]): OpenAPIToolOverrides {
  const out: OpenAPIToolOverrides = {};
  for (const op of operations) {
    const name = op.tool_name && op.tool_name !== op.default_tool_name ? op.tool_name : undefined;
    const description =
      op.tool_description && op.tool_description !== op.default_tool_description ? op.tool_description : undefined;
    if (name || description) out[op.key] = { ...(name ? { name } : {}), ...(description ? { description } : {}) };
  }
  return out;
}

/** Whether two sets of overrides send the same payload. */
export function sameOverrides(a: OpenAPIToolOverrides, b: OpenAPIToolOverrides): boolean {
  const canonical = (value: OpenAPIToolOverrides) =>
    JSON.stringify(
      Object.entries(overridesPayload(value))
        .sort(([x], [y]) => x.localeCompare(y))
        .map(([key, entry]) => [key, entry.name ?? "", entry.description ?? ""]),
    );
  return canonical(a) === canonical(b);
}
