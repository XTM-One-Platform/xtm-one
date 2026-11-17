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

/* globals Office */

import { getPlatformConfig } from "@/lib/api";

Office.onReady(() => {
  // FunctionFile placeholder — the manifest uses <Action xsi:type="ShowTaskpane">
  // so no explicit function registration is needed for the basic ribbon button.
});

// Resolve the admin-configured platform name and replace the brand-neutral
// static ``<title>`` from commands.html so the Office command-page browser
// tab matches the main task pane (same pattern App.tsx uses for index.html).
// Failures are silently swallowed — the neutral fallback stays in place.
if (typeof document !== "undefined") {
  getPlatformConfig()
    .then((cfg) => {
      if (cfg?.platform_name) {
        document.title = cfg.platform_name;
      }
    })
    .catch(() => {
      /* keep the brand-neutral static title */
    });
}
