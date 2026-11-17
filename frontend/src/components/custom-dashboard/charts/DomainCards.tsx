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
 * `domainCards`: a card per domain in a fixed order, as OpenAEV draws its
 * security domains (`domains/SecurityDomainsWidget.tsx`,
 * `SecurityDomainCardWidget.tsx`; reference, OpenAEV section 9).
 *
 * Every listed domain (the data's `reference`, else its outcomes), measured or
 * not: its icon, its name, the share of its resolved checks that succeeded
 * counting up in its band's colour with a meter, and an icon per part (an
 * expectation type) in the part's own band. One card opens at a time beside
 * itself: per part, a tile per status row with its share of the part (a click
 * says the domain, the part and the status) over a bar split by status.
 */
import { useState } from "react";

import { Tip } from "@/components/ui/tooltip";
import { useColorMode } from "@/hooks/useColorMode";
import { useFormatter } from "@/i18n";
import type { WidgetOutcomesData } from "@/types";

import { alpha, domainCards, type DomainCard } from "../viz/outcomes";
import { useChartEvents } from "./common";
import { OutcomeIcon } from "./outcome-common";
import { useCountUp, useOutcomeWords } from "./outcome-hooks";

const GEOLOGICA = "var(--font-sans-geologica)";
const SURFACE = { dark: "#1f3965", light: "#f2f2f3" } as const;
const DIVIDER = { dark: "rgba(255,255,255,0.12)", light: "rgba(0,0,0,0.12)" } as const;

function Card({ card, open, onToggle }: { card: DomainCard; open: boolean; onToggle: () => void }) {
  const { t_i18n } = useFormatter();
  const word = useOutcomeWords();
  const mode = useColorMode();
  const { onSelect } = useChartEvents();
  const hasScore = card.score >= 0;
  const animated = useCountUp(hasScore ? card.score : 0, 1200);
  const surface = SURFACE[mode];
  const band = card.color;
  const track = "color-mix(in srgb, var(--text-default-primary) 8%, transparent)";
  return (
    <section className="flex h-full min-w-0 items-stretch gap-3" style={{ flex: open ? "0 0 auto" : "1 1 0" }}>
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        className="flex min-w-28 flex-col items-stretch justify-between gap-2 rounded-[4px] border p-3 text-left text-default-primary transition-[transform,border-color,box-shadow] duration-200 hover:-translate-y-0.5 hover:[border-color:var(--band-border)] hover:[box-shadow:var(--band-shadow)]"
        style={{
          flex: "1 1 0",
          background: alpha(surface, card.hasData ? 0.5 : 0.25),
          borderColor: open ? alpha(band, 0.55) : DIVIDER[mode],
          ["--band-border" as string]: alpha(band, 0.55),
          ["--band-shadow" as string]: `0 4px 14px ${alpha("#000000", 0.35)}, 0 0 0 1px ${alpha(band, 0.15)}`,
        }}
      >
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex size-[30px] shrink-0 items-center justify-center rounded-[4px]" style={{ background: alpha(band, 0.15) }}>
            <OutcomeIcon name={card.icon ?? "circle-question-mark"} size={18} color={band} />
          </span>
          <span
            className="line-clamp-2 font-semibold"
            style={{ fontSize: 12, lineHeight: 1.25, color: card.hasData ? "var(--text-default-primary)" : "var(--text-default-disabled)" }}
          >
            {word(card.label)}
          </span>
        </div>
        <span style={{ fontFamily: GEOLOGICA, fontWeight: 600, fontSize: 22, lineHeight: 1, color: hasScore ? band : "var(--text-default-disabled)" }}>
          {hasScore ? `${Math.round(animated)}%` : "—"}
        </span>
        <div className="flex flex-col gap-1.5">
          <div className="h-1 overflow-hidden rounded-[2px]" style={{ background: track }}>
            <div className="h-full rounded-[2px]" style={{ width: hasScore ? `${animated}%` : 0, background: band, transition: "width 0.2s linear" }} />
          </div>
          <div className="flex gap-1.5">
            {card.parts.map((part) => (
              <Tip key={part.label} content={word(part.label)}>
                <span className="inline-flex" data-part={part.key}>
                  <OutcomeIcon name={part.icon ?? "circle-question-mark"} size={14} color={part.color} />
                </span>
              </Tip>
            ))}
          </div>
        </div>
      </button>
      {open && (
        <div
          className="flex flex-col justify-center rounded-[4px] border"
          style={{
            flex: "0 0 auto",
            width: 300,
            gap: 10,
            padding: "10px 12px",
            borderColor: alpha(band, 0.35),
            background: alpha(surface, 0.5),
            boxShadow: `inset 2px 0 0 ${band}`,
          }}
          data-testid="domain-cards.panel"
        >
          {card.hasData ? (
            card.parts.map((part) => (
              <div key={part.key} className="flex min-w-0 flex-col gap-1">
                <div className="flex items-center gap-1.5">
                  <OutcomeIcon name={part.icon ?? "circle-question-mark"} size={15} color={part.color} />
                  <span className="flex-1 font-semibold uppercase text-default-secondary" style={{ fontSize: 11, letterSpacing: "0.08em" }}>
                    {word(part.label)}
                  </span>
                  {part.rows.map((row) => (
                    <Tip key={row.key} content={`${word(row.label)} - ${t_i18n("click to investigate")}`}>
                      <button
                        type="button"
                        className="flex h-5 w-[42px] items-center justify-center rounded-[3px] transition-colors"
                        style={{ fontFamily: GEOLOGICA, fontSize: 11, fontWeight: 700, color: row.color, background: alpha(row.color, 0.12) }}
                        onMouseEnter={(event) => (event.currentTarget.style.background = alpha(row.color, 0.28))}
                        onMouseLeave={(event) => (event.currentTarget.style.background = alpha(row.color, 0.12))}
                        onClick={() => onSelect?.({ name: card.label, x: part.key, value: row.key })}
                      >
                        {`${row.percentage.toFixed(0)}%`}
                      </button>
                    </Tip>
                  ))}
                </div>
                <div className="flex h-1 gap-0.5 overflow-hidden rounded-[2px]">
                  {part.rows
                    .filter((row) => row.percentage > 0)
                    .map((row) => (
                      <div key={row.key} className="rounded-[2px]" style={{ width: `${row.percentage}%`, background: row.color }} />
                    ))}
                  {part.rows.every((row) => row.percentage <= 0) && <div className="w-full rounded-[2px]" style={{ background: track }} />}
                </div>
              </div>
            ))
          ) : (
            <p className="max-w-60 self-center text-center content-compact text-default-secondary">
              {t_i18n("No data collected on this domain at this time. Run a scenario to start analyzing your position on this domain.")}
            </p>
          )}
        </div>
      )}
    </section>
  );
}

export default function DomainCards({ data }: { data: WidgetOutcomesData }) {
  const mode = useColorMode();
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="flex h-full w-full items-stretch gap-3 overflow-y-hidden overflow-x-auto p-1">
      {domainCards(data, mode).map((card) => (
        <Card key={card.id} card={card} open={open === card.id} onToggle={() => setOpen((current) => (current === card.id ? null : card.id))} />
      ))}
    </div>
  );
}
