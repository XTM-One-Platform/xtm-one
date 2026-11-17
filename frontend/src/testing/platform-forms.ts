/**
 * The platforms' form descriptions as the server serves them
 * (`GET /custom-dashboards/platform-forms`), read from the fixture
 * `backend/tests/test_dashboard_platform_forms.py` keeps equal to what the
 * adapters describe. Both test runners load it (relative imports only).
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { PlatformFormDescription, PlatformFormsResponse } from "@/types";

// A path, not a `URL`: the DOM runner replaces the global `URL`, which `readFileSync` refuses.
const FIXTURE = resolve(dirname(fileURLToPath(import.meta.url)), "../../../backend/tests/fixtures/dashboards/platform_forms.json");

export const PLATFORM_FORMS: PlatformFormsResponse = JSON.parse(readFileSync(FIXTURE, "utf8"));

/** One platform's description. */
export function platformForm(source: string): PlatformFormDescription {
  const entry = PLATFORM_FORMS.forms.find((item) => item.source === source);
  if (!entry) throw new Error(`No form for ${source}`);
  return entry.form;
}
