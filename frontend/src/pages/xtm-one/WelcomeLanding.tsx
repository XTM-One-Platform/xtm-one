// SPDX-License-Identifier: Apache-2.0

import { usePlatformLogoUrl } from "@/hooks/usePlatformConfig";
import { Shield, Eye, Crosshair, BarChart3, Rocket } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useFormatter } from "@/i18n";
import { useAuth } from "@/hooks/useAuth";
import { useAuthConfig } from "@/hooks/api/useAuthApi";
import { useRegistrationToken } from "@/hooks/api/usePlatformApi";
import { useEnterpriseLocked } from "@/hooks/useEnterpriseUpsell";
import { EnterpriseCtaButtons } from "@/components/ui/enterprise-upsell";
import { platformOrigin, withBasePath } from "@/lib/base-path";
import { DashboardNotices } from "./DashboardNotices";
import { PlatformConnectInstructions } from "./ctem/PlatformConnectInstructions";
import { RegistrationTokenPanel } from "./ctem/RegistrationTokenPanel";
import { connectSnippetToken } from "./ctem/registration-token";

const CTEM_STAGES = [
  { label: "Scoping", icon: Eye, desc: "Define what matters" },
  { label: "Discovery", icon: Crosshair, desc: "Identify threats" },
  { label: "Prioritization", icon: BarChart3, desc: "Rank by risk" },
  { label: "Validation", icon: Shield, desc: "Test defenses" },
  { label: "Mobilization", icon: Rocket, desc: "Take action" },
];

const FLOW_W = 900;
const FLOW_H = 220;

