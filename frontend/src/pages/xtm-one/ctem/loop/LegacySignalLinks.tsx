// SPDX-License-Identifier: Apache-2.0

/**
 * Mount on a page that used to hold the loop trace or the signal sources
 * dialog (#1263): a shared `?loop=` or `?signal-sources=1` link lands on the
 * Signals page with the same filter instead of a dialog nobody draws any more.
 */
import { Navigate, useSearchParams } from "react-router-dom";
import { legacySignalsHref } from "./signals-route";

export function LegacySignalLinks() {
  const [params] = useSearchParams();
  const to = legacySignalsHref(params);
  return to ? <Navigate to={to} replace /> : null;
}
