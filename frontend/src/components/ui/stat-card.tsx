// SPDX-License-Identifier: Apache-2.0

import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { cn, exactMetricValue, formatCompactNumber } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { BUTTON_ROLE_FOCUS_RING, buttonRoleProps } from "@/lib/button-role";

interface StatCardProps {
  icon: LucideIcon;
  label: string;
  value: ReactNode;
  subtitle: ReactNode;
  /** The exact figure behind a compact `value` ("$5,512.30" behind "$5.5K"), shown on hover.
   *  A number `value` gets its exact form by itself once compacted (#10476). */
  exact?: string;
  className?: string;
  /** Replaces the icon's accent colour, e.g. a severity colour. */
  iconClassName?: string;
  /** Makes the card act as a button: Tab reaches it, Enter and Space
   *  activate it, it shows the focus ring and assistive technology announces
   *  it as a button named by its label, value and subtitle. The card draws
   *  its own pointer cursor and hover surface then: callers pass no
   *  `cursor-pointer` or `hover:` class of their own. */
  onClick?: () => void;
  /** A block under the subtitle, inside the card (a status line with its action). Never on a
   *  clickable card: a control inside a button-role card would nest interactive content. */
  footer?: ReactNode;
}

/** Pointer cursor and hover surface of a clickable card, one treatment on every
 *  page (gate: button-role-focus-ring.test.ts). */
const CLICKABLE_CARD = "cursor-pointer transition-colors hover:bg-elevation-hover";

export function StatCard({ icon: Icon, label, value, subtitle, exact, className, iconClassName, onClick, footer }: StatCardProps) {
  // Static labels/subtitles are translated centrally; dynamic strings
  // (entity names, composed values) pass through untouched.
  const { t_i18n } = useFormatter();
  const display = typeof value === "number" ? formatCompactNumber(value) : value;
  // The hover shows the exact figure, never the compact text again (UX B4 on #10529).
  const title = exact ?? (typeof value === "number" ? exactMetricValue(value) : typeof display === "string" ? display : undefined);

  return (
    <Card className={cn("flex min-w-0 flex-col", onClick && [CLICKABLE_CARD, BUTTON_ROLE_FOCUS_RING], className)} {...buttonRoleProps(onClick)}>
      <CardContent className="flex flex-1 flex-col pt-4 pb-3 px-4">
        {/* The label wraps to two lines rather than ending in an ellipsis: a
            long fr label ("Prochain déclenchement") is clipped in a 6-column
            strip at 1280 and 1440. The label block always
            reserves those two lines (`min-h-[2lh]` in the caption line height)
            and the value follows it in normal flow, so the values of one row
            share a baseline whether a label wraps or not, and whatever the
            subtitles below them do. The value is never pinned to the bottom of
            the card: a subtitle wrapping to three lines in ja then pushed its
            value up. */}
        <div className="content-caption min-h-[2lh] mb-1" data-testid="stat-card-label-block">
          <div className="flex items-center gap-2">
            <Icon className={cn("h-3.5 w-3.5 shrink-0 text-accent", iconClassName)} />
            <p className="min-w-0 line-clamp-2 break-words content-caption font-medium text-default-secondary uppercase tracking-wider" data-testid="stat-card-label">{t_i18n(label)}</p>
          </div>
        </div>
        {/* A number value (shown compact, "1.2K") stays on one line. A text
            value wraps to two lines like the label instead of ending in an
            ellipsis: a fr countdown ("23 h 59 min") is wider than a 6-column
            card at 1280 and 1440. It wraps between words
            only, never inside one: `break-normal`, since `break-words` split fr
            "maintenant" at 1280. The branch reads the
            type of `value`, not of `display`: the compact number is a string too.
            */}
        <div>
          <p
            className={cn("text-2xl font-bold mt-1", typeof value === "string" ? "line-clamp-2 break-normal" : "truncate")}
            title={title}
            data-testid="stat-card-value"
          >
            {display}
          </p>
          <p className="content-caption text-default-secondary mt-0.5">{typeof subtitle === "string" ? t_i18n(subtitle) : subtitle}</p>
        </div>
        {/* In normal flow under the subtitle, never pinned to the bottom: the subtitles of one
            grid row end on one line, so every divider of the row shares one y and the spare
            height of a shorter footer falls below its content, never above the divider
            (UX round 2 B3 on #965). */}
        {footer && !onClick ? (
          <div className="pt-3" data-testid="stat-card-footer-wrapper">
            <div className="border-t border-elevation-subtle pt-3" data-testid="stat-card-footer">
              {footer}
            </div>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
