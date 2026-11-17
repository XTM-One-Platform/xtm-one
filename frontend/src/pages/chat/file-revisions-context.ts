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

import { createContext, useContext } from "react";
import type { FileRevision } from "@/pages/chat/file-revisions";

/** The revision of every file a transcript shows (``conversationRevisions``); none outside one. */
export const FileRevisionsContext = createContext<ReadonlyMap<string, FileRevision> | null>(null);

export function useFileRevision(fileId: string | null | undefined): FileRevision | null {
  const revisions = useContext(FileRevisionsContext);
  if (!revisions || !fileId) return null;
  return revisions.get(fileId.toLowerCase()) ?? null;
}
