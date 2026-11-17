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

import { useState, useEffect, useCallback, useRef } from "react";
import type { DocumentContext, OfficeHostApp } from "@/lib/types";
import { getHostApp, getDocumentContext } from "@/lib/office";

/**
 * The Office host, and the document's context as of the last selection
 * change: what the chat panel's synchronous `onBeforeSend` wraps a question
 * in. `refreshContext` reads it again (after a turn wrote to the document).
 */
export function useOffice() {
  const [ready, setReady] = useState(false);
  const [hostApp, setHostApp] = useState<OfficeHostApp>("Unknown");
  const [context, setContext] = useState<DocumentContext | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refreshContext = useCallback(async () => {
    try {
      setContext(await getDocumentContext(getHostApp()));
    } catch {
      /* ignore — host may not support selection reads */
    }
  }, []);

  useEffect(() => {
    if (typeof Office !== "undefined" && Office.onReady) {
      Office.onReady(() => {
        setHostApp(getHostApp());
        setReady(true);
        void refreshContext();

        try {
          Office.context.document.addHandlerAsync(
            Office.EventType.DocumentSelectionChanged,
            () => {
              if (debounceRef.current) clearTimeout(debounceRef.current);
              debounceRef.current = setTimeout(() => {
                void refreshContext();
              }, 300);
            },
          );
        } catch {
          /* event not supported in this host */
        }
      });
    } else {
      setHostApp("Word");
      setReady(true);
    }

    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [refreshContext]);

  return { ready, hostApp, context, refreshContext };
}
