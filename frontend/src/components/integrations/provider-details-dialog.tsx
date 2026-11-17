/**
 * Provider details dialog — what a catalog card holds behind a click.
 *
 * The card shows the two lines that fit on a tile; this shows the rest of what
 * the catalog entry actually says about a provider: how it authenticates, what
 * it can do, which events it can react to, which tools it exposes, and how it
 * is set up. `children` carries whatever the surface adds on top (the
 * administration catalog appends its own controls and registered platforms).
 */

import { useState, type ReactNode } from "react";
import { Boxes, ChevronDown, Plug2, Wrench, Zap } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { SetupGuideDialog } from "@/components/ui/setup-guide-dialog";
import { SettingsGroupTitle } from "@/components/ui/settings-form";
import { AUTH_FACET_LABELS, authModesOf } from "./catalog-facets";
import { INTEGRATION_TYPE_LABELS, integrationCategoryLabel } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { ProviderCatalogItem } from "@/types";

/**
 * What the viewer has to supply to connect this provider.
 *
 * The auth type answers first: a provider that needs no credential, or whose
 * credential the platform holds, asks for nothing per integration, and saying
 * "Provided per integration" for one would contradict the Authentication fact
 * beside it and invent a setup step. Only then does it matter whether an
 * administrator has stored OAuth defaults.
 */
function credentialsFact(
  item: ProviderCatalogItem,
  t: (key: string) => string,
): string {
  if (item.auth_type === "none") return t("No credentials required");
  if (item.auth_type === "platform_managed") return t("Managed by the platform");
  if (item.has_oauth_defaults) return t("Ready to connect");
  return t("Provided per integration");
}

/**
 * Whether this provider does webhooks at all — by any of the four routes the
 * catalog records. A provider that only sets `needs_shared_webhook` or
 * `supports_assignment_webhooks` (OpenCTI, Slack, LinkedIn) still delivers
 * events over a webhook, so reading only the other two reports "Not
 * supported" for a provider whose events are listed right below.
 */
function hasWebhooks(item: ProviderCatalogItem): boolean {
  return Boolean(
    item.supports_webhook_management
    || item.webhook_path
    || item.supports_assignment_webhooks
    || item.needs_shared_webhook,
  );
}

/**
 * A titled block that renders nothing when the provider has nothing to say.
 * Its heading is the settings kit's group title, so every heading of the
 * dialog (and the blocks a surface appends, such as Registered Platforms)
 * reads at one size, in sentence case.
 */
function Section({
  icon: Icon, title, count, children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  count?: number;
  children: ReactNode;
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <SettingsGroupTitle icon={Icon}>{title}</SettingsGroupTitle>
        {count !== undefined && (
          <span className="rounded-sm bg-elevation-hover px-1.5 py-px content-caption font-medium tabular-nums text-default-secondary">
            {count}
          </span>
        )}
      </div>
      {children}
    </div>
  );
}

/**
 * A list that shows its first few rows and unfolds on request.
 *
 * OpenCTI alone carries sixty tools: rendered whole they push everything the
 * dialog exists to show — the provider's controls, its registered platforms —
 * several screens below the fold.
 */
function CollapsingList({ children, preview = 6 }: { children: ReactNode[]; preview?: number }) {
  const { t_i18n } = useFormatter();
  const [expanded, setExpanded] = useState(false);
  const hidden = children.length - preview;

  return (
    <>
      <ul className="space-y-1">{expanded ? children : children.slice(0, preview)}</ul>
      {hidden > 0 && (
        <Button
          variant="ghost"
          size="sm"
          className="h-6 gap-1 px-2 content-compact text-default-secondary"
          onClick={() => setExpanded((v) => !v)}
        >
          <ChevronDown className={cn("h-3 w-3 transition-transform", expanded && "rotate-180")} />
          {expanded ? t_i18n("Show less") : t_i18n("Show {count} more", { count: hidden })}
        </Button>
      )}
    </>
  );
}

function Fact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0">
      <p className="content-compact text-default-secondary">{label}</p>
      <div className="mt-0.5 content-compact text-default-primary">{value}</div>
    </div>
  );
}

