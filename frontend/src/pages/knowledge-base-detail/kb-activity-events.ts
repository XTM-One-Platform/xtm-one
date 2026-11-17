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
 * The events of a knowledge base's Activity tab: the event types of the filter
 * and the icon, tone and English label (a catalog key) of each event the
 * backend writes.
 */
import type { IconName } from "@filigran/design-system";

import type { KBActivityEventType } from "@/hooks/api/useKnowledgeBaseApi";

/** The event types of the filter, in the order of a knowledge base's life (the API's ACTIVITY_EVENT_TYPES). */
export const EVENT_TYPES: readonly KBActivityEventType[] = ["sync", "documents", "embeddings"];
export const EVENT_TYPE_LABELS: Record<KBActivityEventType, string> = {
  // A noun for the filter option: the catalog key "Sync" is the verb (audit action).
  sync: "Syncs",
  documents: "Documents",
  embeddings: "Embeddings",
};

/** The DS icon, its tone and the English label of each event the backend writes. */
export const EVENT_CONFIG: Record<string, { icon: IconName; color: string; label: string }> = {
  sync_started: { icon: "play", color: "text-accent", label: "Sync started" },
  sync_completed: { icon: "circle-check", color: "text-feedback-success-primary", label: "Sync completed" },
  sync_failed: { icon: "circle-x", color: "text-feedback-error-primary", label: "Sync failed" },
  sync_alert: { icon: "triangle-alert", color: "text-feedback-warning-primary", label: "Sync alert" },
  sync_quarantined: { icon: "circle-alert", color: "text-feedback-error-primary", label: "Sync quarantined" },
  sync_config_error: { icon: "wrench", color: "text-feedback-error-primary", label: "Sync configuration error" },
  document_added: { icon: "plus", color: "text-accent", label: "Document added" },
  document_updated: { icon: "pencil", color: "text-accent", label: "Document updated" },
  document_deleted: { icon: "minus", color: "text-default-secondary", label: "Document deleted" },
  embedding_completed: { icon: "circle-check", color: "text-feedback-success-primary", label: "Embedding completed" },
  embedding_failed: { icon: "triangle-alert", color: "text-feedback-error-primary", label: "Embedding failed" },
  reprocess_triggered: { icon: "refresh-cw", color: "text-feedback-warning-primary", label: "Reprocess triggered" },
};
