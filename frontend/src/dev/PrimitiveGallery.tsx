/**
 * Visual reference for every styling axis the design-system migration touches,
 * on one offline page: surfaces, the text ladder, the brand accent, the
 * feedback palette, and the local primitives that library components replace.
 *
 * It exists to be screenshotted before and after the migration. A diff of two
 * captures of this page is the only evidence that says whether a token mapping
 * preserved what it claimed to preserve — green types and green lint say
 * nothing about a surface that lost its contrast.
 *
 * Dev-only: reachable at /gallery.html, and not part of the production bundle.
 */
import { useState } from "react";
import { Bot, Gauge, Plus, Search, Settings, Sparkles } from "lucide-react";
import {
  Header as HeaderBar,
  HeaderGroup,
  Navbar,
  NavbarItem,
  NavbarSeparator,
  NavbarSubmenu,
  NavbarSubmenuItem, IconButton } from "@filigran/design-system";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";
import { Tip, TooltipProvider } from "@/components/ui/tooltip";
import { EmptyState } from "@/components/ui/empty-state";
import { SearchInput } from "@/components/ui/search-input";
import { StatCard } from "@/components/ui/stat-card";
import { TagList } from "@/components/ui/tag-list";
import { agentSelectOption } from "@/components/ui/entity-select-options";
import { ModelSelect } from "@/components/ui/model-select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast, type ToastVariant } from "@/components/ui/toast";
import { applyTheme } from "@/hooks/useThemeEffect";
import { PROVIDER_DISPLAY_NAMES } from "@/lib/constants";
import type { AvailableModel, LLMProvider } from "@/types";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-3">
      <h2 className="font-sans-geologica content-base font-bold uppercase tracking-wide text-default-secondary">
        {title}
      </h2>
      <div className="rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-4">{children}</div>
    </section>
  );
}

function Swatch({ className, label }: { className: string; label: string }) {
  return (
    <div className="space-y-1">
      <div className={`h-12 rounded-sm border border-elevation-subtle ${className}`} />
      <p className="font-mono-plex content-caption text-default-secondary">{label}</p>
    </div>
  );
}

const GALLERY_AGENTS = [
  "[Sam] General Assistant",
  "'HOLE FINDER' Pipeline Executive Assistant",
  "[EMT & ExCo] Executive Assistant",
  "Threat Intel Analyst",
  "Agent 10",
  "Agent 2",
  "Équipe Rouge Planner",
  "Board Deck Writer",
  "SOC Triage",
].map((name, index) => ({ id: `agent-${index}`, name, icon: index % 2 ? "Shield" : "Bot" }));

const GALLERY_PROVIDERS = [
  { id: "p-openai", provider: "openai", display_name: null },
  { id: "p-anthropic", provider: "anthropic", display_name: null },
  { id: "p-gateway", provider: "litellm", display_name: "Corp gateway" },
] as unknown as LLMProvider[];

const GALLERY_MODELS: AvailableModel[] = [
  { model: "gpt-5.1", provider: "openai", provider_id: "p-openai" },
  { model: "gpt-4o", provider: "openai", provider_id: "p-openai" },
  { model: "claude-sonnet-5", provider: "anthropic", provider_id: "p-anthropic" },
  { model: "claude-haiku-4", provider: "anthropic", provider_id: "p-anthropic" },
  { model: "gpt-4o", provider: "litellm", provider_id: "p-gateway" },
];

const GALLERY_TOASTS: Record<ToastVariant, { title: string; description?: string }> = {
  default: { title: "Copied to clipboard" },
  success: { title: "Agent updated", description: "The new persona applies to the next conversation." },
  error: { title: "Could not save", description: "The server refused the change." },
  info: {
    title: "A new version is available",
    description: "This tab still runs the previous version. Reload it to use the new one.",
  },
};

