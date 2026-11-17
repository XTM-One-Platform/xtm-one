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
 * A picture a person picks for a block by hand: the portrait and the
 * company logo beside a quote's credit, a timeline step's logo (an
 * `asset` field of `block-schema.ts`). The candidates are the ASSETS of the
 * deck's template (a `people_photo`, a customer / partner logo...), the same
 * file ids the agent is pointed at through `get_template` - so what a person
 * picks here is a file the save's file check accepts and every reader of the
 * deck may fetch. The list is fetched only once the picker is opened, and
 * bounded, so a brand template of two hundred assets costs nothing until then.
 * A template the reader cannot see answers 404: that is said here, never
 * thrown to the viewer's error boundary, which would drop the unsaved draft.
 */
import { useId, useMemo, useRef, useState } from "react";
import { ImageOff, Images } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tip } from "@/components/ui/tooltip";
import { templateAssetThumbnailUrl, useTemplateAssets } from "@/hooks/api/useTemplateContainerApi";
import { useFirstAuthenticatedSrc } from "@/hooks/useAuthenticatedSrc";
import { useFormatter } from "@/i18n";
import { templateAssetPreviewSources } from "@/lib/template-asset-preview";
import { cn } from "@/lib/utils";

import { fileUrl } from "./blocks/helpers";
import { OWN_UNDO_ATTR } from "./edit-context";

/** How many candidates the grid draws at once; the search narrows the rest. */
const MAX_SHOWN = 24;
/**
 * A candidate the thumbnail route does not render is previewed from its own
 * bytes, as before the route existed: one larger than this is offered by its
 * name only, so opening the grid never downloads a template's biggest files.
 */
const MAX_PREVIEW_BYTES = 2 * 1024 * 1024;

function AssetThumb({
  urls,
  round,
  alt = "",
}: {
  /** What the tile tries, in order: a bounded template thumbnail, then the file itself. */
  urls: readonly (string | null)[];
  round?: boolean;
  alt?: string;
}) {
  const src = useFirstAuthenticatedSrc(urls);
  return (
    <span
      className={cn(
        "flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden border border-elevation-subtle-soft bg-elevation-hover",
        round ? "rounded-full" : "rounded-sm",
      )}
    >
      {src ? (
        <img src={src} alt={alt} className={cn("h-full w-full", round ? "object-cover" : "object-contain p-0.5")} />
      ) : (
        <ImageOff className="h-3.5 w-3.5 text-default-secondary" aria-hidden="true" />
      )}
    </span>
  );
}

export function TemplateAssetField({
  label,
  value,
  templateId,
  roles,
  round,
  onChange,
}: {
  label: string;
  value: string | undefined;
  /** The deck's template (the draft's pick); `null` for a free deck. */
  templateId: string | null;
  /** The asset roles offered (`QUOTE_PHOTO_ROLES`, `LOGO_ROLES`). */
  roles: readonly string[];
  /** Draw the thumbnails as circles (a portrait). */
  round?: boolean;
  onChange: (next: string | undefined) => void;
}) {
  const { t_i18n } = useFormatter();
  const labelId = useId();
  const gridId = useId();
  const chooseRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const { data: assets, isLoading, isError } = useTemplateAssets(open && templateId ? templateId : undefined, {
    throwOnError: false,
  });
  const candidates = useMemo(() => {
    const needle = query.trim().toLowerCase();
    // A role is a label, reassigned apart from the bytes: only an image can
    // be drawn beside a credit, whatever its role says.
    return (assets ?? []).filter(
      (asset) =>
        !!asset.asset_role &&
        roles.includes(asset.asset_role) &&
        (asset.content_type ?? "").startsWith("image/") &&
        (!needle || `${asset.filename} ${asset.description ?? ""}`.toLowerCase().includes(needle)),
    );
  }, [assets, roles, query]);

  return (
    <div className="space-y-1" role="group" aria-labelledby={labelId}>
      <Label id={labelId} className="content-base text-default-primary">
        {t_i18n(label)}
      </Label>
      <div className="flex items-center gap-2 rounded-sm border border-elevation-subtle-soft px-2 py-1.5">
        {value ? (
          <AssetThumb urls={[fileUrl(value)]} round={round} alt={t_i18n(label)} />
        ) : (
          <span className="content-compact italic text-default-secondary">{t_i18n("None")}</span>
        )}
        <span className="flex-1" />
        <Button
          ref={chooseRef}
          variant="secondary"
          className="gap-1"
          disabled={!templateId}
          aria-expanded={open}
          aria-controls={open ? gridId : undefined}
          onClick={() => setOpen((current) => !current)}
        >
          <Images className="h-4 w-4" aria-hidden="true" /> {t_i18n("Choose")}
        </Button>
        {value ? (
          <Button
            variant="secondary"
            className="gap-1"
            onClick={() => onChange(undefined)}
          >
            <ImageOff className="h-4 w-4" aria-hidden="true" /> {t_i18n("Remove")}
          </Button>
        ) : null}
      </div>
      {!templateId ? (
        <p className="content-compact leading-relaxed text-default-secondary">
          {t_i18n("Pictures come from the deck's template: pick one in the Deck tab, or ask your assistant.")}
        </p>
      ) : null}
      {open && templateId ? (
        <div id={gridId} className="space-y-1.5 rounded-sm border border-elevation-subtle-soft p-1.5">
          {/* The search writes nothing to the draft: Ctrl+Z here is its own. */}
          <Input
            value={query}
            placeholder={t_i18n("Search the template's pictures")}
            aria-label={t_i18n("Search the template's pictures")}
            onChange={(event) => setQuery(event.target.value)}
            {...OWN_UNDO_ATTR}
          />
          {isLoading ? (
            <p className="px-1 content-compact text-default-secondary">{t_i18n("Loading...")}</p>
          ) : isError ? (
            <p className="px-1 content-compact text-default-secondary">
              {t_i18n("The template's pictures could not be loaded.")}
            </p>
          ) : candidates.length === 0 ? (
            <p className="px-1 content-compact text-default-secondary">
              {query.trim()
                ? t_i18n("No matching picture in this template.")
                : t_i18n("This template has no picture of this kind.")}
            </p>
          ) : (
            <div className="grid max-h-48 grid-cols-5 gap-1 overflow-y-auto">
              {candidates.slice(0, MAX_SHOWN).map((asset) => (
                <Tip key={asset.id} content={asset.description ?? asset.filename}>
                  <button
                    type="button"
                    className={cn(
                      "flex items-center justify-center rounded-sm p-0.5 hover:bg-elevation-highlight",
                      asset.id === value && "ring-1 ring-accent/60",
                    )}
                    aria-label={asset.description ?? asset.filename}
                    aria-pressed={asset.id === value}
                    onClick={() => {
                      onChange(asset.id);
                      setOpen(false);
                      // The grid (and the button pressed) unmounts: the focus
                      // goes back to the control that opened it.
                      chooseRef.current?.focus();
                    }}
                  >
                    <AssetThumb
                      urls={templateAssetPreviewSources(
                        asset.content_type,
                        templateAssetThumbnailUrl(templateId, asset),
                        asset.size <= MAX_PREVIEW_BYTES ? fileUrl(asset.id) : null,
                      )}
                      round={round}
                    />
                  </button>
                </Tip>
              ))}
            </div>
          )}
          {candidates.length > MAX_SHOWN ? (
            <p className="px-1 content-compact text-default-secondary">
              {t_i18n("{shown} of {count} shown - search to narrow them.", { shown: MAX_SHOWN, count: candidates.length })}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
