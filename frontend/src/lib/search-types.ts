// SPDX-License-Identifier: Apache-2.0

/**
 * How the top bar search and the `/search` page show and open each entity
 * type: its label (English, translated by the caller), its library icon and
 * the route of one hit. One table, so the typeahead and the page never differ.
 */
import { workItemPath } from "./task-routes.ts";
import type { ComponentProps } from "react";
import type { Icon } from "@filigran/design-system";
import type { SearchHit, SearchType } from "@/hooks/api/useSearchApi";

/** A name of the design system's icon registry. */
export type LibraryIconName = ComponentProps<typeof Icon>["name"];

interface SearchTypeView {
  /** The type's tab on the results page (plural). */
  plural: string;
  /** The type of one row (singular). */
  singular: string;
  icon: LibraryIconName;
  href: (hit: SearchHit) => string;
}

const by = (base: string) => (hit: SearchHit) => `${base}/${hit.id}`;

export const SEARCH_TYPE_VIEW: Record<SearchType, SearchTypeView> = {
  agent: { plural: "Agents", singular: "Agent", icon: "bot", href: by("/agents") },
  fleet: { plural: "Fleets", singular: "Fleet", icon: "network", href: by("/fleets") },
  assignment: { plural: "Assignments", singular: "Assignment", icon: "calendar", href: by("/agents/assignments") },
  flow: { plural: "Flows", singular: "Flow", icon: "link-2", href: (hit) => `/flows/library/${hit.id}` },
  intent: { plural: "Intents", singular: "Intent", icon: "compass", href: () => "/intents" },
  conversation: { plural: "Conversations", singular: "Conversation", icon: "quote", href: (hit) => `/chat?c=${hit.id}` },
  work_item: {
    plural: "Work items", singular: "Work item", icon: "circle-check",
    href: (hit) => workItemPath(hit.id),
  },
  knowledge_base: { plural: "Knowledge bases", singular: "Knowledge base", icon: "database", href: by("/knowledge") },
  knowledge_document: {
    plural: "Documents", singular: "Document", icon: "file-text",
    href: (hit) => (hit.parent_id ? `/knowledge/${hit.parent_id}` : "/knowledge"),
  },
  prompt: { plural: "Prompts", singular: "Prompt", icon: "sparkles", href: by("/prompts") },
  skill: { plural: "Skills", singular: "Skill", icon: "graduation-cap", href: by("/skills") },
  template: { plural: "Templates", singular: "Template", icon: "layout-dashboard", href: by("/templates") },
  presentation: { plural: "Presentations", singular: "Presentation", icon: "image", href: by("/presentations") },
  video: { plural: "Videos", singular: "Video", icon: "image", href: by("/videos") },
  report: { plural: "Reports", singular: "Report", icon: "file-text", href: by("/reports") },
  file: { plural: "Files", singular: "File", icon: "paperclip", href: () => "/files" },
  integration: { plural: "Integrations", singular: "Integration", icon: "link-2", href: by("/integrations") },
  mcp_server: { plural: "MCP servers", singular: "MCP server", icon: "server", href: by("/mcp-servers") },
  custom_tool: { plural: "Custom tools", singular: "Custom tool", icon: "wrench", href: by("/tools") },
  user: { plural: "Users", singular: "User", icon: "users", href: by("/users-and-groups/users") },
  group: { plural: "Groups", singular: "Group", icon: "users", href: by("/users-and-groups/groups") },
};

/** The results page of `q`, optionally on one type's tab. */
export function searchPageHref(q: string, type?: SearchType | null): string {
  const params = new URLSearchParams({ q: q.trim() });
  if (type) params.set("type", type);
  return `/search?${params}`;
}
