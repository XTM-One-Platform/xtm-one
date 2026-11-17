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
 * "New presentation": settle the DESIGN before the assistant writes a
 * single slide.
 *
 * A deck is generated in the chat, but the two decisions a person cares about
 * before the content exists — which template it wears and in which colour
 * scheme — are made here, in one small form, so nobody has to know that a
 * template is "used" by naming it to an agent. The dialog hands the chat a
 * message that says what was picked ("Using the "Brand Deck" template (dark
 * theme), create a presentation about ..."), the user finishes the sentence
 * and sends it. Nothing is created here.
 *
 * Three answers, one meaning each:
 *   - a template          -> the deck is dressed in it; the theme is limited to
 *                            the schemes that template supports;
 *   - no template         -> the plain built-in look, dark or light;
 *   - let my assistant ask -> only offered when there are several templates to
 *                            pick from; the agent then asks in the chat before
 *                            it writes the slides (it never asks when there is
 *                            one or none).
 *
 * `templateId` pre-selects a template — the "Create a presentation with this
 * template" action on the Templates pages lands here with the pick made.
 *
 * The dialog also settles WHO writes the deck: only an assistant that carries
 * the `Presentation Generation` tool is offered (`useDeckAgents`, the same list
 * as the Studio rail), and the chat opens on that assistant. Handing the
 * message to whichever agent the chat last showed produced a polite refusal
 * from an agent without the tool — the dead end that made templates look
 * unusable.
 */
import { useEffect, useMemo, useState } from "react";
import { Bot, MessageSquareText, Palette, Sparkles } from "lucide-react";
import { Link, useNavigate } from "react-router-dom";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { agentSelectOption } from "@/components/ui/entity-select-options";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useTemplateContainers } from "@/hooks/api/useTemplateContainerApi";
import { useFormatter } from "@/i18n";
import { primeChatDraft } from "@/lib/chat-launch";
import { CHAT_GENERAL_AGENT_PARAM } from "@/lib/constants";
import type { PresentationTheme, PresentationThemeScheme, TemplateContainer } from "@/types";

import { createWithAssistantPrompt } from "./assistant-handoff";
import { useDeckAgents } from "./deck-agents";
import { DECK_THEMES, fitThemeToSchemes } from "./previewSpec";
import { useSchemeLabel } from "./scheme-label";
import { ThemeSchemePicker } from "./ThemeSchemePicker";
import { Spinner } from "@filigran/design-system";

/** Picker values for the two non-template answers. */
const ASK = "__ask__";
const NONE = "__none__";
/** Picker value for the General Assistant as the deck's writer. */
const GENERAL = "__general__";

const PRESENTATION_TEMPLATES_FILTER = { kind: "presentation" } as const;

