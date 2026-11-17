// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Key } from "lucide-react";
import { AdminApiKeyRow } from "@/components/api-keys/AdminApiKeyRow";
import { useApiKeyRevocation } from "@/components/api-keys/useApiKeyRevocation";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { MultiSelect } from "@/components/ui/multi-select";
import { Pagination } from "@/components/ui/pagination";
import { TotalChip } from "@/components/ui/total-chip";
import { SearchInput, TOOLBAR_SEARCH_HEIGHT } from "@/components/ui/search-input";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useUsers } from "@/hooks/api/useAuthApi";
import { useAdminApiKeys, useApiKeyPolicy } from "@/hooks/api/useMiscApi";
import { useDebounce } from "@/hooks/useDebounce";
import { useNow } from "@/hooks/useTick";
import { useFormatter } from "@/i18n";
import type { AdminApiKeyIssue, AdminApiKeyStatus } from "@/types";
import { Spinner } from "@filigran/design-system";

// ---------------------------------------------------------------------------
// Settings > Access and security > API keys: every personal API key of the
// platform with its owner, scopes, expiry and last use, and its revocation.
// The filters live in the URL so a Security Audit finding can open the list
// on the keys it is about (`?issue=no_expiry`), and a link on the keys of
// given users (`?owner=<user id>`, repeated).
// ---------------------------------------------------------------------------

const PAGE_SIZE = 50;
/** The API refuses more owners than this in one listing. */
const MAX_OWNER_FILTER = 100;

const STATUSES: AdminApiKeyStatus[] = ["all", "active", "expired", "revoked"];
const ISSUES: AdminApiKeyIssue[] = ["no_expiry", "full_scope", "beyond_max_lifetime"];

/** @i18n-keys */
const STATUS_LABELS = {
  all: "All",
  active: "Active",
  expired: "Expired",
  revoked: "Revoked",
} as const satisfies Record<AdminApiKeyStatus, string>;

/** @i18n-keys */
const ISSUE_LABELS = {
  no_expiry: "No expiry",
  full_scope: "Full access",
  beyond_max_lifetime: "Beyond the maximum lifetime",
} as const satisfies Record<AdminApiKeyIssue, string>;

const ANY_ISSUE = "any";

function readStatus(value: string | null): AdminApiKeyStatus {
  return STATUSES.find((status) => status === value) ?? "all";
}

function readIssue(value: string | null): AdminApiKeyIssue | undefined {
  return ISSUES.find((issue) => issue === value);
}

