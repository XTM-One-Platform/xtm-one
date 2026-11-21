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
 * Browser profiles - `/connections`, in Tools & Integrations.
 *
 * A profile is a web application and its sign-in, kept by the platform: the
 * URL, write-only credentials, an optional TOTP secret, the saved session. The
 * agents it is given to browse signed in with it (Agent > Configuration >
 * Browser profiles, and Profile > General Assistant for a person's own
 * assistant), and the Video Studio records with it. A pending MFA prompt is
 * answered on its card.
 */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { KeyRound, Plus } from "lucide-react";

import { BrowserProfileCard } from "@/components/browser-profiles/BrowserProfileCard";
import { BrowserProfileDialog } from "@/components/browser-profiles/BrowserProfileDialog";
import { NavFamilyHeader } from "@/components/layout/NavFamilyHeader";
import { Button } from "@/components/ui/button";
import { CardGridSkeleton, cardSkeletonCount } from "@/components/ui/card-grid";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { SearchInput } from "@/components/ui/search-input";
import { useToast } from "@/components/ui/toast";
import { VisibilityEmptyHint, VisibilityFilter } from "@/components/ui/visibility-filter";
import { useBrowserProfiles, useDeleteBrowserProfile } from "@/hooks/api/useBrowserProfilesApi";
import { useAuth } from "@/hooks/useAuth";
import { useEntityFilter } from "@/hooks/useEntityFilter";
import { useFormatter } from "@/i18n";
import type { BrowserProfile } from "@/types";

/** What the search reads besides the name: the application's address and the description. */
const PROFILE_SEARCH_FIELDS = (profile: BrowserProfile) => [profile.start_url, profile.description];

export default function BrowserProfiles() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { user } = useAuth();
  const [ConfirmEl, confirm] = useConfirm();
  const [params, setParams] = useSearchParams();
  const { data: profiles, isLoading } = useBrowserProfiles();
  const remove = useDeleteBrowserProfile();
  const [editing, setEditing] = useState<BrowserProfile | null | undefined>(params.get("new") ? null : undefined);
  const { search, setSearch, visibilityFilter, setVisibilityFilter, visibilityCounts, allItems, filtered } = useEntityFilter({
    items: profiles,
    userId: user?.id,
    searchFields: PROFILE_SEARCH_FIELDS,
  });

  const closeDialog = () => {
    setEditing(undefined);
    if (params.get("new")) setParams({}, { replace: true });
  };
  const onDelete = async (profile: BrowserProfile) => {
    const ok = await confirm({
      title: t_i18n("Delete this browser profile?"),
      description: t_i18n(
        "Its credentials and saved session are erased. The agents it was given to browse without it; recordings made with it are kept.",
      ),
      confirmLabel: t_i18n("Delete"),
      variant: "danger",
    });
    if (!ok) return;
    try {
      await remove.mutateAsync(profile.id);
    } catch (err) {
      toast({
        title: t_i18n("The profile was not deleted"),
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    }
  };

  return (
    <div className="space-y-6">
      {/* The Connections family header (#9301 N3b): its h1, the page's help
          in the "i" (#10215), and one tab row of the connection pages. */}
      <NavFamilyHeader
        to="/connections"
        title="Browser profiles"
        infoLabel={t_i18n("About browser profiles")}
        info={t_i18n(
          "Saved sign-ins to web applications. Give a profile to an agent and its browser tool opens the application signed in - for an answer, a report, a deck or a recording - without ever seeing the credentials. A session that expired signs in again by itself when it can.",
        )}
        actions={
          <Button className="gap-1.5" onClick={() => setEditing(null)}>
            <Plus className="h-4 w-4" /> {t_i18n("New browser profile")}
          </Button>
        }
      />

      {isLoading ? (
        <CardGridSkeleton count={cardSkeletonCount()} label={t_i18n("Loading")} className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" />
      ) : !profiles?.length ? (
        <EmptyState
          icon={KeyRound}
          title="No browser profiles yet"
          description="A browser profile is a web application and its sign-in: the URL, credentials, an optional TOTP secret. The session is saved, so the agents you give it, and the Video Studio, start signed in."
          actionLabel="New browser profile"
          actionIcon={Plus}
          actionVariant="default"
          onAction={() => setEditing(null)}
        />
      ) : (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-3">
            <SearchInput toolbar placeholder={t_i18n("Search browser profiles...")} value={search} onChange={setSearch} />
            <VisibilityFilter value={visibilityFilter} onChange={setVisibilityFilter} counts={visibilityCounts} />
            <span className="ml-auto rounded-sm bg-elevation-hover px-2.5 py-1 content-compact font-medium tabular-nums text-default-secondary">
              {allItems.length}
            </span>
          </div>
          {filtered.length > 0 ? (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {filtered.map((profile) => (
                <BrowserProfileCard
                  key={profile.id}
                  profile={profile}
                  onEdit={() => setEditing(profile)}
                  onDelete={() => void onDelete(profile)}
                />
              ))}
            </div>
          ) : (
            <VisibilityEmptyHint value={visibilityFilter} counts={visibilityCounts} onShowAll={() => setVisibilityFilter("all")}>
              <p className="py-8 text-center content-base text-default-secondary">{t_i18n("No browser profiles matching your filters")}</p>
            </VisibilityEmptyHint>
          )}
        </div>
      )}

      {editing !== undefined && (
        <BrowserProfileDialog
          key={editing?.id ?? "new"}
          open
          profile={editing}
          onOpenChange={(open) => {
            if (!open) closeDialog();
          }}
        />
      )}
      {ConfirmEl}
    </div>
  );
}
