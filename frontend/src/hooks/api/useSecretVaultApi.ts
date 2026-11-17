// SPDX-License-Identifier: Apache-2.0

/**
 * Secret vaults: the administrators' providers under Settings > Access and security
 * > Secret vaults, and the picker every secret field opens. No answer ever
 * holds a secret value or a vault credential.
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  PickerVaultKeys,
  PickerVaultListing,
  PickerVaultProvider,
  SecretVaultConnectionReport,
  SecretVaultList,
  SecretVaultProvider,
  SecretVaultProviderInput,
  SecretVaultUse,
  VaultReferenceCheck,
} from "@/types";

const ADMIN_KEY = ["secret-vaults"] as const;
const PICKER_KEY = ["secret-vault-picker"] as const;

// --- Administrators ---

export function useSecretVaults() {
  return useQuery<SecretVaultList>({
    queryKey: ADMIN_KEY,
    queryFn: () => api.get("/settings/secret-vaults"),
  });
}

export function useSecretVaultUsage(name: string | null) {
  return useQuery<SecretVaultUse[]>({
    queryKey: [...ADMIN_KEY, "usage", name],
    queryFn: () => api.get(`/settings/secret-vaults/usage/${encodeURIComponent(name ?? "")}`),
    enabled: !!name,
  });
}

/** After any change: the list and every picker reading the providers. */
function useInvalidateVaults() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: ADMIN_KEY });
    void qc.invalidateQueries({ queryKey: PICKER_KEY });
  };
}

export function useCreateSecretVault() {
  const invalidate = useInvalidateVaults();
  return useMutation<SecretVaultProvider, Error, SecretVaultProviderInput>({
    mutationFn: (body) => api.post("/settings/secret-vaults", body),
    onSuccess: invalidate,
  });
}

export type SecretVaultUpdate = Partial<SecretVaultProviderInput> & { clear_cache_seconds?: boolean };

export function useUpdateSecretVault() {
  const invalidate = useInvalidateVaults();
  return useMutation<SecretVaultProvider, Error, { id: string; body: SecretVaultUpdate }>({
    mutationFn: ({ id, body }) => api.patch(`/settings/secret-vaults/${id}`, body),
    onSuccess: invalidate,
  });
}

export function useDeleteSecretVault() {
  const invalidate = useInvalidateVaults();
  return useMutation<void, Error, string>({
    mutationFn: (id) => api.delete(`/settings/secret-vaults/${id}`),
    onSuccess: invalidate,
  });
}

/** Test a stored provider (its status is updated), a draft of the form, or an environment provider. */
export function useTestSecretVault() {
  const invalidate = useInvalidateVaults();
  return useMutation<
    SecretVaultConnectionReport,
    Error,
    | { kind: "stored"; id: string }
    | { kind: "draft"; body: SecretVaultProviderInput & { provider_id?: string | null } }
    | { kind: "environment"; name: string }
  >({
    mutationFn: (target) => {
      if (target.kind === "stored") return api.post(`/settings/secret-vaults/${target.id}/test`);
      if (target.kind === "environment")
        return api.post(`/settings/secret-vaults/environment/${encodeURIComponent(target.name)}/test`);
      return api.post("/settings/secret-vaults/test", target.body);
    },
    onSuccess: (_report, target) => {
      if (target.kind === "stored") invalidate();
    },
  });
}

// --- The secret picker (any user) ---

export function usePickerVaults(enabled = true) {
  return useQuery<PickerVaultProvider[]>({
    queryKey: PICKER_KEY,
    queryFn: () => api.get("/secret-vaults"),
    enabled,
    staleTime: 60_000,
    // A picker inside a form: never a reason to break the form it sits in.
    throwOnError: false,
  });
}

/**
 * The names under *prefix*, a page at a time: a vault answers 25 to 100
 * names per page, so the picker follows `next_page_token` with `fetchNextPage()`.
 * A page the vault refused (`unavailable`) ends the listing.
 */
export function usePickerVaultSecrets(provider: string | null, prefix: string, enabled = true) {
  return useInfiniteQuery({
    queryKey: [...PICKER_KEY, "secrets", provider, prefix],
    queryFn: ({ pageParam }): Promise<PickerVaultListing> =>
      api.get(
        `/secret-vaults/${encodeURIComponent(provider ?? "")}/secrets?prefix=${encodeURIComponent(prefix)}` +
          (pageParam ? `&page_token=${encodeURIComponent(pageParam)}` : ""),
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last: PickerVaultListing) => (last.unavailable ? undefined : last.next_page_token || undefined),
    enabled: enabled && !!provider,
    staleTime: 30_000,
    // A refetch lists every loaded page again, each one a request to the vault.
    refetchOnWindowFocus: false,
    retry: false,
    throwOnError: false,
  });
}

/**
 * The key names of the secret at *path*. Each call reads the secret with the
 * platform's identity: the picker passes a path once it is chosen (picked, or
 * typed and left), never a path being typed.
 */
export function usePickerVaultKeys(provider: string | null, path: string | null) {
  return useQuery<PickerVaultKeys>({
    queryKey: [...PICKER_KEY, "keys", provider, path],
    queryFn: () =>
      api.get(`/secret-vaults/${encodeURIComponent(provider ?? "")}/keys?path=${encodeURIComponent(path ?? "")}`),
    enabled: !!provider && !!path,
    staleTime: 30_000,
    retry: false,
    throwOnError: false,
  });
}

export function useCheckVaultReference() {
  return useMutation<VaultReferenceCheck, Error, string>({
    mutationFn: (reference) => api.post("/secret-vaults/check", { reference }),
  });
}
