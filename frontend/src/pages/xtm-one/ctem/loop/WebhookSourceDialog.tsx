// SPDX-License-Identifier: Apache-2.0

/**
 * A generic signed webhook source of one trigger path (#2506, dev-docs/ctem-signals.md section 4ter):
 * create it (off), change its name and mapping, rotate its secret, and test its mapping on a sample.
 *
 * The secret is shown once, in this dialog, right after a creation or a rotation; the API never answers
 * it again. The receiver URL, the signature header and a signed request example are shown so the
 * sending tool can be set up; the mapping test reads a sample and records nothing.
 */
import { useEffect, useId, useState } from "react";
import { Textarea } from "@filigran/design-system";
import { Tip } from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { useToast } from "@/components/ui/toast";
import {
  type CtemWebhookSource,
  useCreateCtemWebhookSource,
  useRotateCtemWebhookSource,
  useTestCtemWebhookSource,
  useUpdateCtemWebhookSource,
} from "@/hooks/api/useCtemSignalsApi";
import { useFormatter } from "@/i18n";
import { signedRequestExample } from "./signal-sources";

/** The sample the mapping test starts from: the default mapping reads it. */
const SAMPLE = '{\n  "event": {\n    "id": "evt-1",\n    "host": "vpn.example.com",\n    "severity": "high"\n  }\n}';

