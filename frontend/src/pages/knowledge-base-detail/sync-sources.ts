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
 * The sync sources a knowledge base can read from, shared by the Sources tab
 * (the forms) and the Activity tab (the Source column), so a source shows the
 * same human label everywhere, never its raw `source_type` key.
 */
import { withBasePath } from "@/lib/base-path";

export const SYNC_SOURCES = [
  { key: "website", label: "Website", icon: null, lucideIcon: "Globe", description: "Scrape and import content from any public website", requiredProvider: null, fields: [{ key: "url", label: "Starting URL", placeholder: "https://docs.example.com", hint: "The first page read: links are followed from it." }, { key: "max_pages", label: "Max pages", placeholder: "50", hint: "Most pages read in one sync." }, { key: "max_depth", label: "Max depth", placeholder: "2", hint: "How many links away from the starting URL the crawl goes." }] },
  { key: "notion", label: "Notion", icon: withBasePath("/logos/notion.svg"), lucideIcon: null, description: "Import pages from Notion", requiredProvider: "notion", fields: [{ key: "page_id", label: "Page ID", placeholder: "e.g. 1a2b3c4d5e6f7890abcdef1234567890", hint: "Sync a specific page (highest priority)" }, { key: "database_id", label: "Database ID", placeholder: "e.g. 1a2b3c4d5e6f7890abcdef1234567890", hint: "Sync all pages from a database (ignored if Page ID is set)" }, { key: "search_query", label: "Search query", placeholder: "e.g. documentation", hint: "Search Notion pages (only used when no ID is set)" }, { key: "recursive", label: "Include sub-pages", type: "switch" as const, hint: "Recursively index all child pages and nested databases" }] },
  { key: "microsoft-drive", label: "OneDrive / SharePoint", icon: withBasePath("/logos/microsoft.svg"), lucideIcon: null, description: "Import files from Microsoft cloud storage", requiredProvider: "microsoft", fields: [{ key: "folder_path", label: "Folder path", placeholder: "/", hint: "The folder files are imported from: / is the top of the drive." }, { key: "site_id", label: "SharePoint site (optional)", placeholder: "URL, site name, or site ID: leave empty for OneDrive", hint: "Empty imports from your OneDrive; a site imports from its SharePoint library." }, { key: "recursive", label: "Include subfolders", type: "switch" as const, hint: "Recursively import files from all subfolders" }] },
  { key: "google-drive", label: "Google Drive", icon: withBasePath("/logos/google.svg"), lucideIcon: null, description: "Import files from Google Drive", requiredProvider: "google", fields: [{ key: "folder_id", label: "Folder ID", placeholder: "root (for My Drive)", hint: "The ID at the end of the folder's Google Drive URL; root is My Drive." }] },
  { key: "github", label: "GitHub Repository", icon: withBasePath("/logos/github.svg"), lucideIcon: null, description: "Import code and docs from a repository", requiredProvider: "github", fields: [{ key: "repo", label: "Repository", placeholder: "owner/repo-name", hint: "The repository as owner/name." }, { key: "branch", label: "Branch", placeholder: "main", hint: "The branch the files are read from." }, { key: "path", label: "Subdirectory (optional)", placeholder: "docs/", hint: "Only files under this folder are imported; empty imports the whole repository." }] },
  { key: "github_public", label: "GitHub Repository (Public)", icon: withBasePath("/logos/github.svg"), lucideIcon: null, description: "Import code and docs from a public repository (no auth required)", requiredProvider: null, fields: [{ key: "repo", label: "Repository", placeholder: "owner/repo-name", hint: "The repository as owner/name." }, { key: "branch", label: "Branch", placeholder: "main", hint: "The branch the files are read from." }, { key: "path", label: "Subdirectory (optional)", placeholder: "docs/", hint: "Only files under this folder are imported; empty imports the whole repository." }] },
  { key: "drata", label: "Drata Policies", icon: withBasePath("/logos/drata.svg"), lucideIcon: null, description: "Import compliance policies from Drata", requiredProvider: "drata", fields: [{ key: "statuses", label: "Policy statuses", placeholder: "ACTIVE", type: "tags", hint: "Only policies in these statuses are imported." }] },
];

/**
 * The source types of a document added by hand or by upload (`KnowledgeDocument.source_type`,
 * set from the file extension by the backend `source_type_for_filename`), and of a document
 * stored by an agent, an assignment or the workspace conversation index. @i18n-keys
 */
export const DOCUMENT_SOURCE_LABELS = {
  text: "Text",
  markdown: "Markdown",
  pdf: "PDF",
  docx: "Word document",
  pptx: "PowerPoint presentation",
  xlsx: "Excel spreadsheet",
  xls: "Legacy Excel spreadsheet",
  csv: "CSV",
  json: "JSON",
  url: "URL",
  agent: "Agent",
  assignment: "Assignment",
  conversation: "Conversation",
} as const satisfies Record<string, string>;

/** The English label of a source type (a catalog key, translated by the caller); an unknown type stays its key. */
export function sourceLabel(sourceType: string): string {
  const documentLabel = (DOCUMENT_SOURCE_LABELS as Record<string, string>)[sourceType];
  return documentLabel || SYNC_SOURCES.find((s) => s.key === sourceType)?.label || sourceType;
}
