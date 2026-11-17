// SPDX-License-Identifier: Apache-2.0

import { Fragment, type ComponentProps } from "react";

import { cn } from "@/lib/utils";

import { splitConfigTokens } from "./security-audit";

/**
 * How a configuration name reads in Settings > Access and security: the
 * audit's findings and controls and the security options set `DEBUG=false`
 * or `TRUST_PROXY_HEADERS` the same way, as what to type.
 */
export function ConfigCode({ className, ...props }: ComponentProps<"code">) {
  return (
    <code
      className={cn("rounded-sm bg-elevation-hover px-1 py-px font-mono-plex text-[0.95em] text-default-primary", className)}
      {...props}
    />
  );
}

/** Text with its configuration names set as code, so they read (and copy) as what to type. */
export function ConfigText({ text }: { text: string }) {
  return (
    <>
      {splitConfigTokens(text).map((segment, index) =>
        segment.code ? <ConfigCode key={index}>{segment.text}</ConfigCode> : <Fragment key={index}>{segment.text}</Fragment>,
      )}
    </>
  );
}
