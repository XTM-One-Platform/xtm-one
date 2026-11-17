import { AlertTriangle, Tag } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useOpenCrqCompatibility } from "@/hooks/api/useIntegrationApi";
import { useFormatter } from "@/i18n";
import type { Integration, OpenCrqManifestState } from "@/types";

const WARNING_STATES: ReadonlySet<OpenCrqManifestState> = new Set(["pending", "unavailable", "not_accepted_default_token"]);

/**
 * On the integration's Overview: which OpenCRQ version is connected, whether XTM One
 * holds the manifest the instance describes itself with, the tools that instance
 * cannot serve (hidden from the agents) and the fields a tool sends that it refuses.
 *
 * Reads the compatibility route, which answers through the same functions the
 * tool build gates on, so this card never says a tool is there when the agents
 * do not get it. Renders nothing while it loads or when it fails: the card adds
 * to the Overview, it is not what the page is for.
 */
export function OpenCrqVersionCard({ integration }: { integration: Integration }) {
  const { t_i18n, intl } = useFormatter();
  const { data } = useOpenCrqCompatibility(integration.id);
  if (!data) return null;

  const state = data.manifest_state;
  const warning = state !== null && WARNING_STATES.has(state);
  const reported = data.reported_version;

  const copy: Record<OpenCrqManifestState | "none", { label: string; summary: string }> = {
    current: {
      label: t_i18n("Manifest up to date"),
      summary: t_i18n("This OpenCRQ described its tools to XTM One. The agents get only what it can serve."),
    },
    pending: {
      label: t_i18n("Manifest pending"),
      summary: t_i18n("This OpenCRQ announced a new description of its tools that XTM One has not received yet. No tool is hidden until it arrives."),
    },
    unavailable: {
      label: t_i18n("Manifest unavailable"),
      summary: t_i18n("This OpenCRQ does not describe its tools to XTM One. No tool is hidden and no field is checked."),
    },
    not_accepted_default_token: {
      label: t_i18n("Manifest not accepted"),
      summary: t_i18n("XTM One keeps nothing from an OpenCRQ registered with the default registration token. Set a registration token of your own."),
    },
    none: {
      label: t_i18n("Read at the first call"),
      summary: t_i18n(
        "XTM One reads the description of the tools from this OpenCRQ at the first OpenCRQ call of each conversation or run. A tool it cannot serve and a field it does not accept are refused then.",
      ),
    },
  };
  const { label, summary } = copy[state ?? "none"];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex flex-wrap items-center gap-2 text-base">
          <Tag className="h-4 w-4 text-accent" />
          {t_i18n("OpenCRQ version")}
          <Badge variant={warning ? "warning" : state === "current" ? "success" : "secondary"}>{label}</Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 content-base">
        {reported !== null && (
          <p className="content-compact">
            {reported === "" ? t_i18n("Development build") : t_i18n("Version {version}", { version: reported })}
          </p>
        )}

        {warning ? (
          <div className="rounded-sm bg-feedback-alert-primary/5 p-3 flex items-start gap-2.5">
            <AlertTriangle className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0 mt-0.5" />
            <p className="content-compact text-feedback-warning-primary leading-relaxed">{summary}</p>
          </div>
        ) : (
          <p className="content-compact text-default-secondary">{summary}</p>
        )}

        {data.fingerprint && (
          <p className="content-compact text-default-secondary">
            <span>{t_i18n("Fingerprint {fingerprint}", { fingerprint: data.fingerprint })}</span>
            {data.received_at && (
              <span>
                {" · "}
                {t_i18n("Received {date}", {
                  date: intl.formatDate(data.received_at, { dateStyle: "medium", timeStyle: "short" }),
                })}
              </span>
            )}
          </p>
        )}

        {data.hidden_tools.length > 0 && (
          <div className="space-y-2">
            <p className="content-compact font-medium">
              {t_i18n(
                "{count, plural, one {# tool is hidden from the agents: this instance does not support it.} other {# tools are hidden from the agents: this instance does not support them.}}",
                { count: data.hidden_tools.length },
              )}
            </p>
            <ul className="max-h-64 space-y-1.5 overflow-y-auto">
              {data.hidden_tools.map((tool) => (
                <li key={tool.name} className="flex flex-wrap items-center justify-between gap-2">
                  <code className="rounded-sm bg-feedback-alert-primary/10 px-1.5 py-0.5 content-caption font-mono-plex text-feedback-warning-tertiary">
                    {tool.name}
                  </code>
                  <span className="content-compact text-default-secondary font-mono-plex">
                    {t_i18n("Missing: {procedures}", { procedures: tool.missing_procedures.join(", ") })}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {data.limited_tools.length > 0 && (
          <div className="space-y-2">
            <p className="content-compact font-medium">
              {t_i18n(
                "{count, plural, one {# tool has fields this instance does not accept. A call that sends one is refused.} other {# tools have fields this instance does not accept. A call that sends one is refused.}}",
                { count: data.limited_tools.length },
              )}
            </p>
            <ul className="max-h-64 space-y-2 overflow-y-auto">
              {data.limited_tools.map((tool) => (
                <li key={tool.name} className="space-y-1">
                  <code className="rounded-sm bg-elevation-hover px-1.5 py-0.5 text-[11px] font-mono-plex">{tool.name}</code>
                  <div className="flex flex-wrap gap-1.5">
                    {tool.fields.map((field) => (
                      <Badge
                        key={`${field.procedure}:${field.path}:${field.kind}`}
                        variant="outline"
                        className="font-mono-plex content-caption"
                        title={field.kind === "missing" ? t_i18n("The instance requires this field and the tool never sends it.") : undefined}
                      >
                        {field.kind === "missing" ? t_i18n("{field} (required)", { field: field.path }) : field.path}
                      </Badge>
                    ))}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
