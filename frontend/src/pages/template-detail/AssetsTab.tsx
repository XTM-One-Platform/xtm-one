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

import { useMemo, useRef, useState } from "react";
import {
  Paperclip,
  Upload,
  Download,
  Trash2,
  Replace,
  Tag as TagIcon,
  Pencil,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SearchInput } from "@/components/ui/search-input";
import { Tip } from "@/components/ui/tooltip";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { TEMPLATE_ASSET_ROLES } from "@/lib/constants";
import { cn, formatShortDateTime, pluralize, saveBlobAs } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import {
  templateAssetThumbnailUrl,
  useAddTemplateAsset,
  useDeleteTemplateAsset,
  useDownloadTemplateAsset,
  useReplaceTemplateAsset,
  useUpdateTemplateAssetRole,
} from "@/hooks/api/useTemplateContainerApi";
import { useFirstAuthenticatedSrc } from "@/hooks/useAuthenticatedSrc";
import { templateAssetPreviewSources } from "@/lib/template-asset-preview";
import type { TemplateAsset, TemplateContainerDetail } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { Spinner } from "@filigran/design-system";

/** Sentinel for "no role" — an empty string is not a valid Radix SelectItem
 *  value, and the API models an unlabelled asset as `asset_role: null`. */
const NO_ROLE = "__none__";

/** Sentinel for the role filter's "every role" choice (same Radix constraint). */
const ALL_ROLES = "__all__";

/** The same match the agent's `get_template(role=, query=)` applies:
 *  an exact role, and a case-insensitive substring of the filename or the
 *  caption. The Filigran Brand Deck alone holds ~180 assets once the partner /
 *  integration / customer logos live in it, so a flat list is not browsable. */
function assetMatches(asset: TemplateAsset, role: string, needle: string): boolean {
  if (role !== ALL_ROLES && (asset.asset_role ?? NO_ROLE) !== role) return false;
  if (!needle) return true;
  return (
    asset.filename.toLowerCase().includes(needle)
    || (asset.description ?? "").toLowerCase().includes(needle)
  );
}

/** Human-readable byte size. Assets are fonts and images, so KB/MB is the
 *  useful range; `formatCompactNumber` would render "204.8K" without a unit. */
function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** The asset itself, for image assets — a brand container holds a dozen
 *  logos and diagrams, and "xtm-platform.svg" is not a preview. Drawn on a
 *  mid-tone surface so that both a light-on-dark and a dark-on-light artwork
 *  stay legible; the bytes are the asset's bounded thumbnail when the
 *  route renders its type, else - and whenever it has none - the original
 *  through the authenticated download route, as before. A replace keeps the
 *  asset id, so both URLs carry `version`: it is what `useAuthenticatedSrc`
 *  refetches on. */
function AssetThumb({ asset, containerId }: { asset: TemplateAsset; containerId: string }) {
  const isImage = asset.content_type.startsWith("image/");
  const src = useFirstAuthenticatedSrc(
    isImage
      ? templateAssetPreviewSources(
          asset.content_type,
          templateAssetThumbnailUrl(containerId, asset),
          `/api/v1/template-containers/${containerId}/assets/${asset.id}/download?v=${encodeURIComponent(asset.version)}`,
        )
      : [],
  );
  return (
    <div
      className={cn(
        "flex h-12 w-20 shrink-0 items-center justify-center overflow-hidden rounded-sm",
        isImage ? "bg-[var(--text-default-secondary)]" : "bg-accent/10",
      )}
    >
      {isImage && src ? (
        <img src={src} alt="" className="h-full w-full object-contain p-1.5" />
      ) : (
        <Paperclip className="h-4 w-4 text-accent" />
      )}
    </div>
  );
}

/** The caption an agent picks the asset by. Read-only text for viewers; in
 *  manage mode a click turns it into an input that commits on Enter / blur
 *  and cancels on Escape. Sent alone, so it never disturbs the role. */
