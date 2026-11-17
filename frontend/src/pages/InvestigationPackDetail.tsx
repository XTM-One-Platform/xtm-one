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
 * One investigation pack (`/investigation-packs/:id/:tab?`): the
 * builder (the editor beside a preview), its sharing, its versions. `new`
 * opens the builder on an empty pack. A save sends the whole method (the
 * server replaces it, and a changed method is a new version).
 */
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { History, Save, Share2, Telescope, Trash2, Wrench } from "lucide-react";

import { PackBuilder } from "@/components/investigation/pack-builder/PackBuilder";
import { PackAssistant } from "@/components/investigation/pack-builder/PackAssistant";
import { PackPreview } from "@/components/investigation/pack-builder/PackPreview";
import { emptyDraft, refusalText } from "@/components/investigation/pack-builder/pack-draft";
import { ConfirmDeleteDialog } from "@/components/ui/confirm-delete-dialog";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { EntityConfigCard } from "@/components/ui/entity-config-card";
import { Input } from "@/components/ui/input";
import { NotFoundState } from "@/components/ui/not-found-state";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import { useToast } from "@/components/ui/toast";
import {
  useCreateInvestigationPack,
  useDeleteInvestigationPack,
  useInvestigationPack,
  useInvestigationPackVersion,
  useInvestigationPackVersions,
  usePackBuilderCatalog,
  useRestoreInvestigationPackVersion,
  useUpdateInvestigationPack,
} from "@/hooks/api/useInvestigationPackApi";
import { useAuth } from "@/hooks/useAuth";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { useTabParam } from "@/hooks/useTabParam";
import { useUserShareGrants } from "@/hooks/useUserShareGrants";
import { useFormatter } from "@/i18n";
import { isEntityShared } from "@/lib/utils";
import type { InvestigationPack, InvestigationPackFields } from "@/types";
import { formatTimeAgo } from "@/lib/relative-time";

type Tab = "builder" | "sharing" | "versions";

function fieldsOf(pack: InvestigationPack): InvestigationPackFields {
  return {
    name: pack.name,
    description: pack.description,
    subject_types: pack.subject_types,
    recommended: pack.recommended,
    max_iterations: pack.max_iterations,
    definition: { steps: pack.definition.steps ?? [], exposed: pack.definition.exposed ?? [] },
  };
}

export default function InvestigationPackDetail() {
  const { id = "" } = useParams<{ id: string }>();
  const isNew = id === "new";
  const navigate = useNavigate();
  const { user, isAdmin } = useAuth();
  const { data: pack, isLoading } = useInvestigationPack(isNew ? "" : id);
  const { data: catalog, isError: catalogUnavailable } = usePackBuilderCatalog();
  const [tab] = useTabParam<Tab>("builder");

  // The pack and the builder catalog are read together: one skeleton, the loaded page's header and tab bar.
  const packSkeleton = <DetailPageSkeleton backTo="/investigation-packs" backLabel="Back to Investigation Packs" tabs={isNew ? 1 : 3} actions={isNew ? 0 : 1} />;
  if (!isNew && isLoading) return packSkeleton;
  // An unknown pack (404) or the feature off on this platform (403): both
  // reach here as no data, never the ErrorBoundary.
  if ((!isNew && !pack) || (!catalog && catalogUnavailable)) {
    return (
      <NotFoundState
        icon={Telescope}
        title="Investigation pack not found"
        description="This pack may have been deleted or you don't have access."
        actionLabel="View all packs"
        onAction={() => navigate("/investigation-packs")}
        backTo="/investigation-packs"
        backLabel="Back to Investigation Packs"
      />
    );
  }
  if (!catalog) return packSkeleton;

  const canManage = isNew || !!pack?.can_manage;
  const canRecommend = isNew || isAdmin || pack?.user_id === user?.id;
  const tabs: { key: Tab; label: string; icon: React.ElementType }[] = isNew
    ? [{ key: "builder", label: "Builder", icon: Wrench }]
    : [
        { key: "builder", label: "Builder", icon: Wrench },
        { key: "sharing", label: "Sharing", icon: Share2 },
        { key: "versions", label: "Versions", icon: History },
      ];

  return (
    <div className="space-y-6">
      <PackHeader pack={pack ?? null} canManage={canManage} />
      <DetailTabs tabs={tabs} active={isNew ? "builder" : tab} />
      {(isNew || tab === "builder") && (
        <BuilderTab
          key={pack ? `${pack.id}:${pack.version_number}:${pack.updated_at}` : "new"}
          pack={pack ?? null}
          catalog={catalog}
          canManage={canManage}
          canRecommend={canRecommend}
        />
      )}
      {!isNew && pack && tab === "sharing" && <SharingTab pack={pack} />}
      {!isNew && pack && tab === "versions" && <VersionsTab pack={pack} />}
    </div>
  );
}

