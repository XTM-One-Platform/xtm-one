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

/**
 * The person's words for a validator refusal of inline markup - the
 * inspector and the stage show them before the save relays a 422.
 */
import { useFormatter } from "@/i18n";

import type { MarkupProblem } from "./markup";

export const TEXTAREA_CLASS =
  "flex w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-compact " +
  "placeholder:text-default-secondary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus";

export function useMarkupProblemText(): (problem: MarkupProblem | null) => string | null {
  const { t_i18n } = useFormatter();
  return (problem) => {
    if (!problem) return null;
    switch (problem.kind) {
      case "html":
        return t_i18n("HTML is not allowed here - use **bold**, _emphasis_, `code` or [[tone:text]].");
      case "unbalanced":
        if (problem.marker === "](#/") {
          return t_i18n("A slide link is [text](#/slide id): it needs a label and the id of a slide of this deck.");
        }
        return t_i18n("Unbalanced {marker}: every ** and ` must be closed, and [[tone:text]] needs both brackets.", {
          marker: problem.marker,
        });
      case "nested":
        return t_i18n("Markup cannot be nested: the {construct} contains another marker or a line break.", {
          construct: problem.construct,
        });
      case "unknown_tone":
        return t_i18n("Unknown tone {tone}: use accent, info, success, warning, error, muted or gradient.", {
          tone: problem.tone,
        });
      default:
        return null;
    }
  };
}
