// SPDX-License-Identifier: Apache-2.0

import { Icon, IconButton } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";

interface PaginationProps {
  page: number;
  pageSize: number;
  total: number;
  onChange: (page: number) => void;
}

/**
 * Shared pager under a list: First/Prev/"Page X of Y"/Next/Last, nothing else.
 * Renders nothing on an empty list (the list's own empty state speaks) and
 * nothing on a list of one page: no pager with nothing to do and no bottom
 * "N results" line, the list ends at its last row or card. It never says the
 * total either: every list says it once, at the top, with the shared TotalChip
 * (the data table's toolbar, a ListControlBar with `showTotal`, or the list's
 * own toolbar row), so the footer never repeats it (Sam, 11 Nov 2026, #11191;
 * UX r1 B1 and B2 on #11192).
 */
export function Pagination({ page, pageSize, total, onChange }: PaginationProps) {
  const { t_i18n } = useFormatter();
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const singlePage = totalPages <= 1;

  if (total === 0 || singlePage) return null;

  return (
    <div data-testid="pagination" className="flex items-center justify-end pt-2">
      <div className="flex items-center gap-1">
        <Button
          variant="ghost"
          size="sm"
          className="h-7 content-compact px-2"
          disabled={page === 0}
          onClick={() => onChange(0)}
        >
          {t_i18n("First")}
        </Button>
        <IconButton variant="default" priority="tertiary" aria-label={t_i18n("Previous page")} icon={<Icon name="chevron-left" size={16} />} className="h-7 w-7" disabled={page === 0} onClick={() => onChange(page - 1)} />
        <span className="content-compact text-default-secondary px-2">
          {t_i18n("Page {page} of {total}", { page: page + 1, total: totalPages })}
        </span>
        <IconButton variant="default" priority="tertiary" aria-label={t_i18n("Next page")} icon={<Icon name="chevron-right" size={16} />} className="h-7 w-7" disabled={page >= totalPages - 1} onClick={() => onChange(page + 1)} />
        <Button
          variant="ghost"
          size="sm"
          className="h-7 content-compact px-2"
          disabled={page >= totalPages - 1}
          onClick={() => onChange(totalPages - 1)}
        >
          {t_i18n("Last")}
        </Button>
      </div>
    </div>
  );
}
