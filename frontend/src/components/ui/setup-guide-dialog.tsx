// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import { HelpCircle, KeyRound, ListChecks, Info, ExternalLink, FileJson, Copy, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogTrigger,
} from "@/components/ui/dialog";
import { cn, safeExternalUrl } from "@/lib/utils";
import { COPY_FEEDBACK_DELAY } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import type { SetupGuide } from "@/types";

/**
 * Shared dialog component that renders integration setup instructions.
 * Used in both the new-integration flow and the integration detail page.
 *
 * @param variant - "ghost" (default, subtle) or "outline" (more prominent, cyan border)
 */
export function SetupGuideDialog({ guide, providerName, variant = "ghost" }: { guide: SetupGuide; providerName: string; variant?: "ghost" | "outline" }) {
  const { t_i18n, intl } = useFormatter();
  const [copied, setCopied] = useState(false);
  const openSource = guide.open_source;
  const openSourceHref = safeExternalUrl(openSource?.url);
  const consoleHref = safeExternalUrl(guide.url);

  const copyManifest = () => {
    if (!guide.manifest_example) return;
    navigator.clipboard.writeText(guide.manifest_example);
    setCopied(true);
    setTimeout(() => setCopied(false), COPY_FEEDBACK_DELAY);
  };

  const renderSteps = () => {
    const elements: React.ReactNode[] = [];
    let stepNum = 0;
    for (let i = 0; i < guide.steps.length; i++) {
      const text = guide.steps[i];
      if (text.startsWith("  - ") || text.startsWith("  -")) {
        elements.push(
          <li key={i} className="flex gap-3 content-base ml-8">
            <span className="text-accent/60 shrink-0 mt-0.5">•</span>
            <span className="text-default-secondary leading-relaxed">{text.replace(/^\s+-\s*/, "")}</span>
          </li>
        );
      } else {
        stepNum++;
        elements.push(
          <li key={i} className="flex gap-3 content-base">
            <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-accent/10 text-[11px] font-semibold text-accent">{stepNum}</span>
            <span className="text-default-secondary leading-relaxed">{text}</span>
          </li>
        );
      }
    }
    return elements;
  };

  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button variant={variant} size="sm" className={cn(
          "h-8 gap-1.5 content-compact text-accent hover:text-accent",
          variant === "outline" && "border-accent/30 hover:bg-accent/5",
        )}>
          <HelpCircle className="h-3.5 w-3.5" />
          {t_i18n("Setup guide")}
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ListChecks className="h-5 w-5 text-accent" />
            {guide.title}
          </DialogTitle>
          <DialogDescription>
            {t_i18n("Step-by-step instructions to configure {provider}.", { provider: providerName })}
            {openSource && (
              <>
                {" "}
                {intl.formatMessage(
                  {
                    id: openSource.author
                      ? "Built on {project}, an open-source project by {author}."
                      : "Built on {project}, an open-source project.",
                  },
                  {
                    project: openSourceHref ? (
                      <a
                        href={openSourceHref}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-accent hover:underline"
                      >
                        {openSource.name}
                      </a>
                    ) : (
                      openSource.name
                    ),
                    author: openSource.author ?? "",
                  },
                )}
                {openSource.license && (
                  <> {t_i18n("Released under the {license} license.", { license: openSource.license })}</>
                )}
              </>
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-2">
          <h4 className="content-compact font-semibold uppercase tracking-wider text-default-secondary">{t_i18n("Setup steps")}</h4>
          <ol className="space-y-2.5">{renderSteps()}</ol>
        </div>

        {guide.permissions.length > 0 && (
          <div className="space-y-2">
            <h4 className="content-compact font-semibold uppercase tracking-wider text-default-secondary flex items-center gap-1.5">
              <KeyRound className="h-3.5 w-3.5" />
              {t_i18n("Required permissions / scopes")}
            </h4>
            <div className="rounded-sm bg-elevation-hover divide-y divide-[var(--border-elevation-subtle)]">
              {guide.permissions.map((perm) => (
                <div key={perm.scope} className="flex items-start gap-3 px-3 py-2">
                  <code className="shrink-0 rounded-sm bg-accent/10 px-1.5 py-0.5 text-[11px] font-mono-plex text-accent">
                    {perm.scope}
                  </code>
                  <span className="content-compact text-default-secondary leading-relaxed">{perm.description}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {guide.manifest_example && (
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <h4 className="content-compact font-semibold uppercase tracking-wider text-default-secondary flex items-center gap-1.5">
                <FileJson className="h-3.5 w-3.5" />
                {t_i18n("Example app manifest")}
              </h4>
              <Button variant="ghost" size="sm" className="h-7 gap-1.5 content-compact text-default-secondary hover:text-default-primary" onClick={copyManifest}>
                {copied ? <Check className="h-3 w-3 text-feedback-success-primary" /> : <Copy className="h-3 w-3" />}
                {copied ? t_i18n("Copied") : t_i18n("Copy")}
              </Button>
            </div>
            <pre className="rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-3 text-[11px] font-mono-plex text-default-secondary overflow-x-auto max-h-64 overflow-y-auto leading-relaxed">
              {guide.manifest_example}
            </pre>
          </div>
        )}

        {guide.apis_to_enable && guide.apis_to_enable.length > 0 && (
          <div className="space-y-2">
            <h4 className="content-compact font-semibold uppercase tracking-wider text-default-secondary">{t_i18n("APIs to enable")}</h4>
            <div className="flex flex-wrap gap-1.5">
              {guide.apis_to_enable.map((api) => (
                <Badge key={api} variant="outline" className="content-compact">{api}</Badge>
              ))}
            </div>
          </div>
        )}

        {guide.notes && (
          <div className="rounded-sm bg-feedback-alert-primary/5 p-3 flex gap-2.5">
            <Info className="h-4 w-4 shrink-0 text-feedback-warning-primary mt-0.5" />
            <p className="content-compact text-feedback-warning-primary leading-relaxed">{guide.notes}</p>
          </div>
        )}

        {consoleHref && (
          <a
            href={consoleHref}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 content-base text-accent hover:underline"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            {t_i18n("Open {name} console", { name: guide.title.split(" ")[0] })}
          </a>
        )}
      </DialogContent>
    </Dialog>
  );
}
