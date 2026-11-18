import { defineConfig, type Plugin, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

/**
 * Ends the browser's response when the backend's ends mid-way (the backend
 * restarts under a stream). The proxy pipes the backend's response and ends
 * the browser's only on its end, so a chat stream cut by a restart stayed
 * open, silent, instead of failing as it does behind any other proxy.
 */
const endWithBackend: ProxyOptions["configure"] = (proxy) => {
  proxy.on("proxyRes", (backendResponse, _request, response) => {
    backendResponse.on("close", () => {
      if (!backendResponse.complete) response.destroy();
    });
  });
};

/**
 * Lab only (#63): with VITE_LAB_PAGE_BUILD_ID set (the xtm-one lab under
 * LAB_STALE_BUILD=on), the dev server writes it in `<meta name="build-id">`,
 * which `src/lib/build-version.ts` reads; the lab's backend reports another
 * build on /auth/config, so the stale build notice renders natively. Never in
 * a build (`apply: "serve"`): production's meta is written by the backend.
 */
const labPageBuildId = (): Plugin => ({
  name: "lab-page-build-id",
  apply: "serve",
  transformIndexHtml: (html) => {
    const buildId = process.env.VITE_LAB_PAGE_BUILD_ID;
    if (!buildId || !/^[0-9a-f]+$/.test(buildId)) return html;
    return html.replace("</head>", `  <meta name="build-id" content="${buildId}" />\n  </head>`);
  },
});

export default defineConfig({
  plugins: [react(), labPageBuildId()],
  // Relative base: the bundle's OWN references - code-split chunks,
  // fonts, images, CSS `url()` - resolve from the bundle's location, so the
  // same build serves at `/` and under a reverse-proxy path prefix
  // (`BASE_PATH=/xtm-one`). `index.html` is the one file that must not stay
  // relative (it is served for every SPA route); the backend renderer makes
  // its URLs absolute under the prefix and writes `<meta name="base-path">`,
  // which `src/lib/base-path.ts` reads. In dev Vite ignores a relative base.
  base: "./",
  logLevel: (process.env.VITE_LOG_LEVEL as "info" | "warn" | "error" | "silent") || "info",
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: parseInt(process.env.PORT ?? "3100", 10),
    host: true,
    // "frontend": the backend's headless browser loads the render page by the
    // dev compose service name (PRESENTATION_RENDER_BASE_URL).
    allowedHosts: ["frontend"],
    proxy: {
      "/api": {
        target: process.env.VITE_API_URL ?? "http://localhost:8100",
        changeOrigin: true,
        configure: endWithBackend,
        ws: true,
        timeout: 600_000,
      },
      "/metrics": {
        target: process.env.VITE_API_URL ?? "http://localhost:8100",
        changeOrigin: true,
        configure: endWithBackend,
        ws: true,
        timeout: 600_000,
      },
      "/v1": {
        target: process.env.VITE_API_URL ?? "http://localhost:8100",
        changeOrigin: true,
        configure: endWithBackend,
        timeout: 600_000,
      },
      // Anchored so it matches the MCP endpoints (`/mcp`, `/mcp/oauth/...`)
      // WITHOUT also swallowing the SPA's own `/mcp-servers/*` routes — a bare
      // "/mcp" key is a prefix match, which made every hard load or OAuth
      // redirect into the MCP Servers section render the backend's 404 JSON.
      "^/mcp(/|$)": {
        target: process.env.VITE_API_URL ?? "http://localhost:8100",
        changeOrigin: true,
        configure: endWithBackend,
        timeout: 600_000,
      },
      // A2A agent cards (`/a2a/<token>/.well-known/agent-card.json`) on the
      // preview origin (#112). The browser's Host is kept (no changeOrigin):
      // the backend's external_base then writes the card's own URLs (tokenUrl)
      // on the origin the browser used, not on the unreachable backend port.
      "^/a2a/": {
        target: process.env.VITE_API_URL ?? "http://localhost:8100",
        configure: endWithBackend,
      },
      "/xtm/auth": {
        target: process.env.VITE_API_URL ?? "http://localhost:8100",
        changeOrigin: true,
        configure: endWithBackend,
        timeout: 600_000,
      },
      // The Office add-in task pane the backend serves (the lab's build too, #11109).
      "/office": {
        target: process.env.VITE_API_URL ?? "http://localhost:8100",
        changeOrigin: true,
        configure: endWithBackend,
      },
      // Dev only: an OpenCTI served under /opencti (its app:base_path) on the same
      // origin, so CTEM deep links open in the reviewer's browser (the CTEM lab,
      // #12058). VITE_OPENCTI_TOKEN signs every request in as that OpenCTI's user.
      ...(process.env.VITE_OPENCTI_URL
        ? {
            "/opencti": {
              target: process.env.VITE_OPENCTI_URL,
              changeOrigin: true,
              ws: true,
              headers: process.env.VITE_OPENCTI_TOKEN
                ? { Authorization: `Bearer ${process.env.VITE_OPENCTI_TOKEN}` }
                : undefined,
            },
          }
        : {}),
    },
  },
});
