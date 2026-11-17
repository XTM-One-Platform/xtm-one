// SPDX-License-Identifier: Apache-2.0

import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { useFormatter } from "@/i18n";

/**
 * A webhook URL with its copy button, the one row every card of the integration page uses
 * to show a URL to paste elsewhere (Webhooks card, WhatsApp pairing card).
 */
export function CopyableUrl({ url }: { url: string }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex items-center gap-2">
      <code className="flex-1 rounded-sm bg-elevation-hover px-2.5 py-1.5 content-compact text-accent font-mono-plex break-all select-all">
        {url}
      </code>
      <CopyIconButton value={url} label={t_i18n("Copy the webhook URL")} className="shrink-0" />
    </div>
  );
}
