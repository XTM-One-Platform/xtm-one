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
 * Settings > Agents and automations > Platform Health > Companion pets (#8261): the platform's pet library.
 * Administrators add a Codex-format pet (one .zip, or pet.json and its
 * spritesheet) and remove an uploaded one; the built-in pets stay. The
 * server checks every upload and answers the rule a refused one broke.
 */
import { useRef } from "react";
import { PawPrint, Trash2, Upload } from "lucide-react";

import { PetSprite } from "@/components/health/PetSprite";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useDeletePet, usePetSpritesheet, usePets, useUploadPet } from "@/hooks/api/useHealthCompanionApi";
import { useFormatter } from "@/i18n";
import { petUploadErrorKey } from "@/lib/health-pet";
import type { HealthPet } from "@/types";

import { BlockTitle } from "./health-block";
import { Spinner } from "@filigran/design-system";

export function HealthPetLibrary() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const input = useRef<HTMLInputElement>(null);
  const { data: pets = [] } = usePets();
  const upload = useUploadPet();

  const onFiles = (files: FileList | null) => {
    if (!files?.length) return;
    upload.mutate(Array.from(files), {
      onSuccess: (pet) => toast({ title: t_i18n("{pet} added to the library", { pet: pet.display_name }), variant: "success" }),
      onError: (error) => {
        const reason = petUploadErrorKey(error);
        toast({ title: t_i18n("The pet was not added"), description: reason ? t_i18n(reason) : undefined, variant: "error" });
      },
    });
    if (input.current) input.current.value = "";
  };

  return (
    <div data-testid="health.pet-library">
      <BlockTitle
        icon={PawPrint}
        title={t_i18n("Companion pets")}
        hint={t_i18n("The pets users may pick for their companion, in the Codex pet format: pet.json and an 8-column spritesheet of 192x208 cells.")}
      />
      <div className="space-y-3 rounded-sm bg-elevation-default p-3.5 content-compact">
        <div className="flex flex-wrap gap-3">
          {pets.map((pet) => (
            <LibraryPet key={pet.slug} pet={pet} />
          ))}
        </div>
        <input
          ref={input}
          type="file"
          multiple
          accept=".zip,.json,.png,.webp,application/zip,application/json,image/png,image/webp"
          className="hidden"
          onChange={(event) => onFiles(event.target.files)}
          aria-label={t_i18n("Pet package")}
        />
        <Button size="sm" variant="secondary" onClick={() => input.current?.click()} disabled={upload.isPending} aria-busy={upload.isPending || undefined}>
          {upload.isPending && <Spinner size="sm" tone="inherit" />}{!upload.isPending && <Upload className="size-4" />}
          {t_i18n("Add a pet")}
        </Button>
      </div>
    </div>
  );
}

function LibraryPet({ pet }: { pet: HealthPet }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const url = usePetSpritesheet(pet.slug, pet.sha256);
  const remove = useDeletePet();
  const onRemove = async () => {
    const ok = await confirm({
      title: t_i18n("Remove {pet}?", { pet: pet.display_name }),
      description: t_i18n("Users who picked it see the default pet instead."),
      confirmLabel: t_i18n("Remove"),
      variant: "warning",
    });
    if (!ok) return;
    remove.mutate(pet.slug, {
      onError: () => toast({ title: t_i18n("The pet was not removed"), variant: "error" }),
    });
  };
  return (
    <div className="flex w-32 flex-col items-center gap-1 rounded-md border border-elevation-subtle-soft p-2">
      {ConfirmEl}
      <PetSprite url={url} state="idle" version={pet.sprite_version} scale={0.4} label={pet.display_name} />
      <span className="max-w-full truncate text-default-primary">{pet.display_name}</span>
      {pet.builtin ? (
        <span className="content-caption text-default-secondary">{t_i18n("Built-in")}</span>
      ) : (
        <Button size="sm" variant="ghost" onClick={onRemove} disabled={remove.isPending} aria-label={t_i18n("Remove {pet}", { pet: pet.display_name })}>
          <Trash2 className="size-4" />
        </Button>
      )}
    </div>
  );
}
