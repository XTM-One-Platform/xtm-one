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
 * Work hub (`/work`) — the destination behind the sidebar group of the same
 * name: what agents PRODUCE, as opposed to what they are made of (Agent Studio)
 * and what they act through (Tools & Integrations). Assignment runs, generated
 * decks, and every file exchanged with an agent, in one place.
 *
 * Runs, decks, videos and files are server-paginated, so each card shows a recent
 * window plus the true total (`count`), and "Recently modified" merges the windows — which
 * is exactly the question this hub answers: "what did my agents just do?".
 *
 * The cards are the Work group's visible children (`hubCards`, #9301 N2c):
 * this file keys each page's data by route, the menu decides which show.
 */
import { useMemo } from "react";
import { ResourceHub, type HubItem } from "@/components/ui/resource-hub";
import { hubCards, useNavGroup } from "@/components/layout/use-nav-group";
import { useAllAssignmentRuns, useAssignmentRunCount } from "@/hooks/api/useAssignmentApi";
import { useCustomDashboards } from "@/hooks/api/useCustomDashboardApi";
import { useFiles } from "@/hooks/api/useFileApi";
import { usePresentations } from "@/hooks/api/usePresentationApi";
import { useVideoProjects } from "@/hooks/api/useVideoStudioApi";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import type { RepositoryFile } from "@/types";

/** How many recent items a windowed card contributes to "Recently modified". */
const RECENT_WINDOW = 6;
const RECENT_DECKS = { limit: RECENT_WINDOW, offset: 0 };
const RECENT_VIDEOS = { limit: RECENT_WINDOW, offset: 0 };

/** Where a file is opened from the hub: its conversation or run, else the list. */
function fileHref(file: RepositoryFile): string {
  if (file.context === "conversation" && file.conversation_id) return `/chat?c=${file.conversation_id}`;
  if (file.context === "run" && file.assignment_run_id) return `/operations/executions/${file.assignment_run_id}`;
  return "/files";
}

export default function Work() {
  const runs = useAllAssignmentRuns(RECENT_WINDOW);
  const runCount = useAssignmentRunCount();
  const decks = usePresentations(RECENT_DECKS);
  // Behind `VIDEO_STUDIO`, like the nav entry: off, no card and no request.
  const videoStudio = useFeatureFlag("VIDEO_STUDIO");
  const videos = useVideoProjects(RECENT_VIDEOS, videoStudio);
  // Behind `DASHBOARD_STUDIO`, like the nav entry: off, no card and no request.
  const dashboardStudio = useFeatureFlag("DASHBOARD_STUDIO");
  const dashboards = useCustomDashboards(dashboardStudio);
  const files = useFiles({ limit: RECENT_WINDOW });

  const runItems = useMemo<HubItem[] | undefined>(
    () =>
      runs.data?.map((run) => ({
        id: run.id,
        name: run.assignment_name ?? run.agent_name,
        updated_at: run.completed_at ?? run.started_at ?? run.created_at,
      })),
    [runs.data],
  );
  const deckItems = useMemo<HubItem[] | undefined>(
    () =>
      decks.data?.items.map((deck) => ({
        id: deck.id,
        name: deck.title,
        /* Both stamps are nullable on the wire; a deck with neither is a
           legacy row and sorts to the bottom of "Recently modified". */
        updated_at: deck.updated_at ?? deck.created_at ?? new Date(0).toISOString(),
      })),
    [decks.data],
  );
  const videoItems = useMemo<HubItem[] | undefined>(
    () =>
      videos.data?.items.map((video) => ({
        id: video.id,
        name: video.title,
        updated_at: video.updated_at ?? video.created_at ?? new Date(0).toISOString(),
      })),
    [videos.data],
  );
  const dashboardItems = useMemo<HubItem[] | undefined>(
    () =>
      dashboards.data?.map((dashboard) => ({
        id: dashboard.id,
        name: dashboard.title,
        updated_at: dashboard.updated_at ?? dashboard.created_at ?? new Date(0).toISOString(),
      })),
    [dashboards.data],
  );
  const fileItems = useMemo<HubItem[] | undefined>(
    () =>
      files.data?.files.map((file) => ({
        id: file.id,
        name: file.filename,
        updated_at: file.created_at,
      })),
    [files.data],
  );
  const fileHrefs = useMemo(
    () => new Map((files.data?.files ?? []).map((file) => [file.id, fileHref(file)])),
    [files.data],
  );

  const resources = hubCards(useNavGroup("work"), {
    /* Work items, value and workspaces: a page of tabs over lists the menu row opens, no card (#775). */
    "/tasks": null,
    "/operations": {
      description: "Every assignment run and background task: what your agents did and are doing.",
      items: runItems,
      isLoading: runs.isLoading,
      count: runCount.data?.count,
      detailTo: (item) => `/operations/executions/${item.id}`,
    },
    "/presentations": {
      description: "Decks and pages your agents generated: yours, shared with you, and published to the company. Present, share, export.",
      items: deckItems,
      isLoading: decks.isLoading,
      count: decks.data?.total,
      detailTo: (item) => `/presentations/${item.id}`,
    },
    "/videos": {
      description: "Brand films, event loops, explainers and product demos your agents made - watch, render again, share.",
      items: videoItems,
      isLoading: videos.isLoading,
      count: videos.data?.total,
      detailTo: (item: HubItem) => `/videos/${item.id}`,
    },
    "/dashboards": {
      description: "Pages of widgets your agents build for you and keep up to date, on the web and on your phone.",
      items: dashboardItems,
      isLoading: dashboards.isLoading,
      detailTo: (item: HubItem) => `/dashboards/${item.id}`,
    },
    "/files": {
      description: "Uploads and outputs across conversations and runs (reports, exports, data), searchable in one repository.",
      items: fileItems,
      isLoading: files.isLoading,
      count: files.data?.total,
      detailTo: (item) => fileHrefs.get(item.id) ?? "/files",
    },
  });

  return (
    <ResourceHub
      title="Work"
      description={
        videoStudio
          ? dashboardStudio
            ? "What your agents produce: runs, decks and pages, videos, dashboards, and files."
            : "What your agents produce: runs, decks and pages, videos, and files."
          : dashboardStudio
            ? "What your agents produce: runs, decks and pages, dashboards, and files."
            : "What your agents produce: runs, decks and pages, and files."
      }
      resources={resources}
    />
  );
}
