// A refresh of the studio (a save of the assistant, a share) re-mints no media
// token: a new token is a new URL, which reloads every picture and
// player showing it - and asked for a poster not made yet, answers a 404.

import { describe, expect, it } from "vitest";

import { testQueryClient } from "@/testing/providers";

import { invalidateVideoStudio } from "./useVideoStudioApi";

describe("invalidateVideoStudio", () => {
  it("refreshes every studio query but the media tokens", async () => {
    const client = testQueryClient();
    const project = ["video-studio", "projects", "v-1"];
    const poster = ["video-studio", "media", "clip", "c-1", "poster", "file-1"];
    client.setQueryData(project, { id: "v-1" });
    client.setQueryData(poster, { url: "/api/v1/video-studio/media/token", expires_at: 0, content_type: "image/jpeg" });
    await invalidateVideoStudio(client);
    expect(client.getQueryState(project)?.isInvalidated).toBe(true);
    expect(client.getQueryState(poster)?.isInvalidated).toBe(false);
  });
});
