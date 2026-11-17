import { describe, it, expect } from "vitest";
import { platformEmblemUrl, platformLogoUrl } from "./usePlatformConfig";

/**
 * Which logo each theme draws. The backend always fills `platform_logo` - with
 * the bundled WHITE wordmark when no operator set one - so the light theme drew
 * an invisible product name until the light variant came from the config too.
 */

const bundled = {
  platform_mode: "xtm_one",
  platform_logo: "/xtp-one.svg",
  platform_logo_hash: null,
  platform_logo_light: "/xtp-one-light.svg",
};

describe("platformLogoUrl", () => {
  it.each([
    ["the bundled logo, dark", bundled, "dark", "/xtp-one.svg"],
    ["the bundled logo, light", bundled, "light", "/xtp-one-light.svg"],
    ["Copilot's bundled logo, light", { ...bundled, platform_mode: "copilot", platform_logo: "/filigran-copilot.svg", platform_logo_light: "/filigran-copilot-light.svg" }, "light", "/filigran-copilot-light.svg"],
    ["an uploaded logo, dark", { ...bundled, platform_logo: "/api/v1/platform/branding/asset/logo", platform_logo_hash: "abc", platform_logo_light: null }, "dark", "/api/v1/platform/branding/asset/logo?v=abc"],
    ["an uploaded logo, light", { ...bundled, platform_logo: "/api/v1/platform/branding/asset/logo", platform_logo_hash: "abc", platform_logo_light: null }, "light", "/api/v1/platform/branding/asset/logo?v=abc"],
    ["a logo URL, light", { ...bundled, platform_logo: "https://cdn.example.com/logo.png", platform_logo_light: null }, "light", "https://cdn.example.com/logo.png"],
    ["an older backend with no light field, light", { ...bundled, platform_logo_light: undefined }, "light", "/xtp-one.svg"],
    ["a config with no logo at all, light", { ...bundled, platform_logo: null, platform_logo_light: undefined }, "light", "/xtp-one-light.svg"],
  ] as const)("draws %s", (_label, config, mode, expected) => {
    expect(platformLogoUrl(config, mode)).toBe(expected);
  });
});

/**
 * Which mark the collapsed rail draws. A wordmark squeezed into its 28px square
 * shrank to an unreadable strip in Copilot mode, so the rail shows the emblem
 * alone, white on the dark theme and dark on the light one.
 */
const copilotEmblem = {
  platform_emblem: "/filigran-white.svg",
  platform_emblem_hash: null,
  platform_emblem_light: "/filigran-black.svg",
};

describe("platformEmblemUrl", () => {
  it.each([
    ["Copilot's bundled emblem, dark", copilotEmblem, "dark", "/filigran-white.svg"],
    ["Copilot's bundled emblem, light", copilotEmblem, "light", "/filigran-black.svg"],
    ["an operator's favicon, light", { platform_emblem: "/api/v1/platform/branding/asset/favicon", platform_emblem_hash: "f1", platform_emblem_light: null }, "light", "/api/v1/platform/branding/asset/favicon?v=f1"],
    ["nothing beside an operator's logo with no favicon", { platform_emblem: null, platform_emblem_hash: null, platform_emblem_light: null }, "dark", null],
    ["nothing for an older backend with no emblem field", {}, "light", null],
    ["nothing while the config loads", null, "dark", null],
  ] as const)("draws %s", (_label, config, mode, expected) => {
    expect(platformEmblemUrl(config, mode)).toBe(expected);
  });
});
