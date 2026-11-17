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
 * The messages a Deep Investigation step's `detail_code` renders.
 *
 * Kept out of the block so the rule is testable: a message is used only when
 * the stored parameters fill every placeholder in it. A run stored before a
 * code gained a parameter (a list's `ips` and `pivoted`) keeps the text it
 * was stored with, rather than showing `{ips}` to the reader.
 */
import type { InvestigationDetailCode } from "../../types/index.ts";

export const DETAIL_MESSAGES: Partial<Record<InvestigationDetailCode, string>> = {
  "run.all_sources_queried": "All planned sources were queried",
  "run.budget_spent": "Iteration budget spent ({budget})",
  "run.no_covering_source": "No source covers a {subject_type} subject",
  "run.cancelled": "Cancelled",
  "run.interrupted": "Interrupted before completion",
  "source.timed_out": "No response in {seconds}s",
  "source.http_status": "HTTP {status}",
  "source.truncated": "Content truncated at {max_chars} characters",
  "source.thin_response": "The page returned only {chars} characters of text",
  "source.kb_capped": "Showing the top {top_k} passages; the corpus holds more",
  "source.kb_unavailable": "None of these knowledge bases is available to you",
  "source.kb_below_floor":
    "None of the {examined} best-ranked passages reached the relevance floor ({floor})",
  "source.mcp_bad_server_id": "{server_id} is not a valid server id",
  "source.mcp_server_missing": "Server {server_id} no longer exists",
  "source.mcp_server_disabled": "Server {server} is disabled",
  "source.mcp_tool_missing": "{server} no longer offers the tool {tool}",
  "source.mcp_argument_ambiguous":
    "{tool} does not take exactly one text argument, so the subject cannot be placed",
  "source.fingerprint_matched": "{matched} of {tested} signals present",
  "source.fingerprint_capped":
    "Only the first {max_bytes} bytes of the page were tested",
  "source.vt_unavailable":
    "No connected VirusTotal integration is available to you",
  "source.passive_dns_filtered":
    "{kept} of {fetched} resolved sites match the keywords",
  "source.passive_dns_filtered_more":
    "{kept} of {fetched} resolved sites match the keywords; VirusTotal holds more that were not read",
  "source.passive_dns_more":
    "Showing up to the {limit} most recent resolutions; VirusTotal holds more",
  "source.passive_dns_seeds":
    "{resolved} of {seeds} seeds resolved to {ips} IPs, {shared} of them shared by two seeds or more; pivoting on {pivoted}",
  "source.free_http_refused": "This address could not be read",
  "source.free_http_capped": "Only the first part of the response was read",
  "source.opencti_unavailable":
    "No OpenCTI registered with this platform is available to you",
  "source.opencti_known": "{known} of {checked} already in OpenCTI",
  "source.opencti_none_known": "None of the {checked} checked is in OpenCTI yet",
};

/** The message for ``code``, or null when there is none or the stored
 *  ``params`` leave one of its placeholders empty. */
export function detailMessage(
  code: InvestigationDetailCode | null,
  params: Record<string, string | number> | null,
): string | null {
  const message = code ? DETAIL_MESSAGES[code] : undefined;
  if (!message) return null;
  const names = Array.from(message.matchAll(/\{(\w+)\}/g), (m) => m[1]);
  return names.every((name) => params != null && params[name] != null) ? message : null;
}