function PackHeader({ pack, canManage }: { pack: InvestigationPack | null; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { user } = useAuth();
  const remove = useDeleteInvestigationPack();
  const aiOff = useAiActionDisabled();
  const [confirming, setConfirming] = useState(false);
  const shared = pack ? isEntityShared(pack, user?.id) : false;

  return (
    <div>
      <EntityPageHeader
        backTo="/investigation-packs"
        backLabel="Back to Investigation Packs"
        icon={Telescope}
        title={pack ? pack.name : t_i18n("New investigation pack")}
        badges={
          <>
            <span className="content-base text-default-secondary">{t_i18n("Investigation pack")}</span>
            {pack && (
              <Badge variant="outline" className="content-caption">
                {t_i18n("Version {n}", { n: pack.version_number })}
              </Badge>
            )}
            {pack?.is_company_managed && <CompanyManagedBadge size="default" />}
            {shared && <SharedBadge size="default" ownerName={pack?.owner_name} />}
            {pack && !canManage && <ReadOnlyBadge />}
          </>
        }
        actions={
          pack && canManage && (
          <AiDisabledTip><Button
            variant="ghost"
            size="sm"
            className="h-8 w-8 p-0 text-feedback-error-primary hover:bg-feedback-error-primary/10 hover:text-feedback-error-primary"
            aria-label={t_i18n("Delete the pack")}
            disabled={remove.isPending || aiOff}
            onClick={() => setConfirming(true)}
          >
            <Trash2 className="h-4 w-4" />
          </Button></AiDisabledTip>
          )
        }
      />
      {pack && (
          <ConfirmDeleteDialog
            open={confirming}
            onOpenChange={setConfirming}
            name={pack.name}
            title={t_i18n("Delete the pack")}
            description={t_i18n("Delete this pack and its versions? Past runs keep what they queried.")}
            onConfirm={() =>
              remove.mutate(pack.id, {
                onSuccess: () => {
                  toast({ title: t_i18n("Pack deleted"), variant: "success" });
                  navigate("/investigation-packs");
                },
              })
            }
          />
      )}
    </div>
  );
}

function BuilderTab({
  pack,
  catalog,
  canManage,
  canRecommend,
}: {
  pack: InvestigationPack | null;
  catalog: NonNullable<ReturnType<typeof usePackBuilderCatalog>["data"]>;
  canManage: boolean;
  canRecommend: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const navigate = useNavigate();
  const saved = useMemo(
    () => (pack ? fieldsOf(pack) : emptyDraft(catalog.reference_sources)),
    [pack, catalog],
  );
  const [draft, setDraft] = useState<InvestigationPackFields>(saved);
  const [message, setMessage] = useState("");
  const create = useCreateInvestigationPack();
  const update = useUpdateInvestigationPack();
  const aiOff = useAiActionDisabled();
  const dirty = JSON.stringify(draft) !== JSON.stringify(saved);
  const saving = create.isPending || update.isPending;
  const error = refusalText(create.error ?? update.error);

  // Leaving with unsaved changes asks first.
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const save = () => {
    const body = { ...draft, name: draft.name.trim(), commit_message: message.trim() || null };
    if (!pack) {
      create.mutate(body, {
        onSuccess: (created) => {
          toast({ title: t_i18n("Pack created"), variant: "success" });
          navigate(`/investigation-packs/${created.id}`, { replace: true });
        },
      });
      return;
    }
    update.mutate(
      { id: pack.id, ...body },
      {
        onSuccess: (next) => {
          setMessage("");
          toast({
            title:
              next.version_number > pack.version_number
                ? t_i18n("Saved as version {n}", { n: next.version_number })
                : t_i18n("Saved"),
            variant: "success",
          });
        },
      },
    );
  };

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
      <div className="min-w-0 space-y-4">
        <PackBuilder
          draft={draft}
          onChange={setDraft}
          catalog={catalog}
          readOnly={!canManage}
          canRecommend={canRecommend}
        />
        {canManage && (
          <div className="sticky bottom-0 z-10 flex flex-wrap items-center gap-2 rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-3 backdrop-blur">
            {pack && (
              <Input
                aria-label={t_i18n("What changed")}
                value={message}
                maxLength={500}
                className="h-8 max-w-sm content-compact"
                placeholder={t_i18n("What changed (kept in the version history)")}
                onChange={(e) => setMessage(e.target.value)}
              />
            )}
            {error && <span className="content-compact text-feedback-warning-primary">{error}</span>}
            <AiDisabledTip>
              <Button
                className="ml-auto gap-1.5"
                disabled={saving || aiOff || !draft.name.trim() || (!!pack && !dirty)}
                onClick={save}
              >
                <Save className="h-4 w-4" />
                {pack ? t_i18n("Save") : t_i18n("Create the pack")}
              </Button>
            </AiDisabledTip>
          </div>
        )}
      </div>
      <div className="min-w-0 space-y-4">
        {canManage && <PackAssistant draft={draft} onApply={setDraft} />}
        <PackPreview draft={draft} slug={pack?.slug ?? null} dirty={dirty} />
      </div>
    </div>
  );
}

function SharingTab({ pack }: { pack: InvestigationPack }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateInvestigationPack();
  const [editing, setEditing] = useState(false);
  const [companyManaged, setCompanyManaged] = useState(pack.is_company_managed);
  const [groupIds, setGroupIds] = useState<string[] | null>(pack.shared_group_ids ?? null);
  const [groupPermissions, setGroupPermissions] = useState<Record<string, string> | null>(pack.shared_group_permissions ?? null);
  const shareGrants = useUserShareGrants(pack);

  const start = () => {
    setEditing(true);
    setCompanyManaged(pack.is_company_managed);
    setGroupIds(pack.shared_group_ids ?? null);
    setGroupPermissions(pack.shared_group_permissions ?? null);
    shareGrants.reset();
  };

  const save = () =>
    update.mutate(
      {
        id: pack.id,
        ...fieldsOf(pack),
        is_company_managed: companyManaged,
        shared_group_ids: companyManaged ? null : groupIds && groupIds.length ? groupIds : null,
        shared_group_permissions: companyManaged ? null : groupPermissions && Object.keys(groupPermissions).length ? groupPermissions : null,
        ...shareGrants.toPayload(companyManaged),
      },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Sharing saved"), variant: "success" });
          setEditing(false);
        },
      },
    );

  return (
    <EntityConfigCard
      entityLabel="investigation pack"
      isCompanyManaged={pack.is_company_managed}
      userId={pack.user_id}
      sharedGroupIds={pack.shared_group_ids}
      ownerName={pack.owner_name}
      canManage={pack.can_manage}
      editing={editing}
      onStartEditing={start}
      onCancel={() => setEditing(false)}
      onSave={save}
      isSaving={update.isPending}
      editCompanyManaged={companyManaged}
      onCompanyManagedChange={setCompanyManaged}
      editSharedGroupIds={groupIds}
      onSharedGroupIdsChange={setGroupIds}
      editSharedGroupPermissions={groupPermissions}
      onSharedGroupPermissionsChange={setGroupPermissions}
      userSharing={shareGrants.props}
      sharingNotice={
        <p className="content-compact text-default-secondary">
          {t_i18n("Whoever runs a shared pack runs it with their own integrations and corpora, never yours: what they cannot use is named and left out.")}
        </p>
      }
      readView={null}
      editView={null}
    />
  );
}