function AssetDescription({
  asset, containerId, canManage,
}: {
  asset: TemplateAsset;
  containerId: string;
  canManage: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateTemplateAssetRole();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(asset.description ?? "");

  const commit = () => {
    setEditing(false);
    const next = draft.trim() || null;
    if (next === (asset.description ?? null)) return;
    update.mutate(
      { containerId, assetId: asset.id, description: next },
      { onSuccess: () => toast({ title: t_i18n("Asset description updated"), variant: "success" }) },
    );
  };

  if (editing) {
    return (
      <Input aria-label={t_i18n("Description")}
        autoFocus
        value={draft}
        maxLength={500}
        placeholder={t_i18n("What does this asset depict? Agents pick assets by this caption.")}
        className="mt-1 h-7 content-compact"
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
          if (e.key === "Escape") {
            setDraft(asset.description ?? "");
            setEditing(false);
          }
        }}
      />
    );
  }

  if (!canManage) {
    return asset.description ? (
      <p className="content-compact text-default-secondary truncate">{asset.description}</p>
    ) : null;
  }

  return (
    <button
      type="button"
      className="group/desc mt-0.5 flex max-w-full items-center gap-1.5 text-left"
      onClick={() => {
        setDraft(asset.description ?? "");
        setEditing(true);
      }}
    >
      <span
        className={cn(
          "truncate content-compact",
          asset.description ? "text-default-secondary" : "italic text-default-secondary",
        )}
      >
        {asset.description || t_i18n("What does this asset depict? Agents pick assets by this caption.")}
      </span>
      {update.isPending && <Spinner size="sm" tone="inherit" className="shrink-0 text-default-secondary" />}{!update.isPending && <Pencil className="h-3 w-3 shrink-0 text-default-disabled opacity-0 transition-opacity group-hover/desc:opacity-100" />}
    </button>
  );
}

