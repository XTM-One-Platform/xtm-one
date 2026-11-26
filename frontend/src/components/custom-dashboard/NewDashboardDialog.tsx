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
 * A new dashboard from the list (`/dashboards`): its name and an optional
 * description in Markdown, then the studio - as OpenCTI's and OpenAEV's
 * "Create" do. Importing a file is the list's other button.
 *
 * With the assistant (the default), a brief says what the dashboard should
 * show and "Made by" who builds it - the assistants that carry the dashboard
 * tools (`useToolAgents`), defaulting as the New video dialog does. The
 * dashboard is created, the studio opens on it and the brief goes out as the
 * first message of its rail (`DashboardStudioLaunch`), so the user watches the
 * widgets land. Blank opens the empty studio, to fill by hand.
 */
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Spinner } from "@filigran/design-system";
import { Bot, FilePlus2, LayoutGrid, Send, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { agentSelectOption } from "@/components/ui/entity-select-options";
import { Label } from "@/components/ui/label";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { TextAreaField, TextField } from "@/components/video-studio/editor/fields";
import { useCreateCustomDashboard } from "@/hooks/api/useCustomDashboardApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useToolAgents } from "@/hooks/useToolAgents";
import { useFormatter } from "@/i18n";
import type { DashboardStudioLaunch } from "@/pages/DashboardStudio";

/** A title and a description, at most (`custom_dashboard_spec.MAX_TITLE_CHARS`, `MAX_SHORT_TEXT_CHARS`). */
const MAX_TITLE = 120;
const MAX_DESCRIPTION = 200;
/** The built-in tool id of the dashboard tools. */
const DASHBOARD_TOOL_ID = "custom_dashboards";
/** The General Assistant in the "Made by" picker. */
const GENERAL = "__general__";

export function NewDashboardDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const create = useCreateCustomDashboard();
  const aiOff = useAiActionDisabled();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [brief, setBrief] = useState("");

  const makers = useToolAgents(DASHBOARD_TOOL_ID);
  const defaultMaker = makers.defaultAgentId ?? (makers.generalAssistantOffered ? GENERAL : null);
  const [makerPick, setMakerPick] = useState<string | null>(null);
  const maker =
    makerPick !== null && (makerPick === GENERAL || makers.agents.some((a) => a.id === makerPick)) ? makerPick : defaultMaker;
  const makerAgent = maker && maker !== GENERAL ? makers.agents.find((a) => a.id === maker) : undefined;
  const makerName = maker === GENERAL ? t_i18n("General Assistant") : (makerAgent?.name ?? t_i18n("your assistant"));

  useEffect(() => {
    if (!open) return;
    setTitle("");
    setDescription("");
    setBrief("");
    setMakerPick(null);
  }, [open]);

  const name = title.trim();
  const request = brief.trim()
    ? `${name ? t_i18n("Build the dashboard \"{title}\":", { title: name }) : t_i18n("Build this dashboard:")}\n\n${brief.trim()}`
    : "";
  const busy = create.isPending;
  const canBlank = !!name && !busy;
  const canAssist = !aiOff && !makers.none && !makers.resolving && maker !== null && !!brief.trim() && !busy;

  const submit = (withAssistant: boolean) => {
    if (withAssistant ? !canAssist : !canBlank) return;
    create.mutate(
      {
        title: name || t_i18n("Untitled dashboard"),
        ...(description.trim() ? { description: description.trim() } : {}),
      },
      {
        onSuccess: (dashboard) => {
          const launch: DashboardStudioLaunch = withAssistant ? { send: request, agentId: maker === GENERAL ? null : maker } : {};
          onOpenChange(false);
          navigate(`/dashboards/${dashboard.id}/studio`, { state: launch });
        },
        onError: (error) => toast({ title: t_i18n("The dashboard could not be created"), description: error.message, variant: "error" }),
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[90vh] max-w-2xl flex-col gap-4 overflow-y-auto" data-testid="dashboards.create.dialog">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <LayoutGrid className="h-4 w-4 text-accent" aria-hidden="true" />
            {t_i18n("Create a dashboard")}
          </DialogTitle>
          <DialogDescription>
            {t_i18n(
              "Say what the dashboard should show and your assistant builds it with you in the studio: it finds the data in your platforms and integrations, adds the widgets and places them. You can also start from an empty dashboard and fill it by hand.",
            )}
          </DialogDescription>
        </DialogHeader>
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            submit(!aiOff && !!brief.trim());
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

          <div className="space-y-1.5">
            <Label htmlFor="dashboards-new-brief">{t_i18n("What should it show?")}</Label>
            <textarea
              id="dashboards-new-brief"
              rows={4}
              value={brief}
              disabled={aiOff}
              placeholder={t_i18n(
                "The figures and charts you want, where the data comes from (OpenCTI, OpenAEV, an integration, a knowledge base), the period...",
              )}
              onChange={(event) => setBrief(event.target.value)}
              className="w-full resize-y rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 content-base placeholder:text-default-secondary focus:outline-none focus-visible:ring-2 focus-visible:ring-focus disabled:cursor-not-allowed disabled:opacity-60"
              data-testid="dashboards.create.brief"
            />
          </div>

          {!aiOff && (
            <div className="space-y-1.5">
              <Label className="flex items-center gap-1.5">
                <Bot className="h-3.5 w-3.5" />
                {t_i18n("Made by")}
              </Label>
              {makers.resolving ? (
                <div className="flex h-9 items-center gap-2 content-compact text-default-secondary">
                  <Spinner size="sm" tone="inherit" /> {t_i18n("Loading agents...")}
                </div>
              ) : makers.none ? (
                <p className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/10 px-3 py-2 content-caption leading-relaxed text-feedback-warning-tertiary">
                  {t_i18n(
                    "No assistant can build dashboards yet. Turn on Custom Dashboards in an agent's tools (or in the General Assistant's tools) and it will be offered here.",
                  )}{" "}
                  <Link to="/agents" className="text-accent underline-offset-2 hover:underline">
                    {t_i18n("Open agents")}
                  </Link>
                </p>
              ) : (
                <SearchableSelect
                  aria-label={t_i18n("Made by")}
                  options={makers.agents.map((agent) => agentSelectOption(agent))}
                  pinned={
                    makers.generalAssistantOffered
                      ? [{ value: GENERAL, label: t_i18n("General Assistant"), icon: <Sparkles className="h-3.5 w-3.5 text-accent" /> }]
                      : []
                  }
                  value={maker}
                  onValueChange={setMakerPick}
                  placeholder={t_i18n("Search agents...")}
                />
              )}
            </div>
          )}

          {aiOff ? (
            <p className="rounded-sm border border-elevation-subtle bg-elevation-hover px-3 py-2 content-compact text-default-secondary">
              {t_i18n("AI is turned off on this platform: start from an empty dashboard and build it by hand.")}
            </p>
          ) : (
            <div className="space-y-1.5 rounded-sm border border-elevation-subtle bg-elevation-hover p-3" data-testid="dashboards.create.request">
              <p className="flex items-center gap-1.5 content-caption font-semibold uppercase tracking-wide text-default-secondary">
                <Send className="h-3 w-3" />
                {t_i18n("{assistant} receives", { assistant: makerName })}
              </p>
              {request ? (
                <p className="whitespace-pre-wrap break-words content-compact text-default-primary">{request}</p>
              ) : (
                <p className="content-compact text-default-secondary">{t_i18n("Your brief, as you write it above.")}</p>
              )}
              <p className="text-[11px] text-default-secondary">
                {t_i18n("Sent as your first message when the studio opens, beside the dashboard it builds.")}
              </p>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" variant="ghost" onClick={() => submit(false)} disabled={!canBlank} className="gap-1.5" data-testid="dashboards.create.blank">
              <FilePlus2 className="h-4 w-4" />
              {t_i18n("Start from an empty dashboard")}
            </Button>
            <div className="ml-auto flex gap-2">
              <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
                {t_i18n("Cancel")}
              </Button>
              {!aiOff && (
                <Button
                  type="button"
                  onClick={() => submit(true)}
                  disabled={!canAssist}
                  aria-busy={busy || undefined}
                  className="gap-1.5"
                  data-testid="dashboards.create.assistant"
                >
                  {busy && <Spinner size="sm" tone="inherit" />}
                  {!busy && <Sparkles className="h-4 w-4" />}
                  {t_i18n("Create with the assistant")}
                </Button>
              )}
            </div>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
