/**
 * Pick which operations of an OpenAPI document become agent tools.
 *
 * Grouped by tag (an operation's first tag; untagged ones under "Other"),
 * searchable, with bulk actions. Unsupported operations are listed — never
 * hidden — with the reason they cannot become a tool. Large documents start
 * with their groups collapsed so the page stays usable. When the caller
 * passes `onOverridesChange`, each tool's name and description can be edited.
 */
import { useMemo, useState } from "react";
import { BookOpen, ChevronDown, ChevronRight, Globe, Paperclip, Pencil, PenLine, TriangleAlert } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SearchInput } from "@/components/ui/search-input";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { OpenAPIOperation, OpenAPITag, OpenAPIToolOverride, OpenAPIToolOverrides } from "@/types";
import { withOverride } from "./openapi-overrides";

/** Above this many operations, groups start collapsed. */
const EXPAND_ALL_LIMIT = 60;
const OTHER_GROUP = "__other__";

const METHOD_CLASSES: Record<string, string> = {
  GET: "bg-feedback-success-primary/10 text-feedback-success-primary border-feedback-success-primary/20",
  POST: "bg-accent/10 text-accent border-accent/20",
  PUT: "bg-feedback-alert-primary/10 text-feedback-warning-tertiary border-feedback-alert-primary/20",
  PATCH: "bg-feedback-alert-primary/10 text-feedback-warning-tertiary border-feedback-alert-primary/20",
  DELETE: "bg-feedback-error-primary/10 text-feedback-error-primary border-feedback-error-primary/20",
};

function MethodBadge({ method }: { method: string }) {
  return (
    <span
      className={cn(
        "inline-flex w-14 shrink-0 justify-center rounded-sm border px-1 py-0.5 font-mono-plex content-caption font-semibold",
        METHOD_CLASSES[method] ?? "bg-elevation-hover text-default-secondary border-elevation-subtle-soft",
      )}
    >
      {method}
    </span>
  );
}

interface Group {
  id: string;
  label: string;
  description?: string | null;
  operations: OpenAPIOperation[];
}

