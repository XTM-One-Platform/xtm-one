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

import type { ReactNode } from "react";

/**
 * The /chat page's frame: the content area, edge to edge, under the header.
 * Empty while the route chunk loads and while the page resolves its agent, so
 * the first skeleton on screen is the panel's own, in its reading column
 * (`historyPending`), never a page skeleton of another shape before it.
 */
export function ChatFrame({ children }: { children?: ReactNode }) {
  return (
    <div data-testid="chat-frame" className="flex h-[calc(100dvh-var(--fds-header-height,68px))] -m-6">
      {children}
    </div>
  );
}
