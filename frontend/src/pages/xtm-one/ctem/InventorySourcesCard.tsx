// SPDX-License-Identifier: Apache-2.0

/**
 * CTEM > Inventory > Sources (#1178 slice 3): how the inventory is fed and merged.
 * The Sources tab is the shared Sources and syncs table (#1276) scoped to "inventory" alone (the business profile's
 * syncs stay in Settings), so its table is the one scroller of the screen (UX round 2 on #1254,
 * B7). Asset matching opens in a view of its own from the page header: the identifiers that merge
 * the records of several sources into one asset, most reliable first, and per field the order of
 * the sources it is taken from. Read for everyone; Edit and Save for administrators, like
 * `PUT /organization-context`; the IP address never merges by itself.
 */
import { useState } from "react";
import { Icon, Spinner } from "@filigran/design-system";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SettingsCard, SettingsSaveBar } from "@/components/ui/settings-form";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useAssetIdentity } from "@/hooks/api/useOrganizationAssetsApi";
import { useOrganizationContext, usePatchOrganizationContext } from "@/hooks/api/useOrganizationContextApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import {
  type IdentityDraft,
  IDENTITY_KEY_LABELS,
  PRECEDENCE_FIELD_LABELS,
  SOURCE_CLASS_LABELS,
  draftOf,
  moveItem,
  noKeyOn,
  savedOf,
} from "@/lib/asset-identity";
import { PLATFORM_NAMES, fromForm, toForm } from "@/lib/organization-profile";
import { INVENTORY_IMPORT_SECTIONS, OrganizationImportDialog } from "@/pages/settings/OrganizationImportDialog";
import { OrganizationSourcesSyncs, type SyncPreset } from "@/pages/settings/OrganizationSourcesSyncs";

/**
 * The Sources page: the inventory's syncs, nothing below them. Administrators add a source (platform, section, filters,
 * preview, then a schedule) limited to the inventory sections (#1367, point 4: the ability was lost with the move).
 * "Set up a sync" on a platform whose rows outlived their sync opens the same import on that platform and section (#1940).
 */
export function InventorySourcesCard() {
  const { isAdmin } = useAuth();
  // false: closed; true: a blank import; a preset: the import of one platform and section.
  const [adding, setAdding] = useState<boolean | SyncPreset>(false);
  const preset = typeof adding === "object" ? adding : undefined;
  return (
    <div data-testid="ctem-inventory.sources">
      <OrganizationSourcesSyncs scope="inventory" onAddSource={isAdmin ? () => setAdding(true) : undefined} onSetUpSync={isAdmin ? setAdding : undefined} />
      {adding !== false && (
        <OrganizationImportDialog
          open
          onOpenChange={(next) => !next && setAdding(false)}
          sections={INVENTORY_IMPORT_SECTIONS}
          initialSection={preset?.section}
          initialSource={preset?.source}
        />
      )}
    </div>
  );
}

function MoveButtons({ label, index, count, onMove }: { label: string; index: number; count: number; onMove: (delta: -1 | 1) => void }) {
  const { t_i18n } = useFormatter();
  return (
    <span className="inline-flex gap-0.5">
      <Tip content={t_i18n("Move up")}>
        <Button variant="ghost" size="icon" aria-label={t_i18n("Move {name} up", { name: label })} disabled={index === 0} onClick={() => onMove(-1)}>
          <Icon name="arrow-up" size={16} aria-hidden />
        </Button>
      </Tip>
      <Tip content={t_i18n("Move down")}>
        <Button variant="ghost" size="icon" aria-label={t_i18n("Move {name} down", { name: label })} disabled={index === count - 1} onClick={() => onMove(1)}>
          <Icon name="arrow-down" size={16} aria-hidden />
        </Button>
      </Tip>
    </span>
  );
}

