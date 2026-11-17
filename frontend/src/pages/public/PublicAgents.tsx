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

import { Icon } from "@filigran/design-system";
import { useState } from "react";

import { cardSkeletonCount } from "@/components/ui/card-grid";
import { TagFilter } from "@/components/ui/tag-filter";
import { Pagination } from "@/components/ui/pagination";
import { usePublicCatalogAgents, usePublicCatalogTags } from "@/hooks/api/usePublicCatalogApi";
import { useDebounce } from "@/hooks/useDebounce";
import { useFormatter } from "@/i18n";

import { CatalogCard, CatalogEmpty, CatalogPageHeader, CatalogSearch, CatalogListSkeleton } from "./components";

const PAGE_SIZE = 20;

export default function PublicAgents() {
  const { t_i18n } = useFormatter();
  const [search, setSearch] = useState("");
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [page, setPage] = useState(0);
  const debouncedSearch = useDebounce(search, 300);

  const params = {
    search: debouncedSearch || undefined,
    tags: selectedTags.length > 0 ? selectedTags : undefined,
    offset: page * PAGE_SIZE,
    limit: PAGE_SIZE,
  };
  const { data, isLoading } = usePublicCatalogAgents(params);
  const { data: tagsData } = usePublicCatalogTags();

  if (isLoading && !data) return <CatalogListSkeleton count={cardSkeletonCount(PAGE_SIZE)} />;

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const allTags = tagsData?.agents ?? [];

  // Reset page when filters change
  const handleSearchChange = (v: string) => { setSearch(v); setPage(0); };
  const handleTagChange = (t: string[]) => { setSelectedTags(t); setPage(0); };

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
        <CatalogPageHeader icon={<Icon name="bot" size={20} className="text-accent" />} title="Agents" count={total} />
        <CatalogSearch value={search} onChange={handleSearchChange} placeholder="Search agents..." className="w-full sm:max-w-xs" />
      </div>
      {allTags.length > 0 && (
        <TagFilter allTags={allTags} selected={selectedTags} onChange={handleTagChange} />
      )}
      {items.length === 0 ? (
        <CatalogEmpty message="No agents found in the catalog" />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 stagger-children">
          {items.map((a) => (
            <CatalogCard
              key={a.id}
              to={`/catalog/agents/${a.id}`}
              icon={<Icon name="bot" size={16} className="text-accent" />}
              name={a.name}
              description={a.description}
              tags={a.tags}
              meta={<span>{t_i18n("{count, plural, one {# assignment} other {# assignments}}", { count: a.assignment_count })}</span>}
              sharedBy={a.shared_by}
            />
          ))}
        </div>
      )}
      <Pagination page={page} pageSize={PAGE_SIZE} total={total} onChange={setPage} />
    </div>
  );
}