function AssetRow({
  asset, containerId, canManage,
}: {
  asset: TemplateAsset;
  containerId: string;
  canManage: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const replaceInputRef = useRef<HTMLInputElement>(null);

  const relabel = useUpdateTemplateAssetRole();
  const replace = useReplaceTemplateAsset();
  const download = useDownloadTemplateAsset();
  const remove = useDeleteTemplateAsset();

  const handleReplace = (file: File | undefined) => {
    if (!file) return;
    replace.mutate(
      { containerId, assetId: asset.id, file },
      {
        onSuccess: () =>
          toast({
            title: t_i18n("Asset replaced"),
            // Worth stating: the stable id is why replacing a logo does not
            // break a layout that references it.
            description: t_i18n("The binary was swapped in place; the asset id is unchanged."),
            variant: "success",
          }),
      },
    );
  };

  const handleDelete = async () => {
    if (
      await confirm({
        title: "Remove Asset",
        description: `Are you sure you want to remove "${asset.filename}"? Layouts referencing this asset will no longer resolve it.`,
        confirmLabel: "Remove",
        variant: "danger",
      })
    ) {
      remove.mutate(
        { containerId, assetId: asset.id },
        { onSuccess: () => toast({ title: t_i18n("Asset removed"), variant: "success" }) },
      );
    }
  };

  const aiOff = useAiActionDisabled();
  const busy = relabel.isPending || replace.isPending || remove.isPending || aiOff;

  return (
    <div className="flex items-center gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
      {ConfirmEl}
      <input
        ref={replaceInputRef}
        type="file"
        className="hidden"
        onChange={(e) => {
          handleReplace(e.target.files?.[0]);
          // Reset so replacing with the same filename twice still fires change.
          e.target.value = "";
        }}
      />

      <AssetThumb asset={asset} containerId={containerId} />

      <div className="min-w-0 flex-1">
        <p className="content-base font-medium truncate">{asset.filename}</p>
        <AssetDescription asset={asset} containerId={containerId} canManage={canManage} />
        <p className="content-caption text-default-secondary">
          {asset.content_type} · {formatSize(asset.size)} · {formatShortDateTime(asset.created_at)}
        </p>
      </div>

      {/* Role: a select in manage mode, a badge otherwise. Relabelling is its
          own request — it never touches the binary. */}
      {canManage ? (
        <SearchableSelect
          aria-label={t_i18n("Asset role")}
          className="w-44 shrink-0"
          // The backend accepts roles outside the suggested set, so an existing
          // non-standard value must remain selectable - otherwise opening this
          // picker would silently drop it.
          options={[...TEMPLATE_ASSET_ROLES, ...(asset.asset_role ? [asset.asset_role] : [])].map((role) => ({ value: role, label: role }))}
          pinned={[{ value: NO_ROLE, label: t_i18n("No role") }]}
          value={asset.asset_role ?? NO_ROLE}
          onValueChange={(value) =>
            relabel.mutate(
              { containerId, assetId: asset.id, assetRole: value === NO_ROLE ? null : value },
              { onSuccess: () => toast({ title: t_i18n("Asset role updated"), variant: "success" }) },
            )
          }
        />
      ) : (
        <Badge variant="outline" className="content-caption gap-1 shrink-0">
          <TagIcon className="h-2.5 w-2.5" />
          {asset.asset_role || t_i18n("No role")}
        </Badge>
      )}

      <div className="flex items-center gap-1 shrink-0">
        <Tip content={t_i18n("Download")}>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 w-7 p-0"
            disabled={download.isPending}
            onClick={() =>
              download.mutate(
                { containerId, assetId: asset.id },
                {
                  onSuccess: ({ blob, filename }) =>
                    saveBlobAs(blob, filename ?? asset.filename),
                },
              )
            } aria-busy={download.isPending || undefined}>
            {download.isPending && <Spinner size="sm" tone="inherit" />}{!download.isPending && <Download className="h-3.5 w-3.5" />}
          </Button>
        </Tip>
        {canManage && (
          <>
            <Tip content={t_i18n("Replace binary (keeps the asset id)")}>
              <Button
                variant="ghost"
                size="sm"
                className="h-7 w-7 p-0"
                disabled={busy}
                onClick={() => replaceInputRef.current?.click()} aria-busy={replace.isPending || undefined}>
                {replace.isPending && <Spinner size="sm" tone="inherit" />}{!replace.isPending && <Replace className="h-3.5 w-3.5" />}
              </Button>
            </Tip>
            <Tip content={t_i18n("Remove")}>
              <Button aria-label={t_i18n("Remove")}
                variant="ghost"
                size="sm"
                className="h-7 w-7 p-0 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10"
                disabled={busy || aiOff}
                onClick={handleDelete}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </Tip>
          </>
        )}
      </div>
    </div>
  );
}

export function AssetsTab({
  template, canManage,
}: {
  template: TemplateContainerDetail;
  canManage: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const addAsset = useAddTemplateAsset();
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const [pendingRole, setPendingRole] = useState<string>(NO_ROLE);
  const [query, setQuery] = useState("");
  const [roleFilter, setRoleFilter] = useState<string>(ALL_ROLES);

  const assets = useMemo(() => template.assets ?? [], [template.assets]);

  // Roles actually present, with their counts: the filter offers what the
  // container holds, not the whole vocabulary, so an empty choice never exists.
  const roleCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const asset of assets) {
      const key = asset.asset_role ?? NO_ROLE;
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    return [...counts.entries()].sort(([a], [b]) => {
      if (a === NO_ROLE) return 1;
      if (b === NO_ROLE) return -1;
      return a.localeCompare(b);
    });
  }, [assets]);
  const needle = query.trim().toLowerCase();
  const filtering = needle !== "" || roleFilter !== ALL_ROLES;
  const visible = filtering ? assets.filter((asset) => assetMatches(asset, roleFilter, needle)) : assets;

  const handleUpload = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    // Uploaded one at a time on purpose: the API caps assets per container and
    // returns 409 when full, so sequential requests surface exactly which file
    // was refused instead of failing the whole batch opaquely.
    for (const file of Array.from(files)) {
      addAsset.mutate(
        { containerId: template.id, file, assetRole: pendingRole === NO_ROLE ? undefined : pendingRole },
        {
          onSuccess: () => toast({ title: t_i18n("Asset uploaded"), description: file.name, variant: "success" }),
        },
      );
    }
  };

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 p-4 pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Paperclip className="h-4 w-4 text-accent" />
          {t_i18n("Assets")}
          {assets.length > 0 && (
            <CountBadge variant="secondary">{assets.length}</CountBadge>
          )}
        </CardTitle>
        {canManage && (
          <div className="flex items-center gap-2 shrink-0">
            <SearchableSelect
              aria-label={t_i18n("Role of the uploaded assets")}
              className="w-44"
              options={TEMPLATE_ASSET_ROLES.map((role) => ({ value: role, label: role }))}
              pinned={[{ value: NO_ROLE, label: t_i18n("No role") }]}
              value={pendingRole}
              onValueChange={setPendingRole}
            />
            <Button
              variant="secondary"
              size="sm"
              className="gap-1.5"
              disabled={addAsset.isPending}
              onClick={() => uploadInputRef.current?.click()} aria-busy={addAsset.isPending || undefined}>
              {addAsset.isPending && <Spinner size="sm" tone="inherit" />}{!addAsset.isPending && <Upload className="h-3.5 w-3.5" />}
              {t_i18n("Upload Assets")}
            </Button>
          </div>
        )}
      </CardHeader>

      <CardContent className="px-4 pb-4 pt-0 space-y-2">
        <input
          ref={uploadInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => {
            handleUpload(e.target.files);
            e.target.value = "";
          }}
        />

        {assets.length === 0 ? (
          <div className="rounded-sm border border-dashed border-elevation-subtle-soft py-10 text-center">
            <Paperclip className="mx-auto h-8 w-8 text-default-disabled mb-2" />
            <p className="content-base text-default-secondary">{t_i18n("No assets yet")}</p>
            <p className="content-compact text-default-secondary mt-1">
              {t_i18n("Upload the fonts, images and logos this template renders with. Each asset can be replaced or removed on its own.")}
            </p>
          </div>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2 pb-1">
              <SearchInput
                placeholder={t_i18n("Search by name or caption...")}
                value={query}
                onChange={setQuery}
                className="flex-1 min-w-[200px] max-w-md"
                inputClassName="h-8 content-compact"
              />
              <SearchableSelect
                aria-label={t_i18n("Filter by role")}
                searchable="auto"
                sort={false}
                className="w-52"
                options={roleCounts.map(([role, count]) => ({
                  value: role,
                  label: role === NO_ROLE ? t_i18n("No role") : role,
                  description: String(count),
                  selectedLabel: `${role === NO_ROLE ? t_i18n("No role") : role} (${count})`,
                }))}
                pinned={[{
                  value: ALL_ROLES,
                  label: t_i18n("All roles"),
                  description: String(assets.length),
                  selectedLabel: `${t_i18n("All roles")} (${assets.length})`,
                }]}
                value={roleFilter}
                onValueChange={setRoleFilter}
              />
            </div>
            {visible.length === 0 ? (
              <div className="rounded-sm border border-dashed border-elevation-subtle-soft py-8 text-center">
                <p className="content-base text-default-secondary">{t_i18n("No asset matches this search.")}</p>
                <Button
                  variant="ghost"
                  size="sm"
                  className="mt-2 content-compact"
                  onClick={() => {
                    setQuery("");
                    setRoleFilter(ALL_ROLES);
                  }}
                >
                  {t_i18n("Clear filters")}
                </Button>
              </div>
            ) : (
              visible.map((asset) => (
                <AssetRow key={asset.id} asset={asset} containerId={template.id} canManage={canManage} />
              ))
            )}
            <p className="content-caption text-default-secondary pt-1">
              {filtering
                ? t_i18n("{shown} of {total} assets", { shown: visible.length, total: assets.length })
                : `${assets.length} ${t_i18n(pluralize(assets.length, "asset"))}`}
              {" · "}
              {t_i18n("Replacing an asset keeps its id, so layouts referencing it stay valid.")}
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
