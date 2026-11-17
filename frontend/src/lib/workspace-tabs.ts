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
 * The tabs of a workspace page and the list its Operations tab shows.
 * A plain module so the page and its tests share them (fast refresh keeps the
 * component files component-only).
 */
import { ClipboardList, LayoutDashboard, MessagesSquare, Users } from "lucide-react";

export type WorkspaceTab = "overview" | "team" | "operations" | "conversations";

/** The tabs of a workspace; Team only when it hosts a fleet. */
export function workspaceTabs(hostsFleet: boolean): { key: WorkspaceTab; label: string; icon: React.ElementType }[] {
  return [
    { key: "overview", label: "Overview", icon: LayoutDashboard },
    ...(hostsFleet ? [{ key: "team" as const, label: "Team", icon: Users }] : []),
    { key: "operations", label: "Operations", icon: ClipboardList },
    { key: "conversations", label: "Conversations", icon: MessagesSquare },
  ];
}

/** The tab shown: one the workspace has, else its Overview (Team without a fleet, a mistyped URL). */
export function resolveWorkspaceTab(requested: string, hostsFleet: boolean): WorkspaceTab {
  const found = workspaceTabs(hostsFleet).find((t) => t.key === requested);
  return found ? found.key : "overview";
}

export type WorkspaceOpsList = "work-items" | "state" | "worksets";

/** `?list=` of the Operations tab; the work items when it names none. */
export function readWorkspaceOpsList(value: string | null): WorkspaceOpsList {
  return value === "state" || value === "worksets" ? value : "work-items";
}
