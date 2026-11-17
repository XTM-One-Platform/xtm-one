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
 * Where a video card (`VideoCard`) is being rendered, the deck card's
 * rule (`presentation-card-context`): inside the editor's studio rail,
 * `studioVideoId` is the video on the stage, and a card for THAT video
 * collapses to a one-line receipt - opening it would open the page it is on.
 * Cards for other videos (a copy, a teaser cut from it) keep their full card.
 */
import { createContext } from "react";

export interface VideoCardContextValue {
  studioVideoId?: string | null;
}

export const VideoCardContext = createContext<VideoCardContextValue>({});
