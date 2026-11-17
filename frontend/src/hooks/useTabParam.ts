// SPDX-License-Identifier: Apache-2.0

import { useCallback } from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import { escapeRegExp } from "@/lib/utils";

/**
 * URL-backed tab state — drop-in replacement for `useState<Tab>()` on detail pages.
 *
 * Reads from the `/:tab` path segment (via route param) and falls back to `defaultTab`.
 * The setter navigates to the new path so browser back/forward and direct linking work.
 *
 * Routes must define an optional `:tab?` param, e.g. `/agents/:id/:tab?`.
 * Handles nested sub-params (`:section?` after `:tab?`) by stripping from the tab
 * segment onwards, so switching tabs always lands on a clean tab path.
 */
export function useTabParam<T extends string>(defaultTab: T): [T, (tab: T) => void] {
  const params = useParams<{ tab?: string }>();
  const navigate = useNavigate();
  const location = useLocation();

  const rawTab = params.tab;
  const tab = (rawTab as T) || defaultTab;

  // Base path = current path with the tab segment (and anything after it) stripped.
  // Uses a regex anchored at the end so "config" in `/agents/123/config/tooling`
  // strips `/config/tooling`, leaving `/agents/123`. `rawTab` is a user-controlled
  // URL segment, so it MUST be regex-escaped — an unescaped metacharacter (e.g.
  // `/flows/foo(`) would make `new RegExp` throw and crash the page.
  const basePath = rawTab
    ? location.pathname.replace(new RegExp(`/${escapeRegExp(rawTab)}(/.*)?$`), "")
    : location.pathname;

  const setTab = useCallback(
    (next: T) => {
      const target = next === defaultTab ? basePath : `${basePath}/${next}`;
      navigate(target);
    },
    [defaultTab, basePath, navigate],
  );

  return [tab, setTab];
}
