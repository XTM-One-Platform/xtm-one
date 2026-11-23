// SPDX-License-Identifier: Apache-2.0

import { ProviderLogo } from "@/components/ui/provider-logo";

/**
 * The XTM One emblem at an option's 16 px (#1373): what XTM One serves by itself (a capability's
 * built-in option: "XTM One inventory (built in)", "In XTM One") wears the product's mark, as an
 * integration wears its provider logo, never a generic capability icon. The light theme reads its
 * own file (PROVIDER_LOGOS_LIGHT, #964), so the mark keeps its contrast on both themes.
 */
export function XtmOneEmblem({ className }: { className?: string }) {
  return <ProviderLogo provider="xtm_one" className={className ?? "h-4 w-4 shrink-0"} decorative />;
}
