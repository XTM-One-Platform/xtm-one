// Every icon a tile names - a platform's widget types (the forms the server describes) and the data source
// types - is one the icon catalog draws (`DeckIcon`, Lucide's `icons`): an unknown name draws a blank tile.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { icons } from "lucide-react";

const pascal = (name: string) => name.replace(/(^|-)([a-z0-9])/g, (_m, _dash, c: string) => c.toUpperCase());

function formIcons(value: unknown, found: string[] = []): string[] {
  if (Array.isArray(value)) value.forEach((item) => formIcons(item, found));
  else if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    if (typeof record.icon === "string" && typeof record.value === "string") found.push(record.icon);
    Object.values(record).forEach((item) => formIcons(item, found));
  }
  return found;
}

test("every tile's icon is in the catalog", () => {
  const forms = JSON.parse(readFileSync(new URL("../../../../backend/tests/fixtures/dashboards/platform_forms.json", import.meta.url), "utf8"));
  const panel = readFileSync(new URL("./DashboardDataSourcesPanel.tsx", import.meta.url), "utf8");
  const typeIcons = [...(panel.match(/const TYPE_ICONS[^}]+}/)?.[0] ?? "").matchAll(/: "([a-z0-9-]+)"/g)].map((m) => m[1]);
  const named = [...new Set([...formIcons(forms), ...typeIcons])];
  assert.ok(named.length > 20);
  assert.deepEqual(named.filter((name) => !(pascal(name) in icons)), []);
});
