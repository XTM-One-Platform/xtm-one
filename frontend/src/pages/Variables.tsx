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

import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  Braces, Copy, Eye, EyeOff, Lock, Plus, Trash2,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { NavFamilyHeader } from "@/components/layout/NavFamilyHeader";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { CardGridSkeleton, cardSkeletonCount } from "@/components/ui/card-grid";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { SharedBadge } from "@/components/ui/shared-badge";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { AgenticGateDialog, AgenticEEBadgeForButton } from "@/components/ui/license-gate-dialog";
import { TagList } from "@/components/ui/tag-list";
import { EntityTable, EntityNameCell, type EntityTableColumn } from "@/components/ui/entity-table";
import { useAuth } from "@/hooks/useAuth";
import { useEntityFilter } from "@/hooks/useEntityFilter";
import { useViewMode } from "@/hooks/useViewMode";
import { useAgenticAIEnabled, useCustomVariablesEnabled } from "@/hooks/usePlatformConfig";
import {
  useCreateVariable, useDeleteVariable, useUpdateVariable, useVariable, useVariables,
} from "@/hooks/api/useVariableApi";
import { COPY_FEEDBACK_DELAY } from "@/lib/constants";
import { cn, isEntityShared } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { Variable } from "@/types";
import { ResourceSharingWaysBadge } from "@/components/ui/sharing-ways-badge";
import { ResourceSharingCard } from "@/components/ui/resource-sharing-card";
import { SecretField } from "@/components/ui/secret-field";
import { VaultSecretsNotice } from "@/components/ui/vault-secrets";
import { formatTimeAgo } from "@/lib/relative-time";

// ---------------------------------------------------------------------------
// Shared cells
// ---------------------------------------------------------------------------

/**
 * Variable value preview. Secrets render masked behind a per-row reveal toggle;
 * plain values render inline. Shared by the card footer and the list row.
 */
