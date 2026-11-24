// SPDX-License-Identifier: Apache-2.0

import { Check, Image } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { useFormatter } from "@/i18n";
import { providerDisplayName } from "@/lib/utils";
import type { AvailableModel, LLMProvider } from "@/types";

/**
 * The Image Generation card's current model: the saved one, else the one
 * generation really runs on (a built-in or AI Gateway default), marked automatic.
 */
export function ImageModelSummary({
  model,
  providerName,
  inUse,
  providers,
}: {
  model: string | null | undefined;
  /** The saved model's provider, or the first image provider. */
  providerName: string | null;
  inUse: AvailableModel | null | undefined;
  providers: LLMProvider[] | undefined;
}) {
  const { t_i18n } = useFormatter();
  const automatic = !model && inUse ? inUse : null;
  const shownProvider = automatic
    ? providerDisplayName(automatic.provider, automatic.provider_id ?? null, providers)
    : providerName;
  return (
    <div className="flex items-center justify-between rounded-sm bg-elevation-default p-3.5">
      <div className="flex items-center gap-3">
        <div className="flex h-8 w-8 items-center justify-center rounded-sm bg-filigran-ia-primary/10">
          <Image className="h-4 w-4 text-filigran-ia-primary" />
        </div>
        <div>
          <p className="content-base font-medium">
            {model || automatic?.model || t_i18n("No image model selected")}
          </p>
          <span className="content-compact text-default-secondary">
            {shownProvider
              ? t_i18n("Image Provider: {provider}", { provider: shownProvider })
              : t_i18n('Click "Configure" to pick a default image model')}
          </span>
        </div>
      </div>
      {model ? (
        <Badge variant="success" className="content-caption"><Check className="h-3 w-3 mr-1" />{t_i18n("Active")}</Badge>
      ) : automatic ? (
        <Badge variant="secondary" className="content-caption">{t_i18n("Automatic")}</Badge>
      ) : null}
    </div>
  );
}
