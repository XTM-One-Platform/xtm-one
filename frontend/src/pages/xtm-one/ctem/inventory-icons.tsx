// SPDX-License-Identifier: Apache-2.0

/**
 * The marks of the typed columns of CTEM > Inventory (#1367, Sam 23 Nov 2026, point 8): a glyph per asset category
 * and subcategory, the vendor mark of an operating system, the logo of a source. Only the app's existing marks: the
 * design system's icon set, the integration logos (PROVIDER_LOGOS) and the OS brand marks the runner pages already
 * ship (RUNNER_PLATFORM_CONFIG); no hand-drawn icon. A value without a mark of its own takes its category's glyph,
 * never an empty slot, so a column stays aligned.
 */
import type { ComponentProps } from "react";
import { Icon } from "@filigran/design-system";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { PROVIDER_LOGOS, RUNNER_PLATFORM_CONFIG } from "@/lib/constants";
import { isPlatformSource } from "@/lib/organization-provenance";
import { cn } from "@/lib/utils";

type IconName = ComponentProps<typeof Icon>["name"];

/** The glyph of each OpenAEV asset category (the backend's ASSET_TAXONOMY). */
export const CATEGORY_ICONS: Record<string, IconName> = {
  host: "server",
  container_workload: "boxes",
  cloud_resource: "cloud",
  web_application: "earth",
  network_device: "router",
  mobile_device: "monitor-smartphone",
  iot_ot_device: "satellite-dish",
  identity: "user",
  saas_application: "layout-grid",
  ai_target: "brain",
  security_platform: "shield",
  source_code: "code-xml",
  generic_asset: "box",
};

/** The subcategories whose glyph says more than their category's. */
export const SUBCATEGORY_ICONS: Record<string, IconName> = {
  workstation: "monitor",
  laptop: "laptop-minimal",
  thin_client: "monitor",
  virtual_machine: "layers",
  hypervisor: "layers-2",
  container: "box",
  kubernetes_cluster: "network",
  storage: "database",
  database: "database",
  networking: "network",
  iam_principal: "key-round",
  secrets_key_mgmt: "key-round",
  website: "earth",
  web_api: "braces",
  graphql_api: "braces",
  firewall: "brick-wall",
  wireless_ap: "satellite-dish",
  san_nas: "database",
  smartphone: "monitor-smartphone",
  tablet: "monitor-smartphone",
  user_account: "user",
  service_account: "user-lock",
  group: "users",
  role: "key-square",
  shared_mailbox: "mail",
  non_human_identity: "bot",
  llm_model: "brain",
  ai_agent: "bot",
  mcp_server: "puzzle",
  rag_pipeline: "workflow",
  edr: "shield-check",
  xdr: "shield-check",
  siem: "radar",
  vulnerability_scanner: "radar",
  code_repository: "folder",
  ci_cd_pipeline: "workflow",
  package_registry: "package-check",
  software_package: "package-check",
};

/** The glyph of an asset's category: its subcategory's when it has one, else the category's, else a box. */
export function categoryIcon(category?: string | null, subcategory?: string | null): IconName {
  return SUBCATEGORY_ICONS[(subcategory ?? "").toLowerCase()] ?? CATEGORY_ICONS[(category ?? "").toLowerCase()] ?? "box";
}

/** An OS family as the stores write it (`windows`, `macos`, `ios`, `ipados`, `android`, `linux`...), normalised. */
function osKey(family?: string | null): string {
  const value = (family ?? "").trim().toLowerCase().replace(/\s+/g, "");
  if (value.startsWith("win")) return "windows";
  if (value === "macos" || value === "osx" || value === "macosx" || value === "darwin") return "macos";
  if (value === "ios" || value === "ipados") return "apple";
  if (value.includes("linux") || ["ubuntu", "debian", "redhat", "rhel", "centos", "fedora", "suse"].includes(value)) return "linux";
  return value;
}

/** The vendor mark of an OS family: the Windows flag, the Apple mark (macOS, iOS, iPadOS), Tux; else null. */
export function osLogo(family?: string | null): string | null {
  const key = osKey(family);
  if (key === "apple") return RUNNER_PLATFORM_CONFIG.macos?.logo ?? null;
  return RUNNER_PLATFORM_CONFIG[key]?.logo ?? null;
}

/** The OS families with no brand mark the app ships: a glyph instead (Android, ChromeOS...). */
function osGlyph(family?: string | null): IconName {
  const key = osKey(family);
  return key === "android" ? "monitor-smartphone" : "monitor";
}

/** One OS family as its vendor mark (the Apple mark for macOS, iOS and iPadOS), decorative: the name sits beside it. */
export function OsMark({ family, className }: { family?: string | null; className?: string }) {
  if (!family) return null;
  const logo = osLogo(family);
  return logo ? (
    <ProviderLogo provider={osKey(family)} logo={logo} decorative className={cn("h-4 w-4 shrink-0", className)} />
  ) : (
    <Icon name={osGlyph(family)} size={16} className={cn("shrink-0 text-default-secondary", className)} aria-hidden />
  );
}

/** An asset category as its glyph, decorative: the category's words sit beside it. */
export function CategoryMark({ category, subcategory, className }: { category?: string | null; subcategory?: string | null; className?: string }) {
  return <Icon name={categoryIcon(category, subcategory)} size={16} className={cn("shrink-0 text-default-secondary", className)} aria-hidden />;
}

/** The glyphs of the sources without a brand mark (a person, a CSV file, the API, an agent, an SBOM). */
const HAND_GLYPHS: Record<string, IconName> = { manual: "pencil", csv: "file-text", api: "code-xml", sbom: "file-text" };

/** A source as its logo (a platform) or its glyph (a person, a file, the API, an agent), decorative. */
export function SourceLogo({ source, className }: { source: string; className?: string }) {
  const key = source.trim().toLowerCase();
  if (key.startsWith("agent:")) return <Icon name="bot" size={16} className={cn("shrink-0 text-default-secondary", className)} aria-hidden />;
  if (PROVIDER_LOGOS[key] || isPlatformSource(key)) return <ProviderLogo provider={key} decorative className={cn("h-4 w-4 shrink-0", className)} />;
  return <Icon name={HAND_GLYPHS[key] ?? "pencil"} size={16} className={cn("shrink-0 text-default-secondary", className)} aria-hidden />;
}
