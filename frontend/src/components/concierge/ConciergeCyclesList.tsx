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

/** The Concierge cycles the viewer may read, newest first, each opening its digest (#8261). */
import { useState } from "react";
import { ChevronDown, ChevronRight, Compass } from "lucide-react";
import { Link } from "react-router-dom";
import { EmptyState } from "@/components/ui/empty-state";
import { useConciergeCycles, useConciergeName } from "@/hooks/api/useConciergeApi";
import { useFormatter } from "@/i18n";
import {
  CONCIERGE_SIGNAL_LABELS,
  CONCIERGE_SPECIALIST_LABELS,
  CONCIERGE_TRIGGER_LABELS,
  conciergeTargetHref,
  labelOf,
} from "@/lib/concierge";
import { formatShortDateTime } from "@/lib/utils";
import type { ConciergeCycle, ConciergeTargetType } from "@/types";
import { CycleDigestView, DigestStatusBadge } from "./CycleDigestView";

function CycleRow({ cycle }: { cycle: ConciergeCycle }) {
  const { t_i18n } = useFormatter();
  const conciergeName = useConciergeName();
  const [open, setOpen] = useState(false);
  const status = cycle.status === "open" ? "open" : (cycle.digest?.status ?? cycle.status);
  const href = conciergeTargetHref(cycle.target_type, cycle.target_id);
  return (
    <div className="rounded-sm border border-elevation-subtle-soft bg-elevation-default">
      <button
        type="button"
        className="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-elevation-hover"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        {open ? <ChevronDown className="h-4 w-4 shrink-0" /> : <ChevronRight className="h-4 w-4 shrink-0" />}
        <DigestStatusBadge status={status} />
        <span className="shrink-0 content-compact font-medium text-default-primary">
          {cycle.specialist === "orchestrator" ? conciergeName : t_i18n(labelOf(CONCIERGE_SPECIALIST_LABELS, cycle.specialist))}
        </span>
        <span className="content-caption text-default-secondary">
          {t_i18n(labelOf(CONCIERGE_TRIGGER_LABELS, cycle.trigger))}
          {cycle.signal && ` · ${t_i18n(labelOf(CONCIERGE_SIGNAL_LABELS, cycle.signal.kind))}`}
        </span>
        <span className="ml-auto shrink-0 whitespace-nowrap content-caption text-default-secondary">
          {formatShortDateTime(cycle.opened_at)}
        </span>
      </button>
      {open && (
        <div className="space-y-2 border-t border-elevation-subtle-soft p-3">
          {href && (
            <Link to={href} className="content-compact text-accent hover:underline">
              {t_i18n("Open the target")}
            </Link>
          )}
          {cycle.digest ? (
            <CycleDigestView digest={cycle.digest} />
          ) : (
            <p className="content-compact text-default-secondary">
              {cycle.status === "open"
                ? t_i18n("The cycle is still running; its digest is written when it ends.")
                : t_i18n("The digest is being written.")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export function ConciergeCyclesList({ targetType, targetId }: { targetType?: ConciergeTargetType; targetId?: string }) {
  const { t_i18n } = useFormatter();
  const conciergeName = useConciergeName();
  const { data: cycles = [], isLoading, isError } = useConciergeCycles({ targetType, targetId });
  if (isLoading) return null;
  if (isError) return <p className="content-compact text-feedback-error-primary">{t_i18n("{name}'s cycles could not be loaded.", { name: conciergeName })}</p>;
  if (!cycles.length) {
    return (
      <EmptyState
        icon={Compass}
        title={t_i18n("{name} has run no cycle yet", { name: conciergeName })}
        description={t_i18n("Cycles start on their schedule, on a signal, or when a manager asks.")}
      />
    );
  }
  return (
    <div className="space-y-2">
      {cycles.map((cycle) => (
        <CycleRow key={cycle.id} cycle={cycle} />
      ))}
    </div>
  );
}
