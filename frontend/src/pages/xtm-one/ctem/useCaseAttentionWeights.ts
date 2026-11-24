// SPDX-License-Identifier: Apache-2.0

/**
 * The points of the Cases attention score the organization set (#1176, profile
 * `case_attention`), else the defaults: the list, its band and the case page
 * rank with the same weights.
 */
import { useMemo } from "react";
import { useOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import { weightsFromProfile, type CaseAttentionWeights } from "./case-attention";

export function useCaseAttentionWeights(): CaseAttentionWeights {
  const { data } = useOrganizationContext();
  const saved = data?.profile?.case_attention;
  return useMemo(() => weightsFromProfile(saved), [saved]);
}