export function NewPresentationDialog({
  open,
  onOpenChange,
  templateId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Pre-selected template (the Templates pages' "Create with this template"). */
  templateId?: string | null;
}) {
  const { t_i18n } = useFormatter();
  const schemeLabel = useSchemeLabel();
  const navigate = useNavigate();
  const { data: templates, isLoading } = useTemplateContainers(PRESENTATION_TEMPLATES_FILTER);
  const rows: TemplateContainer[] = templates ?? [];

  // "Ask" is only an answer when there is a question: several templates.
  const canAsk = rows.length > 1;
  const initialPick = (): string => {
    if (templateId && rows.some((row) => row.id === templateId)) return templateId;
    if (canAsk) return ASK;
    return rows.length === 1 ? rows[0].id : NONE;
  };
  const [pick, setPick] = useState<string>(initialPick);
  const [theme, setTheme] = useState<PresentationTheme>("dark");
  const [topic, setTopic] = useState("");

  // Who writes it: the assistants that carry the deck tool, defaulting the way
  // the Studio rail does (the user's own agent when it can, else a
  // company-managed one, else any, else the General Assistant).
  const writers = useDeckAgents();
  const defaultWriter = writers.defaultAgentId ?? (writers.generalAssistantOffered ? GENERAL : null);
  const [writerPick, setWriterPick] = useState<string | null>(null);
  const writer = writerPick !== null && (writerPick === GENERAL || writers.agents.some((a) => a.id === writerPick))
    ? writerPick
    : defaultWriter;
  const writerAgent = writer && writer !== GENERAL ? writers.agents.find((a) => a.id === writer) : undefined;

  // Re-seed the pick when the dialog opens or the list arrives: the sensible
  // default depends on how many templates the person has.
  useEffect(() => {
    if (!open) return;
    setPick(initialPick());
    setTopic("");
    setWriterPick(null);
    // `initialPick` reads `rows`/`templateId`, which are what this keys on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, templateId, rows.length]);

  const picked = rows.find((row) => row.id === pick);
  const supported: readonly PresentationThemeScheme[] = picked ? picked.supported_schemes : DECK_THEMES;
  // A template that cannot dress the current theme snaps it — what the deck
  // will wear is what the form shows.
  useEffect(() => {
    const fitted = fitThemeToSchemes(theme, supported);
    if (fitted !== theme) setTheme(fitted);
    // `supported` is rebuilt every render; keyed on its content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme, supported.join("|")]);

  const message = useMemo(
    () =>
      createWithAssistantPrompt(
        pick === ASK
          ? { topic }
          : pick === NONE
            ? { template: null, theme, topic }
            : picked
              ? { template: { name: picked.name }, theme, topic }
              : { topic },
      ),
    [pick, picked, theme, topic],
  );

  const canStart = !isLoading && !writers.resolving && writer !== null;
  const start = () => {
    if (!canStart) return;
    // The chat opens on the assistant picked here: an agent by id, or the
    // General Assistant forced through its hand-off parameter.
    primeChatDraft(message, { agentId: writer === GENERAL ? CHAT_GENERAL_AGENT_PARAM : writer });
    onOpenChange(false);
    navigate("/chat");
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-accent" />
            {t_i18n("New presentation")}
          </DialogTitle>
          <DialogDescription>
            {t_i18n(
              "Your assistant writes the slides in the chat. Choose the design it starts from - you can change the template and the theme later from the deck's Design panel.",
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label className="flex items-center gap-1.5 text-default-secondary">
              <Palette className="h-3.5 w-3.5" />
              {t_i18n("Template")}
            </Label>
            {isLoading ? (
              <div className="flex h-9 items-center gap-2 content-compact text-default-secondary">
                <Spinner size="sm" tone="inherit" /> {t_i18n("Loading templates...")}
              </div>
            ) : (
              <SearchableSelect
                aria-label={t_i18n("Template")}
                options={rows.map((row) => ({
                  value: row.id,
                  label: row.name,
                  description: row.supported_schemes.map(schemeLabel).join(" / "),
                }))}
                pinned={[
                  ...(canAsk
                    ? [{
                        value: ASK,
                        label: t_i18n("Let my assistant ask me in the chat"),
                        icon: <MessageSquareText className="h-3.5 w-3.5 text-default-secondary" />,
                      }]
                    : []),
                  { value: NONE, label: t_i18n("No template - plain look") },
                ]}
                value={pick}
                onValueChange={setPick}
              />
            )}
            <p className="text-[11px] leading-relaxed text-default-secondary">
              {pick === ASK
                ? t_i18n("You have several templates: your assistant lists them and asks which one to use before writing the slides.")
                : picked
                  ? (picked.description?.trim() ||
                    t_i18n("A template is a design: the deck gets its colours, fonts and logos, and keeps them if the template is updated."))
                  : t_i18n("The built-in look, with no brand. You can apply a template to the deck later.")}
              {rows.length === 0 && !isLoading && (
                <>
                  {" "}
                  <Link to="/templates" className="text-accent underline-offset-2 hover:underline">
                    {t_i18n("Create a template")}
                  </Link>
                </>
              )}
            </p>
          </div>

          {pick !== ASK && (
            <div className="space-y-1.5">
              <Label className="text-default-secondary">{t_i18n("Theme")}</Label>
              <ThemeSchemePicker
                value={theme}
                onChange={setTheme}
                supported={supported}
                templateName={picked?.name ?? null}
              />
            </div>
          )}

          <div className="space-y-1.5">
            <Label htmlFor="new-presentation-topic" className="text-default-secondary">
              {t_i18n("About (optional)")}
            </Label>
            <Input aria-label={t_i18n("About (optional)")}
              id="new-presentation-topic"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder={t_i18n("e.g. our Q3 roadmap for the sales team")}
              maxLength={300}
              onKeyDown={(e) => {
                if (e.key === "Enter") start();
              }}
            />
          </div>

          <div className="space-y-1.5">
            <Label className="flex items-center gap-1.5 text-default-secondary">
              <Bot className="h-3.5 w-3.5" />
              {t_i18n("Written by")}
            </Label>
            {writers.resolving ? (
              <div className="flex h-9 items-center gap-2 content-compact text-default-secondary">
                <Spinner size="sm" tone="inherit" /> {t_i18n("Loading agents...")}
              </div>
            ) : writers.none ? (
              <p className="rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/10 px-3 py-2 content-caption leading-relaxed text-feedback-warning-tertiary">
                {t_i18n(
                  "No assistant can write presentations yet. Turn on Presentation Generation in an agent's tools (or in the General Assistant's tools) and it will be offered here.",
                )}{" "}
                <Link to="/agents" className="text-accent underline-offset-2 hover:underline">
                  {t_i18n("Open agents")}
                </Link>
              </p>
            ) : (
              <>
                <SearchableSelect
                  aria-label={t_i18n("Written by")}
                  options={writers.agents.map((agent) => agentSelectOption(agent))}
                  pinned={
                    writers.generalAssistantOffered
                      ? [{ value: GENERAL, label: t_i18n("General Assistant"), icon: <Sparkles className="h-3.5 w-3.5 text-accent" /> }]
                      : []
                  }
                  value={writer}
                  onValueChange={setWriterPick}
                  placeholder={t_i18n("Search agents...")}
                />
                <p className="text-[11px] leading-relaxed text-default-secondary">
                  {writerAgent?.description?.trim() ||
                    t_i18n("Only assistants that carry the Presentation Generation tool are listed. The chat opens on this one.")}
                </p>
              </>
            )}
          </div>

          <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-highlight px-3 py-2 content-compact">
            <p className="mb-1 content-caption uppercase tracking-wide text-default-secondary">
              {t_i18n("Your message to the assistant")}
            </p>
            <p className="font-mono-plex text-[11px] leading-relaxed">
              {message}
              {!topic.trim() && (
                <span className="text-default-secondary">
                  {" "}
                  {t_i18n("... (you finish the sentence in the chat)")}
                </span>
              )}
            </p>
          </div>
        </div>

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>{t_i18n("Cancel")}</Button>
          <Button className="gap-1.5" onClick={start} disabled={!canStart}>
            <Sparkles className="h-4 w-4" /> {t_i18n("Continue in chat")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
