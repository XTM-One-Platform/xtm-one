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
 * What a mailbox agent account files on each email the agent triaged:
 * read, a follow-up flag, a category, and whether an email handed to the owner
 * notifies them. The triage itself - answer, hand over, leave - is the agent's,
 * read by the platform from the structure it answers in; this is what the
 * owner sees of it in the mailbox.
 */
import { Flag, MailCheck, MailMinus, Send } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { useFormatter } from "@/i18n";
import type { AgentAccountMailboxActions as Actions } from "@/types";

type Setter = <K extends keyof Actions>(key: K, value: Actions[K]) => void;

export function AgentAccountMailboxActions({ value, onChange, disabled }: {
  value: Actions;
  onChange: (value: Actions) => void;
  disabled?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const set: Setter = (key, next) => onChange({ ...value, [key]: next });

  return (
    <fieldset disabled={disabled} className="space-y-3 rounded-sm border border-elevation-subtle p-3">
      <div>
        <div className="flex items-center gap-1.5 content-base font-medium">
          <MailCheck className="h-4 w-4 text-accent" />
          {t_i18n("Mailbox triage")}
        </div>
        <p className="content-compact text-default-secondary">
          {t_i18n("For each email a rule hands it, the agent answers, hands it to you when it cannot or should not answer, or leaves it when it needs no reply - and says why. This is what the mailbox shows of each. Shadow mode touches nothing in the mailbox.")}
        </p>
      </div>
      <div className="grid gap-3 md:grid-cols-3">
        <Outcome icon={Send} title={t_i18n("When the agent answers")}>
          <Toggle label={t_i18n("Mark the email as read")} checked={value.mark_read_replied} onChange={(c) => set("mark_read_replied", c)} />
          <Category value={value.category_replied} onChange={(v) => set("category_replied", v)} />
        </Outcome>
        <Outcome icon={Flag} title={t_i18n("When it hands an email to you")}>
          <Toggle label={t_i18n("Flag it for follow-up")} checked={value.flag_needs_owner} onChange={(c) => set("flag_needs_owner", c)} />
          <Toggle
            label={t_i18n("Notify me, with the agent's reason")}
            checked={value.notify_needs_owner}
            onChange={(c) => set("notify_needs_owner", c)}
          />
          <Category value={value.category_needs_owner} onChange={(v) => set("category_needs_owner", v)} />
        </Outcome>
        <Outcome icon={MailMinus} title={t_i18n("When an email needs no reply")}>
          <Toggle label={t_i18n("Mark the email as read")} checked={value.mark_read_no_reply} onChange={(c) => set("mark_read_no_reply", c)} />
          <Category value={value.category_no_reply} onChange={(v) => set("category_no_reply", v)} />
        </Outcome>
      </div>
    </fieldset>
  );
}

function Outcome({ icon: Icon, title, children }: { icon: React.ElementType; title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2 rounded-sm bg-elevation-hover p-2.5">
      <div className="flex items-center gap-1.5 content-compact font-medium">
        <Icon className="h-3.5 w-3.5 text-default-secondary" />
        {title}
      </div>
      {children}
    </div>
  );
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (checked: boolean) => void }) {
  return (
    <label className="flex items-center justify-between gap-2 content-compact">
      {label}
      <Switch checked={checked} onCheckedChange={onChange} />
    </label>
  );
}

function Category({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-1">
      <div className="text-[11px] text-default-secondary">{t_i18n("Category")}</div>
      <Input aria-label={t_i18n("Category")}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        maxLength={255}
        placeholder={t_i18n("No category")}
        className="h-8 content-compact"
      />
    </div>
  );
}
