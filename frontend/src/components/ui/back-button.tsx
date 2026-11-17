// SPDX-License-Identifier: Apache-2.0

import { Icon } from "@filigran/design-system";
import { useLocation, useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";

/**
 * Shared back-navigation button used across detail and form pages.
 *
 * Renders a ghost button with a left chevron and label, navigating to the
 * specified path on click. Pass a number (e.g. `-1`) to go back in history.
 * Use `className` to add spacing (e.g. `mb-4`).
 */
export function BackButton({
  to,
  label,
  className,
}: {
  to: string | number;
  label: string;
  className?: string;
}) {
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();
  return (
    <Button
      variant="ghost"
      size="sm"
      className={`gap-1.5 text-default-secondary ${className ?? ""}`}
      onClick={() => {
        if (typeof to === "number") navigate(to);
        else navigate(to);
      }}
    >
      <Icon name="chevron-left" size={16} /> {t_i18n(label)}
    </Button>
  );
}

/**
 * A BackButton the opening page may redirect: navigating here with
 * `state: { backTo, backLabel }` (the command center's fleet timeline opening
 * a run: "Back to Command Center") wins over `to` and `label`, which stay the
 * default for any other way in.
 */
export function LocationBackButton({
  to,
  label,
  className,
}: {
  to: string | number;
  label: string;
  className?: string;
}) {
  const state = useLocation().state as { backTo?: string; backLabel?: string } | null;
  return <BackButton to={state?.backTo ?? to} label={state?.backLabel ?? label} className={className} />;
}
