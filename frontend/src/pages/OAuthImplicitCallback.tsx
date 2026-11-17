// SPDX-License-Identifier: Apache-2.0

/**
 * Where an OAuth2 implicit sign-in lands. The provider returns the access
 * token in the URL fragment, which never reaches the server: the backend's
 * OAuth callback redirects here (a redirect keeps the fragment), and this
 * page hands the fragment back with its state, then opens the integration.
 */
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { completeImplicitAuthorization } from "@/hooks/api/useIntegrationApi";
import { useFormatter } from "@/i18n";
import { Spinner } from "@filigran/design-system";

const FRAGMENT_KEYS = ["state", "access_token", "token_type", "expires_in", "scope", "error", "error_description"];

export default function OAuthImplicitCallback() {
  const navigate = useNavigate();
  const { t_i18n } = useFormatter();
  const [error, setError] = useState<string | null>(null);
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    // The token must not stay in the address bar or the history.
    window.history.replaceState(null, "", window.location.pathname);
    const fragment = Object.fromEntries([...params.entries()].filter(([key]) => FRAGMENT_KEYS.includes(key)));
    if (!fragment.state) {
      setError(t_i18n("No sign-in answer was found on this page. Start again with Authorize on the integration."));
      return;
    }
    completeImplicitAuthorization(fragment).then(
      // Back to the integration's own page, where the sign-in started.
      (done) => navigate(`/integrations/${encodeURIComponent(done.integration_id)}?connected=${encodeURIComponent(done.provider)}`, { replace: true }),
      (err: unknown) => setError(err instanceof Error ? err.message : t_i18n("The sign-in could not be completed.")),
    );
  }, [navigate, t_i18n]);

  return (
    <div className="mx-auto max-w-lg py-16">
      <Card>
        <CardContent className="space-y-4 py-8 text-center">
          {error ? (
            <>
              <XCircle className="mx-auto h-8 w-8 text-feedback-error-primary" />
              <p className="content-base">{error}</p>
              <Button variant="secondary" onClick={() => navigate("/integrations", { replace: true })}>
                {t_i18n("Back to Integrations")}
              </Button>
            </>
          ) : (
            <>
              <Spinner size="xl" tone="inherit" className="mx-auto text-accent" />
              <p className="content-base text-default-secondary">{t_i18n("Completing the sign-in…")}</p>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
