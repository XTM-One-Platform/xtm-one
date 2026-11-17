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
 * The result a data visualization drew, read from the query cache and never
 * fetched from here: what its frame shows of it (the module's title style,
 * the link into its platform) and what its menu inspects or exports.
 */
import { useMemo } from "react";
import { skipToken, useQuery } from "@tanstack/react-query";

import { widgetDataKey } from "@/hooks/api/useCustomDashboardApi";
import type { WidgetData } from "@/types";

import { presentedOptions } from "./definition";
import { useDashboardLive } from "./live";
import type { DashboardWidget } from "./spec";
import { usePageTokens } from "./token-state";
import { tokensFor } from "./tokens";

type DataWidget = Extract<DashboardWidget, { kind: "platform" | "data" }>;

/**
 * The headers a module titles a widget with in place of its own title, written in the reader's language.
 * @i18n-keys
 */
const HEADER_TITLES = {
  "Entities number": "Entities number",
  "Relationships number": "Relationships number",
  "Audits number": "Audits number",
} as const;

/** A header title in the reader's language when it is one of the modules', else as it is. */
export function headerTitleKey(title: string): string | undefined {
  return Object.prototype.hasOwnProperty.call(HEADER_TITLES, title) ? HEADER_TITLES[title as keyof typeof HEADER_TITLES] : undefined;
}

export function useDrawnResult(widget: DashboardWidget): WidgetData | undefined {
  const live = useDashboardLive();
  const pageTokens = usePageTokens();
  const drawn = widget.kind === "platform" || widget.kind === "data" ? (widget as DataWidget) : undefined;
  const used = useMemo(() => (drawn ? tokensFor(drawn.chain, pageTokens) : {}), [drawn, pageTokens]);
  const { data } = useQuery<WidgetData>({
    queryKey: widgetDataKey(live?.dashboardId, drawn?.id ?? widget.id, live?.range, drawn?.fingerprint ?? "", used),
    queryFn: skipToken,
    enabled: Boolean(drawn && live),
  });
  return drawn ? data : undefined;
}

/** The frame's options of a widget (its title's style and text, whether "Expand" stays shown): its own, under its
 *  module's look when it draws with that module's view. */
export function frameTitleOptions(
  widget: DashboardWidget,
  result: WidgetData | undefined,
): { titleStyle?: string; headerTitle?: string; expandShown?: boolean } {
  if (widget.kind !== "platform" && widget.kind !== "data") return {};
  const options = presentedOptions(widget.options, widget.view, result?.status === "ok" ? result.presentation : undefined) ?? {};
  const titleStyle = options.titleStyle === "capitalize" || options.titleStyle === "caps" ? options.titleStyle : undefined;
  const headerTitle = typeof options.headerTitle === "string" && options.headerTitle.trim() ? options.headerTitle : undefined;
  return { titleStyle, headerTitle, expandShown: options.expandShown === true };
}
