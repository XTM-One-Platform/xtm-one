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

import { useState } from "react";
import { Cog } from "lucide-react";
import { cn } from "@/lib/utils";
import { providerLogoSrc } from "@/lib/constants";

/**
 * An integration provider's logo, with a generic fallback when the platform
 * ships no SVG for it — so an icon is always shown, never a broken image.
 */
export function ProviderLogo({
  provider,
  className,
}: {
  provider: string;
  className?: string;
}) {
  const src = providerLogoSrc(provider);
  // A load failure belongs to the src it was seen for, not to this instance:
  // a list that reuses the instance for another provider must try that
  // provider's logo. The key gives every src its own <img>, so a late error
  // from the previous one cannot be recorded against it.
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  if (failedSrc === src) return <Cog className={cn("text-default-secondary", className)} />;
  return (
    <img
      key={src}
      src={src}
      alt=""
      className={className}
      onError={() => setFailedSrc(src)}
    />
  );
}
