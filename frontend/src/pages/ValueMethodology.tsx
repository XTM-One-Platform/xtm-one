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
 * The methodology page (#9836 R7b, design dev-docs/roi-outcomes.md 10.6):
 * how every value figure is computed, where each input comes from, what is
 * counted, who sees what and how to change a default. Every user, both
 * modes, no admin gate: the product form of dev-docs/roi-methodology.md,
 * opened from "Read the methodology" in every "How this is computed"
 * popover. Static text only: reading it never starts a run nor calls a model.
 */
import { valuePath } from "@/lib/task-routes";
import { Link } from "react-router-dom";
import { BookOpen } from "lucide-react";
import { BackButton } from "@/components/ui/back-button";
import { Card } from "@/components/ui/card";
import { SECTION_ANCHOR_CLASS, SectionIndex } from "@/components/ui/section-index";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { SHOWN_LABEL_NAMES } from "@/lib/roi-figure";

/** The four formulas, the same words as the popover's (design 10.6). @i18n-keys */
const FORMULAS = [
  "Value = units x (manual minutes - review minutes) x hourly rate",
  "Net value = value - AI cost",
  "ROI = net value / AI cost",
  "Payback = setup cost / net value per month",
] as const;

/**
 * One static worked example per formula, in numbers (UX r1 B2, standards section 6):
 * 10 units of 30 manual and 5 review minutes at 60 per hour, 40 of AI cost,
 * 420 of setup cost. Labelled as an example, never read from the platform. @i18n-keys
 */
const EXAMPLES = [
  "10 units x (30 - 5) min x 60 per hour = 250",
  "250 - 40 = 210",
  "210 / 40 = 5.25",
  "420 / 210 per month = 2 months",
] as const;

/** The sections of the page, in order, for the section index (titles are the Section titles below; SectionIndex translates them). */
const SECTIONS = [
  { id: "methodology-model", title: "The model" },
  { id: "methodology-labels", title: "Where each input comes from" },
  { id: "methodology-counted", title: "What is counted" },
  { id: "methodology-access", title: "Who sees what" },
  { id: "methodology-defaults", title: "How to change a default" },
  { id: "methodology-exports", title: "Exports" },
] as const;

/** Each input of the model and what it is. @i18n-keys */
const INPUTS = [
  ["Units", "Counted from the runs: each agent, assignment or fleet declares the unit its output counts. Orchestrators declare none, so the work of a fleet is never counted twice."],
  ["Manual minutes", "The time a person needs without the agent, as a low, likely and high range. The range of every figure comes from the low and high minutes."],
  ["Review minutes", "The time a person spends checking the output: measured where the run records it, the catalog value otherwise."],
  ["Hourly rate", "The loaded hourly rate of the role that would do the work."],
  ["AI cost", "What was spent: the measured cost of each model call in the period, counted once for the person it is attributed to, its agent and that agent's fleet, never shared out over other work. Calls of automations, of chat and every other call count."],
  ["Setup cost", "Zero unless declared. Payback reads \"Under a month\" when one month of net value covers it."],
] as const;

/** Each label an input carries, in the one vocabulary of every surface (design 10.6). @i18n-keys */
const LABELS = {
  default: "Shipped with the catalog. Its source is the publication or the reasoning.",
  published: "Taken from a public study. Its source is the publication.",
  assumption: "A reasoned figure, until a study exists. Its source is the reasoning.",
  measured: "Read from the platform (AI cost, recorded review time). Its source is the period measured.",
  customized: "Changed by an administrator. Its source is who changed it, when, and the default value.",
} as const satisfies Partial<Record<keyof typeof SHOWN_LABEL_NAMES, string>>;

/** What a figure counts and leaves apart. @i18n-keys */
const COUNTED = [
  "Three sources make the total: Automations (runs and work items), Chat (estimated), the deliverables made in a conversation counted on the low side, and Other AI usage, cost only. Every breakdown adds up to the total.",
  "By source lists every source, or only the total when one source would have to be hidden to protect a small group, so a hidden source is never found by subtraction.",
  "Each piece of work and each model call counts for one person: the person the run acted for, the person a work item works for, the person of the conversation. Work with no person counts in the organization, its agent and its fleet, and in nobody's own figures.",
  "A rejected outcome counts no unit.",
  "An unreviewed run is counted and shown apart; the review rate says how much of the figure a person confirmed.",
  "A unit without manual minutes counts its AI cost and no time saved.",
  "The period is 7, 30 or 90 days, this quarter or all time; the trend is weekly.",
  "Older history that cannot be tied to a person is not presented as complete: the figures then read \"since\" a date, the first day from which every piece of work carries its person.",
] as const;

/** Who sees which figure (dev-docs/roi-methodology.md section 4). @i18n-keys */
const ACCESS = [
  "Mine is what you did: your own runs, chats and AI cost, on any agent you use and in general chat, never a colleague's work on an agent you can see.",
  "An agent or a fleet shows the work of everyone who used it, in aggregate, to whoever can open it.",
  "The organization total is the same figure for an administrator and for every user, while the administrator leaves \"Organization totals for every user\" on. Agents and fleets you cannot open are summed in \"Other agents and fleets\".",
  "Nobody is identifiable: a user never sees another person's name, email or deliverable titles. For a user, a group of fewer than k people in the period is summed into an \"Other\" row (k is 5 by default; an administrator sets it from 2 to 50). Your own figures and the organization total are never suppressed; an administrator sees every row.",
  "By person is for administrators only, off by default, and every read of it is recorded in the audit log.",
] as const;

/** Where each default is changed, and what a change moves. @i18n-keys */
const DEFAULTS = [
  ["Units of work", "Settings, Organization, Units of work. Moves the value of every past and future run of that unit."],
  ["Rates by role", "Settings, Organization, Rates by role. Moves the value of every unit valued at that role."],
  ["Outcomes", "On an agent or a fleet. Moves what its runs count from now on."],
] as const;

/** The settings page of each default an administrator opens (none for Outcomes: it lives on each agent and fleet). */
const DEFAULT_PATHS: Partial<Record<(typeof DEFAULTS)[number][0], string>> = {
  "Units of work": "/settings/organization/units",
  "Rates by role": "/settings/organization/rates",
};

function Section({ title, children, testId }: { title: string; children: React.ReactNode; testId: string }) {
  const { t_i18n } = useFormatter();
  return (
    <Card id={testId} className={`space-y-3 p-4 ${SECTION_ANCHOR_CLASS}`} data-testid={testId}>
      <h2 className="content-base font-semibold">{t_i18n(title)}</h2>
      {children}
    </Card>
  );
}

/** A term and its meaning, one row per term (a definition list, not a records table). */
function Terms({ rows, testId }: { rows: readonly { term: React.ReactNode; meaning: string; key: string }[]; testId?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <dl className="divide-y divide-[var(--border-elevation-subtle)] content-compact" data-testid={testId}>
      {rows.map((row) => (
        <div key={row.key} className="grid grid-cols-1 gap-1 py-2 md:grid-cols-[200px_1fr] md:gap-4">
          <dt className="font-medium">{row.term}</dt>
          <dd className="text-default-secondary">{t_i18n(row.meaning)}</dd>
        </div>
      ))}
    </dl>
  );
}

export default function ValueMethodology() {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();

  return (
    <div className="mx-auto max-w-6xl space-y-6">
      <div>
        <BackButton to={valuePath()} label="Back to Value" className="mb-2 -ml-2" />
        <h1 className="flex items-center gap-2 text-3xl font-bold">
          <BookOpen className="h-7 w-7 text-accent" aria-hidden /> {t_i18n("How value is computed")}
        </h1>
        <p className="mt-2 text-default-secondary">
          {t_i18n("Every figure is computed from recorded runs and the catalog below. No figure comes from a model, and reading one never starts a run.")}
        </p>
      </div>

      <div className="grid gap-6 xl:grid-cols-[12rem_minmax(0,1fr)]">
        <SectionIndex entries={SECTIONS} className="hidden xl:block" />

        <div className="min-w-0 space-y-6">
          <Section title="The model" testId="methodology-model">
            <ul className="space-y-3" data-testid="methodology-formulas">
              {FORMULAS.map((formula, i) => (
                <li key={formula} className="space-y-0.5">
                  <p className="font-mono content-compact">{t_i18n(formula)}</p>
                  <p className="content-compact text-default-secondary" data-testid="methodology-example">
                    {t_i18n("Example: {example}", { example: t_i18n(EXAMPLES[i]) })}
                  </p>
                </li>
              ))}
            </ul>
            <Terms rows={INPUTS.map(([term, meaning]) => ({ key: term, term: t_i18n(term), meaning }))} />
          </Section>

          <Section title="Where each input comes from" testId="methodology-labels">
            <p className="content-compact text-default-secondary">
              {t_i18n("Every input carries one label and its source, in \"How this is computed\", in the CSV export and on the last page of the PDF.")}
            </p>
            <Terms
              rows={(Object.keys(LABELS) as (keyof typeof LABELS)[]).map((label) => ({ key: label, term: t_i18n(SHOWN_LABEL_NAMES[label]), meaning: LABELS[label] }))}
            />
          </Section>

          <Section title="What is counted" testId="methodology-counted">
            <ul className="list-disc space-y-1 pl-5 content-compact text-default-secondary" data-testid="methodology-counted-rules">
              {COUNTED.map((line) => (
                <li key={line}>{t_i18n(line)}</li>
              ))}
            </ul>
          </Section>

          <Section title="Who sees what" testId="methodology-access">
            <ul className="list-disc space-y-1 pl-5 content-compact text-default-secondary" data-testid="methodology-access-rules">
              {ACCESS.map((line) => (
                <li key={line}>{t_i18n(line)}</li>
              ))}
            </ul>
            <p className="content-compact text-default-secondary">
              {t_i18n("When an administrator hides cost data, AI cost, net value, ROI and payback read \"Hidden\", on screen and in the exports, never 0.")}
            </p>
          </Section>

          <Section title="How to change a default" testId="methodology-defaults">
            <Terms
              testId="methodology-default-rows"
              rows={DEFAULTS.map(([term, meaning]) => ({
                key: term,
                term:
                  isAdmin && DEFAULT_PATHS[term] ? (
                    <Link to={DEFAULT_PATHS[term]} className="text-accent underline-offset-2 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus">
                      {t_i18n(term)}
                    </Link>
                  ) : (
                    t_i18n(term)
                  ),
                meaning,
              }))}
            />
            <p className="content-compact text-default-secondary">
              {t_i18n("A customized value survives a reseed or an upgrade; \"Reset to default\" removes it. Every change is audited.")}
            </p>
            {!isAdmin && <p className="content-compact text-default-secondary">{t_i18n("Your administrator changes these defaults.")}</p>}
          </Section>

          <Section title="Exports" testId="methodology-exports">
            <ul className="list-disc space-y-1 pl-5 content-compact text-default-secondary">
              <li>{t_i18n("CSV: the figures, then the methodology line and every assumption with its label and source.")}</li>
              <li>{t_i18n("PDF: the value report, then a last page with the formula, the report's own numbers and the assumptions.")}</li>
            </ul>
          </Section>
        </div>
      </div>
    </div>
  );
}
