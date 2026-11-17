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
 * `XtmChatPanel` for a surface inside a page that is prefetched (the studio
 * rails of the Presentation Studio and the Video Studio editor): the panel's
 * module, with the package root and its stylesheet, is its own chunk, never
 * prefetched, loaded once the panel is rendered.
 */
import { Suspense, type ComponentType, type LazyExoticComponent, type ReactNode } from "react";
import type { XtmChatPanel as Panel, XtmChatPanelProps } from "@/components/chat-panel/XtmChatPanel";
import { lazyWithRetry } from "@/lib/lazyWithRetry";

const LazyPanel = lazyWithRetry(
  () =>
    import("@/components/chat-panel/XtmChatPanel").then((m) => ({
      default: m.XtmChatPanel as ComponentType<unknown>,
    })),
  { prefetch: false },
) as unknown as LazyExoticComponent<typeof Panel>;

export function LazyXtmChatPanel({ fallback = null, ...props }: XtmChatPanelProps & { fallback?: ReactNode }) {
  return (
    <Suspense fallback={fallback}>
      <LazyPanel {...props} />
    </Suspense>
  );
}
