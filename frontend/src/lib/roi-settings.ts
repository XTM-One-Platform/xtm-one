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

import { ROI_MIN_GROUP_SIZE_BOUNDS } from "@/hooks/api/useRoiReportApi";

/** The typed k of the anonymity rule (#12077 L6b): a whole number within the bounds `PUT /roi/settings` accepts, else `null`. */
export function parseMinGroupSize(draft: string): number | null {
  const text = draft.trim();
  if (!/^\d+$/.test(text)) return null;
  const value = Number(text);
  return value >= ROI_MIN_GROUP_SIZE_BOUNDS.min && value <= ROI_MIN_GROUP_SIZE_BOUNDS.max ? value : null;
}
