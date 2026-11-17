// SPDX-License-Identifier: Apache-2.0

import { Brain, Key, KeyRound, OctagonX, ShieldCheck, SlidersHorizontal, Vault, type LucideIcon } from "lucide-react";
import { SectionNav } from "@/components/ui/section-nav";
import { useSectionParam } from "@/hooks/useSectionParam";
import { hitlGlyph } from "@/lib/hitl";
import { ApiKeysAdminSection } from "./ApiKeysAdminSection";
import { EmergencyStopTokenSection } from "./EmergencyStopTokenSection";
import { MemoryPurgeSection } from "./MemoryPurgeSection";
import { SecretVaultsSection } from "./SecretVaultsSection";
import { SecurityAuditSection } from "./SecurityAuditSection";
import { SecurityOptionsSection } from "./SecurityOptionsSection";
import { SSOConfigSection } from "./SSOConfigSection";
import { ToolApprovalsSection } from "./ToolApprovalsSection";

// ---------------------------------------------------------------------------
// Security Section – the security audit, the security options, the tool
// approval switches, sign-in, the secret vaults, the platform's API keys, the
// emergency stop API token and the emergency memory purge, one URL-backed
// section each: /settings/security[/<section>]
// ---------------------------------------------------------------------------

type Section = "audit" | "options" | "approvals" | "authentication" | "vaults" | "api-keys" | "emergency" | "memory";

/** @i18n-keys */
const SECTION_LABELS = {
  audit: "Security audit",
  options: "Security options",
  approvals: "Tool approvals",
  authentication: "Authentication",
  vaults: "Secret vaults",
  "api-keys": "API keys",
  emergency: "Emergency stop",
  memory: "Memory purge",
} as const satisfies Record<Section, string>;

const SECTIONS: { key: Section; label: string; icon: LucideIcon }[] = [
  { key: "audit", label: SECTION_LABELS.audit, icon: ShieldCheck },
  { key: "options", label: SECTION_LABELS.options, icon: SlidersHorizontal },
  { key: "approvals", label: SECTION_LABELS.approvals, icon: hitlGlyph("topic") },
  { key: "authentication", label: SECTION_LABELS.authentication, icon: KeyRound },
  { key: "vaults", label: SECTION_LABELS.vaults, icon: Vault },
  { key: "api-keys", label: SECTION_LABELS["api-keys"], icon: Key },
  { key: "emergency", label: SECTION_LABELS.emergency, icon: OctagonX },
  { key: "memory", label: SECTION_LABELS.memory, icon: Brain },
];

export function SecuritySection() {
  const [rawSection, setSection] = useSectionParam<Section>("audit");
  const section: Section = SECTIONS.some(({ key }) => key === rawSection) ? rawSection : "audit";

  return (
    <div className="space-y-6">
      <SectionNav
        sections={SECTIONS}
        active={section}
        onSelect={setSection}
        label="Security sections"
        idPrefix="security"
        testId="security-section-tab"
      />

      {section === "audit" && <SecurityAuditSection />}
      {section === "options" && <SecurityOptionsSection />}
      {section === "approvals" && <ToolApprovalsSection />}
      {section === "authentication" && <SSOConfigSection />}
      {section === "vaults" && <SecretVaultsSection />}
      {section === "api-keys" && <ApiKeysAdminSection />}
      {section === "emergency" && <EmergencyStopTokenSection />}
      {section === "memory" && <MemoryPurgeSection />}
    </div>
  );
}
