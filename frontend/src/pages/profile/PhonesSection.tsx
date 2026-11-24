// SPDX-License-Identifier: Apache-2.0

/**
 * Profile > Phones: the phones linked to this account (the mobile companion).
 *
 * A phone links itself from the companion ("Turn on notifications"); here the
 * person sees them all, sends a test notification, mutes kinds, sets quiet
 * hours and unlinks one; the PIN lock (`PinLockCard`) hides the companion
 * until its PIN is entered. The QR code opens the platform on a phone: an
 * address, never a credential (the phone signs in as usual).
 */
import { useState } from "react";
import { BellOff, BellRing, Moon, Smartphone, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDeleteDialog } from "@/components/ui/confirm-delete-dialog";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useQuery } from "@tanstack/react-query";
import { useLinkedPhones, useTestPhone, useUnlinkPhone, useUpdatePhone } from "@/hooks/api/useMobileDeviceApi";
import { useFormatter } from "@/i18n";
import { api } from "@/lib/api";
import { formatTimeAgo } from "@/lib/relative-time";
import type { LinkedPhone, NotificationKind } from "@/types";
import { PinLockCard } from "./PinLockCard";

/** The kinds a person may mute on a phone, by what they mean. @i18n-keys */
const MUTABLE_KINDS = {
  run: "Finished runs",
  reminder: "Reminders",
  agent_message: "Messages from agents",
  analytics_refresh: "Analytics updates",
  flow: "Flow notifications",
  security_audit: "Security audit findings",
  dashboard_alert: "Dashboard alerts",
} as const;

function PhoneRow({ phone }: { phone: LinkedPhone }) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const update = useUpdatePhone();
  const unlink = useUnlinkPhone();
  const test = useTestPhone();
  const [confirming, setConfirming] = useState(false);
  const muted = new Set<NotificationKind>(phone.muted_kinds);
  const quiet = phone.quiet_hours;

  const toggleKind = (kind: NotificationKind, on: boolean) => {
    const next = new Set(muted);
    if (on) next.delete(kind);
    else next.add(kind);
    update.mutate({ id: phone.id, muted_kinds: [...next] });
  };

  return (
    <li className="flex flex-col gap-4 border-b border-elevation-subtle-soft py-4 last:border-0">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <Smartphone className="h-5 w-5 shrink-0 text-accent" aria-hidden="true" />
          <div className="flex min-w-0 flex-col">
            <span className="truncate content-base font-medium text-default-primary">
              {phone.name}
              {phone.current && <span className="ml-2 content-caption text-accent">{t_i18n("This device")}</span>}
            </span>
            <span className="content-caption text-default-secondary">
              {phone.last_push_at
                ? t_i18n("Last notified {when}", { when: formatTimeAgo(intl, phone.last_push_at) })
                : t_i18n("Not notified yet")}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Switch
            checked={phone.push_enabled}
            onCheckedChange={(on) => update.mutate({ id: phone.id, push_enabled: on })}
            aria-label={t_i18n("Notifications on {name}", { name: phone.name })}
          />
          <Button
            variant="outline"
            size="sm"
            className="gap-1.5"
            disabled={!phone.push_enabled || test.isPending}
            onClick={() =>
              test.mutate(phone.id, {
                onSuccess: (r) =>
                  toast({
                    title: r.sent ? t_i18n("Test notification sent") : t_i18n("The phone could not be reached"),
                    variant: r.sent ? "success" : "error",
                  }),
                onError: (error) => toast({ title: t_i18n("The test notification failed"), description: error.message, variant: "error" }),
              })
            }
          >
            <BellRing className="h-3.5 w-3.5" /> {t_i18n("Send a test")}
          </Button>
          <Tip content={t_i18n("Unlink")}>
            <Button variant="ghost" size="icon" aria-label={t_i18n("Unlink {name}", { name: phone.name })} onClick={() => setConfirming(true)}>
              <Trash2 className="h-4 w-4" />
            </Button>
          </Tip>
        </div>
      </div>
      {phone.push_enabled && (
        <div className="grid gap-4 md:grid-cols-2">
          <div className="flex flex-col gap-2">
            <span className="flex items-center gap-1.5 content-compact font-medium text-default-primary">
              <BellOff className="h-3.5 w-3.5" aria-hidden="true" /> {t_i18n("Notify me about")}
            </span>
            {(Object.keys(MUTABLE_KINDS) as (keyof typeof MUTABLE_KINDS)[]).map((kind) => (
              <label key={kind} className="flex items-center justify-between gap-3 content-compact text-default-secondary">
                {t_i18n(MUTABLE_KINDS[kind])}
                <Switch checked={!muted.has(kind)} onCheckedChange={(on) => toggleKind(kind, on)} aria-label={t_i18n(MUTABLE_KINDS[kind])} />
              </label>
            ))}
            <span className="content-caption text-default-secondary">
              {t_i18n("Approvals and questions waiting on you always notify.")}
            </span>
          </div>
          <div className="flex flex-col gap-2">
            <span className="flex items-center gap-1.5 content-compact font-medium text-default-primary">
              <Moon className="h-3.5 w-3.5" aria-hidden="true" /> {t_i18n("Quiet hours")}
            </span>
            <label className="flex items-center justify-between gap-3 content-compact text-default-secondary">
              {t_i18n("Nothing from 22:00 to 07:00")}
              <Switch
                checked={!!quiet}
                aria-label={t_i18n("Quiet hours")}
                onCheckedChange={(on) =>
                  update.mutate(
                    on
                      ? {
                          id: phone.id,
                          quiet_hours: {
                            start: "22:00",
                            end: "07:00",
                            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
                          },
                        }
                      : { id: phone.id, clear_quiet_hours: true },
                  )
                }
              />
            </label>
            {quiet && (
              <span className="content-caption text-default-secondary">
                {t_i18n("{start} to {end}, {timezone} time. What arrives meanwhile waits in the Inbox.", quiet)}
              </span>
            )}
          </div>
        </div>
      )}
      <ConfirmDeleteDialog
        open={confirming}
        onOpenChange={setConfirming}
        name={phone.name}
        title={t_i18n("Unlink this phone?")}
        description={t_i18n("It stops receiving notifications, and the buttons of the notifications already on it stop working.")}
        confirmLabel={t_i18n("Unlink")}
        onConfirm={() => unlink.mutate(phone.id)}
      />
    </li>
  );
}

