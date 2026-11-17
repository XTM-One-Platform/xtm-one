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
 * The page's token state (`useTokenState`) and its context: what the person
 * set over the definition's defaults, the viewer's environment, the computed
 * tokens and the results the definition names (`tokens.ts`). With the
 * definition's Submit button, inputs change their tokens only when it is
 * pressed (and nothing is read before the first one when it asks so).
 * The inputs themselves are drawn by `DashboardTokens.tsx`.
 */
import { createContext, useCallback, useContext, useMemo, useState } from "react";

import { useDashboardAccess } from "@/hooks/api/dashboard-access";
import { useMe } from "@/hooks/api/useAuthApi";
import { useFormatter } from "@/i18n";
import { safeExternalUrl } from "@/lib/utils";

import { chainOf } from "./definition";
import type { DashboardInput, InputAction, ReadInputs } from "./inputs";
import { formTokens, planHandlers, type ClickEvent, type EventHandler } from "./interactions";
import type { DashboardLive } from "./live";
import { isSafeDashboardHref, type NormalizedDashboard } from "./spec";
import { environmentTokens, fill, namesIn, resolveTokens, type TokenValue, type Tokens } from "./tokens";

export interface DashboardTokensValue {
  /** Every token the page holds now. */
  tokens: Tokens;
  inputs: Map<string, DashboardInput>;
  /** An input's value as shown (staged until Submit when the definition asks for it). */
  valueOf: (input: DashboardInput) => TokenValue | undefined;
  setInput: (input: DashboardInput, value: TokenValue | undefined) => void;
  /** Run a button's actions; the link it opens, if any, is returned for the button to follow. */
  runActions: (actions: InputAction[]) => { url: string; newTab: boolean } | null;
  submitButton: boolean;
  /** Whether an input changed since the last Submit. */
  dirty: boolean;
  submit: () => void;
  /** Nothing is read before the first Submit (`submitOnDashboardLoad: false`). */
  held: boolean;
  /** Run a visualization's handlers on a click (or a brush): tokens, tab, link followed. */
  runHandlers: (handlers: EventHandler[], event: ClickEvent, openUrl?: string | null) => void;
  /** The visualizations with no data now (for `hideWhenNoData`), and how a widget says so. */
  empty: ReadonlySet<string>;
  reportEmpty: (widgetId: string, empty: boolean) => void;
}

export const DashboardTokensContext = createContext<DashboardTokensValue | null>(null);

export function useDashboardTokens(): DashboardTokensValue | null {
  return useContext(DashboardTokensContext);
}

/** The page's tokens where there is no dashboard around (a chat card, a test): none. */
export function usePageTokens(): Tokens {
  return useContext(DashboardTokensContext)?.tokens ?? NO_TOKENS;
}
const NO_TOKENS: Tokens = {};
const NO_INPUTS: ReadInputs = { inputs: [], globalInputs: [], defaults: {}, expressions: {}, submitButton: false, submitOnLoad: true };

function withValue(tokens: Tokens, name: string, value: TokenValue | undefined): Tokens {
  const next = { ...tokens };
  if (value === undefined || value === "" || (Array.isArray(value) && value.length === 0)) delete next[name];
  else next[name] = value;
  return next;
}

