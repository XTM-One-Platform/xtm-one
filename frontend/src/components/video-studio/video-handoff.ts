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
 * What the Video Studio hands to the assistant, the way
 * `presentation/assistant-handoff.ts` does for decks. A new video's first
 * message is `video-format.newVideoRequest`; what is primed here is a
 * sentence the user finishes in the composer.
 */

/** The built-in tool an agent must carry to make videos (`useToolAgents`). */
export const VIDEO_TOOL_ID = "video_studio";

export function recordDemoPrompt(): string {
  return "Record a demo of ";
}
