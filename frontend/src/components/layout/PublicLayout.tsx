// SPDX-License-Identifier: Apache-2.0

import { Bot, Boxes, ChevronRight, FileText, Link2, Sparkles, Wrench } from "lucide-react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { usePlatformName, usePlatformLogoUrl } from "@/hooks/usePlatformConfig";

const NAV_ITEMS = [
  { to: "/catalog", label: "All", icon: Sparkles, end: true },
  { to: "/catalog/agents", label: "Agents", icon: Bot },
  { to: "/catalog/skills", label: "Skills", icon: Boxes },
  { to: "/catalog/prompts", label: "Prompts", icon: FileText },
  { to: "/catalog/mcp-servers", label: "MCP Servers", icon: Link2 },
  { to: "/catalog/tools", label: "Tools", icon: Wrench },
];

export default function PublicLayout() {
  const location = useLocation();
  const isHome = location.pathname === "/catalog" || location.pathname === "/catalog/";
  const platformName = usePlatformName();
  const logoSrc = usePlatformLogoUrl();

  return (
    <div className="relative min-h-screen bg-elevation-default text-default-primary">
      {/* Ambient background glow — only on home */}
      {isHome && (
        <div className="pointer-events-none fixed inset-0 overflow-hidden">
          <div className="absolute -top-40 left-1/2 -translate-x-1/2 h-[500px] w-[800px] rounded-full bg-accent/[0.04] blur-[120px]" />
        </div>
      )}

      {/* Top nav bar */}
      <header className="sticky top-0 z-50 border-b border-elevation-subtle-soft bg-elevation-default backdrop-blur-xl">
        <div className="mx-auto flex h-16 max-w-7xl items-center gap-5 px-4 sm:px-6">
          {/* Branding */}
          <Link to="/catalog" className="flex items-center gap-2.5 shrink-0 group">
            <img
              src={logoSrc}
              alt={platformName}
              className="h-7 transition-opacity group-hover:opacity-80"
            />
          </Link>

          {/* Separator */}
          <div className="h-6 w-px bg-elevation-hover shrink-0 hidden sm:block" />

          {/* Nav tabs */}
          <nav className="flex items-center gap-0.5 overflow-x-auto scrollbar-none">
            {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
              <NavLink
                key={to}
                to={to}
                end={end}
                className={({ isActive }) =>
                  cn(
                    "relative flex items-center gap-1.5 rounded-sm px-3 py-1.5 text-[13px] font-medium transition-all whitespace-nowrap",
                    isActive
                      ? "text-default-primary"
                      : "text-default-secondary hover:text-default-primary",
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    <Icon className={cn("h-3.5 w-3.5 shrink-0", isActive && "text-accent")} />
                    <span>{label}</span>
                    {isActive && (
                      <span className="absolute inset-x-1 -bottom-[11px] h-[2px] rounded-full bg-accent" />
                    )}
                  </>
                )}
              </NavLink>
            ))}
          </nav>

          {/* Right actions */}
          <div className="ml-auto flex items-center gap-2 shrink-0">
            <Button variant="ghost" size="sm" asChild>
              <Link to="/" className="gap-1.5">
                Back to app
                <ChevronRight className="h-3 w-3" />
              </Link>
            </Button>
          </div>
        </div>
      </header>

      {/* Content */}
      <main className="mx-auto max-w-7xl px-4 sm:px-6 py-8">
        <Outlet />
      </main>

      {/* Footer */}
      <footer className="border-t border-elevation-subtle-soft py-8">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 flex items-center justify-center">
          <p className="content-compact text-default-secondary">
            Shared with love by{" "}
            <a
              href="https://filigran.io"
              target="_blank"
              rel="noopener noreferrer"
              className="text-default-secondary hover:text-accent transition-colors"
            >
              Filigran
            </a>
          </p>
        </div>
      </footer>
    </div>
  );
}