function CtemFlowVisualization() {
  const { t_i18n } = useFormatter();
  const cx = FLOW_W / 2;
  const cy = FLOW_H / 2;
  const spread = 280;

  const nodes = [
    { id: "opencti", x: cx - spread, y: cy, label: "OpenCTI", logo: withBasePath("/logos/opencti.svg") },
    { id: "xtm", x: cx, y: cy, label: "XTM One", logo: withBasePath("/xtm-one-emblem.svg"), isHub: true },
    { id: "openaev", x: cx + spread, y: cy, label: "OpenAEV", logo: withBasePath("/logos/openaev.svg") },
  ];

  const edges = [
    { from: nodes[0], to: nodes[1] },
    { from: nodes[1], to: nodes[2] },
  ];

  return (
    <svg viewBox={`0 0 ${FLOW_W} ${FLOW_H}`} className="w-full max-w-2xl mx-auto" preserveAspectRatio="xMidYMid meet">
      <style>{`
        @keyframes wl-dash{to{stroke-dashoffset:-12}}
        @keyframes wl-particle{0%{offset-distance:0%}100%{offset-distance:100%}}
        @keyframes wl-pulse-ring{0%{r:22;opacity:0.25}100%{r:50;opacity:0}}
      `}</style>
      <defs>
        <radialGradient id="wl-hub-glow">
          <stop offset="0%" stopColor="var(--color-accent)" stopOpacity="0.20" />
          <stop offset="50%" stopColor="var(--color-accent)" stopOpacity="0.05" />
          <stop offset="100%" stopColor="var(--color-accent)" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="wl-node-glow">
          <stop offset="0%" stopColor="var(--color-accent)" stopOpacity="0.12" />
          <stop offset="100%" stopColor="var(--color-accent)" stopOpacity="0" />
        </radialGradient>
        <filter id="wl-glow">
          <feGaussianBlur stdDeviation="3" result="blur" />
          <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>
      </defs>

      <pattern id="wl-grid" width="40" height="40" patternUnits="userSpaceOnUse">
        <path d="M 40 0 L 0 0 0 40" fill="none" stroke="var(--border-elevation-subtle)" strokeWidth="0.3" opacity="0.3" />
      </pattern>
      <rect width={FLOW_W} height={FLOW_H} fill="url(#wl-grid)" opacity="0.4" />

      {/* Pulse rings on hub */}
      {[0, 1.5, 3].map((delay, i) => (
        <circle key={i} cx={cx} cy={cy} r="22" fill="none" stroke="var(--color-accent)" strokeWidth="0.5" opacity="0">
          <animate attributeName="r" values="22;55;22" dur="4.5s" begin={`${delay}s`} repeatCount="indefinite" />
          <animate attributeName="opacity" values="0.20;0;0.20" dur="4.5s" begin={`${delay}s`} repeatCount="indefinite" />
        </circle>
      ))}

      {/* Edges */}
      {edges.map((edge, i) => {
        const pathId = `wl-edge-${i}`;
        const d = `M ${edge.from.x} ${edge.from.y} L ${edge.to.x} ${edge.to.y}`;
        return (
          <g key={i}>
            <path id={pathId} d={d} fill="none" stroke="var(--color-accent)" strokeWidth="0.8" opacity="0.15" strokeDasharray="6 4"
              style={{ animation: "wl-dash 4s linear infinite" }} />
            <circle r="2.5" fill="var(--color-accent)" opacity="0" filter="url(#wl-glow)">
              <animateMotion dur={`${3 + i * 0.5}s`} repeatCount="indefinite" begin={`${i * 1.2}s`}>
                <mpath xlinkHref={`#${pathId}`} />
              </animateMotion>
              <animate attributeName="opacity" values="0.7;0.3;0.7" dur="2s" repeatCount="indefinite" begin={`${i * 1.2}s`} />
            </circle>
            <circle r="1.5" fill="var(--color-accent)" opacity="0">
              <animateMotion dur={`${4 + i * 0.5}s`} repeatCount="indefinite" begin={`${1.5 + i * 0.8}s`}>
                <mpath xlinkHref={`#${pathId}`} />
              </animateMotion>
              <animate attributeName="opacity" from="0" to="0.5" dur="0.01s" begin={`${1.5 + i * 0.8}s`} fill="freeze" />
            </circle>
          </g>
        );
      })}

      {/* Floating ambient particles */}
      {[
        { x: 150, y: 50, dx: 20, dy: 12, dur: 12 },
        { x: 750, y: 170, dx: 18, dy: 10, dur: 14 },
        { x: 400, y: 40, dx: 25, dy: 8, dur: 16 },
        { x: 550, y: 180, dx: 15, dy: 14, dur: 11 },
      ].map((p, i) => (
        <circle key={`amb-${i}`} r="0.8" fill="var(--color-accent)" opacity="0.08">
          <animate attributeName="cx" values={`${p.x};${p.x + p.dx};${p.x - p.dx * 0.5};${p.x}`} dur={`${p.dur}s`} repeatCount="indefinite" />
          <animate attributeName="cy" values={`${p.y};${p.y - p.dy};${p.y + p.dy};${p.y}`} dur={`${p.dur * 1.2}s`} repeatCount="indefinite" />
        </circle>
      ))}

      {/* Nodes */}
      {nodes.map((node) => (
        <g key={node.id}>
          <circle cx={node.x} cy={node.y} r={node.isHub ? "38" : "28"} fill={node.isHub ? "url(#wl-hub-glow)" : "url(#wl-node-glow)"} />
          <circle cx={node.x} cy={node.y} r={node.isHub ? "22" : "18"} fill="var(--bg-elevation-default)"
            stroke="var(--color-accent)" strokeWidth={node.isHub ? "1.2" : "0.8"} strokeDasharray={node.isHub ? "none" : "3 3"} opacity={node.isHub ? 1 : 0.6} />
          <image href={node.logo} x={node.x - (node.isHub ? 12 : 10)} y={node.y - (node.isHub ? 12 : 10)}
            width={node.isHub ? 24 : 20} height={node.isHub ? 24 : 20} opacity={node.isHub ? 1 : 0.5} />
          <text x={node.x} y={node.y + (node.isHub ? 32 : 28)} textAnchor="middle" fill="white"
            fontSize={node.isHub ? "10" : "9"} fontWeight={node.isHub ? "600" : "500"}
            fontFamily="'IBM Plex Sans', sans-serif" opacity={node.isHub ? 0.9 : 0.5}>
            {node.label}
          </text>
          {!node.isHub && (
            <text x={node.x} y={node.y + (node.isHub ? 44 : 40)} textAnchor="middle" fill="var(--color-accent)"
              fontSize="7" fontFamily="'IBM Plex Sans', sans-serif" opacity="0.4">
              {t_i18n("waiting to connect...")}
            </text>
          )}
        </g>
      ))}

      {/* Direction arrows at midpoints */}
      {edges.map((edge, i) => {
        const midX = (edge.from.x + edge.to.x) / 2;
        const midY = edge.from.y;
        return (
          <g key={`arrow-${i}`} opacity="0.3">
            <path d={`M ${midX - 4} ${midY - 4} L ${midX + 4} ${midY} L ${midX - 4} ${midY + 4}`}
              fill="none" stroke="var(--color-accent)" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
          </g>
        );
      })}
    </svg>
  );
}

