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

/**
 * Investigation packs (`/investigation-packs`): the Deep Investigation
 * packs users build, in Agent Studio. A pack is a method — ordered steps, each
 * served by sources — run from the chat's source picker with the runner's own
 * access. The built-in packs are listed to start from: duplicating one makes a
 * private copy to edit.
 */
import { useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Copy, Plus, Telescope } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { EmptyState } from "@/components/ui/empty-state";
import { EntityNameCell, EntityTable, type EntityTableColumn } from "@/components/ui/entity-table";
import { AgenticEEBadgeForButton, AgenticGateDialog } from "@/components/ui/license-gate-dialog";
import { SharedBadge } from "@/components/ui/shared-badge";
import { useToast } from "@/components/ui/toast";
import { VisibilityEmptyHint } from "@/components/ui/visibility-filter";
import {
  useBuiltinPacks,
  useDuplicateBuiltinPack,
  useInvestigationPacks,
} from "@/hooks/api/useInvestigationPackApi";
import { useAuth } from "@/hooks/useAuth";
import { useEntityFilter } from "@/hooks/useEntityFilter";
import { useViewMode } from "@/hooks/useViewMode";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { isEntityShared } from "@/lib/utils";
import type { InvestigationPack } from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";

function PackBadges({ pack, userId }: { pack: InvestigationPack; userId?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <>
      {pack.recommended && (
        <Badge variant="cyan" className="px-1.5 py-0 content-caption">
          {t_i18n("recommended")}
        </Badge>
      )}
      {pack.is_company_managed && <CompanyManagedBadge />}
      {isEntityShared(pack, userId) && <SharedBadge ownerName={pack.owner_name} />}
    </>
  );
}