/** The page's token state, and the sources its result tokens need read (`SourceResults`). */
export function useTokenState(
  dashboard: NormalizedDashboard,
  onSwitchTab: (layoutId: string) => void,
  /** The page's live context (it is provided by the same component). */
  live: DashboardLive | null,
  /** How a page of the web app is opened (the router's); a full load without one. */
  onNavigate?: (url: string) => void,
) {
  // The viewer's own tokens (`$env:user$`...): an export's page or a published link has no viewer.
  const access = useDashboardAccess();
  const { data: user } = useMe(access.kind === "session");
  const { locale } = useFormatter();
  // A dashboard built by hand (a test, a chat card) may come without inputs.
  const read = dashboard.inputs ?? NO_INPUTS;
  // A dashboard opened from another one's link starts with the tokens it passed (`?form.<token>=`).
  const [committed, setCommitted] = useState<Tokens>(() => (typeof window === "undefined" ? {} : formTokens(window.location.search)));
  const [empty, setEmpty] = useState<ReadonlySet<string>>(() => new Set());
  const [staged, setStaged] = useState<Tokens | null>(null);
  const [results, setResults] = useState<Record<string, Tokens>>({});
  const [submitted, setSubmitted] = useState(!read.submitButton || read.submitOnLoad);

  const environment = useMemo(
    () =>
      environmentTokens({
        user: user?.email,
        userName: user?.full_name,
        timezone: user?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone,
        locale,
        platform: "xtm_one",
      }),
    [user?.email, user?.full_name, user?.timezone, locale],
  );
  const flatResults = useMemo(() => Object.assign({}, ...Object.values(results)) as Tokens, [results]);
  const tokens = useMemo(
    () =>
      resolveTokens({
        environment,
        defaults: read.defaults,
        set: committed,
        expressions: read.expressions,
        results: flatResults,
      }),
    [environment, read.defaults, committed, read.expressions, flatResults],
  );
  const inputs = useMemo(() => new Map(read.inputs.map((input) => [input.id, input])), [read.inputs]);

  const setInput = useCallback(
    (input: DashboardInput, value: TokenValue | undefined) => {
      if (!input.token) return;
      if (read.submitButton) setStaged((current) => withValue(current ?? committed, input.token as string, value));
      else setCommitted((current) => withValue(current, input.token as string, value));
    },
    [read.submitButton, committed],
  );
  const valueOf = useCallback(
    (input: DashboardInput) => (input.token ? (staged ?? committed)[input.token] ?? tokens[input.token] : undefined),
    [staged, committed, tokens],
  );
  const submit = useCallback(() => {
    if (staged) setCommitted(staged);
    setStaged(null);
    setSubmitted(true);
  }, [staged]);
  const runActions = useCallback(
    (actions: InputAction[]) => {
      let next = committed;
      let link: { url: string; newTab: boolean } | null = null;
      for (const action of actions) {
        if (action.type === "setToken") next = withValue(next, action.token, fill(action.value, tokens) ?? undefined);
        else if (action.type === "unsetToken") next = withValue(next, action.token, undefined);
        else if (action.type === "switchTab") onSwitchTab(action.layoutId);
        else if (action.type === "link") {
          // A page of the web app, or an http(s) address (opened in a new tab); anything else is dropped.
          const url = fill(action.url, tokens, "url");
          if (url && isSafeDashboardHref(url)) link = { url, newTab: action.newTab };
          else if (url && safeExternalUrl(url)) link = { url: safeExternalUrl(url) as string, newTab: true };
        }
      }
      setCommitted(next);
      setStaged(null);
      return link;
    },
    [committed, tokens, onSwitchTab],
  );

  const follow = useCallback(
    (link: { url: string; newTab: boolean }) => {
      // A page of the web app, or an http(s) address in a new tab; anything else goes nowhere.
      if (isSafeDashboardHref(link.url)) {
        if (link.newTab) window.open(link.url, "_blank", "noopener,noreferrer");
        else if (onNavigate) onNavigate(link.url);
        else window.location.assign(link.url);
        return;
      }
      const external = safeExternalUrl(link.url);
      if (external) window.open(external, "_blank", "noopener,noreferrer");
    },
    [onNavigate],
  );
  const runHandlers = useCallback(
    (handlers: EventHandler[], event: ClickEvent, openUrl?: string | null) => {
      const plan = planHandlers(handlers, event, tokens, openUrl);
      if (plan.tokens.length) {
        setCommitted((current) => plan.tokens.reduce((next, [name, value]) => withValue(next, name, value), current));
        setStaged(null);
      }
      if (plan.tab) onSwitchTab(plan.tab);
      if (plan.link) follow(plan.link);
    },
    [tokens, onSwitchTab, follow],
  );
  const reportEmpty = useCallback((widgetId: string, isEmpty: boolean) => {
    setEmpty((current) => {
      if (current.has(widgetId) === isEmpty) return current;
      const next = new Set(current);
      if (isEmpty) next.add(widgetId);
      else next.delete(widgetId);
      return next;
    });
  }, []);

  const onResult = useCallback((sourceId: string, found: Tokens) => {
    setResults((current) => (JSON.stringify(current[sourceId]) === JSON.stringify(found) ? current : { ...current, [sourceId]: found }));
  }, []);
  // The sources whose result a token names: read even when no widget draws them.
  const resultSources = useMemo(() => {
    const sources = dashboard.sources ?? {};
    const widgets = (dashboard.tabs ?? []).flatMap((tab) => tab.widgets);
    const names = namesIn([widgets.map((w) => [w.title, w.kind === "text" ? w.body : null]), read.expressions, sources, read.inputs]);
    const ids = new Set<string>();
    for (const name of names) {
      const [sid, rest] = name.split(":");
      if (rest && sid !== "env" && sid in sources) ids.add(sid);
    }
    return [...ids];
  }, [dashboard.tabs, dashboard.sources, read.expressions, read.inputs]);
  const resultReads = useMemo(
    () => (live ? resultSources.map((sid) => ({ sourceId: sid, chain: chainOf(sid, dashboard.sources ?? {}) })) : []),
    [live, resultSources, dashboard.sources],
  );

  const value = useMemo<DashboardTokensValue>(
    () => ({
      tokens,
      inputs,
      valueOf,
      setInput,
      runActions,
      submitButton: read.submitButton,
      dirty: staged !== null || !submitted,
      submit,
      held: !submitted,
      runHandlers,
      empty,
      reportEmpty,
    }),
    [tokens, inputs, valueOf, setInput, runActions, read.submitButton, staged, submit, submitted, runHandlers, empty, reportEmpty],
  );
  // The sources a result token names, for `SourceResults` to read; `onResult` takes what they answered.
  return { value, resultReads, onResult };
}