export function PhonesSection() {
  const { t_i18n } = useFormatter();
  const { data: phones, isLoading } = useLinkedPhones();
  const { data: link } = useQuery<{ url: string; qr_code: string }>({
    queryKey: ["mobile-devices", "companion-link"],
    queryFn: () => api.get("/mobile-devices/companion-link"),
    staleTime: Infinity,
  });

  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_280px]">
      <div className="flex flex-col gap-6">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="content-base">{t_i18n("Linked phones")}</CardTitle>
            <CardDescription className="content-compact">
              {t_i18n("Phones that receive your notifications: approvals you can answer from the lock screen, reminders and what your agents send you.")}
            </CardDescription>
          </CardHeader>
          <CardContent className="pt-0">
            {isLoading ? null : phones && phones.length > 0 ? (
              <ul>{phones.map((phone) => <PhoneRow key={phone.id} phone={phone} />)}</ul>
            ) : (
              <p className="content-compact text-default-secondary">
                {t_i18n("No phone linked yet. Open the platform on your phone and tap Turn on notifications.")}
              </p>
            )}
          </CardContent>
        </Card>
        <PinLockCard />
      </div>
      {link && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="content-base">{t_i18n("Open on your phone")}</CardTitle>
            <CardDescription className="content-compact">{t_i18n("Scan with your phone's camera, sign in, then install the app.")}</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col items-center gap-2 pt-0">
            <img src={link.qr_code} alt={t_i18n("QR code of {url}", { url: link.url })} className="h-48 w-48 rounded-sm bg-elevation-default p-2" />
            <span className="break-all text-center content-caption text-default-secondary">{link.url}</span>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
