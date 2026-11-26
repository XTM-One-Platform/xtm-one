// SPDX-License-Identifier: Apache-2.0

/**
 * Register this platform with the XTM Hub (Settings > Platform).
 *
 * The Hub page `/redirect/register-xtmone` (the Hub's own login and
 * organization choice) is opened in a popup - never `noopener`, or it could
 * not post back - and posts the platform token to this window. Only a message
 * from that popup, from the Hub's exact origin and with the `state` this page
 * generated is read (`readXtmHubRegistrationMessage`); the token then goes to
 * the backend, which stores it once the Hub confirms it. A token shown by the
 * Hub when it cannot post back can be pasted instead.
 *
 * With a valid trial licence, the platform is the XTM Hub trial's own (the
 * deployer registered it automatically): like OpenCTI, it cannot be
 * disconnected here (no Unregister, no Forget locally) and registers again
 * only with the default XTM Hub, whose URL is shown read-only.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ExternalLink, RefreshCw } from "lucide-react";
import { SettingsCard, SettingsField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { platformOrigin } from "@/lib/base-path";
import { randomId } from "@/lib/random-id";
import { PROVIDER_LOGOS } from "@/lib/constants";
import { formatDateTime, safeExternalUrl } from "@/lib/utils";
import {
  XTM_HUB_PROVIDER,
  isXtmHubPlatformToken,
  readXtmHubRegistrationMessage,
  xtmHubOrigin,
  xtmHubRegistrationPageUrl,
  xtmHubRegistrationUrl,
} from "@/lib/xtm-hub";
import {
  useCheckXtmHubRegistration,
  useForgetXtmHubRegistration,
  useRegisterXtmHub,
  useXtmHubRegistration,
} from "@/hooks/api/useXtmHubApi";
import { useEdition, usePlatformName, useXTMLicense } from "@/hooks/usePlatformConfig";
import type { XtmHubRegistrationStatus } from "@/types";
import { Spinner } from "@filigran/design-system";

const POPUP_NAME = "xtm-hub-registration";
const POPUP_FEATURES = "popup,width=980,height=820";
const POPUP_POLL_MS = 500;

type Waiting = { action: "register" | "unregister"; popup: Window; state: string; hubOrigin: string; hubUrl: string };

const STATUS_BADGE_VARIANTS: Record<XtmHubRegistrationStatus, "success" | "destructive" | "warning" | "outline"> = {
  registered: "success",
  lost_connectivity: "destructive",
  needs_reregistration: "warning",
  unregistered: "outline",
};

function newState(): string {
  return randomId();
}

export function XtmHubRegistrationCard() {
  const { t_i18n } = useFormatter();
  const { toast: showToast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const edition = useEdition();
  const xtmLicense = useXTMLicense();
  const trialLicense = xtmLicense?.valid === true && xtmLicense.license_type === "trial";
  const platformName = usePlatformName("XTM One");
  const { data: registration } = useXtmHubRegistration();
  const register = useRegisterXtmHub();
  const check = useCheckXtmHubRegistration();
  const forget = useForgetXtmHubRegistration();
  // The effect below depends on these, not on the mutation results: those
  // are new objects on every render, mutate is stable.
  const { mutate: registerMutate } = register;
  const { mutate: forgetMutate } = forget;

  const [hubUrlInput, setHubUrlInput] = useState("");
  const [pastedToken, setPastedToken] = useState("");
  const [waiting, setWaiting] = useState<Waiting | null>(null);
  const waitingRef = useRef<Waiting | null>(null);
  waitingRef.current = waiting;

  const status = registration?.status ?? "unregistered";
  // The stored Hub, else the one typed (never on a trial), else the default
  // one - validated, never replaced by the default Hub on a typo (that is the
  // display link's fallback, not this one's).
  const typedHubUrl = trialLicense ? "" : hubUrlInput;
  const targetHubUrl = registration
    ? (registration.hub_url ?? xtmHubRegistrationUrl(typedHubUrl || registration.default_hub_url))
    : null;
  const hubUrlInvalid = !!registration && targetHubUrl === null;
  const hubUrl = targetHubUrl ?? "";
  const badgeLabels: Record<XtmHubRegistrationStatus, string> = {
    registered: t_i18n("Registered"),
    lost_connectivity: t_i18n("Connectivity lost"),
    needs_reregistration: t_i18n("Register again"),
    unregistered: t_i18n("Not registered"),
  };

  const fail = useCallback(
    (description: string) => showToast({ title: t_i18n("XTM Hub registration failed"), description, variant: "error" }),
    [showToast, t_i18n],
  );

  const saveToken = useCallback(
    (token: string, url: string) =>
      registerMutate(
        { hub_url: url, token },
        {
          onSuccess: () => {
            setPastedToken("");
            showToast({ title: t_i18n("This platform is registered with the XTM Hub"), variant: "success" });
          },
          onError: (error) => fail(error.message),
        },
      ),
    [registerMutate, showToast, t_i18n, fail],
  );

  // One listener while a popup is open: the first message that reads as the
  // Hub's answer ends the wait.
  useEffect(() => {
    if (!waiting) return;
    const onMessage = (event: MessageEvent) => {
      const current = waitingRef.current;
      if (!current) return;
      const message = readXtmHubRegistrationMessage(
        { data: event.data, origin: event.origin, source: event.source },
        { hubOrigin: current.hubOrigin, popup: current.popup, state: current.state },
      );
      if (!message) return;
      setWaiting(null);
      current.popup.close();
      if (message.action === "register" && current.action === "register") {
        saveToken(message.token, current.hubUrl);
      } else if (message.action === "unregister" && current.action === "unregister") {
        forgetMutate(undefined, {
          onSuccess: () => showToast({ title: t_i18n("This platform is no longer registered with the XTM Hub"), variant: "success" }),
          onError: (error) => fail(error.message),
        });
      }
    };
    const timer = window.setInterval(() => {
      if (waitingRef.current?.popup.closed) setWaiting(null);
    }, POPUP_POLL_MS);
    window.addEventListener("message", onMessage);
    return () => {
      window.removeEventListener("message", onMessage);
      window.clearInterval(timer);
    };
  }, [waiting, saveToken, forgetMutate, showToast, t_i18n, fail]);

  const openHubPage = (action: "register" | "unregister") => {
    if (!registration || hubUrlInvalid) return;
    const state = newState();
    const url = xtmHubRegistrationPageUrl(action, {
      hubUrl,
      platformId: registration.register_params.platform_id,
      platformUrl: platformOrigin(),
      platformTitle: platformName,
      platformContract: edition === "enterprise" ? "EE" : "CE",
      platformVersion: registration.register_params.platform_version,
      state,
    });
    // Not `noopener`: the Hub page posts its answer to this window.
    const popup = window.open(url, POPUP_NAME, POPUP_FEATURES);
    if (!popup) {
      fail(t_i18n("The browser blocked the XTM Hub window. Allow pop-ups for this site and try again."));
      return;
    }
    setWaiting({ action, popup, state, hubOrigin: xtmHubOrigin(hubUrl), hubUrl });
  };

  const forgetLocally = async () => {
    const ok = await confirm({
      title: t_i18n("Forget the XTM Hub registration?"),
      description: t_i18n(
        "This platform stops checking its registration and forgets its token. The registration itself stays on the XTM Hub: remove it there with Unregister when the Hub is reachable.",
      ),
      confirmLabel: t_i18n("Forget"),
      variant: "warning",
    });
    if (!ok) return;
    forget.mutate(undefined, { onError: (error) => fail(error.message) });
  };

  const busy = register.isPending || check.isPending || forget.isPending;
  const hubLink = safeExternalUrl(hubUrl);

  return (
    <SettingsCard
      id="xtm-hub"
      title={
        <>
          <img src={PROVIDER_LOGOS[XTM_HUB_PROVIDER]} alt="" className="h-4 w-4 shrink-0" />
          {t_i18n("XTM Hub")}
        </>
      }
      description={t_i18n(
        "Register this platform with the XTM Hub, like OpenCTI and OpenAEV: it then appears among your organization's platforms on the XTM Hub, which checks the registration every hour.",
      )}
      action={<Badge variant={STATUS_BADGE_VARIANTS[status]}>{badgeLabels[status]}</Badge>}
    >
      {ConfirmEl}
      <div className="space-y-4">
        {status === "unregistered" ? (
          <SettingsField
            id="xtm-hub-url"
            label={t_i18n("XTM Hub URL")}
            helper={trialLicense ? t_i18n("A trial platform registers with the default XTM Hub.") : t_i18n("Leave empty to use the default XTM Hub.")}
            info={t_i18n("The address of the XTM Hub this platform registers with, for example https://hub.filigran.io. An https URL; http only for an XTM Hub on this machine.")}
            error={hubUrlInvalid ? t_i18n("Enter an https URL (http only for an XTM Hub on this machine).") : undefined}
          >
            <Input
              id="xtm-hub-url"
              className="max-w-md"
              value={trialLicense ? (registration?.default_hub_url ?? "") : hubUrlInput}
              placeholder={registration?.default_hub_url ?? ""}
              onChange={(event) => setHubUrlInput(event.target.value)}
              readOnly={trialLicense}
              disabled={busy || !!waiting}
              aria-invalid={hubUrlInvalid || undefined}
              aria-labelledby={settingsFieldLabelId("xtm-hub-url")}
              aria-describedby={settingsFieldHintId("xtm-hub-url")}
            />
          </SettingsField>
        ) : (
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 content-base">
            <dt className="text-default-secondary">{t_i18n("XTM Hub")}</dt>
            <dd className="truncate">
              {hubLink ? (
                <a href={hubLink} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 hover:text-accent">
                  {hubUrl} <ExternalLink className="h-3 w-3" />
                </a>
              ) : hubUrl}
            </dd>
            {registration?.registered_at && (
              <>
                <dt className="text-default-secondary">{t_i18n("Registered")}</dt>
                <dd>{formatDateTime(registration.registered_at)}</dd>
              </>
            )}
            {registration?.last_check_at && (
              <>
                <dt className="text-default-secondary">{t_i18n("Last check")}</dt>
                <dd>{formatDateTime(registration.last_check_at)}</dd>
              </>
            )}
          </dl>
        )}

        {status === "needs_reregistration" && (
          <p className="content-base text-feedback-warning-primary">
            {registration?.last_check_result === "key_mismatch" ||
            registration?.last_check_result === "token_path_closed"
              ? t_i18n("The XTM Hub no longer accepts this platform's credentials. Register it again to keep it on the XTM Hub.")
              : t_i18n("This platform's identifier changed since it was registered. Register it again to keep it on the XTM Hub.")}
          </p>
        )}
        {status === "lost_connectivity" && (
          <p className="content-base text-feedback-error-primary">
            {t_i18n("The XTM Hub did not confirm the registration at the last check. It is checked again every hour.")}
          </p>
        )}

        {waiting ? (
          <div className="flex items-center gap-2 content-base text-default-secondary">
            <Spinner size="sm" tone="inherit" />
            {t_i18n("Waiting for the XTM Hub window...")}
            <Button variant="ghost" onClick={() => { waiting.popup.close(); setWaiting(null); }}>
              {t_i18n("Cancel")}
            </Button>
          </div>
        ) : (
          <div className="flex flex-wrap gap-2">
            {status !== "registered" && status !== "lost_connectivity" ? (
              <Button onClick={() => openHubPage("register")} disabled={busy || !registration || hubUrlInvalid} aria-busy={register.isPending || undefined}>
                {register.isPending && <Spinner size="sm" tone="inherit" />}
                {t_i18n("Register on the XTM Hub")}
              </Button>
            ) : (
              <>
                <Button variant="outline" onClick={() => check.mutate(undefined, { onError: (error) => fail(error.message) })} disabled={busy}>
                  <RefreshCw className={`h-4 w-4 mr-1 ${check.isPending ? "animate-spin" : ""}`} />
                  {t_i18n("Check now")}
                </Button>
                {!trialLicense && (
                  <Button variant="outline" onClick={() => openHubPage("unregister")} disabled={busy}>
                    {t_i18n("Unregister")}
                  </Button>
                )}
              </>
            )}
            {status !== "unregistered" && !trialLicense && (
              <Button variant="ghost" onClick={forgetLocally} disabled={busy}>
                {t_i18n("Forget locally")}
              </Button>
            )}
          </div>
        )}

        {status !== "registered" && !waiting && (
          <details className="content-base">
            <DisclosureSummary>
              {t_i18n("The XTM Hub showed a platform token instead")}
            </DisclosureSummary>
            <div className="mt-3">
              <SettingsField
                id="xtm-hub-token"
                label={t_i18n("Platform token")}
                helper={t_i18n("Paste the token the XTM Hub showed, then save.")}
                info={t_i18n("The XTM Hub shows this token when its window cannot send it back to this page. It is stored only once the XTM Hub confirms it.")}
              >
                <div className="flex max-w-md gap-2">
                  <Input
                    id="xtm-hub-token"
                    value={pastedToken}
                    onChange={(event) => setPastedToken(event.target.value)}
                    disabled={busy}
                    autoComplete="off"
                    aria-labelledby={settingsFieldLabelId("xtm-hub-token")}
                    aria-describedby={settingsFieldHintId("xtm-hub-token")}
                  />
                  <Button
                    variant="outline"
                    disabled={busy || hubUrlInvalid || !isXtmHubPlatformToken(pastedToken)}
                    onClick={() => saveToken(pastedToken.trim(), hubUrl)}
                  >
                    {t_i18n("Save")}
                  </Button>
                </div>
              </SettingsField>
            </div>
          </details>
        )}
      </div>
    </SettingsCard>
  );
}
