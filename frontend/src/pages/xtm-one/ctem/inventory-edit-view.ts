// SPDX-License-Identifier: Apache-2.0

/**
 * The administrator's edit view of a CTEM > Inventory tab, opened from the header (`?edit=1`) or by a link to one of
 * the tab's cards (`?card=<card>`, Turn on on the Signals sources).
 *
 * The embedded OrganizationSection drops `?card=` once it has landed on the card, so the view cannot derive from the
 * card parameter alone: it closed as soon as it opened (UX round 2 on #1356, B1). A card link therefore writes
 * `edit=1` into the same URL, and the view stays open after the card parameter leaves. The URL alone is not enough
 * either: on a cold load the section's own write can land after ours from an older URL snapshot and drop `edit=1`
 * (UX round 3 on #1356, B1), so the open state is also latched here until Done, which clears both parameters.
 */
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { PROFILE_CARD_PARAM } from "@/lib/organization-profile-tabs";

/** The query parameter of the administrator's edit view: the list and its edit cards never share the screen. */
const EDIT_PARAM = "edit";

/** Whether the edit view is open, and its setter; `editCards` is null on a tab with no profile card. */
export function useInventoryEditView(editCards: readonly string[] | null, isAdmin: boolean): [boolean, (on: boolean) => void] {
  const [searchParams, setSearchParams] = useSearchParams();
  const linkedCard = searchParams.get(PROFILE_CARD_PARAM);
  const cardLinked = Boolean(linkedCard && editCards?.includes(linkedCard));
  const editParam = searchParams.get(EDIT_PARAM) === "1";
  // Set while a card link is in the URL, cleared by Done only: no later URL write can close the view.
  const [latched, setLatched] = useState(false);
  if (isAdmin && cardLinked && !latched) setLatched(true);
  const editing = isAdmin && Boolean(editCards) && (editParam || cardLinked || latched);
  // The URL follows the latch: a late write that dropped `edit=1` gets it back, so a reload keeps the view.
  const opened = cardLinked || latched;
  useEffect(() => {
    if (!isAdmin || !opened || editParam) return;
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set(EDIT_PARAM, "1");
        return next;
      },
      { replace: true },
    );
  }, [isAdmin, opened, editParam, setSearchParams]);
  const setEditing = (on: boolean) => {
    if (!on) setLatched(false);
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (on) next.set(EDIT_PARAM, "1");
        else {
          next.delete(EDIT_PARAM);
          // Done closes the edit view a card link opened too.
          next.delete(PROFILE_CARD_PARAM);
        }
        return next;
      },
      { replace: true },
    );
  };
  return [editing, setEditing];
}