export function OpenAPIOperationPicker({
  operations,
  tags,
  selected,
  onChange,
  disabled = false,
  toolName,
  overrides,
  onOverridesChange,
}: {
  operations: OpenAPIOperation[];
  tags: OpenAPITag[];
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
  disabled?: boolean;
  /** An operation's generated tool name as displayed (e.g. with an edited prefix). */
  toolName?: (op: OpenAPIOperation) => string | null | undefined;
  /** User-set tool names and descriptions by operation key. */
  overrides?: OpenAPIToolOverrides;
  onOverridesChange?: (next: OpenAPIToolOverrides) => void;
}) {
  const { t_i18n } = useFormatter();
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string> | null>(null);

  const groups = useMemo<Group[]>(() => {
    const byId = new Map<string, Group>();
    const byName = new Map(tags.map((tag) => [tag.name, tag]));
    // OpenAPI 3.2 tags may carry a display summary and nest under a parent.
    const tagLabel = (name: string) => byName.get(name)?.summary || name;
    for (const tag of tags) {
      const label = tag.parent && byName.has(tag.parent) ? `${tagLabel(tag.parent)} › ${tagLabel(tag.name)}` : tagLabel(tag.name);
      byId.set(tag.name, { id: tag.name, label, description: tag.description, operations: [] });
    }
    for (const op of operations) {
      const id = op.tags[0] ?? OTHER_GROUP;
      if (!byId.has(id)) {
        byId.set(id, { id, label: id === OTHER_GROUP ? t_i18n("Other") : id, operations: [] });
      }
      byId.get(id)!.operations.push(op);
    }
    return [...byId.values()].filter((g) => g.operations.length > 0);
  }, [operations, tags, t_i18n]);

  const needle = query.trim().toLowerCase();
  const matches = (op: OpenAPIOperation) =>
    !needle ||
    [op.key, op.summary, op.tool_name ?? "", op.operation_id ?? "", overrides?.[op.key]?.name ?? ""].some((v) =>
      v.toLowerCase().includes(needle),
    );

  const supported = operations.filter((op) => op.supported);
  const isCollapsed = (id: string) =>
    !needle && (collapsed ? collapsed.has(id) : operations.length > EXPAND_ALL_LIMIT);

  const toggleCollapsed = (id: string) => {
    const base = collapsed ?? new Set(operations.length > EXPAND_ALL_LIMIT ? groups.map((g) => g.id) : []);
    const next = new Set(base);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setCollapsed(next);
  };

  const setMany = (keys: string[], on: boolean) => {
    const next = new Set(selected);
    for (const key of keys) {
      if (on) next.add(key);
      else next.delete(key);
    }
    onChange(next);
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <SearchInput
          value={query}
          onChange={setQuery}
          placeholder={t_i18n("Filter operations...")}
          className="max-w-xs flex-1"
          inputClassName="h-8 content-compact"
        />
        <span className="content-compact text-default-secondary">
          {t_i18n("{selected} of {total} operations selected", { selected: selected.size, total: supported.length })}
        </span>
        {!disabled && (
          <div className="ml-auto flex gap-1">
            <Button variant="ghost" size="sm" className="h-7 content-compact" onClick={() => onChange(new Set(supported.map((op) => op.key)))}>
              {t_i18n("All")}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 content-compact"
              onClick={() => onChange(new Set(supported.filter((op) => op.tool_type === "read").map((op) => op.key)))}
            >
              {t_i18n("Read only")}
            </Button>
            <Button variant="ghost" size="sm" className="h-7 content-compact" onClick={() => onChange(new Set())}>
              {t_i18n("None")}
            </Button>
          </div>
        )}
      </div>

      <div className="max-h-[32rem] overflow-y-auto rounded-sm border border-elevation-subtle-soft">
        {groups.map((group) => {
          const visible = group.operations.filter(matches);
          if (visible.length === 0) return null;
          const groupSupported = group.operations.filter((op) => op.supported).map((op) => op.key);
          const groupSelected = groupSupported.filter((key) => selected.has(key)).length;
          const open = !isCollapsed(group.id);
          return (
            <div key={group.id} className="border-b border-elevation-subtle-soft last:border-b-0">
              <div className="flex items-center gap-2 bg-elevation-hover px-3 py-2">
                <button
                  type="button"
                  className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                  onClick={() => toggleCollapsed(group.id)}
                  aria-expanded={open}
                >
                  {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0" /> : <ChevronRight className="h-3.5 w-3.5 shrink-0" />}
                  <span className="truncate content-compact font-semibold">{group.label}</span>
                  {group.description && (
                    <span className="hidden truncate text-[11px] text-default-secondary sm:inline">— {group.description}</span>
                  )}
                </button>
                <span className="content-caption text-default-secondary">{groupSelected}/{groupSupported.length}</span>
                <Switch
                  checked={groupSupported.length > 0 && groupSelected === groupSupported.length}
                  disabled={disabled || groupSupported.length === 0}
                  onCheckedChange={(on) => setMany(groupSupported, on)}
                  aria-label={t_i18n("Select every operation of {group}", { group: group.label })}
                />
              </div>
              {open &&
                visible.map((op) => (
                  <OperationRow
                    key={op.key}
                    op={op}
                    defaultName={toolName ? toolName(op) : (op.default_tool_name ?? op.tool_name)}
                    override={overrides?.[op.key]}
                    onOverrideChange={
                      onOverridesChange && !disabled
                        ? (patch) => onOverridesChange(withOverride(overrides ?? {}, op.key, patch))
                        : undefined
                    }
                    checked={selected.has(op.key)}
                    disabled={disabled}
                    onToggle={(on) => setMany([op.key], on)}
                  />
                ))}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function OperationRow({
  op,
  defaultName,
  override,
  onOverrideChange,
  checked,
  disabled,
  onToggle,
}: {
  op: OpenAPIOperation;
  defaultName?: string | null;
  override?: OpenAPIToolOverride;
  onOverrideChange?: (patch: OpenAPIToolOverride) => void;
  checked: boolean;
  disabled: boolean;
  onToggle: (on: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const [editing, setEditing] = useState(false);
  const name = override?.name?.trim() || defaultName;
  const description = override?.description?.trim();
  const edited = Boolean(override?.name?.trim() || description);
  return (
    <div className={cn("flex items-start gap-3 px-3 py-2", !op.supported && "opacity-60")}>
      <Switch
        className="mt-0.5"
        checked={op.supported && checked}
        disabled={disabled || !op.supported}
        onCheckedChange={onToggle}
        aria-label={op.key}
      />
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex flex-wrap items-center gap-2">
          <MethodBadge method={op.method} />
          <code className="truncate font-mono-plex content-compact">{op.path}</code>
          {op.deprecated && <Badge variant="warning" className="text-[9px]">{t_i18n("Deprecated")}</Badge>}
          {op.supported && (
            <Badge variant="outline" className="text-[9px]">
              {op.tool_type === "read" ? (
                <BookOpen className="mr-0.5 h-2.5 w-2.5 text-feedback-success-primary" />
              ) : (
                <Pencil className="mr-0.5 h-2.5 w-2.5 text-feedback-warning-primary" />
              )}
              {op.tool_type === "read" ? t_i18n("Read") : t_i18n("Write")}
            </Badge>
          )}
          {op.supported && op.accepts_files && (
            <Tip content={t_i18n("Takes a file: one available in the conversation, or text the agent writes.")}>
              <Badge variant="outline" className="text-[9px]">
                <Paperclip className="mr-0.5 h-2.5 w-2.5" />
                {t_i18n("File")}
              </Badge>
            </Tip>
          )}
          {edited && <Badge variant="cyan" className="text-[9px]">{t_i18n("Edited")}</Badge>}
          {op.supported && !op.sends_credential && (
            <Tip content={t_i18n("The spec marks this operation as public: calls are sent without the credential.")}>
              <Badge variant="outline" className="text-[9px]">
                <Globe className="mr-0.5 h-2.5 w-2.5" />
                {t_i18n("Public")}
              </Badge>
            </Tip>
          )}
          {op.warnings.length > 0 && (
            <Tip content={op.warnings.join(" ")}>
              <TriangleAlert className="h-3.5 w-3.5 text-feedback-warning-primary" />
            </Tip>
          )}
          {op.supported && onOverrideChange && (
            <Tip content={t_i18n("Edit the tool name and description")}>
              <Button
                variant="ghost"
                size="sm"
                className="ml-auto h-6 w-6 p-0"
                onClick={() => setEditing(!editing)}
                aria-label={t_i18n("Edit the tool name and description")}
                aria-expanded={editing}
              >
                <PenLine className="h-3.5 w-3.5" />
              </Button>
            </Tip>
          )}
        </div>
        {description ? (
          <p className="text-[11px] leading-relaxed text-default-secondary">{description}</p>
        ) : (
          op.summary && <p className="text-[11px] leading-relaxed text-default-secondary">{op.summary}</p>
        )}
        {op.supported ? (
          name && <p className="font-mono-plex content-caption text-default-secondary">{name}</p>
        ) : (
          <p className="content-caption text-feedback-warning-primary">{op.skip_reason}</p>
        )}
        {editing && onOverrideChange && (
          <div className="space-y-2 rounded-sm border border-elevation-subtle-soft p-2">
            <div className="space-y-1">
              <label className="content-caption text-default-secondary">{t_i18n("Tool name")}</label>
              <Input aria-label={t_i18n("Tool name")}
                className="h-7 font-mono-plex content-compact"
                value={override?.name ?? ""}
                placeholder={defaultName ?? ""}
                onChange={(e) => onOverrideChange({ name: e.target.value })}
              />
            </div>
            <div className="space-y-1">
              <label className="content-caption text-default-secondary">{t_i18n("Tool description")}</label>
              <textarea
                rows={3}
                value={override?.description ?? ""}
                placeholder={op.default_tool_description ?? op.tool_description ?? ""}
                onChange={(e) => onOverrideChange({ description: e.target.value })}
                className="w-full resize-y rounded-sm border border-elevation-subtle bg-elevation-default px-2 py-1.5 content-compact placeholder:text-default-disabled focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
              />
            </div>
            <div className="flex justify-end gap-1">
              <Button
                variant="ghost"
                size="sm"
                className="h-6 content-compact"
                disabled={!edited}
                onClick={() => onOverrideChange({ name: "", description: "" })}
              >
                {t_i18n("Reset")}
              </Button>
              <Button variant="ghost" size="sm" className="h-6 content-compact" onClick={() => setEditing(false)}>
                {t_i18n("Done")}
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
