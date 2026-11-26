import { describe, expect, it } from "vitest";

import type { SetupGuide } from "@/types";

import { credentialFieldsFor, setupGuideFor } from "./catalog-variants";

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

describe("credentialFieldsFor", () => {
  const github = {
    fields: ["client_id", "client_secret"],
    fields_by_auth_type: { oauth: ["client_id", "client_secret"], api_key: ["api_key"] },
  };

  it("asks for the token alone when API key is chosen, not the OAuth client", () => {
    expect(credentialFieldsFor(github, "api_key")).toEqual(["api_key"]);
    expect(credentialFieldsFor(github, "oauth")).toEqual(["client_id", "client_secret"]);
  });

  it("falls back to the default mode's fields without a mode or a per-mode map", () => {
    expect(credentialFieldsFor(github, undefined)).toEqual(["client_id", "client_secret"]);
    expect(credentialFieldsFor({ fields: ["api_key"] }, "oauth")).toEqual(["api_key"]);
  });
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
