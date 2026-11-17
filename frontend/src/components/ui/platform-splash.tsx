// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState, type ReactNode } from "react";
import { Shield, type LucideIcon } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { useFormatter } from "@/i18n";

/**
 * The shared full-page splash shell (Principle X).
 *
 * Extracted from `CtemTrialSplash` so the AI-disabled landing is the same
 * pattern rather than a second one that drifts. Everything here is layout and
 * motion; every word arrives through a slot, because product and marketing own
 * the copy, not this component.
 */

export interface SplashValueProp {
  icon: LucideIcon;
  /** Passed through `t_i18n` by this component — give it the English string. */
  title: string;
  desc: string;
}

export function PlatformSplash({
  logo,
  badge,
  headline,
  description,
  actions,
  hero,
  valueProps,
  footer,
  children,
}: {
  logo: ReactNode;
  badge?: ReactNode;
  headline: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  /** Extra strip inside the hero, under the CTAs (e.g. connected platforms). */
  hero?: ReactNode;
  valueProps: SplashValueProp[];
  footer?: ReactNode;
  /** Dialogs and other non-visual siblings. */
  children?: ReactNode;
}) {
  const { t_i18n } = useFormatter();

  // Subtle entrance — staggered fade-in for the value props.
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setMounted(true), 50);
    return () => clearTimeout(t);
  }, []);

  return (
    <div className="space-y-10 animate-fade-in">
      {/* Hero */}
      <div className="relative overflow-hidden rounded-sm bg-gradient-to-br from-[var(--bg-elevation-default)] via-[var(--bg-elevation-default)] to-accent/[0.05] p-8 md:p-12">
        {/* Ambient glow */}
        <div className="absolute top-0 right-0 w-[32rem] h-[32rem] bg-accent/[0.06] rounded-full blur-3xl -translate-y-1/2 translate-x-1/3 pointer-events-none" />
        <div className="absolute bottom-0 left-0 w-96 h-96 bg-accent/[0.04] rounded-full blur-3xl translate-y-1/2 -translate-x-1/3 pointer-events-none" />

        <div className="relative flex flex-col items-center text-center space-y-6 max-w-3xl mx-auto">
          <div className="flex flex-col items-center gap-3">
            {logo}
            {badge}
          </div>

          <div className="space-y-3">
            <h1 className="text-3xl md:text-4xl font-bold font-sans-geologica leading-tight">
              {headline}
            </h1>
            {description && (
              <p className="content-base md:text-base text-default-secondary max-w-xl mx-auto leading-relaxed">
                {description}
              </p>
            )}
          </div>

          {actions && (
            <div className="flex flex-wrap items-center justify-center gap-3 pt-2">
              {actions}
            </div>
          )}

          {hero}
        </div>
      </div>

      {/* Value props */}
      <div className="grid md:grid-cols-3 gap-5 max-w-5xl mx-auto">
        {valueProps.map((vp, i) => {
          const Icon = vp.icon;
          return (
            <Card
              key={vp.title}
              className={`border-elevation-subtle-soft hover:border-accent/30 transition-all duration-500 hover:shadow-[0_0_30px_color-mix(in_srgb,_var(--color-accent)_calc(0.08_*_100%),_transparent)] overflow-hidden ${
                mounted ? "opacity-100 translate-y-0" : "opacity-0 translate-y-2"
              }`}
              style={{ transitionDelay: `${i * 100}ms` }}
            >
              <CardContent className="p-6 space-y-3">
                <div className="h-10 w-10 rounded-sm bg-accent/[0.08] border border-accent/15 flex items-center justify-center">
                  <Icon className="h-5 w-5 text-accent" />
                </div>
                <h3 className="content-base font-semibold">{t_i18n(vp.title)}</h3>
                <p className="content-compact text-default-secondary leading-relaxed">
                  {t_i18n(vp.desc)}
                </p>
              </CardContent>
            </Card>
          );
        })}
      </div>

      {/* Trust footer */}
      <div className="text-center space-y-3 max-w-2xl mx-auto pt-4">
        <div className="flex items-center justify-center gap-2 text-default-secondary">
          <Shield className="h-3.5 w-3.5" />
          <span className="text-[11px] uppercase tracking-wider font-medium">
            {t_i18n("Built by Filigran: the team behind OpenCTI & OpenAEV")}
          </span>
        </div>
        {footer}
      </div>

      {children}
    </div>
  );
}
