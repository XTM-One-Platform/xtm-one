// SPDX-License-Identifier: Apache-2.0

/**
 * Integration Catalog administration (/settings/integrations → Catalog).
 *
 * Browsing is what users get on /integrations: same faceted sidebar, same
 * sectioned grid, same cards — through `IntegrationCatalogBrowser`, so an
 * admin recognises the catalog their users see. What differs is what a card carries and what a
 * click does: every card gets its availability controls, and opening one shows
 * the provider's details rather than starting a connection.
 */

import { useRef, useState, type ChangeEvent } from "react";
import {
  Check,
  X,
  Upload,
  AlertTriangle,
  Lock,
  Trash2,
  ExternalLink,
  Globe,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { SettingsGroupDescription, SettingsGroupTitle } from "@/components/ui/settings-form";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { IntegrationCatalogBrowser } from "@/components/integrations/integration-catalog-browser";
import { ProviderDetailsDialog } from "@/components/integrations/provider-details-dialog";
import {
  useImportIntegrationBundle,
  useProviderCatalog,
  useSetCustomIntegrationProviderEnabled,
  useUninstallCustomIntegrationProvider,
} from "@/hooks/api/useIntegrationApi";
import { useSetProviderDisabled } from "@/hooks/api/useSettingsApi";
import {
  usePlatformRegistrations,
  usePurgeRegistration,
} from "@/hooks/api/usePlatformApi";
import { INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { safeHttpUrl } from "@/lib/safe-url";
import { useFormatter } from "@/i18n";
import type { ProviderCatalogItem } from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";
import { Spinner } from "@filigran/design-system";

function registrationMatchesProvider(
  platformIdentifier: string,
  provider: string,
): boolean {
  if (provider === "openaev") {
    return platformIdentifier === "openaev" || platformIdentifier === "openbas";
  }
  return platformIdentifier === provider;
}

// ---------------------------------------------------------------------------
// Integration Catalog Management (admin section)
// ---------------------------------------------------------------------------
export function IntegrationCatalogSection() {
  const { t_i18n, intl } = useFormatter();
  // Explicitly include the disabled providers. The endpoint also serves them
  // to admins regardless, but a surface whose whole job is to switch a
  // disabled provider back on should not depend on that implicit behaviour.
  const { data: catalog } = useProviderCatalog("integration", true);
  const importBundle = useImportIntegrationBundle();
  const setProviderDisabled = useSetProviderDisabled();
  const uninstallCustom = useUninstallCustomIntegrationProvider();
  const setCustomEnabled = useSetCustomIntegrationProviderEnabled();
  const { data: registrations } = usePlatformRegistrations();
  const purgeRegistration = usePurgeRegistration();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const [selected, setSelected] = useState<ProviderCatalogItem | null>(null);
  // Which provider is being switched, so one toggle does not replace every
  // switch in the grid with a spinner and hide which row is working.
  const [pending, setPending] = useState<string | null>(null);
  // Same, per registration: purging one platform must not spin every other
  // purge button in the dialog.
  const [purging, setPurging] = useState<string | null>(null);
  const importInputRef = useRef<HTMLInputElement | null>(null);

  const onImportClick = () => {
    importInputRef.current?.click();
  };

  const onImportFileSelected = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    event.currentTarget.value = "";

    try {
      // Single upload: the import endpoint validates the bundle itself and
      // its response carries name/version, so a separate validate round-trip
      // would only double the bandwidth and add a race window.
      const imported = await importBundle.mutateAsync(file);
      toast({
        title: t_i18n("Bundle imported"),
        description: imported.integration_created
          ? t_i18n('{name} ({version}) installed with integration "{integration}".', { name: imported.name, version: imported.version, integration: imported.integration_name ?? imported.name })
          : t_i18n("{name} ({version}) installed successfully.", { name: imported.name, version: imported.version }),
        variant: "success",
      });
    } catch (error) {
      toast({
        title: t_i18n("Import failed"),
        description: error instanceof Error ? error.message : t_i18n("Unable to import bundle."),
        variant: "error",
      });
    }
  };

  const providers = catalog ?? [];

  const handleToggle = async (item: ProviderCatalogItem, currentlyEnabled: boolean) => {
    const provider = item.provider;
    const providerLabel = INTEGRATION_PROVIDER_NAMES[provider] || provider;

    if (currentlyEnabled) {
      const confirmed = await confirm({
        title: t_i18n("Disable {provider}?", { provider: providerLabel }),
        // Translated, like every other string here: this is the sentence that
        // tells an admin they are about to delete OTHER users' integrations.
        // Leaving it in English for a non-English admin is a data-loss hazard,
        // not a missing polish item.
        description: t_i18n("This will remove this provider from the integration catalog for all users. Any existing integrations using this provider will be deleted, and their bindings will be removed from all agents."),
        confirmLabel: t_i18n("Disable"),
        variant: "danger",
      });
      if (!confirmed) return;
    }

    setPending(provider);
    const onDone = {
      onSuccess: () => {
        toast({
          title: currentlyEnabled ? t_i18n("Provider disabled") : t_i18n("Provider enabled"),
          description: currentlyEnabled
            ? t_i18n("{provider} has been disabled and existing integrations deleted.", { provider: providerLabel })
            : t_i18n("{provider} is now available in the catalog.", { provider: providerLabel }),
        });
        // The open details dialog holds a copy of the entry it was opened
        // with, whose availability this toggle just invalidated — the
        // neighbouring destructive handlers close it for the same reason.
        setSelected(null);
      },
      onError: (err: Error) => {
        toast({
          title: t_i18n("Failed to update catalog"),
          description: err.message,
          variant: "error" as const,
        });
      },
      onSettled: () => setPending(null),
    };

    // An imported provider carries its own `enabled` flag and its own
    // endpoint. Writing the platform-wide disabled list for one would leave
    // the flag untouched: the card would keep reading Disabled, and the admin
    // would have no way to switch it back on from here.
    if (item.is_custom) {
      setCustomEnabled.mutate({ provider, enabled: !currentlyEnabled }, onDone);
      return;
    }
    setProviderDisabled.mutate({ provider, disabled: currentlyEnabled }, onDone);
  };

  const handlePurge = async (registrationId: string, platformTitle: string) => {
    const confirmed = await confirm({
      title: t_i18n("Purge {platform}?", { platform: platformTitle }),
      description: t_i18n("This will permanently delete this platform registration and ALL integrations for ALL users connected to this platform. Agent bindings will also be cleaned up. This action cannot be undone."),
      confirmLabel: t_i18n("Purge"),
      variant: "danger",
    });
    if (!confirmed) return;

    setPurging(registrationId);
    purgeRegistration.mutate(registrationId, {
      onSettled: () => setPurging(null),
      onSuccess: (summary) => {
        toast({
          title: t_i18n("Platform purged"),
          description: t_i18n("Deleted {integrations, plural, one {# integration} other {# integrations}} and cleaned {agents, plural, one {# agent} other {# agents}}.", { integrations: summary.integrations_deleted, agents: summary.agents_cleaned }),
        });
        setSelected(null);
      },
      onError: (err) => {
        toast({
          title: t_i18n("Failed to purge platform"),
          description: err.message,
          variant: "error",
        });
      },
    });
  };

  const handleUninstallCustom = async (providerKey: string, providerName: string) => {
    const confirmed = await confirm({
      title: t_i18n("Delete imported provider {provider}?", { provider: providerName }),
      description: t_i18n("This removes the imported provider definition, deletes integrations using it, and cleans related agent bindings."),
      confirmLabel: t_i18n("Delete"),
      variant: "danger",
    });
    if (!confirmed) return;

    setPending(providerKey);
    uninstallCustom.mutate(providerKey, {
      onSettled: () => setPending(null),
      onSuccess: () => {
        toast({
          title: t_i18n("Imported provider deleted"),
          description: t_i18n("{provider} has been removed from the catalog.", { provider: providerName }),
          variant: "success",
        });
        setSelected(null);
      },
      onError: (err) => {
        toast({
          title: t_i18n("Failed to delete imported provider"),
          description: err.message,
          variant: "error",
        });
      },
    });
  };

  /**
   * Whether a provider is offered to users — read from the catalog entry, and
   * only from there.
   *
   * Two mechanisms can switch a provider off: the platform-wide disabled list
   * and, for an imported bundle, its own `enabled` flag. The catalog folds
   * both into `is_disabled`, which is also what every other surface renders
   * from. Deciding here from a second, independently cached query would let
   * the two drift — this card reading Enabled while the user catalog hides
   * the provider — so the settings list is written to and never read.
   */
  const isEnabled = (provider: ProviderCatalogItem) => !provider.is_disabled;

  const registrationsOf = (provider: string) =>
    (registrations ?? []).filter((r) =>
      registrationMatchesProvider(r.platform_identifier, provider),
    );

  /** Availability controls — in the card footer and again in the details. */
  const availabilityControls = (provider: ProviderCatalogItem) => {
    const enabled = isEnabled(provider);
    const regOnly = provider.is_registration_only;
    const regCount = regOnly ? registrationsOf(provider.provider).length : 0;

    return (
      <>
        <div className="flex flex-1 flex-wrap items-center gap-1.5">
          {regOnly ? (
            <>
              <Badge variant="outline" className="content-caption">
                <Lock className="mr-1 h-3 w-3" />
                {t_i18n("Registration only")}
              </Badge>
              {regCount > 0 && (
                <Badge variant="secondary" className="content-caption">
                  {t_i18n("{count} registered", { count: regCount })}
                </Badge>
              )}
            </>
          ) : enabled ? (
            <Badge variant="success" className="content-caption">
              <Check className="mr-1 h-3 w-3" />
              {t_i18n("Enabled")}
            </Badge>
          ) : (
            <Badge variant="destructive" className="content-caption">
              <X className="mr-1 h-3 w-3" />
              {t_i18n("Disabled")}
            </Badge>
          )}
        </div>
        <div className="flex items-center gap-2">
          {provider.is_custom && (
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7 text-default-secondary hover:text-feedback-error-primary"
              aria-label={t_i18n("Delete imported provider {provider}", { provider: provider.name })}
              onClick={() => handleUninstallCustom(provider.provider, provider.name)}
              disabled={pending === provider.provider} aria-busy={(pending === provider.provider) || undefined}>
              {pending === provider.provider && <Spinner size="sm" tone="inherit" />}{!(pending === provider.provider) && <Trash2 className="h-3.5 w-3.5" />}
            </Button>
          )}
          {regOnly ? null : pending === provider.provider ? (
            <Spinner size="sm" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} />
          ) : (
            <Switch
              checked={enabled}
              aria-label={t_i18n("Enable {provider}", { provider: provider.name })}
              onCheckedChange={() => handleToggle(provider, enabled)}
            />
          )}
        </div>
      </>
    );
  };

  const selectedRegistrations = selected ? registrationsOf(selected.provider) : [];

  return (
    <>
      {ConfirmEl}
      <input
        ref={importInputRef}
        type="file"
        accept=".zip,application/zip"
        className="hidden"
        onChange={onImportFileSelected}
      />

      <IntegrationCatalogBrowser
        catalog={providers}
        variant="admin"
        onSelect={setSelected}
        searchPlaceholder={t_i18n("Search providers...")}
        heroTitle={t_i18n("Integration Catalog")}
        heroDescription={t_i18n("Control which integration providers are available to users. Open a provider to see what it offers.")}
        heroActions={
          <Button
            variant="secondary"
            size="sm"
            className="gap-1.5"
            onClick={onImportClick}
            disabled={importBundle.isPending}
          >
            <Upload className="h-3.5 w-3.5" />
            {importBundle.isPending ? t_i18n("Importing...") : t_i18n("Import Bundle")}
          </Button>
        }
        heroNotice={
          <div className="flex items-start gap-2 rounded-sm bg-feedback-alert-primary/5 p-2.5">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" />
            <p className="content-compact text-feedback-warning-primary">
              {t_i18n("Disabling a provider will immediately remove it from the catalog for all users, delete any existing integrations using that provider, and clean up agent bindings.")}
            </p>
          </div>
        }
        cardFooter={availabilityControls}
      />

      {/* Provider details — what the card holds behind a click */}
      <ProviderDetailsDialog
        item={selected}
        open={selected !== null}
        onOpenChange={(open) => { if (!open) setSelected(null); }}
      >
        {selected && (
          <div className="space-y-5">
            <div className="flex items-center gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3">
              {availabilityControls(selected)}
            </div>

            {selected.is_registration_only && (
              <div className="space-y-2">
                <div>
                  <SettingsGroupTitle icon={Globe}>{t_i18n("Registered Platforms")}</SettingsGroupTitle>
                  <SettingsGroupDescription>
                    {t_i18n("Platforms registered via XTM One. Purging a platform will delete its registration and all integrations for all users.")}
                  </SettingsGroupDescription>
                </div>

                {selectedRegistrations.length === 0 ? (
                  <div className="py-6 text-center content-base text-default-secondary">
                    <Globe className="mx-auto mb-2 h-8 w-8 opacity-40" />
                    {t_i18n("No platforms registered for this provider.")}
                  </div>
                ) : (
                  selectedRegistrations.map((reg) => (
                    <div
                      key={reg.id}
                      className="space-y-3 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-4"
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0 flex-1">
                          <p className="content-base font-medium">{reg.platform_title}</p>
                          <RegisteredPlatformUrl url={reg.platform_url} />
                        </div>
                        <Badge
                          variant={reg.status === "active" ? "success" : "destructive"}
                          className="shrink-0 content-caption"
                        >
                          {reg.status}
                        </Badge>
                      </div>

                      <div className="grid grid-cols-2 gap-x-4 gap-y-1 content-compact text-default-secondary">
                        <span>{t_i18n("Version:")} <span className="text-default-primary">{reg.platform_version || "—"}</span></span>
                        <span>{t_i18n("Contract:")} <span className="text-default-primary">{reg.contract}</span></span>
                        <span>{t_i18n("Registered:")} <span className="text-default-primary">{formatTimeAgo(intl, reg.registered_at)}</span></span>
                        <span>{t_i18n("Last heartbeat:")} <span className="text-default-primary">{formatTimeAgo(intl, reg.last_heartbeat_at)}</span></span>
                      </div>

                      <div className="border-t border-elevation-subtle-soft pt-2">
                        <Button
                          variant="destructive"
                          size="sm"
                          className="h-7 w-full content-compact"
                          onClick={() => handlePurge(reg.id, reg.platform_title)}
                          disabled={purging === reg.id} aria-busy={(purging === reg.id) || undefined}>
                          {purging === reg.id && <Spinner size="sm" tone="inherit" />}{!(purging === reg.id) && <Trash2 className="mr-1 h-3 w-3" />}
                          {t_i18n("Purge platform & all integrations")}
                        </Button>
                      </div>
                    </div>
                  ))
                )}
              </div>
            )}
          </div>
        )}
      </ProviderDetailsDialog>

    </>
  );
}

/**
 * A registered platform's address, which the platform itself sent: a link
 * only when it is a web address (`safeHttpUrl`), else the text.
 */
function RegisteredPlatformUrl({ url }: { url: string }) {
  const href = safeHttpUrl(url);
  if (!href) return <p className="mt-0.5 break-all content-compact text-default-secondary">{url}</p>;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="mt-0.5 inline-flex items-center gap-1 content-compact text-accent hover:underline"
    >
      {url}
      <ExternalLink className="h-3 w-3" />
    </a>
  );
}
