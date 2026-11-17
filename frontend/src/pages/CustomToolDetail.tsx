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

import { useState } from "react";
import { Icon } from "@filigran/design-system";
import { useParams, useNavigate } from "react-router-dom";
import { useTabParam } from "@/hooks/useTabParam";
import {
  Wrench, Trash2, TestTube2,
  Settings2, BarChart3, Activity,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import { useToast } from "@/components/ui/toast";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { NotFoundState } from "@/components/ui/not-found-state";
import { ToolTestDialog } from "@/components/ui/tool-test-dialog";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { useAuth } from "@/hooks/useAuth";
import {
  useCustomTools, useDeleteCustomTool, useTestCustomTool, useUploadToolTestFile, useCleanupToolTestFiles,
  useRunCustomToolTests, useDetachCustomTool,
} from "@/hooks/api/useCustomToolApi";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { isEntityShared, canManageEntity } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { NewToolForm } from "./custom-tool-detail/NewToolForm";
import { BuiltinToolDetail } from "./custom-tool-detail/BuiltinToolDetail";
import { OverviewTab } from "./custom-tool-detail/OverviewTab";
import { ConfigTab } from "./custom-tool-detail/ConfigTab";
import { ActivityTab } from "./custom-tool-detail/ActivityTab";
import { TestsTab, TestStatusBadge } from "./custom-tool-detail/TestsTab";

type Tab = "overview" | "config" | "tests" | "activity";

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

export default function CustomToolDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const { user, isAdmin } = useAuth();
  // The admin scope: an admin opens a colleague's tool from Objects or an agent.
  const { data: tools, isLoading } = useCustomTools({ scope: "all" });
  const deleteTool = useDeleteCustomTool();
  const testTool = useTestCustomTool();
  const uploadFile = useUploadToolTestFile();
  const cleanupFiles = useCleanupToolTestFiles();
  const [tab, setTab] = useTabParam<Tab>("overview");
  const [testDialogOpen, setTestDialogOpen] = useState(false);
  const [detachOpen, setDetachOpen] = useState(false);
  const runTests = useRunCustomToolTests();
  const detachTool = useDetachCustomTool();
  const [ConsentEl, confirmSharing] = useCredentialSharingConfirm();

  if (id === "new") {
    return <NewToolForm />;
  }

  // Built-in tool detail pages
  if (id?.startsWith("builtin-")) {
    return <BuiltinToolDetail toolId={id} />;
  }

  const tool = tools?.find((t) => t.id === id);
  const isShared = isEntityShared(tool, user?.id);
  const canManage = canManageEntity(tool, user?.id, isAdmin);
  // Installed by a skill package: its write routes (delete, test file uploads,
  // configuration) refuse everyone, so none of them is offered.
  const isPackageOrigin = !!tool?.is_package_origin;
  // Synced from a repository: its content (code, parameters, tests) changes
  // there, and the API refuses a delete until it is detached.
  const isSynced = !!tool?.source_id;
  const hasDeclaredTests = !!(tool?.test_cases?.length || tool?.test_code?.trim());

  const handleDelete = async () => {
    if (!tool) return;
    if (await confirmSharing({ entity_type: "custom_tool", entity_id: tool.id, change: { delete: true } }, { mode: "delete", fallback: { title: "Delete Custom Tool", description: `Are you sure you want to delete "${tool.name}"? Agents using this tool will lose access.` } })) {
      deleteTool.mutate(tool.id, {
        onSuccess: () => {
          toast({ title: t_i18n("Tool deleted"), variant: "success" });
          navigate("/tools/custom");
        },
      });
    }
  };

  const handleTest = () => {
    if (!tool) return;
    setTestDialogOpen(true);
  };

  if (isLoading) {
    return <DetailPageSkeleton backTo="/tools/custom" backLabel="Back to Tools" tabs={3} actions={1} />;
  }

  if (!tool) {
    return (
      <NotFoundState
        icon={Wrench}
        title="Tool not found"
        description="This tool may have been deleted or you don't have access."
        actionLabel="View All Tools"
        onAction={() => navigate("/tools/custom")}
        backTo="/tools/custom"
        backLabel="Back to Tools"
      />
    );
  }

  const handleRunTests = () => {
    setTab("tests");
    runTests.mutate(
      { id: tool.id },
      {
        onSuccess: (report) => {
          toast({
            title: report.status === "passed" ? t_i18n("Tests passed") : t_i18n("Some tests did not pass"),
            variant: report.status === "passed" ? "success" : "error",
          });
        },
        onError: (err) => toast({ title: t_i18n("Tests could not run"), description: err.message, variant: "error" }),
      },
    );
  };

  const tabs: { key: Tab; label: string; icon: React.ElementType }[] = [
    { key: "overview", label: "Overview", icon: BarChart3 },
    { key: "config", label: "Configuration", icon: Settings2 },
    { key: "tests", label: "Tests", icon: TestTube2 },
    { key: "activity", label: "Activity", icon: Activity },
  ];

  return (
    <div className="space-y-6">
      {ConsentEl}

      {/* Tool test dialog */}
      <ToolTestDialog
        open={testDialogOpen}
        onOpenChange={setTestDialogOpen}
        toolName={tool.name}
        toolDescription={tool.description ?? undefined}
        inputSchema={tool.input_schema}
        dryRunOption
        onRun={async (args) => {
          return new Promise((resolve) => {
            testTool.mutate(
              { id: tool.id, inputs: args },
              {
                onSuccess: (data) => resolve(data),
                onError: (err) => resolve({ status: "error", message: err.message }),
              },
            );
          });
        }}
        onUploadFile={isPackageOrigin ? undefined : async (file) => {
          return uploadFile.mutateAsync({ toolId: tool.id, file });
        }}
        onCleanupFiles={isPackageOrigin ? undefined : (fileIds) => {
          cleanupFiles.mutate({ toolId: tool.id, fileIds });
        }}
      />

      <ConfirmDialog
        open={detachOpen}
        onOpenChange={setDetachOpen}
        title={t_i18n("Detach from the repository?")}
        description={t_i18n("The tool becomes an ordinary tool you can edit here. Its source stops updating it, even when the repository changes.")}
        confirmLabel={t_i18n("Detach")}
        variant="warning"
        onConfirm={() => {
          detachTool.mutate(tool.id, {
            onSuccess: () => toast({ title: t_i18n("Tool detached"), variant: "success" }),
            onError: (err) => toast({ title: t_i18n("Could not detach the tool"), description: err.message, variant: "error" }),
          });
        }}
      />

      <EntityPageHeader
        backTo="/tools/custom"
        backLabel="Back to Tools"
        icon={
          tool.icon_url ? (
            <img src={tool.icon_url} alt="" className="h-7 w-7 object-contain" />
          ) : (
            Wrench
          )
        }
        title={tool.name}
        badges={
          <>
            <span className="content-base text-default-secondary">{t_i18n("Custom JavaScript Tool")}</span>
            <span className="text-default-disabled">·</span>
            <Badge variant={tool.enabled ? "success" : "outline"} className="content-caption">
              {tool.enabled ? t_i18n("Enabled") : t_i18n("Disabled")}
            </Badge>
            {tool.is_company_managed && <CompanyManagedBadge size="default" />}
            {isShared && <SharedBadge size="default" ownerName={tool.owner_name} />}
            {((tool.is_company_managed && !isAdmin) || (isShared && !canManage) || isPackageOrigin) && (
              <ReadOnlyBadge />
            )}
            {isSynced && (
              <Badge variant="cyan" className="content-caption gap-1" title={tool.source_commit_sha ?? undefined}>
                <Icon name="link-2" size={16} />
                {tool.source_url ? (
                  <a href={tool.source_url} target="_blank" rel="noreferrer" className="hover:underline">
                    {t_i18n("Synced from {source}", { source: tool.source_name ?? "" })}
                  </a>
                ) : (
                  t_i18n("Synced from {source}", { source: tool.source_name ?? "" })
                )}
              </Badge>
            )}
            {(hasDeclaredTests || tool.last_test_status) && <TestStatusBadge status={tool.last_test_status} />}
          </>
        }
        actions={
          <>
            {/* The API runs a test (and its file uploads) for a manage right only. */}
            {canManage && (
              <Button variant="secondary" size="sm" className="gap-1.5" onClick={handleTest}>
                <TestTube2 className="h-3.5 w-3.5" />
                {t_i18n("Test Tool")}
              </Button>
            )}
            {/* Runs the tests the tool declares (data cases and test code). */}
            {canManage && hasDeclaredTests && (
              <Button
                variant="secondary"
                size="sm"
                className="gap-1.5"
                onClick={handleRunTests}
                disabled={runTests.isPending}
                aria-busy={runTests.isPending || undefined}
              >
                <Icon name="flask-conical" size={16} />
                {runTests.isPending ? t_i18n("Running...") : t_i18n("Run tests")}
              </Button>
            )}
            {canManage && isSynced && (
              <Button variant="secondary" size="sm" className="gap-1.5" onClick={() => setDetachOpen(true)} disabled={detachTool.isPending}>
                <Icon name="unlink" size={16} />
                {t_i18n("Detach")}
              </Button>
            )}
            {canManage && !isPackageOrigin && !isSynced && (
              /* No gate: `delete_custom_tool` is un-gated server-side (T091), so
                 blocking it here would leave tools unmanageable with AI off for
                 no server-side reason (/code-review, 2026-08-28). */
              <Button aria-label={t_i18n("Delete tool")} variant="ghost" size="sm" className="h-8 w-8 p-0 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10" onClick={handleDelete} disabled={deleteTool.isPending}>
                <Trash2 className="h-4 w-4" />
              </Button>
            )}
          </>
        }
      />

      {/* Tabs */}
      <DetailTabs tabs={tabs} active={tab} />

      {tab === "overview" && <OverviewTab tool={tool} />}
      {tab === "config" && <ConfigTab tool={tool} canManage={!!canManage} />}
      {tab === "tests" && <TestsTab tool={tool} canManage={!!canManage} />}
      {tab === "activity" && <ActivityTab tool={tool} />}
    </div>
  );
}
