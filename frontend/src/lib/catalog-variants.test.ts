import { describe, expect, it } from "vitest";

import type { SetupGuide } from "@/types";

import { setupGuideFor } from "./catalog-variants";

const guide = (title: string): SetupGuide => ({
  title,
  url: null,
  steps: [],
  permissions: [],
  apis_to_enable: null,
  notes: null,
  manifest_example: null,
  open_source: null,
});

describe("setupGuideFor", () => {
  const entry = {
    setup_guide: guide("Both modes"),
    auth_setup_guides: { oauth: guide("OAuth"), api_key: guide("API key") },
  };

  it("shows the guide of the chosen authentication mode", () => {
    expect(setupGuideFor(entry, "oauth")?.title).toBe("OAuth");
    expect(setupGuideFor(entry, "api_key")?.title).toBe("API key");
  });

  it("falls back to the entry's guide without a mode or for a mode with none", () => {
    expect(setupGuideFor(entry, undefined)?.title).toBe("Both modes");
    expect(setupGuideFor(entry, "platform_managed")?.title).toBe("Both modes");
    expect(setupGuideFor({ setup_guide: guide("Only one") }, "oauth")?.title).toBe("Only one");
  });
});
