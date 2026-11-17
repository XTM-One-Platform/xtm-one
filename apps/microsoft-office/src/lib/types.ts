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

export type OfficeHostApp = "Word" | "Excel" | "PowerPoint" | "Outlook" | "Unknown";
export type Theme = "dark" | "light";

// Three-tier smart context

export interface SelectionInfo {
  text: string;
  location: string;
}

export interface DocumentMeta {
  wordCount?: number;
  pageCount?: number;
  sheetNames?: string[];
  slideCount?: number;
  headings?: string[];
  tableCount?: number;
  usedRangeSize?: string;
}

export interface DocumentContext {
  hostApp: OfficeHostApp;
  selection: SelectionInfo;
  surrounding: string;
  documentMeta: DocumentMeta;
}

/** A run of a Word document's paragraphs, reviewed as one section. */
export interface DocumentChunk {
  index: number;
  text: string;
  paragraphStart: number;
  paragraphEnd: number;
}

// Auth

export interface AuthResponse {
  /** Absent when `mfa_required` is true — no session is issued until the
   *  second factor is verified. */
  access_token?: string | null;
  token_type: string;
  /** True when the account has TOTP two-factor enabled. The add-in cannot
   *  complete that flow yet, so `login()` reports it rather than storing an
   *  undefined token. */
  mfa_required?: boolean;
  mfa_token?: string | null;
}

/** The account `/auth/me` answers: its display name is `full_name`. */
export interface User {
  id: string;
  email: string;
  full_name?: string | null;
  /** Read as a fallback only: older payloads of the add-in's tests used it. */
  name?: string | null;
  role: string;
}

// Platform config (public — same shape as the main frontend's
// PlatformConfig, but only the fields the Office add-in needs).
export interface PlatformConfig {
  platform_mode: string;
  platform_name: string;
  platform_logo_url: string | null;
  platform_version: string;
}

// Action protocol

export interface ActionPayload {
  action:
    | "replace_selection"
    | "insert_at_end"
    | "add_comment"
    | "insert_paragraph"
    | "insert_table"
    | "insert_html";
  content: string;
}
