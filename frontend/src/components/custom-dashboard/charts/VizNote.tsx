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

import { Inbox } from "lucide-react";
import type { ReactNode } from "react";

/** One sentence where a drawing would be: no data, or data its view cannot draw; in the middle of
 *  the drawing's whole height when `fill`, under an inbox when `inbox`, in the text's colour when `strong`. */
export function VizNote({
  children,
  height,
  fill = false,
  inbox = false,
  strong = false,
}: {
  children: ReactNode;
  height?: number;
  fill?: boolean;
  inbox?: boolean;
  strong?: boolean;
}) {
  if (inbox) {
    return (
      <div className="flex items-center justify-center" style={height ? { height } : undefined}>
        <InboxNote message={children} />
      </div>
    );
  }
  return (
    <div
      className={`flex items-center justify-center px-4 text-center content-compact ${strong ? "text-default-primary" : "text-default-secondary"}`}
      style={fill && height ? { height } : { minHeight: Math.min(height ?? 80, 120) }}
    >
      {children}
    </div>
  );
}

/** Nothing to show: an inbox medallion over a sentence. */
export function InboxNote({ message }: { message: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-4 py-10 text-center">
      <span
        className="flex size-11 items-center justify-center rounded-full"
        style={{ background: "color-mix(in srgb, var(--text-default-primary) 4%, transparent)", border: "1px solid color-mix(in srgb, var(--text-default-primary) 8%, transparent)" }}
      >
        <Inbox className="size-[22px] text-default-disabled" aria-hidden="true" />
      </span>
      <span className="text-[13px] text-default-secondary">{message}</span>
    </div>
  );
}