function VersionsTab({ pack }: { pack: InvestigationPack }) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const { data: versions } = useInvestigationPackVersions(pack.id);
  const restore = useRestoreInvestigationPackVersion();
  const aiOff = useAiActionDisabled();
  const [viewing, setViewing] = useState<number | null>(null);
  const { data: detail } = useInvestigationPackVersion(pack.id, viewing);

  return (
    <ol className="space-y-2" data-pack-versions>
      {(versions ?? []).map((v) => (
        <li key={v.version_number} className="flex flex-wrap items-center gap-3 rounded-sm border border-elevation-subtle px-4 py-3">
          <Badge variant={v.is_current ? "cyan" : "outline"} className="tabular-nums">
            v{v.version_number}
          </Badge>
          <div className="min-w-0 flex-1">
            <p className="truncate content-base">{v.commit_message || t_i18n("No message")}</p>
            <p className="text-[11px] text-default-secondary">
              {v.author_name ?? t_i18n("Unknown author")} · {formatTimeAgo(intl, v.created_at)}
            </p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setViewing(viewing === v.version_number ? null : v.version_number)}
          >
            {viewing === v.version_number ? t_i18n("Hide") : t_i18n("View")}
          </Button>
          {v.is_current ? (
            <span className="content-compact text-accent">{t_i18n("Current")}</span>
          ) : (
            pack.can_manage && (
              <AiDisabledTip><Button
                variant="outline"
                size="sm"
                disabled={restore.isPending || aiOff}
                onClick={() =>
                  restore.mutate(
                    { id: pack.id, version: v.version_number },
                    {
                      onSuccess: (next) =>
                        toast({ title: t_i18n("Restored as version {n}", { n: next.version_number }), variant: "success" }),
                    },
                  )
                }
              >
                {t_i18n("Restore")}
              </Button></AiDisabledTip>
            )
          )}
          {viewing === v.version_number && detail?.version_number === v.version_number && (
            <ol className="basis-full space-y-0.5 pl-12 content-compact text-default-secondary">
              {detail.definition.steps.map((step, i) => (
                <li key={step.key}>
                  {i + 1}. {step.label}
                  <span className="text-default-secondary">
                    {" "}· {step.sources.map((src) => src.name || src.reference || src.kind).join(", ") || t_i18n("fed by a pivot")}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </li>
      ))}
    </ol>
  );
}