export function ApiKeysAdminSection() {
  const { t_i18n } = useFormatter();
  const [searchParams, setSearchParams] = useSearchParams();
  const status = readStatus(searchParams.get("status"));
  const issue = readIssue(searchParams.get("issue"));
  const ownerParam = searchParams.getAll("owner").join(",");
  const ownerIds = useMemo(() => (ownerParam ? ownerParam.split(",") : []), [ownerParam]);
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounce(search.trim(), 300);
  const [page, setPage] = useState(0);
  const now = useNow();

  const { data: policy } = useApiKeyPolicy();
  const { data: users } = useUsers();
  const ownerOptions = useMemo(
    () =>
      (users ?? []).map((user) => ({
        value: user.id,
        label: user.full_name ? `${user.full_name} (${user.email})` : user.email,
      })),
    [users],
  );
  const { data, isLoading, isError, error, isFetching } = useAdminApiKeys({
    status,
    issue,
    search: debouncedSearch || undefined,
    ownerIds,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  });
  const { confirmElement, revoke, pendingId } = useApiKeyRevocation();

  const toggleOwner = (ownerId: string) => {
    const next = ownerIds.includes(ownerId)
      ? ownerIds.filter((id) => id !== ownerId)
      : ownerIds.length < MAX_OWNER_FILTER
        ? [...ownerIds, ownerId]
        : ownerIds;
    setSearchParams(
      (params) => {
        const updated = new URLSearchParams(params);
        updated.delete("owner");
        for (const id of next) updated.append("owner", id);
        return updated;
      },
      { replace: true },
    );
    setPage(0);
  };

  const setFilter = (name: "status" | "issue", value: string | undefined) => {
    setSearchParams(
      (params) => {
        const next = new URLSearchParams(params);
        if (value) next.set(name, value);
        else next.delete(name);
        return next;
      },
      { replace: true },
    );
    setPage(0);
  };

  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  return (
    <Card className="border-elevation-subtle-soft layer-1 bg-elevation-default backdrop-blur-sm" data-testid="admin-api-keys">
      {confirmElement}
      <CardHeader className="pb-4">
        <CardTitle className="flex items-center gap-2 font-heading text-lg">
          <Key className="h-5 w-5 text-accent" />
          {t_i18n("API keys")}
          {isFetching && !isLoading && <Spinner size="sm" tone="inherit" className="text-default-secondary" />}
        </CardTitle>
        <CardDescription>
          {t_i18n(
            "Every personal API key of the platform, with its owner, scopes, expiry and last use. Revoking a key stops it at once and its owner is notified.",
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <SearchInput
            inputClassName={TOOLBAR_SEARCH_HEIGHT}
            placeholder={t_i18n("Search by key name, prefix or owner")}
            value={search}
            onChange={(value) => {
              setSearch(value);
              setPage(0);
            }}
            className="min-w-[240px] flex-1"
          />
          <SegmentedFilter
            options={STATUSES.map((key) => ({ key, label: STATUS_LABELS[key] }))}
            value={status}
            onChange={(value) => setFilter("status", value === "all" ? undefined : value)}
            ariaLabel={t_i18n("Key status")}
          />
          <Select value={issue ?? ANY_ISSUE} onValueChange={(value) => setFilter("issue", value === ANY_ISSUE ? undefined : value)}>
            {/* 300 px: the longest exposure label (fr, about 232 px at 14 px) fits beside the chevron; api-keys-exposure-width.test.ts keeps it so. */}
            <SelectTrigger className="h-8 w-[300px]" aria-label={t_i18n("Key exposure")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ANY_ISSUE}>{t_i18n("Any scope or expiry")}</SelectItem>
              {ISSUES.map((key) => (
                <SelectItem key={key} value={key}>
                  {t_i18n(ISSUE_LABELS[key])}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <MultiSelect
            options={ownerOptions}
            selected={ownerIds}
            onToggle={toggleOwner}
            placeholder={t_i18n("Any owner")}
            searchPlaceholder={t_i18n("Search users...")}
            emptyOptionsLabel={t_i18n("No users yet.")}
            noMatchLabel={t_i18n("No matching people.")}
            className="w-[240px]"
            height="h-8"
          />
          {/* The total once, at the top; the pager under the rows never repeats it (#11191). */}
          {total > 0 && <TotalChip total={total} className="ml-auto" />}
        </div>

        {isLoading ? (
          <div className="flex items-center justify-center py-12">
            <Spinner size="lg" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} />
          </div>
        ) : isError ? (
          <p className="py-8 text-center content-base text-feedback-error-primary" role="alert">
            {error?.message ?? t_i18n("Could not load the API keys.")}
          </p>
        ) : items.length === 0 ? (
          <div className="rounded-sm border border-dashed border-elevation-subtle-soft py-10 text-center">
            <p className="content-base text-default-secondary">{t_i18n("No API key matches these filters.")}</p>
          </div>
        ) : (
          <div className="space-y-2">
            {items.map((key) => (
              <AdminApiKeyRow
                key={key.id}
                apiKey={key}
                maxLifetimeDays={policy?.max_lifetime_days ?? 0}
                now={now}
                showOwner
                revoking={pendingId === key.id}
                onRevoke={() => void revoke(key)}
              />
            ))}
          </div>
        )}

        <Pagination page={page} pageSize={PAGE_SIZE} total={total} onChange={setPage} />
      </CardContent>
    </Card>
  );
}
