// SPDX-License-Identifier: Apache-2.0

/**
 * In-dashboard "connect a platform" dialog.
 *
 * OpenCTI / OpenAEV register themselves *into* XTM One - so this is not an
 * outbound connect/install. The dialog shows the inbound-registration config
 * (XTM One URL + registration token) to paste into the platform, plus a
 * secondary "explore the product" link for users who do not have it yet. The
 * registration token is only revealed to admins (it is a shared secret), and
 * only when it is one of the operator's own (see RegistrationTokenPanel).
 */
import { platformOrigin } from "@/lib/base-path";
import { ExternalLink, ArrowDownToLine } from "lucide-react";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from "@/components/ui/dialog";
import { CTEM_PRODUCT_INFO } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import { useAuth } from "@/hooks/useAuth";
import { useAuthConfig } from "@/hooks/api/useAuthApi";
import { useRegistrationToken } from "@/hooks/api/usePlatformApi";
import { usePlatformName } from "@/hooks/usePlatformConfig";
import { PlatformConnectInstructions } from "./PlatformConnectInstructions";
import { RegistrationTokenPanel } from "./RegistrationTokenPanel";
import { connectSnippetToken } from "./registration-token";

type Product = "opencti" | "openaev" | "opencrq";

export function ConnectPlatformDialog({
  product,
  onOpenChange,
}: {
  product: Product | null;
  onOpenChange: (open: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  // Token is a shared secret; only admins may fetch it (endpoint is admin-only).
  const { data: tokenData, isLoading: tokenLoading } = useRegistrationToken(isAdmin);
  const { data: authConfig } = useAuthConfig();
  const platformName = usePlatformName("XTM One");

  const platformUrl = authConfig?.base_url ?? platformOrigin();
  // The token is a shared secret - only embed it for admins; everyone else
  // sees a placeholder and a pointer to their administrator.
  const token = connectSnippetToken(isAdmin ? tokenData : undefined);
  const info = product ? CTEM_PRODUCT_INFO[product] : undefined;

  return (
    <Dialog open={!!product} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
        {product && info && (
          <>
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2.5">
                <img src={info.logo} alt="" className="h-6 w-6" />
                {t_i18n("Connect {product} to {platform}", { product: info.name, platform: platformName })}
              </DialogTitle>
              <DialogDescription>
                {t_i18n(
                  "{product} registers itself with {platform}. Add the {platform} URL and registration token to your {product} configuration - it appears here automatically once it checks in. No manual integration to create.",
                  { product: info.name, platform: platformName },
                )}
              </DialogDescription>
            </DialogHeader>

            <PlatformConnectInstructions product={product} platformUrl={platformUrl} token={token} />

            {isAdmin ? (
              <RegistrationTokenPanel info={tokenData} isLoading={tokenLoading} />
            ) : (
              <p className="text-[11px] text-default-secondary flex items-center gap-1.5">
                <ArrowDownToLine className="h-3.5 w-3.5 shrink-0" />
                {t_i18n("Ask your administrator for the registration token (Settings).")}
              </p>
            )}

            <div className="flex items-center justify-between border-t border-elevation-subtle-soft pt-3">
              <span className="text-[11px] text-default-secondary">{t_i18n("Don't have {product} yet?", { product: info.name })}</span>
              <a
                href={info.productUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 content-compact text-accent hover:underline"
              >
                {t_i18n("Explore {product}", { product: info.name })} <ExternalLink className="h-3 w-3" />
              </a>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
