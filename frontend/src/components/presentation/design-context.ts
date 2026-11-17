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
 * The template's design tokens for the layouts that change their DOM,
 * not only their style, when a template opts into a composition:
 * the agenda's column rule and the section slide's footer. The
 * root attributes (`designRootAttributes`) carry everything a stylesheet can
 * decide; this context carries the rest. `null` (the default) is today's
 * rendering.
 */
import { createContext, useContext } from "react";

import type { TemplateDesign } from "@/types";

export const DeckDesignContext = createContext<TemplateDesign | null>(null);

/** The deck's template design, `null` when the template declares none. */
export function useDeckDesign(): TemplateDesign | null {
  return useContext(DeckDesignContext);
}
