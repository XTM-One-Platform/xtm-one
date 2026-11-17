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

/**
 * Said while the flow node catalog on screen is the last one that loaded
 * because refreshing it failed: the builder and the agent wizard keep
 * working from it, but it may still list something deleted, or miss something
 * created, since it loaded.
 *
 * `onRetry` is the page's own catalog `refetch`: the notice holds no query of
 * its own, so showing it never fetches anything.
 */

import { AlertCircle, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";

export function CatalogRefreshNotice({
  retrying,
  onRetry,
  className,
}: {
  retrying: boolean;
  onRetry: () => void;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div
      role="status"
      className={cn(
        "flex items-center gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.07] px-3 py-2 content-compact text-feedback-warning-tertiary",
        className,
      )}
    >
      <AlertCircle className="h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" />
      <span className="min-w-0 flex-1">
        {t_i18n("The available options could not be refreshed, so they may be out of date.")}
      </span>
      <Button
        variant="ghost"
        size="sm"
        className="h-6 shrink-0 gap-1.5 px-2 text-[11px]"
        disabled={retrying}
        onClick={onRetry}
      >
        <RefreshCw className={cn("h-3 w-3", retrying && "animate-spin")} />
        {retrying ? t_i18n("Refreshing...") : t_i18n("Retry")}
      </Button>
    </div>
  );
}
