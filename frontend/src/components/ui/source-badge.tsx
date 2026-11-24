// SPDX-License-Identifier: Apache-2.0

/**
 * A source named with its emblem (#1357): the module's official mark (the
 * canonical assets of `PROVIDER_LOGOS`, drawn per theme by `ProviderLogo`) then
 * its name. A source with no official asset keeps its name alone: no fallback
 * plug glyph next to a brand-less source.
 *
 * A cut name is named whole by the design-system tooltip (`TruncatedText`, UX r1
 * B1 on #1358). The badge sits on the top of its line box (`align-top`), never on
 * the emblem's bottom edge, so its name shares the baseline of the neighbouring
 * cells and labels (UX r1 B3 on #1358).
 */
import { ProviderLogo } from "@/components/ui/provider-logo";
import { TruncatedText } from "@/components/ui/truncated-text";
import { INTEGRATION_PROVIDER_NAMES, PROVIDER_LOGOS } from "@/lib/constants";
import { pivotModuleOf } from "@/lib/module-pivot";
import { cn } from "@/lib/utils";

/** The name a source is shown under: a module, an integration, else the given label or the key. */
function sourceDisplayName(source: string, label?: string | null): string {
  const key = pivotModuleOf(source) ?? source;
  return INTEGRATION_PROVIDER_NAMES[key] ?? label ?? source;
}

export function SourceBadge({
  source,
  label,
  className,
  testId = "source-badge",
}: {
  /** The source key: opencti, openaev, opencrq, an integration provider, or any other. */
  source: string;
  /** The name to show for a source with no known name, already translated (a module or provider name is a brand, never translated). */
  label?: string | null;
  className?: string;
  testId?: string;
}) {
  const key = pivotModuleOf(source) ?? source;
  const name = sourceDisplayName(source, label);
  return (
    <span className={cn("inline-flex min-w-0 max-w-full items-center gap-1.5 align-top", className)} data-testid={testId} data-source={key}>
      {PROVIDER_LOGOS[key] && <ProviderLogo provider={key} decorative className="size-4 shrink-0" />}
      <TruncatedText text={name} />
    </span>
  );
}
