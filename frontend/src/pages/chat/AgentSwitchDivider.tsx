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
 * Where a conversation changed agent: the note the server writes when
 * it is continued with another agent - by its owner, or by a deletion naming
 * a successor - drawn as a divider in the transcript, not as a reply. Its
 * words come from the note's fields, in the reader's language; the stored
 * text is what the next agent and an export read.
 */
import { ArrowRightLeft } from "lucide-react";

import { useFormatter } from "@/i18n";
import { formatShortDateTime } from "@/lib/utils";
import type { AgentSwitchNote, AgentSwitchSide } from "@/types";

export function AgentSwitchDivider({ note, at }: { note: AgentSwitchNote; at: string }) {
  const { t_i18n } = useFormatter();
  const name = (side: AgentSwitchSide | null) =>
    side?.general_assistant ? t_i18n("General Assistant") : (side?.agent_name ?? t_i18n("an agent"));
  const when = formatShortDateTime(at);
  const text =
    note.reason === "agent_deleted" && note.from_agent
      ? t_i18n("{from} was deleted - continued by {to} on {date}", {
          from: name(note.from_agent),
          to: name(note.to_agent),
          date: when,
        })
      : t_i18n("Continued by {to} on {date}", { to: name(note.to_agent), date: when });
  return (
    <div role="note" className="flex items-center gap-3 py-1" data-testid="chat.agent-switch">
      <div className="flex-1 border-t border-elevation-subtle-soft" />
      <span className="inline-flex items-center gap-1.5 content-caption text-default-secondary">
        <ArrowRightLeft className="h-3 w-3 text-accent" aria-hidden="true" />
        {text}
      </span>
      <div className="flex-1 border-t border-elevation-subtle-soft" />
    </div>
  );
}
