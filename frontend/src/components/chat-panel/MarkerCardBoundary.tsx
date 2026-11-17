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

import type React from "react";
import { ErrorBoundary } from "@/components/ui/error-boundary";
import { useFormatter } from "@/i18n";

/**
 * One marker card of a transcript: a card that fails says so in its
 * own place, and the conversation around it - the chat page, a studio rail -
 * stays on screen.
 */
export function MarkerCardBoundary({ children }: { children: React.ReactNode }) {
  const { t_i18n } = useFormatter();
  return (
    <ErrorBoundary
      fallback={
        <div className="my-2 rounded-sm border border-dashed border-elevation-subtle px-3 py-2 content-compact text-default-secondary">
          {t_i18n("This item could not be loaded.")}
        </div>
      }
    >
      {children}
    </ErrorBoundary>
  );
}