function VariableValue({ variable }: { variable: Variable }) {
  const [revealed, setRevealed] = useState(false);

  if (!variable.is_secret) {
    return (
      <span className="content-caption text-default-secondary font-mono-plex truncate max-w-[200px]">
        {variable.value || "—"}
      </span>
    );
  }

  return (
    <button
      onClick={(e: React.MouseEvent) => { e.stopPropagation(); setRevealed(!revealed); }}
      className="flex items-center gap-1.5 content-caption text-default-secondary hover:text-default-primary transition-colors"
    >
      {revealed ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
      {revealed ? variable.value : "••••••••"}
    </button>
  );
}

/** Copy-the-`{{var_id.…}}`-reference button. */
function CopyReferenceButton({ variableId, onCopy }: { variableId: string; onCopy: (id: string) => void }) {
  const { t_i18n } = useFormatter();
  return (
    <Tip content={t_i18n("Copy reference")}>
      <button aria-label={t_i18n("Copy reference")}
        onClick={(e: React.MouseEvent) => { e.stopPropagation(); onCopy(variableId); }}
        className="flex items-center gap-1 content-caption text-default-disabled hover:text-accent transition-colors"
      >
        <Copy className="h-3 w-3" />
      </button>
    </Tip>
  );
}

// ---------------------------------------------------------------------------
// Variable Card
// ---------------------------------------------------------------------------

function VariableCard({
  variable,
  userId,
  onEdit,
  onCopy,
}: {
  variable: Variable;
  userId?: string;
  onEdit: (v: Variable) => void;
  onCopy: (id: string) => void;
}) {
  const { t_i18n, intl } = useFormatter();
  const isShared = isEntityShared(variable, userId);

  return (
    <Card
      className={cn(
        "transition-all cursor-pointer hover:bg-elevation-hover h-full",
        "border-elevation-subtle",
      )}
      onClick={() => onEdit(variable)}
    >
      <CardContent className="p-4 flex flex-col h-full">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-accent/10">
            {variable.is_secret ? (
              <Lock className="h-5 w-5 text-feedback-warning-primary" />
            ) : (
              <Braces className="h-5 w-5 text-accent" />
            )}
          </div>
          <div className="min-w-0 flex-1">
            <p className="content-base font-medium truncate">{variable.name}</p>
            <p className="content-caption text-default-secondary truncate">
              {variable.description || t_i18n("No description")}
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {variable.is_secret && (
              <Badge variant="outline" className="content-caption text-feedback-warning-primary border-feedback-alert-primary/20">
                {t_i18n("Secret")}
              </Badge>
            )}
            {variable.is_company_managed && <CompanyManagedBadge />}
            {isShared && <SharedBadge />}
            <ResourceSharingWaysBadge kind="variable" id={variable.id} />
          </div>
        </div>

        {/* Value preview + metadata */}
        <div className="flex items-center gap-3 mt-auto pt-3 border-t border-elevation-subtle-soft">
          <VariableValue variable={variable} />
          <CopyReferenceButton variableId={variable.id} onCopy={onCopy} />
          <span className="content-caption text-default-disabled ml-auto">{formatTimeAgo(intl, variable.updated_at)}</span>
          <TagList tags={variable.tags} />
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Variables List View
// ---------------------------------------------------------------------------

/** List-view columns — name, value, secret flag, tags, last modified, copy. */
function useVariableColumns({
  userId,
  onCopy,
}: {
  userId?: string;
  onCopy: (id: string) => void;
}): EntityTableColumn<Variable>[] {
  const { t_i18n, intl } = useFormatter();
  return useMemo(() => [
    {
      key: "name",
      header: "Name",
      sortValue: (v) => v.name,
      cell: (v) => (
        <EntityNameCell
          icon={v.is_secret
            ? <Lock className="h-4 w-4 text-feedback-warning-primary" />
            : <Braces className="h-4 w-4 text-accent" />}
          name={v.name}
          description={v.description || null}
          badges={
            <>
              {v.is_company_managed && <CompanyManagedBadge />}
              {isEntityShared(v, userId) && <SharedBadge />}
              <ResourceSharingWaysBadge kind="variable" id={v.id} />
            </>
          }
        />
      ),
    },
    {
      key: "value",
      header: "Value",
      interactive: true,
      cell: (v) => <VariableValue variable={v} />,
    },
    {
      key: "secret",
      header: "Type",
      className: "whitespace-nowrap",
      sortValue: (v) => v.is_secret,
      cell: (v) => v.is_secret ? (
        <Badge variant="outline" className="content-caption text-feedback-warning-primary border-feedback-alert-primary/20">
          {t_i18n("Secret")}
        </Badge>
      ) : (
        <Badge variant="outline" className="content-caption">{t_i18n("Plain")}</Badge>
      ),
    },
    {
      key: "tags",
      header: "Tags",
      cell: (v) => <TagList tags={v.tags} />,
    },
    {
      key: "updated",
      header: "Updated",
      align: "right",
      className: "whitespace-nowrap text-default-secondary",
      sortValue: (v) => v.updated_at,
      cell: (v) => formatTimeAgo(intl, v.updated_at),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      interactive: true,
      cell: (v) => <CopyReferenceButton variableId={v.id} onCopy={onCopy} />,
    },
  ], [t_i18n, intl, userId, onCopy]);
}

// ---------------------------------------------------------------------------
// Variable Form Dialog
// ---------------------------------------------------------------------------

function VariableFormDialog({
  open,
  onOpenChange,
  title,
  initial,
  onSave,
  onDelete,
  isPending,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  initial?: Variable | null;
  onSave: (data: {
    name: string;
    value: string;
    is_secret: boolean;
    description: string;
    tags: string[];
  }) => void;
  onDelete?: () => void;
  isPending: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [name, setName] = useState(initial?.name || "");
  const [value, setValue] = useState(initial?.is_secret ? "" : initial?.value || "");
  const [isSecret, setIsSecret] = useState(initial?.is_secret || false);
  const [description, setDescription] = useState(initial?.description || "");
  const [tags, setTags] = useState<string[]>(initial?.tags || []);
  const [copied, setCopied] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSave({ name, value, is_secret: isSecret, description, tags });
  };

  const copyReference = () => {
    if (!initial) return;
    navigator.clipboard.writeText(`{{var_id.${initial.id}}}`);
    setCopied(true);
    toast({ title: t_i18n("Copied to clipboard") });
    setTimeout(() => setCopied(false), COPY_FEEDBACK_DELAY);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t_i18n(title)}</DialogTitle>
          <DialogDescription>
            {t_i18n("Variables are inserted via the variable picker in personas, prompts, skills, and tools.")}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4 mt-2">
          <div className="space-y-2">
            <Label htmlFor="variable-name">{t_i18n("Name")}</Label>
            <Input id="variable-name" aria-label={t_i18n("Name")}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t_i18n("My variable")}
              required
            />
            {initial && (
              <button
                type="button"
                onClick={copyReference}
                className="flex items-center gap-1.5 content-caption text-default-secondary hover:text-accent transition-colors"
              >
                <Copy className="h-3 w-3" />
                {copied ? t_i18n("Copied!") : t_i18n("Copy reference")}
              </button>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="variable-value">{t_i18n("Value")}</Label>
            {isSecret ? (
              <SecretField
                id="variable-value"
                aria-label={t_i18n("Value")}
                value={value}
                onChange={setValue}
                hasStoredValue={Boolean(initial?.is_secret)}
                stored={initial?.vault_secrets?.locations?.value?.[0] ?? null}
                placeholder={t_i18n("Value")}
              />
            ) : (
              <Input id="variable-value" aria-label={t_i18n("Value")}
                value={value}
                onChange={(e) => setValue(e.target.value)}
                placeholder={t_i18n("Value")}
              />
            )}
            {initial?.is_secret && <VaultSecretsNotice status={initial.vault_secrets} />}
          </div>
          <div className="flex items-center gap-3">
            <Switch aria-label={t_i18n("Secret")} checked={isSecret} onCheckedChange={setIsSecret} />
            <div>
              <p className="content-base font-medium">{t_i18n("Secret")}</p>
              <p className="content-caption text-default-secondary">{t_i18n("Encrypted at rest, hidden in UI")}</p>
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="variable-description">{t_i18n("Description")}</Label>
            <textarea
              id="variable-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t_i18n("What is this variable used for?")}
              rows={2}
              className="flex min-h-[60px] w-full rounded-sm border border-elevation-subtle bg-transparent px-3 py-2 content-base shadow-xs placeholder:text-default-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus disabled:cursor-not-allowed disabled:opacity-50"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="variable-tags">{t_i18n("Tags")}</Label>
            <TagInput id="variable-tags" value={tags} onChange={setTags} />
          </div>
          <div className="flex items-center justify-between pt-2">
            <div>
              {onDelete && (
                <Button type="button" variant="ghost" size="sm" className="text-feedback-error-primary/70 hover:text-feedback-error-primary" onClick={onDelete}>
                  <Trash2 className="h-3.5 w-3.5 mr-1" /> {t_i18n("Delete")}
                </Button>
              )}
            </div>
            <div className="flex gap-2">
              <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>{t_i18n("Cancel")}</Button>
              <Button type="submit" disabled={!name || isPending}>
                {isPending ? t_i18n("Saving...") : t_i18n("Save")}
              </Button>
            </div>
          </div>
        </form>
        {/* To whom it is shared and through what, for its managers. */}
        {initial && <ResourceSharingCard kind="variable" id={initial.id} />}
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Variables Page
// ---------------------------------------------------------------------------

export default function Variables() {
  // Resolve the security flag BEFORE issuing the variables query so a
  // platform with DISABLE_CUSTOM_VARIABLES set never fetches /variables
  // even though the page mounts to render the "disabled" empty state.
  // Hook calls stay unconditional (rules-of-hooks) — the gating happens
  // inside the React Query ``enabled`` option instead of around the call.
  const customVariablesEnabled = useCustomVariablesEnabled();
  const { data: variables, isLoading } = useVariables({
    enabled: customVariablesEnabled,
  });
  const createMutation = useCreateVariable();
  const updateMutation = useUpdateVariable();
  const deleteMutation = useDeleteVariable();
  const { user } = useAuth();
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const [confirmNode, confirmSharing] = useCredentialSharingConfirm();
  const agenticAIEnabled = useAgenticAIEnabled();

  const searchFields = useCallback((v: Variable) => [v.description, v.value], []);
  const { search, setSearch, selectedTags, setSelectedTags, clearAll, allItems, allTags, filtered } =
    // No visibility control on this page yet: every row it lists stays shown (dev:visibility-filters-consistency).
    useEntityFilter({ items: variables, searchFields, withVisibility: false });

  /* Variables are created from a dialog rather than a route of its own, so
     ``?new=1`` deep-links it open — that is how the Tools & Integrations hub
     card and the ⌘K "New variable" action reach it. The param is dropped
     immediately (replace, not push) so closing the dialog and reloading the
     page do not reopen it. */
  const [searchParams, setSearchParams] = useSearchParams();
  const [createOpen, setCreateOpen] = useState(() => searchParams.get("new") === "1");
  const [editVar, setEditVar] = useState<Variable | null>(null);

  useEffect(() => {
    if (searchParams.get("new") !== "1") return;
    const next = new URLSearchParams(searchParams);
    next.delete("new");
    setSearchParams(next, { replace: true });
    setCreateOpen(true);
  }, [searchParams, setSearchParams]);

  /* ``/connections/variables/:id`` opens one variable's dialog, read through the detail
     route: that is how Administration > Objects opens a colleague's private
     variable, which this list does not carry for an admin either. */
  const { id: routeVariableId } = useParams<{ id?: string }>();
  const navigate = useNavigate();
  const { data: routeVariable, isError: routeVariableMissing } = useVariable(routeVariableId, {
    enabled: customVariablesEnabled,
  });
  const shownVar = editVar ?? routeVariable ?? null;
  const closeEdit = useCallback(() => {
    setEditVar(null);
    if (routeVariableId) navigate("/connections/variables", { replace: true });
  }, [navigate, routeVariableId]);

  useEffect(() => {
    if (routeVariableMissing) navigate("/connections/variables", { replace: true });
  }, [routeVariableMissing, navigate]);

  const handleCreate = async (data: {
    name: string; value: string; is_secret: boolean; description: string; tags: string[];
  }) => {
    try {
      await createMutation.mutateAsync(data);
      setCreateOpen(false);
      toast({ title: t_i18n("Variable created") });
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : t_i18n("Unknown error");
      toast({ title: t_i18n("Error"), description: msg, variant: "error" });
    }
  };

  const handleUpdate = async (data: {
    name: string; value: string; is_secret: boolean; description: string; tags: string[];
  }) => {
    if (!shownVar) return;
    try {
      await updateMutation.mutateAsync({ id: shownVar.id, ...data });
      closeEdit();
      toast({ title: t_i18n("Variable updated") });
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : t_i18n("Unknown error");
      toast({ title: t_i18n("Error"), description: msg, variant: "error" });
    }
  };

  const handleDelete = async () => {
    if (!shownVar) return;
    // Who loses this variable through the agents, assignments, flows, prompts
    // and tools that reference it, or the plain confirmation.
    const ok = await confirmSharing(
      { entity_type: "variable", entity_id: shownVar.id, change: { delete: true } },
      {
        mode: "delete",
        fallback: {
          title: "Delete variable",
          description: `Delete "${shownVar.name}"? Any references to this variable will stop resolving.`,
        },
      },
    );
    if (!ok) return;
    try {
      await deleteMutation.mutateAsync(shownVar.id);
      closeEdit();
      toast({ title: t_i18n("Variable deleted") });
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : t_i18n("Unknown error");
      toast({ title: t_i18n("Error"), description: msg, variant: "error" });
    }
  };

  const copyRef = useCallback((id: string) => {
    navigator.clipboard.writeText(`{{var_id.${id}}}`);
    toast({ title: t_i18n("Copied to clipboard") });
  }, [toast, t_i18n]);

  const [viewMode, setViewMode] = useViewMode("variables");
  const columns = useVariableColumns({ userId: user?.id, onCopy: copyRef });

  return (
    <div className="space-y-6">
      {/* The Connections family header (#9301 N3b): its h1, the page's help
          in the "i" (#10215), and one tab row of the connection pages. */}
      <NavFamilyHeader
        to="/connections/variables"
        title="Variables"
        infoLabel={t_i18n("About variables")}
        info={customVariablesEnabled
          ? t_i18n("Platform variables resolved at runtime in personas, prompts, skills, and tools. Insert them using the variable picker in text editors.")
          : t_i18n("Custom variables are disabled on this platform.")}
        actions={customVariablesEnabled ? (
          <AgenticGateDialog feature="Creating variables" requiresAi={false}>  {/* variables are not AI */}
            <Button className="gap-1.5" onClick={() => setCreateOpen(true)}>
              <Plus className="h-4 w-4" /> {t_i18n("New Variable")}
              <AgenticEEBadgeForButton />
            </Button>
          </AgenticGateDialog>
        ) : undefined}
      />

      {!customVariablesEnabled ? (
        <EmptyState
          icon={Braces}
          title="Variables disabled"
          description="Custom variables have been disabled by the platform administrator."
        />
      ) : (
        <>
          {!isLoading && allItems.length === 0 ? (
            <EmptyState
              icon={Braces}
              title="No variables yet"
              description="Create variables to use dynamic values in personas, prompts, skills, and tools."
              actionLabel={agenticAIEnabled ? "New Variable" : undefined}
              onAction={agenticAIEnabled ? () => setCreateOpen(true) : undefined}
            />
          ) : (
            // Standard v2: the shared toolbar of the records table (search, tags,
            // Clear all | total, Columns, page size, view), in list and grid.
            <EntityTable
              isLoading={isLoading}
              items={filtered}
              columns={columns}
              rowKey={(v) => v.id}
              onRowClick={setEditVar}
              defaultSortKey="name"
              storageKey="variables"
              search={{ value: search, onChange: setSearch, placeholder: t_i18n("Search variables...") }}
              tags={{ all: allTags, selected: selectedTags, onChange: setSelectedTags }}
              view={{ value: viewMode, onChange: setViewMode }}
              // The first read draws this page's own card: no tag row, its footer at its own height (#11345).
              renderGridSkeleton={(pageSize) => <CardGridSkeleton count={cardSkeletonCount(pageSize)} label={t_i18n("Loading")} tags={false} meta="caption" />}
              renderGrid={
                viewMode === "grid"
                  ? (page) => (
                      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                        {page.map((v) => (
                          <VariableCard key={v.id} variable={v} userId={user?.id} onEdit={setEditVar} onCopy={copyRef} />
                        ))}
                      </div>
                    )
                  : undefined
              }
              totalAll={allItems.length}
              externalFilterCount={selectedTags.length}
              onClearAll={() => clearAll()}
              noResultsLabel={t_i18n("No variables matching your filters")}
            />
          )}
        </>
      )}

      {customVariablesEnabled && (
        <VariableFormDialog
          open={createOpen}
          onOpenChange={setCreateOpen}
          title="New Variable"
          onSave={handleCreate}
          isPending={createMutation.isPending}
        />
      )}

      {shownVar && (
        <VariableFormDialog
          key={shownVar.id}
          open
          onOpenChange={(open) => !open && closeEdit()}
          title="Edit Variable"
          initial={shownVar}
          onSave={handleUpdate}
          onDelete={handleDelete}
          isPending={updateMutation.isPending}
        />
      )}

      {confirmNode}
    </div>
  );
}