function PackCard({ pack, userId }: { pack: InvestigationPack; userId?: string }) {
  const { t_i18n, intl } = useFormatter();
  const steps = pack.definition.steps ?? [];
  return (
    <Link to={`/investigation-packs/${pack.id}`} className="block h-full">
      <Card className="h-full cursor-pointer border-elevation-subtle transition-all hover:bg-elevation-hover">
        <CardContent className="flex h-full flex-col p-4">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-sm bg-accent/10">
              <Telescope className="h-5 w-5 text-accent" />
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate content-base font-medium">{pack.name}</p>
              <p className="truncate content-caption text-default-secondary">
                {pack.description || t_i18n("No description")}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <PackBadges pack={pack} userId={userId} />
            </div>
          </div>
          <ol className="mt-3 space-y-0.5 text-[11px] text-default-secondary">
            {steps.slice(0, 4).map((step, i) => (
              <li key={step.key} className="truncate">
                {i + 1}. {step.label}
                {step.optional && <span className="text-default-secondary"> · {t_i18n("optional")}</span>}
              </li>
            ))}
            {steps.length > 4 && <li>{t_i18n("and {count} more", { count: steps.length - 4 })}</li>}
          </ol>
          <div className="mt-auto flex items-center gap-3 border-t border-elevation-subtle-soft pt-3 content-caption text-default-secondary">
            <span>{t_i18n("Version {n}", { n: pack.version_number })}</span>
            <span className="ml-auto">{formatTimeAgo(intl, pack.updated_at)}</span>
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}

function usePackColumns(userId?: string): EntityTableColumn<InvestigationPack>[] {
  const { t_i18n, intl } = useFormatter();
  return useMemo(
    () => [
      {
        key: "name",
        header: "Name",
        sortValue: (p) => p.name,
        cell: (p) => (
          <EntityNameCell
            icon={<Telescope className="h-4 w-4 text-accent" />}
            name={p.name}
            description={p.description || null}
            badges={<PackBadges pack={p} userId={userId} />}
          />
        ),
      },
      {
        key: "steps",
        header: "Steps",
        align: "right",
        className: "whitespace-nowrap tabular-nums text-default-secondary",
        sortValue: (p) => p.definition.steps?.length ?? 0,
        cell: (p) => t_i18n("{count} steps", { count: p.definition.steps?.length ?? 0 }),
      },
      {
        key: "version",
        header: "Version",
        align: "right",
        className: "whitespace-nowrap tabular-nums text-default-secondary",
        sortValue: (p) => p.version_number,
        cell: (p) => String(p.version_number),
      },
      {
        key: "updated",
        header: "Updated",
        align: "right",
        className: "whitespace-nowrap text-default-secondary",
        sortValue: (p) => p.updated_at,
        cell: (p) => formatTimeAgo(intl, p.updated_at),
      },
    ],
    [t_i18n, intl, userId],
  );
}

export default function InvestigationPacks() {
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { user } = useAuth();
  const agenticAIEnabled = useAgenticAIEnabled();
  const { data: packs, isLoading } = useInvestigationPacks();
  const { data: builtins } = useBuiltinPacks();
  const duplicate = useDuplicateBuiltinPack();
  const [viewMode, setViewMode] = useViewMode("investigation-packs");
  const columns = usePackColumns(user?.id);
  const { search, setSearch, visibilityFilter, setVisibilityFilter, visibilityCounts, clearAll, allItems, filtered } = useEntityFilter({
    items: packs,
    userId: user?.id,
  });

  const startFrom = (slug: string) =>
    duplicate.mutate(slug, {
      onSuccess: (copy) => {
        toast({ title: t_i18n("A private copy is ready to edit"), variant: "success" });
        navigate(`/investigation-packs/${copy.id}`);
      },
    });

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-3xl font-bold">{t_i18n("Investigation packs")}</h1>
          <p className="mt-2 text-default-secondary">
            {t_i18n("Your team's investigation methods: ordered steps, the sources each step reads, the choices offered at launch.")}
          </p>
        </div>
        <AgenticGateDialog feature={t_i18n("Building investigation packs")}>
          <Button className="gap-1.5" onClick={() => navigate("/investigation-packs/new")}>
            <Plus className="h-4 w-4" /> {t_i18n("New pack")}
            <AgenticEEBadgeForButton />
          </Button>
        </AgenticGateDialog>
      </div>

        <>
          {!isLoading && allItems.length === 0 ? (
            <EmptyState
              icon={Telescope}
              title="No investigation pack yet"
              requiresAi
              description="Build your team's method as a pack, or start from a built-in one below."
              actionLabel={agenticAIEnabled ? "New pack" : undefined}
              onAction={agenticAIEnabled ? () => navigate("/investigation-packs/new") : undefined}
            />
          ) : (
            // Standard v2: the shared toolbar of the records table (search, scope,
            // Clear all | total, Columns, page size, view), in list and grid.
            <EntityTable
              isLoading={isLoading}
              items={filtered}
              columns={columns}
              rowKey={(p) => p.id}
              rowHref={(p) => `/investigation-packs/${p.id}`}
              defaultSortKey="name"
              storageKey="investigation-packs"
              search={{ value: search, onChange: setSearch, placeholder: t_i18n("Search packs...") }}
              scope={{ value: visibilityFilter, onChange: setVisibilityFilter, counts: visibilityCounts }}
              view={{ value: viewMode, onChange: setViewMode }}
              renderGrid={
                viewMode === "grid"
                  ? (page) => (
                      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                        {page.map((pack) => <PackCard key={pack.id} pack={pack} userId={user?.id} />)}
                      </div>
                    )
                  : undefined
              }
              totalAll={allItems.length}
              onClearAll={() => clearAll(undefined, { keepScope: true })}
              noResultsLabel={t_i18n("No pack matching your filters")}
              emptyContent={
                <VisibilityEmptyHint value={visibilityFilter} counts={visibilityCounts} onShowAll={() => setVisibilityFilter("all")}>
                  <p className="py-8 text-center content-base text-default-secondary">{t_i18n("No pack matching your filters")}</p>
                </VisibilityEmptyHint>
              }
            />
          )}
        </>

      {builtins && builtins.length > 0 && (
        <section className="space-y-3">
          <div>
            <h2 className="text-lg font-semibold">{t_i18n("Start from a built-in pack")}</h2>
            <p className="content-compact text-default-secondary">
              {t_i18n("A private copy with every source the built-in pack can hold, resolved for each runner.")}
            </p>
          </div>
          <div className="grid gap-3 md:grid-cols-3">
            {builtins.map((b) => (
              <Card key={b.slug} className="border-dashed">
                <CardContent className="flex h-full flex-col gap-2 p-4">
                  <p className="content-base font-medium">{t_i18n(b.label)}</p>
                  <p className="line-clamp-3 text-[11px] text-default-secondary">{b.description}</p>
                  <AgenticGateDialog feature={t_i18n("Building investigation packs")}>
                    <Button
                      variant="outline"
                      size="sm"
                      className="mt-auto gap-1.5"
                      disabled={duplicate.isPending}
                      onClick={() => startFrom(b.slug)}
                      data-builtin-pack={b.slug}
                    >
                      <Copy className="h-3.5 w-3.5" />
                      {t_i18n("Duplicate")}
                    </Button>
                  </AgenticGateDialog>
                </CardContent>
              </Card>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
