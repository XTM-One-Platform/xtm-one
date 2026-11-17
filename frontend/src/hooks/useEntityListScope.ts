// SPDX-License-Identifier: Apache-2.0

import { useAuth } from "@/hooks/useAuth";
import { effectiveListScope } from "@/lib/entity-list-scope";
import type { EntityListOptions } from "@/types";

/**
 * The scope an entity list hook requests for these options: `all` for an admin
 * who asked for it, the library set otherwise (see `lib/entity-list-scope.ts`).
 */
export function useEntityListScope(options?: EntityListOptions): "all" | undefined {
  const { isAdmin } = useAuth();
  return effectiveListScope(options?.scope, isAdmin);
}
