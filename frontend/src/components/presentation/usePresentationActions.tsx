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
 * The deck actions shared by the library card and the viewer toolbar,
 * so "duplicate", "delete" and "edit with my assistant" behave identically
 * wherever they are offered.
 *
 * "Edit with my assistant" is the collaboration primitive: it opens the deck in
 * the viewer with the Presentation Studio rail out — a chat BOUND to the deck,
 * whose agent is told which deck is on the stage, which slide the user is
 * looking at and what access they hold, and is guaranteed the deck tool. The
 * user's OWN agent then resolves the deck through the sharing model — a
 * `config` grantee edits it in place, a `read_only` grantee is refused and told
 * to duplicate — so nothing here needs to know which agent will pick it up. For
 * a read-only deck the action duplicates first and opens the COPY, which is the
 * same answer the tool would give, one round-trip earlier.
 */
import { useCallback } from "react";
import { useNavigate } from "react-router-dom";

import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import {
  useDeletePresentation,
  useDuplicatePresentation,
} from "@/hooks/api/usePresentationApi";
import { useFormatter } from "@/i18n";
import type { Presentation, PresentationSummary } from "@/types";

import { studioUrl } from "./studio-url";

type Deck = Presentation | PresentationSummary;

export function usePresentationActions() {
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const duplicateDeck = useDuplicatePresentation();
  const deleteDeck = useDeletePresentation();

  const editWithAssistant = useCallback(
    async (deck: Deck) => {
      let target: Deck = deck;
      if (!deck.can_manage) {
        try {
          target = await duplicateDeck.mutateAsync({ id: deck.id });
          toast({
            title: t_i18n("Copy created"),
            description: t_i18n(
              "You can only view the original, so your assistant will work on your own copy.",
            ),
            variant: "info",
          });
        } catch (err) {
          toast({
            title: t_i18n("Could not copy the presentation"),
            description: err instanceof Error ? err.message : undefined,
            variant: "error",
          });
          return;
        }
      }
      navigate(studioUrl(target.id));
    },
    [duplicateDeck, navigate, t_i18n, toast],
  );

  const duplicate = useCallback(
    async (deck: Deck) => {
      try {
        const copy = await duplicateDeck.mutateAsync({ id: deck.id });
        toast({ title: t_i18n("Presentation duplicated"), description: copy.title, variant: "success" });
        navigate(`/presentations/${copy.id}`);
      } catch (err) {
        toast({
          title: t_i18n("Could not copy the presentation"),
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        });
      }
    },
    [duplicateDeck, navigate, t_i18n, toast],
  );

  const remove = useCallback(
    async (deck: Deck, { afterDelete }: { afterDelete?: () => void } = {}) => {
      const ok = await confirm({
        title: t_i18n("Delete this presentation?"),
        description: t_i18n(
          '"{title}" will be removed for everyone it is shared with. Chat messages that embedded it will show it as no longer available.',
          { title: deck.title },
        ),
        confirmLabel: t_i18n("Delete"),
        variant: "danger",
      });
      if (!ok) return;
      try {
        await deleteDeck.mutateAsync(deck.id);
        toast({ title: t_i18n("Presentation deleted"), variant: "success" });
        afterDelete?.();
      } catch (err) {
        toast({
          title: t_i18n("Could not delete the presentation"),
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        });
      }
    },
    [confirm, deleteDeck, t_i18n, toast],
  );

  return {
    /** Render once wherever the hook is used — the confirm dialog element. */
    ConfirmEl,
    editWithAssistant,
    duplicate,
    remove,
    isBusy: duplicateDeck.isPending || deleteDeck.isPending,
  };
}
