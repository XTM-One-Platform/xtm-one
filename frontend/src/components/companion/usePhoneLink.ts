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

import { useCallback, useEffect, useState } from "react";

import { useLinkedPhones, useRefreshPhones } from "@/hooks/api/useMobileDeviceApi";
import { currentSubscription, endpointHash, linkThisPhone, pushState, type PushState } from "@/lib/push";

/** Whether this phone receives notifications, and the tap that links it. */
export function usePhoneLink(): {
  state: PushState;
  linked: boolean;
  loading: boolean;
  busy: boolean;
  failed: boolean;
  link: () => Promise<void>;
} {
  const [hash, setHash] = useState<string | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [denied, setDenied] = useState(false);
  const phones = useLinkedPhones(hash ?? null);
  const refresh = useRefreshPhones();

  useEffect(() => {
    let live = true;
    currentSubscription()
      .then(async (subscription) => {
        const value = subscription ? await endpointHash(subscription.endpoint) : null;
        if (live) setHash(value);
      })
      .catch(() => live && setHash(null));
    return () => {
      live = false;
    };
  }, []);

  const link = useCallback(async () => {
    setBusy(true);
    setFailed(false);
    try {
      if ((await linkThisPhone()) === "denied") {
        setDenied(true);
        return;
      }
      const subscription = await currentSubscription();
      setHash(subscription ? await endpointHash(subscription.endpoint) : null);
      await refresh();
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }, [refresh]);

  const linked = !!hash && !!phones.data?.some((phone) => phone.current && phone.push_enabled);
  return {
    state: denied ? "denied" : pushState(),
    linked,
    loading: hash === undefined || phones.isLoading,
    busy,
    failed,
    link,
  };
}
