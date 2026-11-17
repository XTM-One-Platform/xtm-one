// SPDX-License-Identifier: Apache-2.0

import { useCallback } from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";

/**
 * URL-backed section state for sub-tabs within a detail page tab.
 *
 * Reads from the `/:section` path segment (via route param) and falls back
 * to `defaultSection`.  The setter navigates to the new path so browser
 * back/forward and direct linking work.
 *
 * Routes must define an optional `:section?` param after `:tab?`,
 * e.g. `/agents/:id/:tab?/:section?`.
 *
 * URL examples:
 *   /agents/123/config           → section = "general" (default)
 *   /agents/123/config/tooling   → section = "tooling"
 *   /agents/123/config/channels  → section = "channels"
 */
export function useSectionParam<T extends string>(defaultSection: T): [T, (section: T) => void] {
  const params = useParams<{ section?: string }>();
  const navigate = useNavigate();
  const location = useLocation();

  const rawSection = params.section;
  const section = (rawSection as T) || defaultSection;

  // Path without the section segment (if present).
  // e.g. `/agents/123/config/tooling` → `/agents/123/config`
  const basePath = rawSection
    ? location.pathname.slice(0, location.pathname.lastIndexOf("/"))
    : location.pathname;

  const setSection = useCallback(
    (next: T) => {
      // Default section uses the clean tab path (no trailing section segment)
      const target = next === defaultSection ? basePath : `${basePath}/${next}`;
      navigate(target, { replace: true });
    },
    [defaultSection, basePath, navigate],
  );

  return [section, setSection];
}
