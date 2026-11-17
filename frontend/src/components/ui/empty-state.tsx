// SPDX-License-Identifier: Apache-2.0

/**
 * Shared empty-state card for entity list pages.
 *
 * Replaces the identical `<Card className="border-dashed">` + icon +
 * title + description + button pattern duplicated across Agents,
 * Integrations, KnowledgeBases, MCPServers, and CustomTools list pages.
 *
 * `variant="inset"` is the same content without the dashed card: for an empty
 * state that already sits inside a card (the fleet graph card, a live feed, a
 * filtered table), where a second card would draw a card in a card.
 * The inset block grows to the free height of a flex column parent and centres
 * its content there: a card stretched by a taller sibling (the fleet
 * graph card next to the members panel) keeps the empty state in its middle.
 * That growth needs a flex column parent: in a block parent (a plain
 * `<CardContent>`) `min-h-full flex-1` does nothing and the block is as tall as
 * its content, which is right for a card sized to its content (the map's
 * activity stream, the security audit lists, the secret vaults card). A card
 * stretched by a sibling gives its `<CardContent>` `flex h-full flex-col`, as
 * FleetPage.tsx does, to centre the empty state in the free height.
 * Guard: src/empty-state-inset.test.ts fails an `<EmptyState>` inside a
 * `<CardContent>` of src/pages or src/components without `variant="inset"`.
 */

import { Plus, Sparkles } from "lucide-react";
import { Card, CardContent, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { useAiDisabledReason } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";

interface EmptyStateProps {
  /** Lucide icon rendered above the title. */
  icon: React.ElementType;
  /** Main heading (e.g. "No agents yet"). */
  title: string;
  /**
   * Descriptive subtitle. Omit it where the title says it all (a list emptied
   * by its filters: "No files match your filters.").
   */
  description?: string;
  /** Label on the CTA button (e.g. "Create Your First Agent"). Omit to hide the button. */
  actionLabel?: string;
  /** Called when the CTA button is clicked. */
  onAction?: () => void;
  /**
   * Set on AI list views (FR-027). With AI disabled this explains that
   * the area is empty because nothing was provisioned — not because a load
   * failed — and suppresses the CTA, which would refuse (FR-018).
   *
   * Only for the *never-provisioned* branch. A list emptied by filtering has
   * its own message; overriding that one would hide the filter as the cause.
   */
  requiresAi?: boolean;
  /**
   * Button style for the CTA. Defaults to ``secondary`` — an empty state is
   * usually one of several on a page and should not shout. Pass ``default``
   * where this CTA is the *only* way to create the thing, so it carries the
   * weight the page header's button would have.
   */
  actionVariant?: React.ComponentProps<typeof Button>["variant"];
  /**
   * Icon on the CTA. Defaults to ``Plus`` because the usual empty state is
   * "create your first X" — but an empty state is not always an invitation to
   * create. A retry ("Try again") read as "＋ Try again", which promises a new
   * thing rather than another attempt at the same one.
   */
  actionIcon?: React.ElementType<{ className?: string }>;
  /**
   * Actions of their own inside the card, under the description, for a list
   * created more than one way (the Fleets tab: "New fleet" and "Create with a
   * prompt"). Hidden with the CTA when AI is disabled on a `requiresAi` list.
   */
  children?: React.ReactNode;
  /**
   * ``card`` (default) draws its own dashed card, for an empty page or list.
   * ``inset`` draws no border and no surface of its own: use it when the empty
   * state is the content of a card that is already on the page. It fills and
   * centres in the free height only inside a flex column parent.
   */
  variant?: "card" | "inset";
  /**
   * ``compact`` for an inset empty state in a dashboard widget whose body is
   * a few rows tall (the home's Top value drivers at its minimum height): a
   * smaller icon and tighter spacing, so the state fits without scrolling.
   * It also takes only the free height under the widget header, never the
   * parent's full height.
   */
  density?: "default" | "compact";
}

export function EmptyState({ icon: Icon, title, description, actionLabel, onAction, requiresAi, actionVariant = "secondary", actionIcon: ActionIcon = Plus, children, variant = "card", density = "default" }: EmptyStateProps) {
  const compact = density === "compact";
  // Translated centrally — dynamic strings pass through untouched.
  const { t_i18n } = useFormatter();
  /* Called unconditionally — `requiresAi &&` here would make the hook order
     depend on a prop. */
  const aiReason = useAiDisabledReason();
  const aiDisabled = !!requiresAi && !!aiReason;
  const body = aiDisabled
    ? t_i18n(
        "This area is empty because AI is disabled on this platform. Nothing failed to load, nothing has been removed, and enabling AI populates it again.",
      )
    : description
      ? t_i18n(description)
      : null;
  const content = (
    <>
      {aiDisabled ? (
        <Sparkles className={compact ? "h-6 w-6 text-feedback-warning-primary mb-1.5" : "h-10 w-10 text-feedback-warning-primary mb-3"} />
      ) : (
        <Icon className={compact ? "h-6 w-6 text-default-secondary mb-1.5" : "h-10 w-10 text-default-secondary mb-3"} />
      )}
      <CardTitle className="text-base mb-1">{t_i18n(title)}</CardTitle>
      {body && <CardDescription className="max-w-md">{body}</CardDescription>}
      {actionLabel && onAction && !aiDisabled && (
        <Button className="mt-4 gap-1.5" variant={actionVariant} onClick={onAction}>
          <ActionIcon className="h-4 w-4" /> {t_i18n(actionLabel)}
        </Button>
      )}
      {children && !aiDisabled && <div className={compact ? "mt-2" : "mt-4"}>{children}</div>}
    </>
  );
  const layout = `flex flex-col items-center justify-center ${compact ? "py-2" : "py-12"} text-center`;
  if (variant === "inset") {
    /* The compact block never claims the parent's full height: in a widget
       body under a header, `min-h-full` resolves to the whole card and the
       block overflows its slot by the header's height. It takes the free
       height only (`flex-1 min-h-0`). */
    return (
      <div
        data-slot="empty-state"
        data-density={density}
        data-height={compact ? "free" : "full"}
        className={`${layout} ${compact ? "min-h-0 px-4" : "min-h-full px-6"} flex-1`}
      >
        {content}
      </div>
    );
  }
  return (
    <Card className="border-dashed">
      <CardContent data-slot="empty-state" className={layout}>
        {content}
      </CardContent>
    </Card>
  );
}
