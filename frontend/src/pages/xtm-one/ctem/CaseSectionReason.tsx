// SPDX-License-Identifier: Apache-2.0

/**
 * The not-started state of a case page section (#1266): what the section will
 * hold, the product it is written from with that product's logo and connection
 * state, and "Connect" where the product is missing. Who works the case is
 * said once, at the top of the page (`CaseWorkCard`), never repeated here. The
 * page provides the connection states once (`CaseLoopProvider`); a section
 * rendered without it shows its sentence alone.
 */
import { createContext, useContext, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import type { IconName } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { withBasePath } from "@/lib/base-path";
import { useFormatter } from "@/i18n";
import type { CaseSource, CaseSources } from "./case-workers";
import { CaseIcon } from "./case-icons";

interface CaseLoopState {
  sources: CaseSources;
}

const UNKNOWN: CaseLoopState = { sources: { opencrq: null, opencti: null, openaev: null } };
const CaseLoopContext = createContext<CaseLoopState>(UNKNOWN);

export function CaseLoopProvider({ value, children }: { value: CaseLoopState; children: ReactNode }) {
  return <CaseLoopContext.Provider value={value}>{children}</CaseLoopContext.Provider>;
}

/** The products a section is written from. */
const SOURCE_PRODUCT: Record<CaseSource, { name: string; logo: string }> = {
  opencrq: { name: "OpenCRQ", logo: "/logos/opencrq.svg" },
  opencti: { name: "OpenCTI", logo: "/logos/opencti.svg" },
  openaev: { name: "OpenAEV", logo: "/logos/openaev.svg" },
};

export function CaseSectionReason({
  own,
  source,
  icon,
  testId,
}: {
  /** What the section will hold, already translated. */
  own: string;
  /**
   * The section's own icon (`CASE_SECTION_ICON`), drawn before the sentence so every
   * empty state of the case page carries one (#1679, point 5); the product's logo
   * then marks the line that names the product.
   */
  icon?: IconName;
  /** The product the section is written from; none for a section XTM One writes alone. */
  source?: CaseSource;
  testId: string;
}) {
  const { t_i18n } = useFormatter();
  const { sources } = useContext(CaseLoopContext);
  const product = source ? SOURCE_PRODUCT[source] : null;
  const connected = source ? sources[source] : null;
  const missing = connected === false;

  return (
    <div className="flex flex-wrap items-center justify-between gap-3" data-testid={testId} data-reason={missing ? "source_missing" : "own"}>
      <span className="flex min-w-0 items-center gap-3">
        {icon ? (
          <CaseIcon name={icon} className="text-default-secondary" />
        ) : (
          product && <img src={withBasePath(product.logo)} alt="" className="h-6 w-6 shrink-0" />
        )}
        <span className="flex min-w-0 flex-col">
          <span className="content-base text-default-secondary">{own}</span>
          {product && connected !== null && (
            <span
              className={
                missing
                  ? "flex items-center gap-1.5 content-compact text-feedback-warning-primary"
                  : "flex items-center gap-1.5 content-compact text-default-secondary"
              }
            >
              {icon && <img src={withBasePath(product.logo)} alt="" className="h-4 w-4 shrink-0" />}
              {missing
                ? t_i18n("{product} is not connected", { product: product.name })
                : t_i18n("Written from {product}, connected", { product: product.name })}
            </span>
          )}
        </span>
      </span>
      {product && missing && <ConnectButton product={product.name} />}
    </div>
  );
}

/** Mounted only when a product is missing, so a section read outside the router (a test) still renders. */
function ConnectButton({ product }: { product: string }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  return (
    <Button variant="outline" size="sm" onClick={() => navigate("/integrations")}>
      {t_i18n("Connect {product}", { product })}
    </Button>
  );
}