export function PrimitiveGallery() {
  const [mode, setMode] = useState<"dark" | "light">("dark");
  const [on, setOn] = useState(true);
  const [provider, setProvider] = useState("gemini");
  const [agent, setAgent] = useState("agent-0");
  const [model, setModel] = useState("__none__");
  const { toast } = useToast();

  // Goes through the app's own theme function rather than setting the attribute
  // directly, so the gallery exercises the real bridge: the attribute the app
  // reads AND the class the design system themes through. Setting only the
  // attribute here is what made an earlier capture of "light" mode render dark.
  const setTheme = (next: "dark" | "light") => {
    setMode(next);
    applyTheme(next);
  };

  return (
    <TooltipProvider>
    <main className="min-h-screen bg-elevation-default p-8">
      <header className="mb-8 flex items-center justify-between">
        <div>
          <h1 className="font-sans-geologica text-3xl font-bold text-default-primary">Primitive gallery</h1>
          <p className="content-base text-default-secondary">
            Visual reference for the design-system migration.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant={mode === "dark" ? "default" : "outline"} size="sm" onClick={() => setTheme("dark")}>
            Dark
          </Button>
          <Button variant={mode === "light" ? "default" : "outline"} size="sm" onClick={() => setTheme("light")}>
            Light
          </Button>
        </div>
      </header>

      <div className="grid grid-cols-1 gap-8 lg:grid-cols-2">
        <Section title="Surfaces">
          <div className="grid grid-cols-4 gap-3">
            <Swatch className="bg-elevation-default" label="bg-elevation-default" />
            <Swatch className="layer-1 bg-elevation-default" label="layer-1 bg-elevation-default" />
            <Swatch className="layer-2 bg-elevation-default" label="layer-2 bg-elevation-default" />
            <Swatch className="bg-elevation-highlight" label="bg-elevation-highlight" />
            <Swatch className="bg-elevation-highlight" label="bg-elevation-highlight" />
            <Swatch className="bg-elevation-hover" label="bg-elevation-hover" />
            <Swatch className="bg-elevation-hover" label="bg-elevation-hover" />
            <Swatch className="bg-elevation-hover" label="bg-elevation-hover" />
          </div>
        </Section>

        <Section title="Brand accent and feedback">
          <div className="grid grid-cols-4 gap-3">
            <Swatch className="bg-accent" label="bg-accent" />
            <Swatch className="bg-accent/20" label="bg-accent/20" />
            <Swatch className="bg-accent/10" label="bg-accent/10" />
            <Swatch className="bg-feedback-error-primary" label="bg-feedback-error-primary" />
            <Swatch className="bg-feedback-success-primary" label="bg-feedback-success-primary" />
            <Swatch className="bg-feedback-alert-primary" label="bg-feedback-alert-primary" />
            <Swatch className="bg-filigran-ia-primary" label="bg-filigran-ia-primary" />
            <Swatch className="bg-feedback-info-primary" label="bg-feedback-info-primary" />
          </div>
        </Section>

        <Section title="Text ladder">
          <div className="space-y-1">
            <p className="font-sans-geologica text-2xl font-bold text-default-primary">Heading — font-sans-geologica bold</p>
            <p className="text-base text-default-primary">content-base / text-default-primary</p>
            <p className="content-base text-default-secondary">content-base / text-default-secondary</p>
            <p className="content-base text-default-secondary">content-base / text-default-secondary</p>
            <p className="content-compact text-default-secondary">content-compact / text-default-secondary</p>
            <p className="content-compact text-default-secondary">content-compact / text-default-secondary</p>
            <p className="content-compact text-default-disabled">content-compact / text-default-disabled</p>
            <p className="font-mono-plex content-compact text-accent">font-mono-plex text-accent</p>
            <p className="content-compact text-feedback-warning-primary">text-feedback-warning-primary</p>
            <p className="content-compact text-feedback-error-primary">text-feedback-error-primary</p>
            <p className="content-compact text-feedback-success-primary">text-feedback-success-primary</p>
            <p className="content-compact text-filigran-ia-primary">text-filigran-ia-primary</p>
          </div>
        </Section>

        <Section title="Borders">
          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-sm border border-elevation-subtle p-3 content-compact text-default-secondary">border-elevation-subtle</div>
            <div className="rounded-sm border border-elevation-subtle-soft p-3 content-compact text-default-secondary">border-elevation-subtle-soft</div>
            <div className="rounded-sm border border-elevation-subtle-soft p-3 content-compact text-default-secondary">border-elevation-subtle-soft</div>
            <div className="rounded-sm border border-accent/30 p-3 content-compact text-default-secondary">border-accent/30</div>
          </div>
        </Section>

        <Section title="Button">
          <div className="space-y-3">
            {(["default", "secondary", "outline", "ghost", "destructive", "link"] as const).map((variant) => (
              <div key={variant} className="flex items-center gap-3">
                <span className="w-20 font-mono-plex content-caption text-default-secondary">{variant}</span>
                <Button variant={variant} size="sm">Small</Button>
                <Button variant={variant}>Default</Button>
                <Button variant={variant} size="lg">Large</Button>
                <IconButton variant="default" priority="primary" aria-label="icon" icon={<Plus className="h-4 w-4" />} />
                <Button variant={variant} disabled>Disabled</Button>
              </div>
            ))}
          </div>
        </Section>

        <Section title="Toast">
          <div className="flex flex-wrap gap-2">
            {(Object.keys(GALLERY_TOASTS) as ToastVariant[]).map((variant) => (
              <Button
                key={variant}
                variant="outline"
                size="sm"
                onClick={() =>
                  toast({
                    ...GALLERY_TOASTS[variant],
                    variant,
                    durationMs: 60_000,
                    ...(variant === "info" && { action: { label: "Reload", onClick: () => {} } }),
                  })
                }
              >
                {variant}
              </Button>
            ))}
          </div>
        </Section>

        <Section title="Badge">
          <div className="flex flex-wrap gap-2">
            {(["default", "secondary", "outline", "success", "warning", "destructive", "cyan"] as const).map((variant) => (
              <Badge key={variant} variant={variant}>{variant}</Badge>
            ))}
          </div>
        </Section>

        <Section title="Form controls">
          <div className="space-y-4">
            <div className="space-y-1">
              <Label htmlFor="g-name">Label</Label>
              <Input aria-label="Label" id="g-name" placeholder="Placeholder text" />
            </div>
            <div className="space-y-1">
              <Label htmlFor="g-disabled">Disabled</Label>
              <Input aria-label="Disabled" id="g-disabled" placeholder="Disabled" disabled />
            </div>
            <SearchInput value="" onChange={() => {}} />
            <div className="flex items-center gap-3">
              <Switch checked={on} onCheckedChange={setOn} id="g-switch" />
              <Label htmlFor="g-switch">Switch</Label>
            </div>
            <Tip content="Tooltip content">
              <span className="content-compact text-accent underline decoration-dotted">hover for a tooltip</span>
            </Tip>
          </div>
        </Section>

        <Section title="Searchable select">
          <div className="space-y-4">
            <SearchableSelect
              aria-label="Provider"
              options={Object.keys(PROVIDER_DISPLAY_NAMES).map((key) => ({ value: key, label: PROVIDER_DISPLAY_NAMES[key], keywords: [key] }))}
              value={provider}
              onValueChange={setProvider}
            />
            <SearchableSelect
              aria-label="Agent"
              options={GALLERY_AGENTS.map((each) => agentSelectOption(each))}
              pinned={[{ value: "__general__", label: "General Assistant", icon: <Sparkles className="h-3.5 w-3.5 text-accent" /> }]}
              value={agent}
              onValueChange={setAgent}
            />
            <ModelSelect
              aria-label="Model"
              models={GALLERY_MODELS}
              providers={GALLERY_PROVIDERS}
              pinned={[{ value: "__none__", label: "None" }]}
              value={model}
              onValueChange={setModel}
            />
            <SearchableSelect
              aria-label="Disabled"
              options={[{ value: "a", label: "Disabled picker" }]}
              value="a"
              onValueChange={() => {}}
              disabled
            />
          </div>
        </Section>

        <div className="lg:col-span-2">
        <Section title="Shell — library Navbar and Header">
          {/* Rendered from static items rather than the app's own Sidebar: the
              point of this artboard is whether the LIBRARY components paint
              correctly in this app's token context — the gradient surface, the
              elevation layers, the fonts — which is what breaks when a host
              wires the tokens wrong. The real adapter is covered by
              Sidebar.test.tsx instead, which needs the app's hooks. */}
          <div className="relative flex h-96 overflow-hidden rounded-sm border border-elevation-subtle">
            <Navbar aria-label="Gallery navigation" defaultCollapsed={false}>
              <NavbarItem icon={<Bot className="size-4" />}>Agents</NavbarItem>
              <NavbarItem icon={<Sparkles className="size-4" />}>Chat</NavbarItem>
              <NavbarSeparator />
              <NavbarSubmenu label="Observability" icon={<Gauge className="size-4" />} defaultOpen>
                <NavbarSubmenuItem>Traces</NavbarSubmenuItem>
                <NavbarSubmenuItem>Analytics</NavbarSubmenuItem>
              </NavbarSubmenu>
              <NavbarItem icon={<Settings className="size-4" />}>Settings</NavbarItem>
            </Navbar>
            <div className="flex min-w-0 flex-1 flex-col">
              <HeaderBar>
                <HeaderGroup>
                  <span className="content-compact text-default-secondary">Search…</span>
                </HeaderGroup>
                <HeaderGroup className="gap-2">
                  <Button variant="outline" size="sm">Action</Button>
                  <CountBadge variant="cyan">3</CountBadge>
                </HeaderGroup>
              </HeaderBar>
              <div className="flex-1 p-6">
                <p className="content-base text-default-secondary">
                  Content zone. The bar above owns its own 68px height, gradient
                  surface and hairline.
                </p>
              </div>
            </div>
          </div>
        </Section>
      </div>

      <Section title="Card and composites">
          <div className="space-y-4">
            <Card>
              <CardHeader>
                <CardTitle>Card title</CardTitle>
                <CardDescription>Card description in muted text.</CardDescription>
              </CardHeader>
              <CardContent className="content-base text-default-secondary">Card content.</CardContent>
            </Card>
            <div className="grid grid-cols-2 gap-3">
              <StatCard icon={Bot} label="Agents" value={12} subtitle="3 active" />
              <StatCard icon={Search} label="Searches" value={348} subtitle="last 7 days" />
            </div>
            <TagList tags={["cti", "automation", "beta"]} />
            <div className="space-y-2">
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-1/2" />
            </div>
            <EmptyState icon={Bot} title="Nothing here yet" description="An empty state as the app renders it." />
          </div>
        </Section>
      </div>
    </main>
    </TooltipProvider>
  );
}
