/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

import { Component, useEffect, type ReactNode } from "react";
import { useAuth } from "@/hooks/useAuth";
import { useOffice } from "@/hooks/useOffice";
import { usePlatformName } from "@/hooks/usePlatformConfig";
import { useTheme } from "@/hooks/useTheme";
import { LoginScreen } from "./LoginScreen";
import { OfficeChatPanel } from "./OfficeChatPanel";

class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="app-container">
          <div className="empty-state">
            <span style={{ color: "var(--red)" }}>Something went wrong</span>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
              {this.state.error.message}
            </span>
            <button
              className="btn btn-sm"
              onClick={() => this.setState({ error: null })}
            >
              Retry
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

function AppInner() {
  const { user, loading: authLoading, error: authError, signIn, signOut } = useAuth();
  const { ready, hostApp, context, refreshContext } = useOffice();
  const { theme, setTheme } = useTheme();
  const platformName = usePlatformName();

  // Replace the brand-neutral static ``<title>`` from index.html with the
  // resolved platform name so the Office task pane and browser tab follow
  // the admin-configured branding (the static title only shows for the
  // brief window before ``/api/v1/platform/config`` resolves).
  useEffect(() => {
    if (typeof document === "undefined") return;
    if (platformName) {
      document.title = platformName;
    }
  }, [platformName]);

  if (!ready || authLoading) {
    return (
      <div className="app-container">
        <div className="loading-center">
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: 12,
            }}
          >
            <div className="spinner" />
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
              {!ready ? "Initializing Office..." : "Loading..."}
            </span>
          </div>
        </div>
      </div>
    );
  }

  if (!user) {
    return (
      <div className="app-container">
        <LoginScreen onLogin={signIn} error={authError} theme={theme} />
      </div>
    );
  }

  return (
    <div className="app-container">
      <OfficeChatPanel
        user={user}
        hostApp={hostApp}
        context={context}
        onRefreshContext={refreshContext}
        theme={theme}
        onToggleTheme={() => setTheme(theme === "dark" ? "light" : "dark")}
        onSignOut={signOut}
      />
    </div>
  );
}

export function App() {
  return (
    <ErrorBoundary>
      <AppInner />
    </ErrorBoundary>
  );
}
