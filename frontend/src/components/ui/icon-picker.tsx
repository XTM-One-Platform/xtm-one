// SPDX-License-Identifier: Apache-2.0

import { useState, useRef, useEffect } from "react";
import { useClickOutside } from "@/hooks/useClickOutside";
import { Search } from "lucide-react";
import { cn } from "@/lib/utils";
import { ICON_MAP } from "@/lib/constants";
import { Input } from "@/components/ui/input";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";

// ---------------------------------------------------------------------------
// IconPicker — a popover grid for choosing a Lucide icon from ICON_MAP.
// Uses the shared useClickOutside hook (no extra Radix dependency).
// ---------------------------------------------------------------------------

interface IconPickerProps {
  value: string;
  onChange: (icon: string) => void;
  disabled?: boolean;
}

const iconEntries = Object.entries(ICON_MAP);

/** Split a PascalCase or camelCase name into space-separated lowercase words. */
function humanise(name: string): string {
  return name.replace(/([a-z0-9])([A-Z])/g, "$1 $2").replace(/(\d+)/g, " $1 ").toLowerCase();
}

export function IconPicker({ value, onChange, disabled }: IconPickerProps) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const containerRef = useRef<HTMLDivElement>(null);

  // Close on click outside
  useClickOutside(containerRef, () => setOpen(false), open);

  // Close on Escape
  useEffect(() => {
    if (!open) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [open]);

  const filtered = search
    ? iconEntries.filter(([name]) => humanise(name).includes(search.toLowerCase()))
    : iconEntries;

  const SelectedIcon = ICON_MAP[value] ?? ICON_MAP.Bot;

  return (
    <div className="relative" ref={containerRef}>
      {/* Trigger */}
      <Tip content={t_i18n("Change icon")}>
        <button aria-label={t_i18n("Change icon")}
          type="button"
          disabled={disabled}
          onClick={() => !disabled && setOpen((o) => !o)}
          className={cn(
            "flex h-9 w-9 shrink-0 items-center justify-center rounded-sm border border-elevation-subtle bg-elevation-default transition-colors hover:bg-elevation-hover focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
            disabled && "opacity-50 cursor-not-allowed",
            open && "ring-2 ring-accent/40",
          )}
        >
          <SelectedIcon className="h-4 w-4 text-accent" />
        </button>
      </Tip>

      {/* Popover panel */}
      {open && (
        <div className="absolute left-0 top-full z-50 mt-1.5 w-[320px] rounded-sm border border-elevation-subtle layer-1 bg-elevation-default shadow-overlay animate-in fade-in-0 zoom-in-95 duration-150">
          {/* Search */}
          <div className="p-2 border-b border-elevation-subtle">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-default-secondary" />
              <Input aria-label={t_i18n("Search icons...")}
                className="h-8 pl-8 content-compact"
                placeholder={t_i18n("Search icons...")}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                autoFocus
              />
            </div>
          </div>

          {/* Grid */}
          <div className="p-2 max-h-[240px] overflow-y-auto">
            {filtered.length === 0 ? (
              <p className="py-4 text-center content-compact text-default-secondary">{t_i18n("No icons found")}</p>
            ) : (
              <div className="grid grid-cols-8 gap-1">
                {filtered.map(([name, Icon]) => (
                  <Tip key={name} content={name}>
                    <button aria-label={name}
                      type="button"
                      onClick={() => { onChange(name); setOpen(false); setSearch(""); }}
                      className={cn(
                        "flex h-8 w-8 items-center justify-center rounded-sm transition-colors",
                        value === name
                          ? "bg-accent/15 text-accent ring-1 ring-accent/40"
                          : "text-default-secondary hover:bg-elevation-hover hover:text-default-primary",
                      )}
                    >
                      <Icon className="h-4 w-4" />
                    </button>
                  </Tip>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