export function ProviderDetailsDialog({
  item, open, onOpenChange, children,
}: {
  item: ProviderCatalogItem | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children?: ReactNode;
}) {
  const { t_i18n, t_text } = useFormatter();

  // No provider, nothing to show: a DialogContent without its DialogTitle has
  // no accessible name, so the dialog is not rendered at all.
  if (!item) return null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        {item && (
          <>
            <DialogHeader>
              <div className="flex items-start gap-3 pr-8">
                <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-sm border border-elevation-subtle-soft bg-elevation-hover">
                  {item.logo
                    ? <img src={item.logo} alt="" className="h-6 w-6 object-contain" />
                    : <Plug2 className="h-5 w-5 text-default-secondary" />}
                </div>
                <div className="min-w-0 flex-1">
                  <DialogTitle className="truncate">{item.name}</DialogTitle>
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                    <Badge variant="outline" className="content-caption font-normal text-default-secondary">
                      {item.provider}
                    </Badge>
                    <Badge variant="cyan" className="content-caption">
                      {t_i18n(integrationCategoryLabel(item.integration_category))}
                    </Badge>
                    {item.is_custom && (
                      <Badge variant="outline" className="border-feedback-alert-primary/40 content-caption text-feedback-warning-primary">
                        {t_i18n("Imported")}
                      </Badge>
                    )}
                    {item.is_registration_only && (
                      <Badge variant="secondary" className="content-caption">
                        {t_i18n("Registration only")}
                      </Badge>
                    )}
                    {item.is_bot && (
                      <Badge variant="warning" className="content-caption">{t_i18n("Bot")}</Badge>
                    )}
                  </div>
                </div>
              </div>
              <DialogDescription className="pt-2 text-left">
                {item.description || t_i18n(integrationCategoryLabel(item.integration_category))}
              </DialogDescription>
            </DialogHeader>

            <div className="space-y-5">
              <div className="grid grid-cols-2 gap-x-4 gap-y-3 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3 sm:grid-cols-3">
                <Fact
                  label={t_i18n("Authentication")}
                  value={authModesOf(item).map((m) => t_i18n(AUTH_FACET_LABELS[m])).join(", ") || item.auth_type}
                />
                <Fact label={t_i18n("Credentials")} value={credentialsFact(item, t_i18n)} />
                <Fact
                  label={t_i18n("Webhooks")}
                  value={hasWebhooks(item) ? t_i18n("Supported") : t_i18n("Not supported")}
                />
              </div>

              {children}

              {item.integration_types.length > 0 && (
                <Section icon={Boxes} title={t_i18n("Capabilities")} count={item.integration_types.length}>
                  <div className="flex flex-wrap gap-1">
                    {item.integration_types.map((type) => (
                      <Badge key={type} variant="outline" className="border-elevation-subtle-soft content-caption font-normal text-default-secondary">
                        {t_i18n(INTEGRATION_TYPE_LABELS[type] ?? type)}
                      </Badge>
                    ))}
                  </div>
                </Section>
              )}

              {item.available_events.length > 0 && (
                <Section icon={Zap} title={t_i18n("Events")} count={item.available_events.length}>
                  <CollapsingList>
                    {item.available_events.map((event) => (
                      <li key={event.event} className="flex items-start gap-2 content-compact">
                        <Badge variant="secondary" className="mt-px shrink-0 text-[9px] uppercase">
                          {event.mechanism}
                        </Badge>
                        <span className="min-w-0">
                          <span className="font-medium">{t_text(event.label)}</span>
                          {event.description && (
                            <span className="text-default-secondary"> — {t_text(event.description)}</span>
                          )}
                        </span>
                      </li>
                    ))}
                  </CollapsingList>
                </Section>
              )}

              {item.available_tools.length > 0 && (
                <Section icon={Wrench} title={t_i18n("Tools")} count={item.available_tools.length}>
                  <CollapsingList>
                    {item.available_tools.map((tool) => (
                      <li key={tool.name} className="flex items-start gap-2 content-compact">
                        <Badge
                          variant={tool.tool_type === "write" ? "warning" : "secondary"}
                          className="mt-px shrink-0 text-[9px] uppercase"
                        >
                          {tool.tool_type}
                        </Badge>
                        <span className="min-w-0">
                          <span className="font-mono-plex text-[11px]">{tool.name}</span>
                          {tool.description && (
                            <span className="text-default-secondary"> — {tool.description}</span>
                          )}
                        </span>
                      </li>
                    ))}
                  </CollapsingList>
                </Section>
              )}

              {item.setup_guide && (
                <SetupGuideDialog guide={item.setup_guide} providerName={item.name} variant="outline" />
              )}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
