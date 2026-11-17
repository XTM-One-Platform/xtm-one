// SPDX-License-Identifier: Apache-2.0

/**
 * Inbound platform-registration instructions.
 *
 * OpenCTI / OpenAEV are `registration_only` in XTM One: they are never
 * "connected" outbound from XTM One - the platform registers itself by adding
 * the XTM One URL + registration token to its own configuration. These config
 * snippets are the single source of truth shared by the WelcomeLanding hero and
 * the in-dashboard ConnectPlatformDialog so they can never diverge.
 */

import { useFormatter } from "@/i18n";

type Product = "opencti" | "openaev" | "opencrq";

const SNIPPETS: Record<
  Product,
  {
    configFile: string;
    config: (url: string, token: string) => string;
    env: (url: string, token: string) => string;
  }
> = {
  opencti: {
    configFile: "config/default.json",
    config: (url, token) => `"xtm": {\n  "xtm_one_url": "${url}",\n  "xtm_one_token": "${token}"\n}`,
    env: (url, token) => `XTM__XTM_ONE_URL=${url}\nXTM__XTM_ONE_TOKEN=${token}`,
  },
  openaev: {
    configFile: "application.properties",
    config: (url, token) =>
      `openaev.xtm.one.enable=true\nopenaev.xtm.one.url=${url}\nopenaev.xtm.one.token=${token}`,
    env: (url, token) =>
      `OPENAEV_XTM_ONE_ENABLE=true\nOPENAEV_XTM_ONE_URL=${url}\nOPENAEV_XTM_ONE_TOKEN=${token}`,
  },
  opencrq: {
    configFile: "config/production.json",
    config: (url, token) =>
      `"xtm_one": {\n  "enabled": true,\n  "url": "${url}",\n  "token": "${token}",\n  "platform_url": "<your-opencrq-public-url>"\n}`,
    env: (url, token) =>
      `XTM_ONE__ENABLED=true\nXTM_ONE__URL=${url}\nXTM_ONE__TOKEN=${token}\nXTM_ONE__PLATFORM_URL=<your-opencrq-public-url>`,
  },
};

function Snippet({ label, file, body }: { label: string; file: string; body: string }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-2">
      <p className="text-[11px] font-medium text-default-secondary/80 uppercase tracking-wider">{t_i18n(label)}</p>
      <div className="rounded-sm bg-elevation-default border border-elevation-subtle-soft p-3 overflow-hidden">
        <p className="content-caption text-default-secondary mb-1.5 font-mono-plex">{file}</p>
        <pre className="text-[11px] text-default-secondary font-mono-plex whitespace-pre-wrap break-all leading-relaxed">{body}</pre>
      </div>
    </div>
  );
}

export function PlatformConnectInstructions({
  product,
  platformUrl,
  token,
}: {
  product: Product;
  platformUrl: string;
  token: string;
}) {
  const s = SNIPPETS[product];
  return (
    <div className="space-y-4">
      <Snippet label="Option 1 - Configuration file" file={s.configFile} body={s.config(platformUrl, token)} />
      <Snippet label="Option 2 - Environment variables" file=".env" body={s.env(platformUrl, token)} />
    </div>
  );
}
