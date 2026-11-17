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
 * A dashboard made by hand from the list (`/dashboards`): its title and an
 * optional description in Markdown, then the studio to fill it - as OpenCTI's
 * and OpenAEV's "Create" do. Importing a file is the list's other button.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Spinner } from "@filigran/design-system";
import { LayoutGrid } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useToast } from "@/components/ui/toast";
import { TextAreaField, TextField } from "@/components/video-studio/editor/fields";
import { useCreateCustomDashboard } from "@/hooks/api/useCustomDashboardApi";
import { useFormatter } from "@/i18n";

/** A title and a description, at most (`custom_dashboard_spec.MAX_TITLE_CHARS`, `MAX_SHORT_TEXT_CHARS`). */
const MAX_TITLE = 120;
const MAX_DESCRIPTION = 200;

export function NewDashboardDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const create = useCreateCustomDashboard();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const ready = title.trim().length > 0 && !create.isPending;

  const submit = () => {
    if (!ready) return;
    create.mutate(
      { title: title.trim(), ...(description.trim() ? { description: description.trim() } : {}) },
      {
        onSuccess: (dashboard) => {
          onOpenChange(false);
          setTitle("");
          setDescription("");
          navigate(`/dashboards/${dashboard.id}/studio`);
        },
        onError: (error) => toast({ title: t_i18n("The dashboard could not be created"), description: error.message, variant: "error" }),
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-w-lg flex-col gap-4" data-testid="dashboards.create.dialog">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <LayoutGrid className="h-4 w-4 text-accent" aria-hidden="true" />
            {t_i18n("Create a dashboard")}
          </DialogTitle>
          <DialogDescription>{t_i18n("An empty dashboard, to fill in the studio with widgets of your platforms and of your agents.")}</DialogDescription>
        </DialogHeader>
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <TextField label={t_i18n("Name")} value={title} maxLength={MAX_TITLE} onChange={(value) => setTitle(value ?? "")} />
          <TextAreaField
            label={t_i18n("Description")}
            value={description}
            maxLength={MAX_DESCRIPTION}
            onChange={(value) => setDescription(value ?? "")}
            hint={t_i18n("Markdown is supported.")}
          />
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              {t_i18n("Cancel")}
            </Button>
            <Button type="submit" disabled={!ready} aria-busy={create.isPending || undefined} className="gap-1.5">
              {create.isPending && <Spinner size="sm" tone="inherit" />}
              {t_i18n("Create")}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
