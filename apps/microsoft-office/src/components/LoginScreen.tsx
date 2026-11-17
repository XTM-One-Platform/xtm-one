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

import { useState } from "react";
import { Eye, EyeOff } from "lucide-react";
import { usePlatformName } from "@/hooks/usePlatformConfig";
import type { Theme } from "@/lib/types";

const hosted =
  typeof window !== "undefined" &&
  window.location.pathname.startsWith("/office");

interface LoginScreenProps {
  onLogin: (email: string, password: string, baseUrl: string) => Promise<void>;
  error: string | null;
  theme: Theme;
}

export function LoginScreen({ onLogin, error, theme }: LoginScreenProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [baseUrl, setBaseUrl] = useState(
    hosted ? window.location.origin : "http://localhost:8000",
  );
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  // Resolve branding from whichever instance the user is about to sign
  // in to — the editable ``baseUrl`` is exposed in the form for the
  // non-hosted (development) flow, so reactively refetch the public
  // platform config whenever it changes.  The same admin-customisable
  // ``platform_name`` that the main frontend / widget show is mirrored
  // here.
  const platformName = usePlatformName(baseUrl);

  const assetBase = hosted ? "/office" : "";
  const emblemSrc =
    theme === "dark"
      ? `${assetBase}/assets/logos/emblem-dark.svg`
      : `${assetBase}/assets/logos/emblem-light.svg`;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email || !password || !baseUrl) return;
    setLoading(true);
    try {
      await onLogin(email, password, baseUrl);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-container">
      <div className="login-logo">
        <img
          src={emblemSrc}
          alt={platformName}
          style={{ height: 48, width: 48 }}
        />
        <span className="login-logo-text">{platformName}</span>
        <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
          Office Add-in
        </span>
      </div>

      <form className="login-form" onSubmit={handleSubmit}>
        {error && <div className="login-error">{error}</div>}

        {!hosted && (
          <div className="field">
            <label className="input-label">Instance URL</label>
            <input
              className="input"
              type="url"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="http://localhost:8000"
            />
          </div>
        )}

        <div className="field">
          <label className="input-label">Email</label>
          <input
            className="input"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@company.com"
            autoComplete="email"
          />
        </div>

        <div className="field">
          <label className="input-label">Password</label>
          <div style={{ position: "relative" }}>
            <input
              className="input"
              type={showPassword ? "text" : "password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              autoComplete="current-password"
              style={{ paddingRight: 36 }}
            />
            <button
              type="button"
              className="btn-icon"
              onClick={() => setShowPassword(!showPassword)}
              style={{
                position: "absolute",
                right: 4,
                top: "50%",
                transform: "translateY(-50%)",
              }}
            >
              {showPassword ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
        </div>

        <button
          className="btn btn-primary"
          type="submit"
          disabled={loading || !email || !password}
          style={{ width: "100%", marginTop: 4 }}
        >
          {loading ? <span className="spinner" /> : "Sign In"}
        </button>
      </form>
    </div>
  );
}
