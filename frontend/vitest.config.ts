import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";
import { nodeTestFiles } from "./scripts/node-test-files.mjs";

/**
 * Vitest's half of `yarn test:unit` (Node's own runner takes the other half,
 * see scripts/node-test-files.mjs). Deliberately separate from
 * `vite.config.ts`: the dev server config carries a proxy table and a port that
 * a test run has no use for.
 *
 * Two projects, because two suites with different needs share the runner:
 *
 *   - `components`: the `src/**\/*.test.tsx` files, rendered in happy-dom with
 *     Testing Library;
 *   - `unit`: `tests_unit/` and the `src/**\/*.test.ts` files that are not
 *     node-native, in jsdom (they read the design system's stylesheets and
 *     forwarded DOM attributes the way a real document does).
 *
 * `tests_e2e/` is in neither: those are Playwright specs, driving a real
 * browser against a running stack. The node-native suite is DISCOVERED and
 * excluded from both, never listed: no glob separates the two runners' files,
 * and naming the paths went stale the first time a second one landed.
 */
const excludeNodeNative = ["**/node_modules/**", "**/dist/**", ...nodeTestFiles(__dirname)];

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    projects: [
      {
        extends: true,
        test: {
          name: "components",
          include: ["src/**/*.test.tsx"],
          exclude: excludeNodeNative,
          environment: "happy-dom",
          environmentOptions: { happyDOM: { url: "http://localhost:3100/" } },
          setupFiles: ["./vitest.setup.ts"],
          restoreMocks: true,
        },
      },
      {
        extends: true,
        test: {
          name: "unit",
          include: ["tests_unit/**/*.test.{ts,tsx}", "src/**/*.test.ts"],
          exclude: excludeNodeNative,
          environment: "jsdom",
          setupFiles: ["./tests_unit/setup.ts"],
        },
      },
    ],
  },
});
