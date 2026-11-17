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
 * The ROI unit catalog and the rates by role (#9252, #9321), as Settings >
 * Organization > Units of work and Rates by role edit them (#9448, slice O5
 * of #9206). Reads and writes only: nothing here starts a run or calls a model.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

/**
 * Where a catalog value comes from (#9836 R3, design 10.4): the seed's own
 * value (`default`), an administrator's change over it (`customized`, Reset
 * to default drops it), or a value with no default under it (`custom`).
 */
export type RoiOrigin = "default" | "customized" | "custom";

/** The origin fields `GET /roi/units`, `/roi/baselines`, `/roi/rates` and `/roi/declarations` answer. */
export interface RoiOriginFields {
  origin?: RoiOrigin;
  /** Who changed it: full name, else e-mail; `null` on a default. */
  customized_by?: string | null;
  customized_at?: string | null;
}

/** The catalog's own minutes of a built-in unit, the left side of Compare with default. */
interface RoiBaselineDefault {
  manual_min_low: number;
  manual_min_likely: number;
  manual_min_high: number;
  review_min: number;
  minutes_label?: string;
  minutes_source?: string;
}

/** The built-in rate under an organization rate. */
interface RoiRateDefault {
  amount: number;
  label: string;
  source: string;
}

/** One unit of work of `GET /roi/units`: a built-in (read-only, overridable) or a custom unit. */
export interface RoiUnit extends RoiOriginFields {
  key: string;
  name: string;
  role: string;
  rate_role: string;
  manual_min_low: number;
  manual_min_likely: number;
  manual_min_high: number;
  review_min: number;
  builtin: boolean;
  archived_at: string | null;
  /** Custom units: the free-text source of the minutes. */
  source?: string;
  /** Custom units: bumped at every change. */
  version?: number;
  /** Built-ins: the organization overrides the catalog minutes (kept for older readers; `origin` says it). */
  overridden?: boolean;
  /** Built-ins: the catalog's own minutes. */
  default?: RoiBaselineDefault | null;
  /** Built-ins: where the catalog minutes come from. */
  minutes_source?: string;
}

/** The fields a custom unit is created or changed with (`key` on create only). */
export interface RoiUnitInput {
  key?: string;
  name: string;
  role: string;
  manual_min_low: number;
  manual_min_likely: number;
  manual_min_high: number;
  review_min: number;
  rate_role: string;
  source: string;
}

/** The minutes an organization sets on a built-in unit. */
export interface RoiBaselineInput {
  manual_min_low: number;
  manual_min_likely: number;
  manual_min_high: number;
  review_min: number;
  source: string;
}

/** An organization rate of `GET /roi/rates`. */
export interface RoiRate extends RoiOriginFields {
  role: string;
  currency: string;
  amount: number;
  source: string;
  builtin_role: boolean;
  updated_at: string | null;
  /** The built-in rate of this role and currency, `null` for a role the catalog has none for. */
  default?: RoiRateDefault | null;
}

/** A catalog rate, read-only. */
export interface RoiBuiltinRate {
  role: string;
  currency: string;
  amount: number;
  label: string;
  source: string;
  /** `customized` while the organization sets its own amount for this role and currency. */
  origin?: RoiOrigin;
}

export interface RoiRates {
  rates: RoiRate[];
  builtin: RoiBuiltinRate[];
}

export interface RoiRateInput {
  role: string;
  currency: string;
  amount: number;
  source: string;
}

const UNITS_KEY = ["roi-units"] as const;
const RATES_KEY = ["roi-rates"] as const;

// `throwOnError: false`: a read error stays in the section's own `role="alert"`
// line instead of the app ErrorBoundary replacing the whole Settings page.
export function useRoiUnits() {
  return useQuery<RoiUnit[]>({ queryKey: UNITS_KEY, queryFn: () => api.get("/roi/units"), throwOnError: false });
}

export function useRoiRates() {
  return useQuery<RoiRates>({ queryKey: RATES_KEY, queryFn: () => api.get("/roi/rates"), throwOnError: false });
}

/** Every write refreshes the catalog and the rates: a unit names a rate role, a rate role is offered to a unit. */
function useCatalogMutation<TVars>(write: (vars: TVars) => Promise<unknown>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: write,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: UNITS_KEY });
      qc.invalidateQueries({ queryKey: RATES_KEY });
    },
  });
}

export function useCreateRoiUnit() {
  return useCatalogMutation((body: RoiUnitInput) => api.post("/roi/units", body));
}

export function useUpdateRoiUnit() {
  return useCatalogMutation(({ key, ...body }: RoiUnitInput & { key: string }) =>
    api.put(`/roi/units/${encodeURIComponent(key)}`, body),
  );
}

export function useArchiveRoiUnit() {
  return useCatalogMutation((key: string) => api.post(`/roi/units/${encodeURIComponent(key)}/archive`));
}

export function useOverrideRoiBaseline() {
  return useCatalogMutation(({ unit, ...body }: RoiBaselineInput & { unit: string }) =>
    api.put(`/roi/baselines/${encodeURIComponent(unit)}`, body),
  );
}

export function useResetRoiBaseline() {
  return useCatalogMutation((unit: string) => api.delete(`/roi/baselines/${encodeURIComponent(unit)}`));
}

export function usePutRoiRate() {
  return useCatalogMutation(({ role, ...body }: RoiRateInput) => api.put(`/roi/rates/${encodeURIComponent(role)}`, body));
}

export function useDeleteRoiRate() {
  return useCatalogMutation(({ role, currency }: { role: string; currency: string }) =>
    api.delete(`/roi/rates/${encodeURIComponent(role)}?currency=${encodeURIComponent(currency)}`),
  );
}
