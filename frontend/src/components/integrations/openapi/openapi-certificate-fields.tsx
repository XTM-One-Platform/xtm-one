/**
 * The client certificate an OpenAPI integration presents (mutual TLS): a PEM
 * certificate (a chain is fine, the client's own first), its PEM private key
 * and the key's passphrase when it is encrypted — pasted or loaded from
 * files. Used by the creation form and by the API Spec tab.
 */
import { useRef } from "react";
import { Icon } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";

export interface CertificateDraft {
  certificate: string;
  privateKey: string;
  passphrase: string;
}

const PEM_AREA =
  "w-full resize-y rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 font-mono-plex text-[11px] leading-snug ring-offset-focus placeholder:text-default-disabled focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2";

function PemField({
  label,
  value,
  onChange,
  placeholder,
  masked,
  accept,
}: {
  label: string;
  value: string;
  onChange: (next: string) => void;
  placeholder: string;
  masked?: boolean;
  accept: string;
}) {
  const { t_i18n } = useFormatter();
  const picker = useRef<HTMLInputElement>(null);
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <Label className="block text-default-primary">{label}</Label>
        <Button type="button" variant="ghost" size="sm" className="ml-auto h-6 gap-1 px-2 content-compact" onClick={() => picker.current?.click()}>
          <Icon name="file-up" size={16} />
          {t_i18n("Load from file")}
        </Button>
        <input
          ref={picker}
          type="file"
          accept={accept}
          className="hidden"
          aria-label={label}
          onChange={async (e) => {
            const file = e.target.files?.[0];
            e.target.value = "";
            if (file) onChange(await file.text());
          }}
        />
      </div>
      <textarea
        rows={4}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        spellCheck={false}
        autoComplete="off"
        aria-label={label}
        className={cn(PEM_AREA, masked && "[-webkit-text-security:disc]")}
      />
    </div>
  );
}

export function ClientCertificateFields({
  value,
  onChange,
  showSecrets,
}: {
  value: CertificateDraft;
  onChange: (next: CertificateDraft) => void;
  showSecrets: boolean;
}) {
  const { t_i18n } = useFormatter();
  const set = (patch: Partial<CertificateDraft>) => onChange({ ...value, ...patch });
  return (
    <div className="space-y-3">
      <PemField
        label={t_i18n("Client certificate (PEM)")}
        value={value.certificate}
        onChange={(certificate) => set({ certificate })}
        placeholder="-----BEGIN CERTIFICATE-----"
        accept=".pem,.crt,.cer,.txt"
      />
      <PemField
        label={t_i18n("Private key (PEM)")}
        value={value.privateKey}
        onChange={(privateKey) => set({ privateKey })}
        placeholder="-----BEGIN PRIVATE KEY-----"
        masked={!showSecrets}
        accept=".pem,.key,.txt"
      />
      <div className="space-y-1">
        <Label className="block text-default-primary">{t_i18n("Key passphrase (if the key is encrypted)")}</Label>
        <Input aria-label={t_i18n("Key passphrase (if the key is encrypted)")}
          type={showSecrets ? "text" : "password"}
          value={value.passphrase}
          onChange={(e) => set({ passphrase: e.target.value })}
          autoComplete="new-password"
        />
      </div>
      <p className="content-compact text-default-secondary">
        {t_i18n("The certificate is presented on every call and every token request. The key is stored encrypted and never shown again.")}
      </p>
    </div>
  );
}
