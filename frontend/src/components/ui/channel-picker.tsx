// SPDX-License-Identifier: Apache-2.0

import { useState, useMemo, useRef } from "react";
import { Search, X, ChevronDown, Hash } from "lucide-react";
import { Spinner } from "@filigran/design-system";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { useClickOutside } from "@/hooks/useClickOutside";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";

/**
 * ChannelPicker — autocomplete multi-select for choosing channels.
 *
 * Replaces the full button-grid pattern with a searchable dropdown.
 * Supports both `ChannelTarget` and `AllowedChannel` (same shape).
 */

interface ChannelItem {
  id: string;
  name: string;
  group?: string;
}

interface ChannelPickerProps {
  /** Currently selected channels */
  selected: ChannelItem[];
  /** All available channels to pick from */
  available: ChannelItem[];
  /** Callback when selection changes */
  onChange: (channels: ChannelItem[]) => void;
  /** Color variant: "emerald" for receive, "blue" for publish */
  variant?: "emerald" | "blue";
  /** Placeholder text for the search input */
  placeholder?: string;
  /** Whether the picker is disabled */
  disabled?: boolean;
  /** Whether available channels are still loading */
  loading?: boolean;
  /** Custom hint shown when nothing is selected. Defaults to the translated No channels selected. */
  emptyHint?: string;
  /** Accessible name of the search field (the caller's visible label, e.g. "Restrict to specific channels"). */
  ariaLabel?: string;
  /** Id of the caller's visible caption that names the search field (preferred over ariaLabel). */
  ariaLabelledBy?: string;
}

export function ChannelPicker({
  selected,
  available,
  onChange,
  variant = "emerald",
  placeholder,
  disabled = false,
  loading = false,
  emptyHint,
  ariaLabel,
  ariaLabelledBy,
}: ChannelPickerProps) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const dropdownRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useClickOutside(dropdownRef, () => { setOpen(false); setSearch(""); }, open);

  const selectedIds = useMemo(() => new Set(selected.map((ch) => ch.id)), [selected]);

  const filteredChannels = useMemo(() => {
    const unselected = available.filter((ch) => !selectedIds.has(ch.id));
    if (!search) return unselected;
    const q = search.toLowerCase();
    return unselected.filter(
      (ch) =>
        ch.name.toLowerCase().includes(q) ||
        ch.id.toLowerCase().includes(q) ||
        (ch.group ?? "").toLowerCase().includes(q),
    );
  }, [available, selectedIds, search]);

  const handleSelect = (ch: ChannelItem) => {
    onChange([...selected, { id: ch.id, name: ch.name, group: ch.group }]);
    setSearch("");
    inputRef.current?.focus();
  };

  const handleRemove = (channelId: string) => {
    onChange(selected.filter((ch) => ch.id !== channelId));
  };

  // Chip text stays on the neutral primary text token in both variants: the
  // info blue over its own 10% tint measures 4.24:1 in light (below 4.5:1 at
  // 12 px). The variant colour carries on the tint, the border and the # icon.
  const colors = variant === "emerald"
    ? {
        badge: "bg-feedback-success-primary/10 text-default-primary border-feedback-success-primary/30",
        removeBtnHover: "hover:text-feedback-error-primary",
        hash: "text-feedback-success-primary",
      }
    : {
        badge: "bg-feedback-info-primary/10 text-default-primary border-feedback-info-primary/30",
        removeBtnHover: "hover:text-feedback-error-primary",
        hash: "text-feedback-info-primary",
      };

  return (
    <div className="relative" ref={dropdownRef}>
      {/* Selected channels as badges */}
      {selected.length > 0 && (
        <div className="flex flex-wrap gap-1 mb-1.5">
          {selected.map((ch) => (
            <Badge
              key={ch.id}
              variant="outline"
              className={cn("gap-1 content-compact pr-1 py-0.5", colors.badge)}
            >
              <Hash className={cn("h-3 w-3", colors.hash)} />
              {ch.name}{ch.group ? ` (${ch.group})` : ""}
              {!disabled && (
                <button aria-label={t_i18n("Remove channel")}
                  className={cn("ml-0.5 transition-colors text-current/60", colors.removeBtnHover)}
                  onClick={() => handleRemove(ch.id)}
                >
                  <X className="h-3 w-3" />
                </button>
              )}
            </Badge>
          ))}
        </div>
      )}

      {/* Search input */}
      {!disabled && (
        <div className="relative">
          <Input
            ref={inputRef}
            aria-label={ariaLabelledBy ? undefined : ariaLabel}
            aria-labelledby={ariaLabelledBy}
            className="w-full"
            startIcon={loading ? <Spinner size="sm" tone="inherit" label={t_i18n("Loading channels...")} /> : <Search className="h-4 w-4" />}
            endIcon={{ type: "icon", icon: <ChevronDown className="h-4 w-4" /> }}
            placeholder={loading ? t_i18n("Loading channels...") : (placeholder ?? t_i18n("Search channels..."))}
            value={search}
            onChange={(e) => { setSearch(e.target.value); setOpen(true); }}
            onFocus={() => setOpen(true)}
            disabled={loading}
          />
        </div>
      )}

      {/* Dropdown */}
      {open && !disabled && !loading && (
        <div className="absolute z-50 mt-1 w-full rounded-sm border border-elevation-subtle layer-2 bg-elevation-default shadow-overlay max-h-40 overflow-y-auto">
          {filteredChannels.length > 0 ? (
            filteredChannels.map((ch) => (
              <button
                key={ch.id}
                className="flex items-center gap-2 w-full px-2.5 py-1.5 text-left content-base transition-colors hover:bg-elevation-hover"
                onClick={() => handleSelect(ch)}
              >
                <Hash className="h-3 w-3 text-default-secondary shrink-0" />
                <span className="truncate flex-1">
                  {ch.name}{ch.group ? ` (${ch.group})` : ""}
                </span>
                <span className="content-compact text-default-secondary font-mono-plex shrink-0">
                  {ch.id.length > 16 ? `${ch.id.slice(0, 16)}…` : ch.id}
                </span>
              </button>
            ))
          ) : (
            <p className="px-2.5 py-2 content-compact text-default-secondary">
              {search ? t_i18n("No matching channels.") : t_i18n("All channels selected.")}
            </p>
          )}
        </div>
      )}

      {/* Empty state hint */}
      {selected.length === 0 && !open && (
        <p className="content-compact text-default-secondary italic mt-1">{emptyHint ?? t_i18n("No channels selected")}</p>
      )}
    </div>
  );
}
