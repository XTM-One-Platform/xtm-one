// SPDX-License-Identifier: Apache-2.0

/**
 * The flag of a country (#10855): a bundled SVG (country-flag-icons, MIT),
 * drawn the same on Windows, macOS, iOS and Linux. Never a regional-indicator
 * emoji: Windows draws those as two letters ("FR"). No network fetch: the
 * flags ship with the app. A code with no flag draws nothing, so the name or
 * the code next to it still says which country it is.
 */
import type { ComponentType, SVGProps } from "react";
import * as FLAGS from "country-flag-icons/react/3x2";

import { cn } from "@/lib/utils";

type FlagComponent = ComponentType<SVGProps<SVGSVGElement> & { title?: string }>;

const FLAG_BY_CODE = FLAGS as unknown as Record<string, FlagComponent | undefined>;

interface CountryFlagProps {
  /** ISO 3166-1 alpha-2 code, any case. */
  code: string;
  /** The country's name: the image's accessible name and tooltip. */
  name: string;
  className?: string;
}

export function CountryFlag({ code, name, className }: CountryFlagProps) {
  const upper = code.toUpperCase();
  const Flag = /^[A-Z]{2}$/.test(upper) ? FLAG_BY_CODE[upper] : undefined;
  if (!Flag) return null;
  // The tooltip is on a wrapper: the SVG's own `title` prop adds a <title> child whose text joins the label's.
  return (
    <span title={name} className="inline-flex shrink-0">
      <Flag
        role="img"
        aria-label={name}
        data-testid={`country-flag-${upper}`}
        className={cn("inline-block h-3 w-[18px] shrink-0 rounded-[2px] align-[-1px] shadow-[0_0_0_1px_rgba(0,0,0,0.08)]", className)}
      />
    </span>
  );
}
