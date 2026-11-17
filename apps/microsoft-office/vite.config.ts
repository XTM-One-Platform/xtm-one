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

import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import fs from "fs";
import os from "os";

const isProd = process.env.NODE_ENV === "production";

const certDir = path.join(os.homedir(), ".office-addin-dev-certs");
const certFile = path.join(certDir, "localhost.crt");
const keyFile = path.join(certDir, "localhost.key");
const hasOfficeCerts = fs.existsSync(certFile) && fs.existsSync(keyFile);

export default defineConfig({
  base: isProd ? "/office/" : "/",
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 3100,
    https: hasOfficeCerts
      ? { cert: fs.readFileSync(certFile), key: fs.readFileSync(keyFile) }
      : undefined,
    headers: {
      "Access-Control-Allow-Origin": "*",
    },
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        timeout: 600_000,
      },
    },
  },
  build: {
    outDir: "dist",
    rollupOptions: {
      input: {
        taskpane: path.resolve(__dirname, "index.html"),
        commands: path.resolve(__dirname, "commands.html"),
      },
    },
  },
});
