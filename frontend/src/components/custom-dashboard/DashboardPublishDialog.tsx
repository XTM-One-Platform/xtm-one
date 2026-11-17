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
 * Publish or export a custom dashboard - the other direction of the interop.
 *
 * Publish sends the dashboard to OpenCTI or OpenAEV as one of their own
 * dashboards - or to OpenCTI as a custom view, when its OpenCTI widgets read an
 * entity input: they then read the entity of the page OpenCTI shows them on -
 * through an integration the person can use and with their own rights there;
 * only what that platform draws is sent (its own widgets, text for OpenCTI)
 * and the rest is listed. "XTM Hub" shares its OpenCTI export on
 * the XTM Hub, in a dashboards library the person's Hub account may publish
 * to, as an inactive resource they then activate there. Export downloads the
 * same files, or an XTM One widget pack. OpenCRQ has no custom dashboards: its
 * widgets stay here.
 */
import { useEffect, useState } from "react";
import { Spinner } from "@filigran/design-system";
import { Download, ExternalLink, Send } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useToast } from "@/components/ui/toast";
import {
  useDashboardPublications,
  usePublishDashboard,
  useShareDashboardOnHub,
} from "@/hooks/api/useCustomDashboardApi";
import { usePackIntegrations } from "@/hooks/api/useWidgetPackApi";
import { useFormatter } from "@/i18n";
import { api } from "@/lib/api";
import { safeExternalUrl, saveBlobAs, timeAgo } from "@/lib/utils";
import type { CustomDashboard } from "@/types";

import { PLATFORM_NAMES } from "./spec";

type Target = "opencti" | "opencti-custom-view" | "openaev" | "xtm_hub";

/** Where a dashboard goes, as the dialog names it. */
const TARGET_NAMES: Record<Target, string> = { ...PLATFORM_NAMES, "opencti-custom-view": "OpenCTI", xtm_hub: "XTM Hub" };
/** The integration each target is reached through. */
const PROVIDER_OF: Record<Target, string> = { opencti: "opencti", "opencti-custom-view": "opencti", openaev: "openaev", xtm_hub: "xtm_hub" };

