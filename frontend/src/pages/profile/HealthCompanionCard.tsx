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
 * The user's Health companion (#8261), on Profile > Profile: a switch, off
 * by default, and the pet of the library it shows. Built in since #101 (S1).
 */
import { Sparkles } from "lucide-react";

import { PetSprite } from "@/components/health/PetSprite";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useHealthName } from "@/hooks/api/useHealthApi";
import { useCompanion, usePetSpritesheet, usePets, useSaveCompanion } from "@/hooks/api/useHealthCompanionApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { HealthCompanion, HealthPet } from "@/types";

export function HealthCompanionCard() {
  const { data } = useCompanion(true);
  if (!data) return null;
  return <CompanionCard companion={data} />;
}

function CompanionCard({ companion }: { companion: HealthCompanion }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const healthName = useHealthName();
  const { data: pets = [] } = usePets();
  const save = useSaveCompanion();
  const update = (next: { enabled: boolean; pet: string }) =>
    save.mutate(next, {
      onError: () => toast({ title: t_i18n("Your companion was not changed"), variant: "error" }),
    });
  return (
    <Card data-testid="profile.health-companion">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 content-base">
          <Sparkles className="size-4 text-default-secondary" />
          {t_i18n("Companion")}
        </CardTitle>
        <CardDescription className="content-compact">
          {t_i18n("A small pet in the corner of the screen shows what {name} and your runs are doing.", {
            name: healthName,
          })}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 pt-0">
        <div className="flex items-center justify-between gap-3 rounded-sm bg-elevation-default p-3.5">
          <span className="content-base text-default-primary">{t_i18n("Show the companion")}</span>
          <Switch
            checked={companion.enabled}
            disabled={save.isPending}
            onCheckedChange={(on) => update({ enabled: on, pet: companion.pet })}
            aria-label={t_i18n("Show the companion")}
          />
        </div>
        <div role="radiogroup" aria-label={t_i18n("Pet")} className="flex flex-wrap gap-3">
          {pets.map((pet) => (
            <PetChoice
              key={pet.slug}
              pet={pet}
              selected={pet.slug === companion.pet}
              disabled={save.isPending}
              onSelect={() => update({ enabled: companion.enabled, pet: pet.slug })}
            />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function PetChoice({
  pet,
  selected,
  disabled,
  onSelect,
}: {
  pet: HealthPet;
  selected: boolean;
  disabled: boolean;
  onSelect: () => void;
}) {
  const url = usePetSpritesheet(pet.slug, pet.sha256);
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      disabled={disabled}
      onClick={onSelect}
      title={pet.description ?? undefined}
      className={cn(
        "flex w-28 flex-col items-center gap-1 rounded-md border p-2 content-caption",
        selected ? "border-accent bg-elevation-hover" : "border-elevation-subtle-soft hover:bg-elevation-hover",
      )}
    >
      <PetSprite url={url} state="idle" version={pet.sprite_version} scale={0.4} still label={pet.display_name} />
      <span className="truncate text-default-primary">{pet.display_name}</span>
    </button>
  );
}