export function WelcomeLanding() {
  const logoSrc = usePlatformLogoUrl();
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const unlicensed = useEnterpriseLocked();
  // The token endpoint is admin-only; only fetch it for admins. The connect
  // snippets embed it only when it is one of the operator's own, a neutral
  // placeholder otherwise (never the published default, never "loading...").
  const { data: tokenData, isLoading: tokenLoading } = useRegistrationToken(isAdmin);
  const { data: authConfig } = useAuthConfig();
  const registrationToken = connectSnippetToken(isAdmin ? tokenData : undefined);
  const platformUrl = authConfig?.base_url ?? platformOrigin();

  return (
    <div className="space-y-10 animate-fade-in">
      {/* Hero */}
      <div className="text-center space-y-3 pt-4">
        <img src={logoSrc} alt="XTM One" className="h-14 mx-auto" />
        <p className="text-lg text-default-secondary max-w-xl mx-auto">
          {t_i18n("Unified Agentic AI for Continuous Threat Exposure Management")}
        </p>
        <p className="content-base text-default-secondary max-w-md mx-auto">
          {unlicensed
            ? t_i18n("Connect your OpenCTI and OpenAEV instances to build a unified CTEM pipeline. An Enterprise Edition platform activates AI here as it connects.")
            : t_i18n("Connect your OpenCTI and OpenAEV instances to build a unified CTEM pipeline.")}
        </p>
        {/* Community Edition: the trial is the primary call to action, before
            any setup - a person who connects a CE platform and configures a
            provider is still fully blocked at the end of it. */}
        {unlicensed && <EnterpriseCtaButtons className="justify-center pt-2" />}
      </div>

      {/* The unified notices region: on Community Edition it leads with the
          upgrade path and keeps the model / provider warnings silent (they are
          a setup for an edition that cannot use it); on Enterprise Edition it
          shows exactly those setup steps. */}
      <DashboardNotices />

      {/* Animated flow visualization */}
      <div className="relative overflow-hidden rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default">
        <CtemFlowVisualization />
      </div>

      {/* Connect cards */}
      <div className="grid md:grid-cols-2 gap-6 max-w-4xl mx-auto">
        {/* OpenCTI */}
        <Card className="border-elevation-subtle-soft hover:border-accent/30 transition-all duration-300 hover:shadow-[0_0_30px_color-mix(in_srgb,_var(--color-accent)_calc(0.08_*_100%),_transparent)] overflow-hidden">
          <CardHeader>
            <CardTitle className="flex items-center gap-3">
              <img src={withBasePath("/logos/opencti.svg")} alt="OpenCTI" className="h-8 w-8" />
              {t_i18n("Connect OpenCTI")}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4 content-base text-default-secondary">
            <p>{t_i18n("Threat Intelligence Platform: connect your OpenCTI instance to enable threat landscape monitoring, ATT&CK coverage analysis, and AI-powered threat analysis.")}</p>

            <PlatformConnectInstructions product="opencti" platformUrl={platformUrl} token={registrationToken} />
          </CardContent>
        </Card>

        {/* OpenAEV */}
        <Card className="border-elevation-subtle-soft hover:border-accent/30 transition-all duration-300 hover:shadow-[0_0_30px_color-mix(in_srgb,_var(--color-accent)_calc(0.08_*_100%),_transparent)] overflow-hidden">
          <CardHeader>
            <CardTitle className="flex items-center gap-3">
              <img src={withBasePath("/logos/openaev.svg")} alt="OpenAEV" className="h-8 w-8" />
              {t_i18n("Connect OpenAEV")}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4 content-base text-default-secondary">
            <p>{t_i18n("Adversarial Exposure Validation: connect your OpenAEV instance to enable scenario simulation, validation scoring, and continuous testing of your defenses.")}</p>

            <PlatformConnectInstructions product="openaev" platformUrl={platformUrl} token={registrationToken} />
          </CardContent>
        </Card>
      </div>

      {/* Registration token */}
      {isAdmin && (
        <div className="max-w-lg mx-auto">
          <RegistrationTokenPanel info={tokenData} isLoading={tokenLoading} />
        </div>
      )}

      {/* CTEM stages */}
      <div className="max-w-3xl mx-auto space-y-5">
        <h2 className="text-center content-compact font-semibold text-default-secondary uppercase tracking-widest">
          {t_i18n("What is CTEM?")}
        </h2>
        <div className="grid grid-cols-5 gap-3">
          {CTEM_STAGES.map((stage, i) => (
            <div key={stage.label} className="relative flex flex-col items-center gap-2 text-center group">
              <div className="relative">
                <div className="h-11 w-11 rounded-full bg-accent/[0.06] border border-accent/10 flex items-center justify-center group-hover:bg-accent/10 group-hover:border-accent/25 transition-all duration-300">
                  <stage.icon className="h-5 w-5 text-accent/70 group-hover:text-accent transition-colors" />
                </div>
                {i < CTEM_STAGES.length - 1 && (
                  <div className="absolute top-1/2 -right-3 w-3 h-px bg-[var(--border-elevation-subtle)] hidden md:block" />
                )}
              </div>
              <span className="content-compact font-medium text-default-secondary">{t_i18n(stage.label)}</span>
              <span className="content-caption text-default-secondary leading-snug">{t_i18n(stage.desc)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
