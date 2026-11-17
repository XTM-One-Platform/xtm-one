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

import { useFormatter } from "@/i18n";
import type { ToolGateEntry } from "@/hooks/api/useHitlWhitelist";

/** Why a tool runs without asking, in words rather than a layer name. */
export function useExemptReason() {
  const { t_i18n } = useFormatter();
  return (entry: ToolGateEntry): string => {
    if (entry.exempt_source === "admin_added") {
      return t_i18n("An administrator lets it run without asking");
    }
    if (entry.exempt_source === "shipped_default") {
      return t_i18n("Read-only, so it runs without asking by default");
    }
    if (entry.exempt_source === "platform_read") {
      return t_i18n("Read-only tool of a connected platform, so it runs without asking");
    }
    if (entry.exempt_source === "platform_switch") {
      return t_i18n("Approvals are suspended for the whole platform");
    }
    if (entry.exempt_source === "connection_switch") {
      return t_i18n("Its connection is set to run its tools without asking");
    }
    if (entry.exempt_source === "connected_read") {
      return t_i18n("Read-only tool of a connected integration, so it runs without asking");
    }
    return t_i18n("Runs without asking");
  };
}

/**
 * Wording for the hand-gated download tools, which share one reason in
 * ``HAND_GATED`` (``backend/app/services/hitl_whitelist_platform.py``). Any
 * other hand-gate gets the general statement.
 */
const DOWNLOAD_GATE_REASON =
  "Asks first even though it only reads: it brings an outside document into the conversation.";

/** Why a tool asks first, when "it is not on any list" is not the whole answer. */
export function useGateReason() {
  const { t_i18n } = useFormatter();
  return (entry: ToolGateEntry): string => {
    if (entry.hand_gated) {
      /* The API sends the flag, not the sentence: the backend's reason strings
         are English constants and would land untranslated in a French UI. So
         the wording lives here, and a hand-gate outside the download family
         falls back to the general statement rather than inheriting its reason. */
      return t_i18n(
        entry.tool_key.startsWith("download_")
          ? DOWNLOAD_GATE_REASON
          : "Asks first even though it only reads: the product keeps a person in the loop for it.",
      );
    }
    if (entry.regated) {
      return t_i18n("An administrator made it ask first (it is read-only, so it would not by default)");
    }
    /* The one place where the row contradicts itself on its face: a bot's
       read tool is labelled Read and still asks. Reads of everything else
       connected run without asking, and a bot's deliberately do not, so the
       row has to say why rather than restate that it asks. */
    if (entry.source === "channel") {
      return t_i18n(
        "Asks first: a bot's tools read and post in people's conversations, so none runs without asking by default.",
      );
    }
    return t_i18n("Asks first: it is not known to be read-only");
  };
}
