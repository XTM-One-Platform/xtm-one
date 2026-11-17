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

import { createContext, type ReactNode } from "react";

/**
 * How a page draws a marker kept inside a table row, a list item or a quote
 * (`inlineContainerMarkers` turns it into a `#inline-marker/<KIND>/<args>`
 * link, #11697): its compact chip. `ChatMarkdown` reads it; without a provider
 * such a link draws nothing, never a raw link.
 */
export const InlineMarkerContext = createContext<((kind: string, args: string) => ReactNode) | null>(null);
