// SPDX-License-Identifier: Apache-2.0

/**
 * Settings > Platform > Brand asset source: a GitHub folder of brand asset
 * packs the built-in templates read beside the packs the platform ships
 * (`services/brand_asset_source.py`), with the connected GitHub integration
 * whose token reads it. Saving downloads the folder and seeds its packs at
 * once; every restart does it again. Administrators only, like the route
 * (`/settings/brand-asset-source`).
 */
import { useId, useState } from "react";
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Input } from "@/components/ui/input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import {
  SettingsCard,
  SettingsField,
  SettingsSaveBar,
  settingsFieldHintId,
  settingsFieldLabelId,
} from "@/components/ui/settings-form";
import { useToast } from "@/components/ui/toast";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import {
  useBrandAssetSource,
  useSaveBrandAssetSource,
  useSyncBrandAssetSource,
} from "@/hooks/api/useTemplateContainerApi";
import { useFormatter } from "@/i18n";
import type { BrandAssetSourceConfig, BrandAssetSourceSync } from "@/types";

/** The card's glyph, at the size and colour SettingsCard gives it. */
const FolderGlyph = ({ className }: { className?: string }) => <Icon name="folder" size={16} className={className} />;

/** A GitHub folder URL, as the API parses it (`parse_github_folder_url`, whose host match ignores case). */
const FOLDER_URL = /^https:\/\/(www\.)?github\.com\/[^/?#]+\/[^/?#]+(\/tree\/[^/?#]+(\/[^?#]*)?)?\/?$/i;

export function BrandAssetSourceCard() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const urlId = useId();
  const integrationId = useId();
  const { data: source } = useBrandAssetSource();
  const { data: integrations } = useIntegrations({ scope: "all" });
  const save = useSaveBrandAssetSource();
  const sync = useSyncBrandAssetSource();
  const [draft, setDraft] = useState<BrandAssetSourceConfig | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  if (!source) return null;

  const value: BrandAssetSourceConfig = draft ?? { url: source.url, integration_id: source.integration_id };
  // The source is read unattended, so an integration acting with each person's
  // own account cannot read it: the server refuses one (422), so it is not offered.
  const github = (integrations ?? []).filter(
    (i) => i.provider === "github" && i.status === "connected" && i.credential_mode !== "per_user",
  );
  const url = (value.url ?? "").trim();
  const urlInvalid = url !== "" && !FOLDER_URL.test(url);
  const integrationMissing = url !== "" && !value.integration_id;
  const busy = save.isPending || sync.isPending;

  const report = (result: BrandAssetSourceSync | null) => {
    if (!result) return;
    if (result.error) {
      toast({ title: t_i18n("The brand asset source was not read"), description: result.error, variant: "error" });
    } else {
      const read = t_i18n("{count, plural, one {# pack} other {# packs}} read, {uploaded, plural, one {# new or changed file} other {# new or changed files}} seeded.", {
        count: result.packs.length,
        uploaded: result.assets_uploaded,
      });
      // A pack the platform also ships loads from the platform: say so, or
      // the folder would look read while its copy is not used.
      const shadowed = result.shadowed.length
        ? ` ${t_i18n("Also shipped by the platform, which uses its own: {names}.", { names: result.shadowed.join(", ") })}`
        : "";
      toast({
        title: t_i18n("Brand asset source read"),
        description: read + shadowed,
        variant: result.shadowed.length ? "info" : "success",
      });
    }
  };
  const failed = (title: string) => (err: unknown) =>
    toast({ title, description: err instanceof Error ? err.message : undefined, variant: "error" });

  const submit = (body: BrandAssetSourceConfig) =>
    save.mutate(body, {
      onSuccess: (data) => {
        setDraft(null);
        if (data.sync) report(data.sync);
        else toast({ title: t_i18n("Brand asset source cleared"), variant: "success" });
      },
      onError: failed(t_i18n("The brand asset source was not saved")),
    });

  return (
    <SettingsCard
      icon={FolderGlyph}
      title={t_i18n("Brand asset source")}
      description={t_i18n(
        "A GitHub folder of brand asset packs the built-in templates read, beside the ones the platform ships. Read on every restart and when saved.",
      )}
      action={
        source.url && !draft ? (
          <>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => sync.mutate(undefined, { onSuccess: (data) => report(data.sync), onError: failed(t_i18n("The brand asset source was not read")) })}
            >
              {t_i18n("Sync now")}
            </Button>
            <Button variant="ghost" disabled={busy} onClick={() => setConfirmClear(true)}>
              {t_i18n("Clear")}
            </Button>
          </>
        ) : undefined
      }
    >
      <div className="space-y-4">
        <SettingsField
          id={urlId}
          label={t_i18n("GitHub folder")}
          helper={t_i18n("For example https://github.com/owner/repository/tree/main/brand-assets")}
          info={t_i18n(
            "Each sub-folder holding a manifest.json is a pack. With no /tree/ part, the root of the repository's default branch is read.",
          )}
          error={urlInvalid ? t_i18n("Enter a GitHub folder URL") : undefined}
        >
          <Input
            id={urlId}
            aria-labelledby={settingsFieldLabelId(urlId)}
            aria-describedby={settingsFieldHintId(urlId)}
            className="max-w-xl font-mono-plex"
            value={value.url ?? ""}
            placeholder="https://github.com/owner/repository/tree/main/brand-assets"
            onChange={(e) => setDraft({ ...value, url: e.target.value })}
            disabled={busy}
          />
        </SettingsField>
        <SettingsField
          id={integrationId}
          label={t_i18n("GitHub integration")}
          helper={
            github.length > 0
              ? t_i18n("A connected GitHub integration that can read the repository (Contents: Read) and shares its account.")
              : t_i18n("Connect a GitHub integration in Integrations first.")
          }
          error={integrationMissing ? t_i18n("Select the GitHub integration that reads the folder") : undefined}
        >
          <SearchableSelect
            id={integrationId}
            aria-labelledby={settingsFieldLabelId(integrationId)}
            aria-describedby={settingsFieldHintId(integrationId)}
            className="max-w-md"
            options={github.map((i) => ({ value: i.id, label: i.name }))}
            value={value.integration_id ?? ""}
            placeholder={source.integration_name ?? t_i18n("Select an integration")}
            onValueChange={(next) => setDraft({ ...value, integration_id: next || null })}
            disabled={busy || github.length === 0}
          />
        </SettingsField>
      </div>
      <ConfirmDialog
        open={confirmClear}
        onOpenChange={setConfirmClear}
        variant="warning"
        title={t_i18n("Clear the brand asset source?")}
        description={t_i18n(
          "The platform stops reading the folder. At the next restart or sync, a picture only this folder gave a template directly is removed from it, and from the slides that place it.",
        )}
        confirmLabel={t_i18n("Clear")}
        onConfirm={() => {
          setConfirmClear(false);
          submit({ url: null, integration_id: null });
        }}
      />
      {draft && (
        <SettingsSaveBar
          dirty={draft.url !== source.url || draft.integration_id !== source.integration_id}
          saving={save.isPending}
          saveDisabled={urlInvalid || integrationMissing}
          onSave={() => submit({ url: url || null, integration_id: url ? value.integration_id : null })}
          onCancel={() => setDraft(null)}
        />
      )}
    </SettingsCard>
  );
}
