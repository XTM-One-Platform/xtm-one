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

import { useCallback, useEffect } from "react";
import { useSearchParams } from "react-router-dom";

import { useAccessibleFleets } from "@/hooks/api/useFleetApi";
import { effectiveFleetParam, fleetParamPending } from "@/lib/fleet-operations";

/**
 * The fleet an Operations list filters on: the locked one when the
 * list is embedded in a fleet page, else `?fleet=` (undefined when none), with
 * its setter. Changing the fleet resets the page and the agent, which may not
 * be one of its members. A `?fleet=` is ignored for a reader who can access
 * no fleet: the list is unfiltered and the picker hidden.
 *
 * The third value is true while the URL names a fleet and the
 * reader's fleet access is still loading: the list waits for it instead of
 * asking with a `fleet=` it may drop a moment later (a filtered empty result
 * flashing before the full list). Once access is known to be none, `?fleet=`
 * leaves the URL with a replace navigation, so a copied link no longer
 * carries a filter the page does not apply.
 */
export function useFleetParam(
  locked?: string,
): [string | undefined, (next: string | undefined) => void, boolean] {
  const [searchParams, setSearchParams] = useSearchParams();
  const { access } = useAccessibleFleets({ enabled: !locked });
  const raw = searchParams.get("fleet") || undefined;
  const fromUrl = effectiveFleetParam(raw, access);
  const pending = !locked && fleetParamPending(raw, access);
  const ignored = !locked && access === "none" && raw !== undefined;
  useEffect(() => {
    if (!ignored) return;
    setSearchParams(
      (prev) => {
        const params = new URLSearchParams(prev);
        params.delete("fleet");
        return params;
      },
      { replace: true },
    );
  }, [ignored, setSearchParams]);
  const set = useCallback(
    (next: string | undefined) =>
      setSearchParams(
        (prev) => {
          const params = new URLSearchParams(prev);
          if (next) params.set("fleet", next);
          else params.delete("fleet");
          params.delete("agent");
          params.delete("page");
          return params;
        },
        { replace: true },
      ),
    [setSearchParams],
  );
  return [locked ?? fromUrl, set, pending];
}