export function DashboardPublishDialog({
  dashboard,
  open,
  onOpenChange,
}: {
  dashboard: CustomDashboard;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t_i18n, locale } = useFormatter();
  const { toast } = useToast();
  const integrations = usePackIntegrations(open);
  const publications = useDashboardPublications(dashboard.id, open);
  const publish = usePublishDashboard(dashboard.id);
  const share = useShareDashboardOnHub(dashboard.id);
  const [summary, setSummary] = useState("");
  const pending = publish.isPending || share.isPending;
  const [target, setTarget] = useState<Target>("opencti");
  const [integrationId, setIntegrationId] = useState("");
  const [name, setName] = useState(dashboard.title);
  const [result, setResult] = useState<{ url: string | null; notes: string[] } | null>(null);

  useEffect(() => {
    if (!open) return;
    setName(dashboard.title);
    setResult(null);
  }, [open, dashboard.title]);
  const choices = (integrations.data ?? []).filter((i) => i.provider === PROVIDER_OF[target]);
  useEffect(() => {
    setIntegrationId(choices.length === 1 ? choices[0].id : "");
    // `choices` follows the target and the integrations.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, integrations.data]);

  const download = (format: "opencti" | "opencti-custom-view" | "openaev" | "xtm") =>
    api
      .download(`/custom-dashboards/${dashboard.id}/export?format=${encodeURIComponent(format)}`)
      .then(({ blob, filename }) => saveBlobAs(blob, filename ?? `dashboard.${format === "openaev" ? "zip" : "json"}`))
      .catch((error: Error) => toast({ title: t_i18n("The dashboard could not be exported"), description: error.message, variant: "error" }));

  const handlers = {
    onSuccess: ({ publication, notes }: { publication: { remote_url: string | null }; notes: string[] }) => {
      setResult({ url: publication.remote_url, notes });
      toast({ title: t_i18n("Published to {platform}", { platform: TARGET_NAMES[target] }), variant: "success" });
    },
    onError: (error: Error) =>
      toast({ title: t_i18n("The dashboard could not be published"), description: error.message, variant: "error" }),
  };
  const doPublish = () =>
    target === "xtm_hub"
      ? share.mutate(
          { integration_id: integrationId, name: name.trim() || undefined, short_description: summary.trim() || undefined },
          handlers,
        )
      : publish.mutate({ target, integration_id: integrationId, name: name.trim() || undefined }, handlers);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg" data-testid="dashboard.publish.dialog">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Send className="h-4 w-4 text-accent" />
            {t_i18n("Publish or export")}
          </DialogTitle>
          <DialogDescription>
            {t_i18n(
              "Send this dashboard to OpenCTI or OpenAEV as one of their own dashboards, with your own account there. Only the widgets that platform draws are sent; OpenCRQ has no custom dashboards, its widgets stay here.",
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <SegmentedFilter
            ariaLabel={t_i18n("Platform")}
            value={target}
            onChange={setTarget}
            options={[
              { key: "opencti", label: "OpenCTI" },
              { key: "opencti-custom-view", label: t_i18n("OpenCTI custom view") },
              { key: "openaev", label: "OpenAEV" },
              { key: "xtm_hub", label: "XTM Hub" },
            ]}
          />
          {choices.length === 0 && !integrations.isPending ? (
            <p className="content-compact text-feedback-warning-primary">
              {t_i18n("You can use no {platform} integration.", { platform: TARGET_NAMES[target] })}
            </p>
          ) : (
            <SearchableSelect
              aria-label={t_i18n("Integration")}
              options={choices.map((i) => ({ value: i.id, label: i.name }))}
              value={integrationId}
              onValueChange={setIntegrationId}
              placeholder={t_i18n("Choose an integration")}
            />
          )}
          <Input label={t_i18n("Name there")} value={name} onChange={(event) => setName(event.target.value)} maxLength={120} />
          {target === "opencti-custom-view" && (
            <p className="content-caption text-default-secondary">
              {t_i18n(
                "Sent as a custom view of the type of the dashboard's OpenCTI entity input: the widgets reading it read the entity of the page OpenCTI shows them on.",
              )}
            </p>
          )}
          {target === "xtm_hub" && (
            <>
              <Input
                label={t_i18n("Short description")}
                value={summary}
                onChange={(event) => setSummary(event.target.value)}
                maxLength={300}
              />
              <p className="content-caption text-default-secondary">
                {t_i18n(
                  "Shared as an OpenCTI dashboard, in a dashboards library your XTM Hub account may publish to. It stays hidden until you activate it on the XTM Hub.",
                )}
              </p>
            </>
          )}
          <Button className="w-full gap-1.5" disabled={!integrationId || pending} aria-busy={pending || undefined} onClick={doPublish}>
            {pending && <Spinner size="sm" tone="inherit" />}
            {!pending && <Send className="h-4 w-4" />}
            {target === "xtm_hub"
              ? t_i18n("Share on XTM Hub")
              : t_i18n("Publish to {platform}", { platform: TARGET_NAMES[target] })}
          </Button>
          {result && (
            <div className="space-y-1 rounded-sm border border-elevation-subtle p-3 content-compact">
              {safeExternalUrl(result.url) && (
                <a
                  href={safeExternalUrl(result.url)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-accent hover:underline"
                >
                  {t_i18n("Open it in {platform}", { platform: TARGET_NAMES[target] })}
                  <ExternalLink className="h-3 w-3" aria-hidden="true" />
                </a>
              )}
              {result.notes.length > 0 && (
                <ul className="list-disc pl-4 content-caption text-default-secondary">
                  {result.notes.slice(0, 10).map((note, index) => (
                    <li key={index}>{note}</li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>

        <div className="space-y-2 border-t border-elevation-subtle pt-3">
          <p className="content-caption font-medium uppercase tracking-wide text-default-secondary">{t_i18n("Export")}</p>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" className="gap-1.5" onClick={() => void download("opencti")}>
              <Download className="h-3.5 w-3.5" /> {t_i18n("OpenCTI dashboard")}
            </Button>
            <Button variant="outline" size="sm" className="gap-1.5" onClick={() => void download("opencti-custom-view")}>
              <Download className="h-3.5 w-3.5" /> {t_i18n("OpenCTI custom view")}
            </Button>
            <Button variant="outline" size="sm" className="gap-1.5" onClick={() => void download("openaev")}>
              <Download className="h-3.5 w-3.5" /> {t_i18n("OpenAEV dashboard")}
            </Button>
            <Button variant="outline" size="sm" className="gap-1.5" onClick={() => void download("xtm")}>
              <Download className="h-3.5 w-3.5" /> {t_i18n("Widget pack")}
            </Button>
          </div>
        </div>

        {(publications.data ?? []).length > 0 && (
          <div className="space-y-1 border-t border-elevation-subtle pt-3">
            <p className="content-caption font-medium uppercase tracking-wide text-default-secondary">{t_i18n("Published")}</p>
            <ul className="space-y-1 content-caption text-default-secondary">
              {(publications.data ?? []).slice(0, 5).map((p) => (
                <li key={p.id} className="flex items-center justify-between gap-2">
                  <span>
                    {TARGET_NAMES[p.target]} · {t_i18n("Revision {revision}", { revision: p.revision })}
                    {p.published_at ? ` · ${timeAgo(p.published_at, locale)}` : ""}
                  </span>
                  {safeExternalUrl(p.remote_url) && (
                    <a href={safeExternalUrl(p.remote_url)} target="_blank" rel="noopener noreferrer" className="text-accent hover:underline">
                      {t_i18n("Open")}
                    </a>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
