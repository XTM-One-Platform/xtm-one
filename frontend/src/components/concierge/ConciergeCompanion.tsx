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
 * The Concierge companion (#8261): a small floating pet, for who turned it on
 * in their profile, showing what the Concierge and their own work are doing
 * (`GET /concierge/me/activity`, plus the chat turn on screen). It waves once
 * when it appears, then plays the state's row; a click opens its bubble,
 * with the way to the Concierge page and to hide it.
 */
import { useCallback, useState } from "react";
import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { useConciergeName } from "@/hooks/api/useConciergeApi";
import {
  useCompanion,
  useCompanionActivity,
  usePetSpritesheet,
  usePets,
  useSaveCompanion,
} from "@/hooks/api/useConciergeCompanionApi";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { usePrefersReducedMotion } from "@/hooks/usePrefersReducedMotion";
import { useFormatter } from "@/i18n";
import { useChatActivity } from "@/lib/companion-chat-activity";
import { COMPANION_STATE_LABELS, PET_ROW_FOR, companionState } from "@/lib/concierge-pet";

import { PetSprite } from "./PetSprite";

export function ConciergeCompanion() {
  const flagOn = useFeatureFlag("CONCIERGE");
  const { data: companion } = useCompanion(flagOn);
  if (!flagOn || !companion?.enabled) return null;
  return <FloatingPet slug={companion.pet} fallback={companion.default_pet} />;
}

function FloatingPet({ slug, fallback }: { slug: string; fallback: string }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const conciergeName = useConciergeName();
  const { data: pets = [] } = usePets();
  // A pet removed since the companion was read shows the default one, as the server would.
  const pet = pets.find((candidate) => candidate.slug === slug) ?? pets.find((candidate) => candidate.slug === fallback);
  const url = usePetSpritesheet(pet?.slug, pet?.sha256);
  const { data: activity } = useCompanionActivity(true);
  const chat = useChatActivity();
  const state = companionState(activity?.state, chat);
  // Under reduced motion the greeting, which never plays, is skipped.
  const reducedMotion = usePrefersReducedMotion();
  const [greetedOnce, setGreeted] = useState(false);
  const greeted = greetedOnce || reducedMotion;
  const [open, setOpen] = useState(false);
  const save = useSaveCompanion();
  const onGreeted = useCallback(() => setGreeted(true), []);
  const status = t_i18n(COMPANION_STATE_LABELS[state], { name: conciergeName });

  const hide = () =>
    save.mutate(
      { enabled: false, pet: slug },
      {
        onSuccess: () => toast({ title: t_i18n("Companion hidden"), description: t_i18n("Show it again from your profile.") }),
        onError: () => toast({ title: t_i18n("The companion was not hidden"), variant: "error" }),
      },
    );

  if (!pet) return null;
  return (
    <div className="fixed bottom-4 right-4 z-40 flex flex-col items-end gap-2" data-testid="concierge.companion">
      {open && (
        <div className="w-64 rounded-md border border-elevation-subtle-soft bg-elevation-default p-3 shadow-overlay content-compact">
          <div className="mb-2 flex items-start justify-between gap-2">
            <p className="font-medium text-default-primary">{pet.display_name}</p>
            <button
              type="button"
              className="text-default-secondary hover:text-default-primary"
              onClick={() => setOpen(false)}
              aria-label={t_i18n("Close")}
            >
              <Icon name="x" size={16} />
            </button>
          </div>
          <p className="text-default-secondary">{status}</p>
          <div className="mt-3 flex items-center justify-between gap-2">
            <Button asChild size="sm" variant="secondary">
              <Link to="/concierge" onClick={() => setOpen(false)}>
                {t_i18n("Open {name}", { name: conciergeName })}
              </Link>
            </Button>
            <Button size="sm" variant="ghost" onClick={hide} disabled={save.isPending}>
              <Icon name="eye-off" size={16} />
              {t_i18n("Hide")}
            </Button>
          </div>
        </div>
      )}
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-label={pet.display_name}
        className="rounded-md focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2"
      >
        <PetSprite
          url={url}
          state={greeted ? PET_ROW_FOR[state] : "waving"}
          version={pet.sprite_version}
          onLoop={greeted ? undefined : onGreeted}
          label={t_i18n("{pet}, the companion", { pet: pet.display_name })}
        />
      </button>
      <span className="sr-only" role="status" aria-live="polite">
        {status}
      </span>
    </div>
  );
}
