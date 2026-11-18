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

import type { A2AExchange } from "@/types";

type Translate = (id: string, values?: Record<string, string | number>) => string;

/**
 * What the Result card says of an external run with no result of its own because
 * it asked its caller a question: "No result produced" read as a run that failed
 * to answer, when its A2A task was waiting on the calling agent. Null otherwise.
 */
export function a2aNoResultText(exchange: A2AExchange | null | undefined, t_i18n: Translate): string | null {
  switch (exchange?.asked?.state) {
    case "waiting":
      return t_i18n("No result yet: the task is waiting for its caller to answer the question above.");
    case "answered":
      return t_i18n("No result in this run: it asked its caller a question, and the answer continued the task in another run.");
    case "withdrawn":
      return t_i18n("No result: the task was cancelled before its caller answered.");
    default:
      return null;
  }
}
