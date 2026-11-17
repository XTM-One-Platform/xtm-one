// SPDX-License-Identifier: Apache-2.0

/* eslint-disable react-refresh/only-export-components */
/**
 * Left-navigation preferences: which groups are expanded, and whether the
 * sidebar is collapsed to an icon rail.
 *
 * Context rather than a plain hook because two components need the *same*
 * value: the `Sidebar` renders it and `AppLayout` offsets the main column to
 * match the resulting width (180px full, 56px rail — Tailwind needs those as
 * literal classes, so they live in the markup rather than here). Persisted in
 * localStorage: durable personal preferences (same rationale as `useViewMode`),
 * not shareable state, so they deliberately stay out of the URL.
 *
 * The rail is opt-in: the nav icons are not universal enough to drop the
 * labels by default (Material reserves compact rails for a handful of
 * well-known destinations), so full width stays the initial presentation.
 *
 * A page may ask for the rail while it is open (`useRailHold`: a custom
 * dashboard whose definition says `collapseNavigation`). The hold is never
 * persisted: leaving the page gives the person's own preference back, and a
 * toggle of the rail meanwhile is theirs for the rest of the visit.
 */
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode,
} from "react";

const EXPANDED_KEY = "copilot-nav:expanded";
const RAIL_KEY = "copilot-nav:rail";

/**
 * Groups open on a first visit. Work is open because it holds Operations and
 * Presentations, which used to be top-level entries — grouping them must not
 * hide them; Agent Studio is the group people live in. The rest start closed
 * so the nav opens short and grows on demand.
 */
const DEFAULT_EXPANDED: Record<string, boolean> = { work: true, "agent-studio": true };

function readExpanded(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(EXPANDED_KEY);
    if (!raw) return DEFAULT_EXPANDED;
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return DEFAULT_EXPANDED;
    /* Merged over the defaults so a group added in a later release starts from
       its intended default instead of "closed" for everyone who ever toggled
       a group before it existed. */
    return { ...DEFAULT_EXPANDED, ...(parsed as Record<string, boolean>) };
  } catch {
    // Private browsing / storage disabled / corrupt JSON.
    return DEFAULT_EXPANDED;
  }
}

function readRail(): boolean {
  try {
    return localStorage.getItem(RAIL_KEY) === "1";
  } catch {
    return false;
  }
}

function persist(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch { /* quota exceeded or private browsing */ }
}

interface NavPrefs {
  /** Explicit user state per group id. */
  expanded: Record<string, boolean>;
  /** No toggle on purpose: every caller says which state it wants. */
  setGroupExpanded: (id: string, value: boolean) => void;
  isExpanded: (id: string) => boolean;
  rail: boolean;
  setRail: (rail: boolean) => void;
  /** Show the rail until the returned release is called (`useRailHold`). */
  hold?: () => () => void;
}

const NavPrefsContext = createContext<NavPrefs | null>(null);

export function NavPrefsProvider({ children }: { children: ReactNode }) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>(readExpanded);
  const [stored, setRailState] = useState<boolean>(readRail);
  // While a page holds the rail: what it shows (the person may still toggle it).
  const [held, setHeld] = useState<boolean | null>(null);
  const rail = held ?? stored;

  const setGroupExpanded = useCallback((id: string, value: boolean) => {
    setExpanded((prev) => {
      if (!!prev[id] === value) return prev;
      const next = { ...prev, [id]: value };
      persist(EXPANDED_KEY, JSON.stringify(next));
      return next;
    });
  }, []);

  const isExpanded = useCallback((id: string) => !!expanded[id], [expanded]);

  const holds = useRef(0);
  const hold = useCallback(() => {
    holds.current += 1;
    if (holds.current === 1) setHeld(true);
    return () => {
      holds.current -= 1;
      if (holds.current === 0) setHeld(null);
    };
  }, []);

  const setRail = useCallback((value: boolean) => {
    if (holds.current > 0) {
      setHeld(value);
      return;
    }
    setRailState(value);
    persist(RAIL_KEY, value ? "1" : "0");
  }, []);

  const value = useMemo<NavPrefs>(
    () => ({
      expanded,
      setGroupExpanded,
      isExpanded,
      rail,
      setRail,
      hold,
    }),
    [expanded, setGroupExpanded, isExpanded, rail, setRail, hold],
  );

  return <NavPrefsContext.Provider value={value}>{children}</NavPrefsContext.Provider>;
}

/** While `active`, show the navigation as a rail without changing the person's preference. */
export function useRailHold(active: boolean) {
  const ctx = useContext(NavPrefsContext);
  const hold = ctx?.hold;
  useEffect(() => {
    if (!active || !hold) return;
    return hold();
  }, [active, hold]);
}

export function useNavPrefs(): NavPrefs {
  const ctx = useContext(NavPrefsContext);
  if (!ctx) throw new Error("useNavPrefs must be used inside <NavPrefsProvider>");
  return ctx;
}
