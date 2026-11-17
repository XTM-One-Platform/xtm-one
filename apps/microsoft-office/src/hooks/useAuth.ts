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

import { useState, useEffect, useCallback } from "react";
import { login, getMe, getToken, clearToken, setBaseUrl } from "@/lib/api";
import type { User } from "@/lib/types";

export function useAuth() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const checkAuth = useCallback(async () => {
    const token = getToken();
    if (!token) {
      setLoading(false);
      return;
    }
    try {
      const me = await getMe();
      setUser(me);
      setError(null);
    } catch {
      clearToken();
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  const signIn = useCallback(
    async (email: string, password: string, baseUrl: string) => {
      setError(null);
      try {
        setBaseUrl(baseUrl);
        await login(email, password, baseUrl);
        const me = await getMe();
        setUser(me);
        setError(null);
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Login failed";
        setError(msg);
        throw err;
      }
    },
    [],
  );

  /** Sign out; `reason` is what the sign-in screen then says (a session the server ended). */
  const signOut = useCallback((reason?: string) => {
    clearToken();
    setUser(null);
    setError(reason ?? null);
  }, []);

  return { user, loading, error, signIn, signOut };
}
