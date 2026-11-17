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
 * The right end of the Agentic Flow page header, offered to the active tab.
 * A tab with page-level actions (the Fleets tab: "New fleet",
 * "Create with a prompt") renders them there through a portal, as the
 * Presentations page carries its actions beside its title, so the tab's own
 * toolbar keeps its row for the search and the filters.
 */

import { createContext, useContext, type ReactNode } from "react";
import { createPortal } from "react-dom";

export const FlowsHeaderSlotContext = createContext<HTMLElement | null>(null);

/** Renders its children in the page header when the page offers the slot, else in place. */
export function FlowsHeaderActions({ children }: { children: ReactNode }) {
  const slot = useContext(FlowsHeaderSlotContext);
  return slot ? createPortal(children, slot) : <>{children}</>;
}
