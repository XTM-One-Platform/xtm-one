import { readFileSync } from "node:fs";
import path from "node:path";

/**
 * The design-system tokens the app's stylesheets name, as one `:root` block a
 * test document can load.
 *
 * The deck palette (`presentation.css`) names the app's tokens rather than
 * literals - `--deck-success: var(--color-feedback-success-primary)` - and
 * those are declared by the library's token sheet and the app's `index.css`,
 * which a component test does not load. Without them every tone resolves to
 * the same nothing and no colour can be told from another. The browser loads
 * them through Tailwind's `@theme`, a block happy-dom does not read, so the
 * declarations are lifted out of whatever block holds them, the dark theme's
 * winning as it does in the app by default.
 */
export function designTokenStylesheet(): string {
  const library = readFileSync(
    path.resolve(__dirname, "../../node_modules/@filigran/design-system/dist/tokens/theme.css"),
    "utf8",
  );
  // The one token the app adds on top: the accent seam (see index.css).
  const app = "--color-accent: var(--color-filigran-brand-primary);";
  const declarations = new Map<string, string>();
  for (const match of `${library}\n${app}`.matchAll(/(--[\w-]+)\s*:\s*([^;{}]+);/g)) {
    declarations.set(match[1], match[2].trim());
  }
  const body = [...declarations].map(([name, value]) => `  ${name}: ${value};`).join("\n");
  return `:root {\n${body}\n}\n`;
}
