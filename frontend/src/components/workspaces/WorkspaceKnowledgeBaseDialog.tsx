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
 * Bind knowledge bases to a workspace, from the actions menu of a
 * workspace wherever it is offered (`WorkspaceActionsMenu`).
 *
 * The endpoint REPLACES the set, so `Save` stays disabled until the current
 * bindings are known: before they arrive, or when they could not be read,
 * the selection starts from nothing, and saving it would unbind every
 * knowledge base it does not list.
 */
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { MultiSelect } from "@/components/ui/multi-select";
import { useToast } from "@/components/ui/toast";
import { useBindableKnowledgeBases } from "@/hooks/api/useKnowledgeBaseApi";
import {
  useSetWorkspaceKnowledgeBases,
  useWorkspaceKnowledgeBases,
} from "@/hooks/api/useWorkspaceApi";
import { useFormatter } from "@/i18n";
import type { Workspace } from "@/types";

export function WorkspaceKnowledgeBaseDialog({
  workspace,
  onClose,
}: {
  workspace: Workspace;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const bases = useBindableKnowledgeBases();
  const bound = useWorkspaceKnowledgeBases(workspace.id);
  const save = useSetWorkspaceKnowledgeBases();
  const [selected, setSelected] = useState<string[] | null>(null);

  // Seeded from the server once the bindings arrive, and only while the user
  // has not touched the selection — re-seeding on every refetch would discard
  // what they just unchecked.
  const current =
    selected ?? bound.data?.map((b) => b.knowledge_base_id) ?? [];
  // Not "the person changed something": a selection made before the bindings
  // arrived started from nothing, and saving it would drop the others too.
  const known = bound.isSuccess;

  const options = useMemo(
    () =>
      (bases.data ?? []).map((kb) => ({ value: kb.id, label: kb.name })),
    [bases.data],
  );

  const submit = async () => {
    try {
      await save.mutateAsync({ id: workspace.id, knowledgeBaseIds: current });
      onClose();
    } catch (err) {
      toast({
        title: t_i18n("Error"),
        description:
          err instanceof Error && err.message ? err.message : t_i18n("Unknown error"),
        variant: "error",
      });
    }
  };

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{t_i18n("Knowledge bases")}</DialogTitle>
          <DialogDescription>
            {/* The reassurance is the point: binding looks like it might move
                or claim the corpus, and it does neither. */}
            {t_i18n(
              "Bind the corpora this workspace works with. Binding does not move or copy anything: the knowledge base keeps its own owner and sharing.",
            )}
          </DialogDescription>
        </DialogHeader>

        {/* The reserved height sits UNDER the select, not on the dialog.
            `MultiSelect` opens an ABSOLUTE dropdown sized for a page, and this
            is the only place in the app that puts it inside a Dialog — whose
            height is its content's, so the list opened into nothing and was
            clipped to one truncated row by the dialog's own scroll container.
            Putting `min-h` on the dialog fixed the clipping but distributed
            the slack, leaving a blank band under the description; reserving it
            here keeps the header tight and gives the list its room. */}
        <div className="min-h-[17rem]">
          <MultiSelect
            options={options}
            selected={current}
            onToggle={(value) =>
              setSelected(
                current.includes(value)
                  ? current.filter((v) => v !== value)
                  : [...current, value],
              )
            }
            placeholder={t_i18n("Select knowledge bases")}
            searchPlaceholder={t_i18n("Search knowledge bases...")}
            emptyOptionsLabel={t_i18n("No knowledge bases yet")}
            noMatchLabel={t_i18n("No match")}
          />
          {bound.isError && (
            <p className="mt-2 content-compact text-feedback-error-primary">
              {t_i18n("Could not load the knowledge bases bound to this workspace.")}
            </p>
          )}
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="outline" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button disabled={save.isPending || !known} onClick={submit}>
            {t_i18n("Save")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
