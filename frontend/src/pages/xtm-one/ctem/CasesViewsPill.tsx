// SPDX-License-Identifier: Apache-2.0

/**
 * "Views" of the CTEM Cases list (#1176): a toolbar pill like Group by. Its
 * panel lists the saved views (a click opens one, the trash deletes it) and
 * saves the list as shown under a name: the state tab, filters, search, sort
 * and grouping, all of which the URL already holds (columns stay the table's own
 * choice, the page number is never kept). Views are kept in
 * this browser. The pill is accented and names the view the list matches.
 */
import { useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { FilterPillPanel, FilterPillTrigger, useFilterPopover } from "@/components/ui/filter-pill";
import { Input } from "@/components/ui/input";
import { RowRemoveButton } from "@/components/ui/row-remove-button";
import { useFormatter } from "@/i18n";
import { randomId } from "@/lib/random-id";
import {
  CASE_SAVED_VIEWS_STORAGE_KEY,
  activeCaseView,
  addCaseSavedView,
  applyCaseView,
  caseViewQuery,
  readCaseSavedViews,
  removeCaseSavedView,
  type CaseSavedView,
} from "./cases-saved-views";

/** The pill's glyph, from the design system. */
const ViewsGlyph = ({ className }: { className?: string }) => <Icon name="eye" size={16} className={className ?? "size-3"} />;

function loadViews(): CaseSavedView[] {
  try {
    return readCaseSavedViews(window.localStorage.getItem(CASE_SAVED_VIEWS_STORAGE_KEY));
  } catch {
    return [];
  }
}

function storeViews(views: CaseSavedView[]) {
  try {
    window.localStorage.setItem(CASE_SAVED_VIEWS_STORAGE_KEY, JSON.stringify(views));
  } catch {
    // Storage full or disabled: the views stay for this page only.
  }
}

export function CasesViewsPill() {
  const { t_i18n } = useFormatter();
  const [searchParams, setSearchParams] = useSearchParams();
  const [open, setOpen] = useState(false);
  const [views, setViews] = useState<CaseSavedView[]>(loadViews);
  const [name, setName] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  useFilterPopover(ref, open, setOpen);

  const query = caseViewQuery(searchParams);
  const active = activeCaseView(views, query);
  const label = t_i18n("Views");
  // Under 1280 px an active view is the glyph and an accent dot, its name in the
  // tooltip and the accessible name, so the nowrap toolbar keeps Export CSV on
  // screen at 1080 px in every locale (retest 88c2fe79 D1, UX r1 B1 on #1324: the
  // cut name still took 170 px); from 1280 px the name alone, cut at 7 rem; from
  // 1728 px "View: name".
  const pillText = active ? t_i18n("View: {name}", { name: active.name }) : label;
  const update = (next: CaseSavedView[]) => {
    setViews(next);
    storeViews(next);
  };
  const save = () => {
    if (!name.trim()) return;
    update(addCaseSavedView(views, name, query, randomId()));
    setName("");
  };

  return (
    <div ref={ref} className="relative" data-testid="ctem-cases-views">
      <FilterPillTrigger
        icon={ViewsGlyph}
        label={
          active ? (
            <>
              <span className="inline-flex items-center min-[1280px]:hidden" data-testid="ctem-cases-view-pill-dot">
                <span className="size-1.5 rounded-full bg-accent" aria-hidden />
                <span className="sr-only">{pillText}</span>
              </span>
              <span className="hidden max-w-28 truncate min-[1280px]:inline min-[1728px]:hidden" data-testid="ctem-cases-view-pill-short">
                {active.name}
              </span>
              <span className="hidden max-w-48 truncate min-[1728px]:inline">{pillText}</span>
            </>
          ) : (
            <span className="max-w-48 truncate">{pillText}</span>
          )
        }
        title={active ? pillText : undefined}
        open={open}
        active={Boolean(active)}
        className="h-8"
        onClick={() => setOpen(!open)}
      />
      {open && (
        <FilterPillPanel aria-label={label} className="w-72 p-1">
          {views.length === 0 ? (
            <p className="px-2.5 py-1.5 content-compact text-default-secondary">
              {t_i18n("No saved view yet. Save the list as shown: state, filters, search, sort and grouping.")}
            </p>
          ) : (
            <ul role="list" className="max-h-64 overflow-y-auto">
              {views.map((v) => (
                <li key={v.id} className="flex items-center gap-1">
                  <button
                    type="button"
                    data-testid="ctem-cases-view"
                    aria-current={v.id === active?.id ? "true" : undefined}
                    onClick={() => {
                      setSearchParams(applyCaseView(searchParams, v.query), { replace: true });
                      setOpen(false);
                    }}
                    className="flex min-w-0 flex-1 items-center gap-2 rounded-sm px-2.5 py-1.5 text-left content-compact hover:bg-elevation-hover"
                  >
                    <span className="min-w-0 flex-1 truncate" title={v.name}>
                      {v.name}
                    </span>
                    {v.id === active?.id && <Icon name="check" size={16} className="size-3.5 shrink-0" aria-hidden />}
                  </button>
                  <RowRemoveButton label={t_i18n("Delete the view {name}", { name: v.name })} onClick={() => update(removeCaseSavedView(views, v.id))} />
                </li>
              ))}
            </ul>
          )}
          <form
            className="mt-1 flex items-center gap-1 border-t border-elevation-subtle-soft px-1 pt-2 pb-1"
            onSubmit={(e) => {
              e.preventDefault();
              save();
            }}
          >
            {/* No maxLength: its counter would paint below the panel; addCaseSavedView cuts the name. */}
            <Input
              value={name}
              aria-label={t_i18n("Name of the view")}
              placeholder={t_i18n("Name of the view")}
              onChange={(e) => setName(e.target.value)}
              data-testid="ctem-cases-view-name"
            />
            <Button type="submit" variant="outline" className="shrink-0" disabled={!name.trim()} data-testid="ctem-cases-view-save">
              {t_i18n("Save view")}
            </Button>
          </form>
        </FilterPillPanel>
      )}
    </div>
  );
}
