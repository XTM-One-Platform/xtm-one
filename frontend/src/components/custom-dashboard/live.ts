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
 * Which stored dashboard the widgets on screen belong to, so a live platform
 * widget can ask the server for its data (`useWidgetData`). Without it - a
 * dashboard drawn from a spec that is not stored, a test - a platform widget
 * says what it is and draws nothing.
 */
import { createContext, useContext } from "react";

import type { WidgetDataRange } from "@/types";

import type { DashboardInput } from "./inputs";
import type { TokenValue } from "./tokens";

export interface DashboardLive {
  dashboardId: string;
  /** A range the viewer picked for every live widget; unset, each widget's own. */
  range?: WidgetDataRange;
  /** The definition's `hideOpenInPlatform`: no "Open in OpenCTI" link on its widgets. */
  hideOpenLinks?: boolean;
  /** The definition's `hideExport`: no CSV export of a visualization. */
  hideExport?: boolean;
  /** The definition's data sources, as stored: what Inspect shows. */
  sources?: Record<string, unknown>;
  /** Keep an input's value as its default, saved with the dashboard: offered to those who may edit it. */
  keepInput?: (input: DashboardInput, value: TokenValue) => void;
}

export const DashboardLiveContext = createContext<DashboardLive | null>(null);

export function useDashboardLive(): DashboardLive | null {
  return useContext(DashboardLiveContext);
}