/** Asset matching, the view the Sources tab opens from the page header. */
export function AssetMatchingCard() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const identity = useAssetIdentity();
  const { data: context } = useOrganizationContext();
  const save = usePatchOrganizationContext();
  const [draft, setDraft] = useState<IdentityDraft | null>(null);

  const defaults = identity.data?.defaults;
  const effective = identity.data?.effective;
  const sourceName = (source: string) => PLATFORM_NAMES[source] ?? (SOURCE_CLASS_LABELS[source] ? t_i18n(SOURCE_CLASS_LABELS[source]) : source);
  const fieldName = (field: string) => (PRECEDENCE_FIELD_LABELS[field] ? t_i18n(PRECEDENCE_FIELD_LABELS[field]) : field);
  const keyName = (key: string) => (IDENTITY_KEY_LABELS[key] ? t_i18n(IDENTITY_KEY_LABELS[key]) : key);

  const view = draft ?? (effective && defaults ? draftOf(effective, defaults) : null);
  const saved = draft && defaults ? savedOf(draft, defaults) : null;
  const current = context?.profile.asset_identity ?? { key_order: [], precedence: {} };
  const dirty = saved !== null && JSON.stringify(saved) !== JSON.stringify({ key_order: current.key_order, precedence: current.precedence });
  const blocked = draft !== null && noKeyOn(draft);

  const onSave = () => {
    if (!context || !saved || blocked) return;
    const parsed = fromForm({ ...toForm(context.profile), asset_identity: saved });
    if ("invalid" in parsed) return;
    // #1752: the matching rules alone travel, never the asset rows.
    save.mutate(
      { fields: { asset_identity: parsed.profile.asset_identity }, based_on_version: context.version ?? null },
      {
        onSuccess: () => {
          setDraft(null);
          void identity.refetch();
          toast({ title: t_i18n("Asset matching saved. The next sync merges with it."), variant: "success" });
        },
        onError: (err) => toast({ title: t_i18n("Asset matching was not saved"), description: err.message, variant: "error" }),
      },
    );
  };

  const changed = (saved ?? current).key_order.length > 0 || Object.keys((saved ?? current).precedence).length > 0;

  return (
    <div data-testid="ctem-inventory.matching-view">
      <SettingsCard
        data-testid="ctem-inventory.matching"
        title={t_i18n("Asset matching")}
        description={t_i18n("Which identifier merges the records of several sources into one asset, and which source each field is taken from.")}
        meta={changed ? <Badge variant="outline">{t_i18n("Changed from the defaults")}</Badge> : undefined}
        action={
          !draft && isAdmin ? (
            <Button
              variant="secondary"
              className="gap-1.5"
              disabled={!view || !context}
              aria-label={t_i18n("Edit {name}", { name: t_i18n("Asset matching") })}
              onClick={() => view && setDraft({ keys: view.keys.map((k) => ({ ...k })), precedence: { ...view.precedence } })}
              data-testid="ctem-inventory.matching.edit"
            >
              <Icon name="pencil" size={16} aria-hidden /> {t_i18n("Edit")}
            </Button>
          ) : undefined
        }
      >
        {identity.isLoading && !view ? (
          <Spinner size="sm" label={t_i18n("Loading")} />
        ) : identity.isError || !view ? (
          <div className="flex items-center gap-2">
            <p className="text-default-secondary">{t_i18n("Asset matching could not be read.")}</p>
            <Button variant="secondary" onClick={() => void identity.refetch()}>
              {t_i18n("Retry")}
            </Button>
          </div>
        ) : (
          <div className="space-y-6">
            <section aria-labelledby="ctem-inventory-matching-keys" className="space-y-2">
              <h3 id="ctem-inventory-matching-keys" className="title-sm">
                {t_i18n("Identifiers, most reliable first")}
              </h3>
              <p className="content-compact text-default-secondary">
                {t_i18n("Two records with different values of an earlier identifier never merge on a later one. The IP address never merges by itself.")}
              </p>
              <ol className="divide-y divide-[var(--border-elevation-subtle)] rounded-sm border border-elevation-subtle" data-testid="ctem-inventory.matching.keys">
                {view.keys.map((entry, index) => (
                  <li key={entry.key} className="flex items-center justify-between gap-3 px-3 py-1.5">
                    <span className={entry.on ? undefined : "text-default-secondary"}>
                      {entry.on ? `${view.keys.slice(0, index + 1).filter((k) => k.on).length}. ` : ""}
                      {keyName(entry.key)}
                      {!entry.on && ` (${t_i18n("never merges")})`}
                    </span>
                    {draft && (
                      <span className="inline-flex items-center gap-2">
                        <Switch
                          checked={entry.on}
                          aria-label={t_i18n("Merge on {name}", { name: keyName(entry.key) })}
                          onCheckedChange={(on) => setDraft({ ...draft, keys: draft.keys.map((k) => (k.key === entry.key ? { ...k, on } : k)) })}
                        />
                        <MoveButtons label={keyName(entry.key)} index={index} count={view.keys.length} onMove={(delta) => setDraft({ ...draft, keys: moveItem(draft.keys, index, delta) })} />
                      </span>
                    )}
                  </li>
                ))}
              </ol>
              {blocked && (
                <p className="text-feedback-error-primary" role="alert">
                  {t_i18n("Keep at least one identifier on, or every record stays an asset of its own.")}
                </p>
              )}
            </section>

            <section aria-labelledby="ctem-inventory-matching-fields" className="space-y-2">
              <h3 id="ctem-inventory-matching-fields" className="title-sm">
                {t_i18n("Source of each field")}
              </h3>
              <p className="content-compact text-default-secondary">
                {t_i18n("A field shows the value of the first source in its order that knows it. Environment, group, description and business service are only set by a person.")}
              </p>
              <dl className="divide-y divide-[var(--border-elevation-subtle)] rounded-sm border border-elevation-subtle" data-testid="ctem-inventory.matching.fields">
                {Object.entries(view.precedence).map(([field, order]) => (
                  <div key={field} className="grid gap-2 px-3 py-2 sm:grid-cols-[12rem_1fr]">
                    <dt className="font-medium">{fieldName(field)}</dt>
                    <dd>
                      {draft ? (
                        <ol className="flex flex-wrap gap-2">
                          {order.map((source, index) => (
                            <li key={source} className="inline-flex items-center gap-1 rounded-sm border border-elevation-subtle pl-2">
                              {sourceName(source)}
                              <MoveButtons
                                label={`${fieldName(field)}: ${sourceName(source)}`}
                                index={index}
                                count={order.length}
                                onMove={(delta) => setDraft({ ...draft, precedence: { ...draft.precedence, [field]: moveItem(order, index, delta) } })}
                              />
                            </li>
                          ))}
                        </ol>
                      ) : (
                        order.map(sourceName).join(" > ")
                      )}
                    </dd>
                  </div>
                ))}
              </dl>
            </section>
            {draft && (
              <div className="flex items-center justify-between gap-3">
                <Button variant="ghost" onClick={() => defaults && setDraft(draftOf(defaults, defaults))}>
                  {t_i18n("Reset to the defaults")}
                </Button>
              </div>
            )}
            {draft && <SettingsSaveBar dirty={dirty} saving={save.isPending} saveDisabled={!dirty || blocked} onSave={onSave} onCancel={() => setDraft(null)} />}
          </div>
        )}
      </SettingsCard>
    </div>
  );
}
