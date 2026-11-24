// SPDX-License-Identifier: Apache-2.0

import { Plug2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { PROVIDER_LOGOS, PROVIDER_LOGOS_LIGHT } from "@/lib/constants";
import { useColorMode } from "@/hooks/useColorMode";

/**
 * Shared provider logo component – displays the provider's logo image
 * or a fallback Plug2 icon when no logo is available. `decorative` when the
 * provider is already named in text next to it: the mark then stays out of
 * the accessibility tree instead of announcing the name twice.
 */
export function ProviderLogo({
  provider,
  logo,
  className,
  decorative = false,
}: {
  provider: string;
  logo?: string | null;
  className?: string;
  decorative?: boolean;
}) {
  const light = useColorMode() === "light";
  // A mark drawn per theme takes its light file on the light theme (the XTM One emblem, #964).
  const src = logo || (light && PROVIDER_LOGOS_LIGHT[provider]) || PROVIDER_LOGOS[provider];
  if (src) {
    return (
      <img
        src={src}
        alt={decorative ? "" : provider}
        aria-hidden={decorative || undefined}
        className={cn("object-contain", className ?? "h-5 w-5")}
      />
    );
  }
  return <Plug2 className={cn(className ?? "h-5 w-5", "text-default-secondary")} />;
}
