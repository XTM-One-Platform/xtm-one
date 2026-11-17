/**
 * Guard (UX round 1 of #9991, B2): the MCP server create form and the
 * Configuration edit view render the Transport and Authentication pickers
 * from one shared component (ConnectionPickers.tsx), so their labels and
 * height cannot drift again. No page keeps its own transport or auth button.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (file: string) => readFileSync(join(HERE, file), "utf8");

describe.each(["NewServerForm.tsx", "ConfigTab.tsx"])("%s", (file) => {
  const source = read(file);

  it("imports the shared Transport and Authentication pickers", () => {
    expect(source).toMatch(/import \{ AuthModePicker, TransportPicker \} from "\.\/ConnectionPickers";/);
    expect(source).toContain("<TransportPicker");
    expect(source).toContain("<AuthModePicker");
  });

  it("keeps no local transport or auth mode button", () => {
    expect(source).not.toMatch(/onClick=\{\(\) => set(Edit)?Transport\(/);
    expect(source).not.toMatch(/onClick=\{\(\) => set(Edit)?AuthMode\(/);
  });
});

describe("ConnectionPickers", () => {
  const source = read("ConnectionPickers.tsx");

  it("uses the translated transport labels and the default 36 px button", () => {
    expect(source).toContain('t_i18n("Subprocess (stdio)")');
    expect(source).toContain('t_i18n("SSE (legacy)")');
    expect(source).not.toMatch(/size="sm"|\bh-10\b|\bh-8\b/);
  });
});