export function WebhookSourceDialog({
  open,
  pathId,
  pathLabel,
  source,
  onClose,
}: {
  open: boolean;
  pathId: string;
  pathLabel: string;
  /** The source to configure; null creates one on the path. */
  source: CtemWebhookSource | null;
  onClose: () => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const create = useCreateCtemWebhookSource();
  const update = useUpdateCtemWebhookSource();
  const rotate = useRotateCtemWebhookSource();
  const test = useTestCtemWebhookSource();
  const ids = { name: useId(), id: useId(), label: useId(), severity: useId(), sample: useId() };
  const [name, setName] = useState("");
  const [idPath, setIdPath] = useState("event.id");
  const [labelPath, setLabelPath] = useState("event.host");
  const [severityPath, setSeverityPath] = useState("event.severity");
  const [sample, setSample] = useState(SAMPLE);
  const [error, setError] = useState<string | null>(null);
  const [testLine, setTestLine] = useState<{ ok: boolean; text: string } | null>(null);
  // The source as last answered, with its secret right after a creation or a rotation (shown once).
  const [shown, setShown] = useState<CtemWebhookSource | null>(null);

  useEffect(() => {
    if (!open) return;
    setName(source?.name ?? "");
    setIdPath(source?.mapping.id ?? "event.id");
    setLabelPath(source?.mapping.label ?? (source ? "" : "event.host"));
    setSeverityPath(source?.mapping.severity ?? (source ? "" : "event.severity"));
    setSample(SAMPLE);
    setError(null);
    setTestLine(null);
    setShown(source);
  }, [open, source]);

  const current = shown ?? source;
  const mapping = { id: idPath.trim(), label: labelPath.trim() || null, severity: severityPath.trim() || null };
  const busy = create.isPending || update.isPending || rotate.isPending;
  const valid = name.trim().length > 0 && mapping.id.length > 0;

  const save = () => {
    setError(null);
    if (!current) {
      create.mutate(
        { trigger_path: pathId, name: name.trim(), mapping },
        {
          onSuccess: (created) => {
            setShown(created);
            toast({ title: t_i18n("Webhook source created, off"), variant: "success" });
          },
          onError: (err) => setError(err.message),
        },
      );
      return;
    }
    update.mutate(
      { id: current.id, name: name.trim(), mapping },
      {
        onSuccess: (saved) => {
          setShown({ ...saved, secret: current.secret });
          toast({ title: t_i18n("Webhook source saved"), variant: "success" });
        },
        onError: (err) => setError(err.message),
      },
    );
  };

  const rotateSecret = () => {
    if (!current) return;
    rotate.mutate(current.id, {
      onSuccess: (rotated) => {
        setShown(rotated);
        toast({ title: t_i18n("New secret made: the old one is refused from now on"), variant: "success" });
      },
      onError: (err) => toast({ title: t_i18n("The secret was not rotated"), description: err.message, variant: "error" }),
    });
  };

  const runTest = () => {
    if (!current) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(sample);
    } catch {
      setTestLine({ ok: false, text: t_i18n("The sample is not valid JSON.") });
      return;
    }
    test.mutate(
      { id: current.id, sample: parsed },
      {
        onSuccess: (result) =>
          setTestLine(
            result.ok
              ? {
                  ok: true,
                  text: t_i18n("Reads id {id}, label {label}, severity {severity}. Nothing was recorded.", {
                    id: result.id ?? "",
                    label: result.label ?? t_i18n("None"),
                    severity: result.severity ?? t_i18n("None"),
                  }),
                }
              : { ok: false, text: result.reason ?? t_i18n("The mapping reads no id in the sample.") },
          ),
        onError: (err) => setTestLine({ ok: false, text: err.message }),
      },
    );
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent size="lg" data-testid="ctem-webhook-source-dialog">
        <DialogHeader>
          <DialogTitle>{current ? t_i18n("Signed webhook: {name}", { name: current.name }) : t_i18n("New signed webhook on {path}", { path: pathLabel })}</DialogTitle>
          <DialogDescription>
            {t_i18n(
              "A tool posts JSON events to this source's own address, signed with its secret. The source is off until you switch it on in the path's table; a wrong or missing signature is refused and records nothing.",
            )}
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="flex flex-col gap-4">
          <SettingsField id={ids.name} label={t_i18n("Name")} required error={error ?? undefined}>
            <Input id={ids.name} aria-label={t_i18n("Name")} value={name} maxLength={120} onChange={(e) => setName(e.target.value)} data-testid="ctem-webhook-source-name" />
          </SettingsField>
          <SettingsField
            id={ids.id}
            label={t_i18n("Path of the event id")}
            required
            helper={t_i18n("Dotted path in the posted JSON, e.g. event.id or items.0.id. The id names the signal and drops a repeated delivery.")}
          >
            <Input
              id={ids.id}
              aria-label={t_i18n("Path of the event id")}
              value={idPath}
              aria-describedby={settingsFieldHintId(ids.id)}
              onChange={(e) => setIdPath(e.target.value)}
              data-testid="ctem-webhook-source-map-id"
            />
          </SettingsField>
          <div className="grid grid-cols-2 gap-4">
            <SettingsField id={ids.label} label={t_i18n("Path of the label")}>
              <Input id={ids.label} aria-label={t_i18n("Path of the label")} value={labelPath} onChange={(e) => setLabelPath(e.target.value)} data-testid="ctem-webhook-source-map-label" />
            </SettingsField>
            <SettingsField id={ids.severity} label={t_i18n("Path of the severity")}>
              <Input id={ids.severity} aria-label={t_i18n("Path of the severity")} value={severityPath} onChange={(e) => setSeverityPath(e.target.value)} data-testid="ctem-webhook-source-map-severity" />
            </SettingsField>
          </div>

          {current && (
            <div className="flex flex-col gap-2 border-t border-elevation-subtle pt-4 content-compact" data-testid="ctem-webhook-source-receiver">
              <span className="font-medium">{t_i18n("Receiver")}</span>
              <span className="flex min-w-0 items-center gap-2">
                <Tip content={current.receiver_url}>
                  <code tabIndex={0} className="min-w-0 truncate rounded-sm">
                    {current.receiver_url}
                  </code>
                </Tip>
                <CopyIconButton value={current.receiver_url} label={t_i18n("Copy the address")} />
              </span>
              <span className="text-default-secondary">
                {t_i18n("Sign each request: header {header} with sha256= and the hex HMAC-SHA256 of the raw body, keyed with the secret.", {
                  header: current.signature_header,
                })}
              </span>
              {current.secret ? (
                <span className="flex flex-col gap-1 rounded-sm bg-elevation-hover p-3" data-testid="ctem-webhook-source-secret">
                  <span className="font-medium">{t_i18n("Secret, shown this once: copy it now")}</span>
                  <span className="flex min-w-0 items-center gap-2">
                    <code className="min-w-0 truncate">{current.secret}</code>
                    <CopyIconButton value={current.secret} label={t_i18n("Copy the secret")} />
                  </span>
                </span>
              ) : (
                <span className="text-default-secondary">{t_i18n("The secret was shown once when it was made. Rotate it to get a new one.")}</span>
              )}
              <span className="flex min-w-0 items-start gap-2">
                <pre className="min-w-0 flex-1 overflow-x-auto rounded-sm bg-elevation-hover p-2 text-xs" data-testid="ctem-webhook-source-example">
                  {signedRequestExample(current.receiver_url, current.signature_header)}
                </pre>
                <CopyIconButton value={signedRequestExample(current.receiver_url, current.signature_header)} label={t_i18n("Copy the example")} />
              </span>
              <span>
                <Button variant="secondary" onClick={rotateSecret} disabled={busy} data-testid="ctem-webhook-source-rotate">
                  {t_i18n("Rotate the secret")}
                </Button>
              </span>
            </div>
          )}

          {current && (
            <div className="flex flex-col gap-2 border-t border-elevation-subtle pt-4 content-compact" data-testid="ctem-webhook-source-test">
              <SettingsField id={ids.sample} label={t_i18n("Test the mapping on a sample")} helper={t_i18n("Reads the saved mapping. Nothing is recorded.")}>
                <Textarea id={ids.sample} aria-label={t_i18n("Test the mapping on a sample")} rows={6} value={sample} onChange={(e) => setSample(e.target.value)} className="font-mono" />
              </SettingsField>
              <span className="flex items-center gap-3">
                <Button variant="secondary" onClick={runTest} disabled={test.isPending} data-testid="ctem-webhook-source-run-test">
                  {t_i18n("Test")}
                </Button>
                {testLine && (
                  <span className={testLine.ok ? "text-default-primary" : "text-feedback-error-primary"} data-testid="ctem-webhook-source-test-result">
                    {testLine.text}
                  </span>
                )}
              </span>
              {current.last_error && (
                <span className="text-feedback-error-primary" data-testid="ctem-webhook-source-last-error">
                  {t_i18n("Last delivery recorded nothing: {reason}", { reason: current.last_error })}
                </span>
              )}
            </div>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {t_i18n("Close")}
          </Button>
          <Button onClick={save} disabled={!valid || busy} data-testid="ctem-webhook-source-save">
            {current ? t_i18n("Save") : t_i18n("Create, off")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
