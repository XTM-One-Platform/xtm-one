// SPDX-License-Identifier: Apache-2.0

import { useAuthConfig } from "@/hooks/api/useAuthApi";

/**
 * Runtime cost-visibility gate sourced from backend env config.
 * Defaults to true for backward compatibility with older API payloads.
 */
export function useCostDataAvailability(): boolean {
  const { data: authConfig } = useAuthConfig();

  if (!authConfig) return false;
  return authConfig.cost_data_available ?? true;
}
