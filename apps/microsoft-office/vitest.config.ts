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

import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

// The pane's component tests (`npm run check`): the task pane in jsdom, Office.js and fetch faked.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
    // A linked `@filigran/chatbot` would otherwise bring its own checkout's React.
    dedupe: ["react", "react-dom", "react-markdown", "remark-gfm", "remark-breaks"],
  },
  // agentIcons.test.ts reads the web app's frontend/src/lib/constants.ts (`?raw`) for its parity check.
  server: {
    fs: {
      allow: [path.resolve(import.meta.dirname, "."), path.resolve(import.meta.dirname, "../../frontend/src/lib")],
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
