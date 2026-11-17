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

import type { TriggerConfig, TriggerProvisioningState } from "@/types";

/**
 * What an integration-event assignment holds in its source platform
 * (services/trigger_provisioning.py: the OpenCTI and OpenAEV passes write
 * `trigger_config.<provider>_provisioning`).
 *
 * - `provisioned`: the trigger(s) and the notifier exist and were checked;
 * - `error`: the last pass failed, retried at the next one;
 * - `orphaned`: no longer wanted, but the integration did not answer, so
 *   what was created is still there;
 * - `pending`: declared (`trigger_config.provision`), enabled, not created yet;
 * - `disabled`: declared, but nothing is created until the assignment is on.
 */
export type ProvisioningKind = "provisioned" | "error" | "orphaned" | "pending" | "disabled";

interface ProvisioningView {
  kind: ProvisioningKind;
  provider: string;
  state: TriggerProvisioningState | null;
  /** Triggers held: one on OpenCTI, one per event on OpenAEV. */
  triggerCount: number;
}

/** The providers whose platform XTM One provisions a trigger in. */
const PROVISIONED_PROVIDERS = ["opencti", "openaev"] as const;

function stateOf(config: TriggerConfig, provider: string): TriggerProvisioningState | null {
  const value = provider === "opencti" ? config.opencti_provisioning : config.openaev_provisioning;
  return value && typeof value === "object" && !Array.isArray(value) ? value : null;
}

function countTriggers(state: TriggerProvisioningState): number {
  if (state.trigger_ids && typeof state.trigger_ids === "object") {
    return Object.values(state.trigger_ids).filter(Boolean).length;
  }
  return state.trigger_id ? 1 : 0;
}

/** `null` when the assignment declares no provisioning and holds none. */
export function provisioningView(config: TriggerConfig | undefined, enabled: boolean): ProvisioningView | null {
  if (!config) return null;
  const provider = config.provider ?? "";
  if (!(PROVISIONED_PROVIDERS as readonly string[]).includes(provider)) return null;
  const state = stateOf(config, provider);
  const declared = Boolean(config.provision && typeof config.provision === "object");
  if (state) {
    const kind: ProvisioningKind =
      state.status === "provisioned" ? "provisioned" : state.status === "orphaned" ? "orphaned" : "error";
    return { kind, provider, state, triggerCount: countTriggers(state) };
  }
  if (!declared) return null;
  return { kind: enabled ? "pending" : "disabled", provider, state: null, triggerCount: 0 };
}
