// SPDX-License-Identifier: Apache-2.0

/**
 * The secret picker: choose a vault the user may use, browse to a
 * secret, pick the key to read, check it on the server, and hand back the
 * `vault://<provider>/<path>#<key>` reference. Nothing here ever holds a value:
 * the server lists names and key names only, and a check answers OK or why.
 *
 * - `VaultSecretPicker` - the dialog every secret field and the variable
 *   insertion open.
 * - `VaultReferenceChip` - a stored or picked reference, read as the vault's
 *   name, the path and the key, with Change and Clear.
 * - `VaultTypeLogo` - a provider type's brand mark.
 */
import { useEffect, useMemo, useState } from "react";
import { Spinner } from "@filigran/design-system";
import { CheckCircle2, ChevronRight, Clock, FileKey2, Folder, KeyRound, Search, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { SearchableSelect, type SelectOption } from "@/components/ui/searchable-select";
import {
  useCheckVaultReference,
  usePickerVaultKeys,
  usePickerVaults,
  usePickerVaultSecrets,
} from "@/hooks/api/useSecretVaultApi";
import { useDebounce } from "@/hooks/useDebounce";
import { useFormatter } from "@/i18n";
import { withBasePath } from "@/lib/base-path";
import {
  VAULT_FAILURE_REASONS,
  VAULT_TYPE_LOGOS,
  formatVaultReference,
  parentPrefix,
  parseVaultReference,
  pathWithin,
  recentVaultPaths,
  rememberVaultPath,
  type VaultReference,
} from "@/lib/secret-vaults";
import { cn } from "@/lib/utils";
import type { PickerVaultEntry, SecretVaultType } from "@/types";

/** The pseudo key that reads a secret holding one value whole. */
const WHOLE_VALUE = "";

/**
 * A search over a partial listing loads the next pages by itself, up to this
 * many pages in all: a name further down is reached with Load more.
 */
const SEARCH_PAGE_LIMIT = 10;

/** How long the search waits for the typing to stop before it loads more pages. */
const SEARCH_DEBOUNCE_MS = 400;

function normalizedPath(path: string): string {
  return path.trim().replace(/^\/+|\/+$/g, "");
}

export function VaultTypeLogo({ type, className }: { type: SecretVaultType | string; className?: string }) {
  const src = VAULT_TYPE_LOGOS[type as SecretVaultType];
  if (!src) return <KeyRound className={cn("h-4 w-4 text-default-secondary", className)} aria-hidden />;
  return <img src={withBasePath(src)} alt="" aria-hidden className={cn("h-4 w-4 shrink-0 object-contain", className)} />;
}

function reasonText(t_i18n: (key: string) => string, reason: string | null | undefined, fallback?: string | null): string {
  const known = VAULT_FAILURE_REASONS[(reason ?? "") as keyof typeof VAULT_FAILURE_REASONS];
  return known ? t_i18n(known) : fallback || reason || "";
}

/** A reference shown as a chip: the vault's name, the path (when the viewer may see it) and the key. */
export function VaultReferenceChip({
  providerName,
  type,
  path,
  keyName,
  onChange,
  onClear,
  disabled,
  className,
}: {
  providerName: string;
  type?: string;
  path: string | null;
  keyName: string | null;
  onChange?: () => void;
  onClear?: () => void;
  disabled?: boolean;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div
      className={cn(
        "flex min-h-9 items-center gap-2 rounded-sm border border-accent/30 bg-accent/10 px-2.5 py-1.5",
        className,
      )}
      data-testid="vault-reference-chip"
    >
      {type ? <VaultTypeLogo type={type} /> : <KeyRound className="h-4 w-4 text-accent shrink-0" aria-hidden />}
      <div className="min-w-0 flex-1 content-compact">
        <span className="font-medium text-default-primary">{providerName}</span>
        <span className="text-default-secondary"> · </span>
        {path ? (
          <span className="font-mono-plex text-default-primary break-all">{path}</span>
        ) : (
          <span className="italic text-default-secondary">{t_i18n("a path you may not see")}</span>
        )}
        {keyName && (
          <span className="font-mono-plex text-default-secondary">
            {" "}
            #{keyName}
          </span>
        )}
      </div>
      {onChange && (
        <Button type="button" variant="ghost" size="sm" className="h-7 px-2" onClick={onChange} disabled={disabled}>
          {t_i18n("Change")}
        </Button>
      )}
      {onClear && (
        <Button type="button" variant="ghost" size="sm" className="h-7 px-2" onClick={onClear} disabled={disabled}>
          {t_i18n("Clear")}
        </Button>
      )}
    </div>
  );
}

/** The last segment of an entry's path when the vault names it otherwise (a 1Password id): what a grant or a typed path uses. */
function entryId(entry: PickerVaultEntry): string | null {
  const id = entry.path.replace(/\/+$/, "").split("/").pop() ?? "";
  return id && id !== entry.name.replace(/\/+$/, "") ? id : null;
}

function storageOrNull(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
}

export function VaultSecretPicker({
  open,
  onOpenChange,
  onPick,
  initial,
  title,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The reference picked, as stored: `vault://<provider>/<path>#<key>`. */
  onPick: (reference: string) => void;
  /** Start from this reference (Change on a stored one). */
  initial?: VaultReference | null;
  title?: string;
}) {
  const { t_i18n } = useFormatter();
  const { data: providers, isLoading: providersLoading } = usePickerVaults(open);
  const [provider, setProvider] = useState<string | null>(initial?.provider ?? null);
  const [prefix, setPrefix] = useState("");
  const [search, setSearch] = useState("");
  const [path, setPath] = useState(initial?.path ?? "");
  // The path whose keys are read: a picked one at once, a typed one when the user leaves the
  // field, presses Enter or Check - never a request per keystroke.
  const [keysPath, setKeysPath] = useState<string | null>(initial?.path ? normalizedPath(initial.path) : null);
  const [keyName, setKeyName] = useState<string>(initial?.key ?? WHOLE_VALUE);
  const [pasted, setPasted] = useState("");
  const [pasting, setPasting] = useState(false);
  const check = useCheckVaultReference();

  useEffect(() => {
    if (!open) return;
    setProvider(initial?.provider ?? null);
    setPath(initial?.path ?? "");
    setKeysPath(initial?.path ? normalizedPath(initial.path) : null);
    setKeyName(initial?.key ?? WHOLE_VALUE);
    setPrefix(initial?.path ? parentPrefix(initial.path) : "");
    setSearch("");
    setPasted("");
    setPasting(false);
    check.reset();
    // `check` is a stable mutation object; re-running on its identity would reset every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initial?.provider, initial?.path, initial?.key]);

  const chosen = useMemo(() => providers?.find((entry) => entry.name === provider) ?? null, [providers, provider]);
  useEffect(() => {
    // One vault only: it is the choice.
    if (open && !provider && providers?.length === 1) setProvider(providers[0].name);
  }, [open, provider, providers]);

  const browsable = Boolean(chosen?.browsable);
  const listing = usePickerVaultSecrets(chosen?.name ?? null, prefix, open && browsable);
  const trimmedPath = normalizedPath(path);
  const reference: VaultReference | null =
    chosen && trimmedPath ? { provider: chosen.name, path: trimmedPath, key: keyName || null } : null;
  const referenceText = reference ? formatVaultReference(reference) : "";
  const referenceValid = Boolean(reference && parseVaultReference(referenceText));
  const outsideGrant = Boolean(chosen && trimmedPath && !pathWithin(trimmedPath, chosen.paths));
  const keysReadable = referenceValid && !outsideGrant && keysPath === trimmedPath;
  const keys = usePickerVaultKeys(keysReadable ? chosen?.name ?? null : null, keysReadable ? trimmedPath : null);

  const recent = useMemo(() => (chosen ? recentVaultPaths(chosen.name, storageOrNull()) : []), [chosen]);
  const pages = listing.data?.pages;
  // Every page loaded, a name listed twice (a folder two pages both hold) shown once.
  const loaded = useMemo(() => {
    const seen = new Map<string, PickerVaultEntry>();
    for (const page of pages ?? []) for (const entry of page.entries) if (!seen.has(entry.path)) seen.set(entry.path, entry);
    return [...seen.values()];
  }, [pages]);
  const query = search.trim().toLowerCase();
  const entries = useMemo(
    () => (query ? loaded.filter((entry) => entry.name.toLowerCase().includes(query)) : loaded),
    [loaded, query],
  );
  const partial = Boolean(listing.hasNextPage);
  const pageCount = pages?.length ?? 0;
  const debouncedQuery = useDebounce(query, SEARCH_DEBOUNCE_MS);
  const { fetchNextPage, isFetchingNextPage, isFetchNextPageError } = listing;
  useEffect(() => {
    // A search covers the names loaded: over a partial list it loads the next pages, a bounded number.
    if (!debouncedQuery || !partial || isFetchingNextPage || isFetchNextPageError || pageCount >= SEARCH_PAGE_LIMIT) return;
    void fetchNextPage();
  }, [debouncedQuery, partial, isFetchingNextPage, isFetchNextPageError, pageCount, fetchNextPage]);
  const laterProblem = pages?.slice(1).find((page) => page.unavailable)?.unavailable ?? null;

  const providerOptions: SelectOption[] = (providers ?? []).map((entry) => ({
    value: entry.name,
    label: entry.display_name,
    description: entry.display_name !== entry.name ? entry.name : undefined,
    keywords: [entry.name, entry.type],
    icon: <VaultTypeLogo type={entry.type} />,
  }));

  const keyOptions: SelectOption[] = [
    ...(keys.data?.single_value || !keys.data?.keys.length ? [{ value: WHOLE_VALUE, label: t_i18n("Whole value") }] : []),
    ...(keys.data?.keys ?? []).map((name) => {
      const shown = keys.data?.labels?.[name];
      return { value: name, label: shown ?? name, description: shown ? name : undefined, keywords: [name] };
    }),
  ];

  const crumbs = prefix.replace(/\/+$/, "").split("/").filter(Boolean);

  /** Set the path; *settled* (picked, not being typed) reads its keys at once. */
  const choosePath = (next: string, settled = true) => {
    setPath(next);
    setKeysPath(settled && normalizedPath(next) ? normalizedPath(next) : null);
    setKeyName(WHOLE_VALUE);
    check.reset();
  };

  /** The typed path is done: read its keys (once, however long it took to type). */
  const settleTypedPath = () => setKeysPath(trimmedPath || null);

  const applyPasted = () => {
    const parsed = parseVaultReference(pasted);
    if (!parsed) return;
    setProvider(parsed.provider);
    choosePath(parsed.path);
    setKeyName(parsed.key ?? WHOLE_VALUE);
    setPrefix(parentPrefix(parsed.path));
    setPasting(false);
  };

  const confirm = () => {
    if (!reference || !referenceValid || outsideGrant) return;
    rememberVaultPath(reference.provider, reference.path, storageOrNull());
    onPick(referenceText);
    onOpenChange(false);
  };

  const listingProblem = pages?.[0]?.unavailable;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{title ?? t_i18n("Pick a secret from a vault")}</DialogTitle>
          <DialogDescription>
            {t_i18n(
              "Choose the vault, browse to the secret and the key to read. The platform reads the value each time the field is used; it is never shown or stored here.",
            )}
          </DialogDescription>
        </DialogHeader>

        {providersLoading ? (
          <div className="flex items-center gap-2 content-compact text-default-secondary" role="status">
            <Spinner size="sm" tone="inherit" /> {t_i18n("Loading...")}
          </div>
        ) : !providers?.length ? (
          <p className="rounded-sm border border-dashed border-elevation-subtle p-4 content-compact text-default-secondary" data-testid="vault-picker-none">
            {t_i18n(
              "No vault is available to you. An administrator connects vaults in Settings > Access and security > Secret vaults and grants them to groups or users.",
            )}
          </p>
        ) : (
          <div className="space-y-4">
            <div className="space-y-1">
              <label className="content-compact text-default-secondary" htmlFor="vault-picker-provider">
                {t_i18n("Vault")}
              </label>
              <SearchableSelect
                id="vault-picker-provider"
                aria-label={t_i18n("Vault")}
                searchable="auto"
                options={providerOptions}
                value={provider}
                onValueChange={(value) => {
                  setProvider(value);
                  setPrefix("");
                  choosePath("");
                }}
                placeholder={t_i18n("Select a vault...")}
              />
              {chosen?.paths && (
                <p className="content-caption text-default-secondary">
                  {t_i18n("You may use the paths under: {paths}", { paths: chosen.paths.join(", ") })}
                </p>
              )}
            </div>

            {chosen && (
              <>
                {browsable && !listingProblem ? (
                  <div className="rounded-sm border border-elevation-subtle">
                    <div className="flex flex-wrap items-center gap-1 border-b border-elevation-subtle px-3 py-2 content-compact">
                      <button
                        type="button"
                        className="text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus rounded-sm"
                        onClick={() => setPrefix("")}
                      >
                        {t_i18n("Top")}
                      </button>
                      {crumbs.map((crumb, index) => (
                        <span key={`${crumb}-${index}`} className="flex items-center gap-1">
                          <ChevronRight className="h-3 w-3 text-default-secondary" aria-hidden />
                          <button
                            type="button"
                            className="font-mono-plex text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus rounded-sm"
                            onClick={() => setPrefix(`${crumbs.slice(0, index + 1).join("/")}/`)}
                          >
                            {crumb}
                          </button>
                        </span>
                      ))}
                    </div>
                    <div className="flex items-center gap-2 border-b border-elevation-subtle px-3 py-1.5">
                      <Search className="h-3.5 w-3.5 text-default-secondary shrink-0" aria-hidden />
                      <input
                        type="text"
                        value={search}
                        onChange={(event) => setSearch(event.target.value)}
                        placeholder={t_i18n("Search secrets...")}
                        aria-label={t_i18n("Search secrets...")}
                        className="flex-1 bg-transparent content-compact outline-none placeholder:text-default-secondary"
                      />
                    </div>
                    {partial && (
                      <p
                        className="border-b border-elevation-subtle px-3 py-1.5 content-caption text-default-secondary"
                        data-testid="vault-picker-partial"
                      >
                        {t_i18n("{count} names loaded: the vault holds more, and the search covers the loaded names only.", {
                          count: loaded.length,
                        })}
                      </p>
                    )}
                    <div className="max-h-56 overflow-y-auto" role="listbox" aria-label={t_i18n("Secrets")}>
                      {listing.isLoading ? (
                        <div className="flex items-center gap-2 px-3 py-3 content-compact text-default-secondary" role="status">
                          <Spinner size="sm" tone="inherit" /> {t_i18n("Loading...")}
                        </div>
                      ) : entries.length === 0 ? (
                        <p className="px-3 py-3 content-compact text-default-secondary">
                          {query ? t_i18n("No secret matches the search.") : t_i18n("No secret here.")}
                        </p>
                      ) : (
                        entries.map((entry) => (
                          <button
                            key={entry.path}
                            type="button"
                            role="option"
                            aria-selected={!entry.folder && entry.path === trimmedPath}
                            onClick={() => (entry.folder ? setPrefix(entry.path) : choosePath(entry.path))}
                            className={cn(
                              "flex w-full items-center gap-2 px-3 py-1.5 text-left content-compact transition-colors hover:bg-elevation-hover",
                              "focus-visible:outline-none focus-visible:bg-elevation-hover",
                              !entry.folder && entry.path === trimmedPath && "bg-accent/10 text-accent",
                            )}
                          >
                            {entry.folder ? (
                              <Folder className="h-3.5 w-3.5 text-default-secondary shrink-0" aria-hidden />
                            ) : (
                              <FileKey2 className="h-3.5 w-3.5 text-accent shrink-0" aria-hidden />
                            )}
                            <span className="font-mono-plex truncate">{entry.name}</span>
                            {entryId(entry) && (
                              <span className="ml-auto shrink-0 font-mono-plex content-caption text-default-secondary">{entryId(entry)}</span>
                            )}
                          </button>
                        ))
                      )}
                    </div>
                    {(partial || isFetchNextPageError || laterProblem) && (
                      <div className="flex flex-wrap items-center gap-2 border-t border-elevation-subtle px-3 py-1.5">
                        {partial && (
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            className="h-7 px-2"
                            disabled={isFetchingNextPage}
                            aria-busy={isFetchingNextPage || undefined}
                            onClick={() => void fetchNextPage()}
                          >
                            {isFetchingNextPage && <Spinner size="sm" tone="inherit" />}
                            {t_i18n("Load more")}
                          </Button>
                        )}
                        {isFetchNextPageError && (
                          <span className="content-caption text-feedback-error-primary" role="status">
                            {t_i18n("The next names could not be loaded.")}
                          </span>
                        )}
                        {laterProblem && (
                          <span className="content-caption text-feedback-error-primary" role="status">
                            {t_i18n("The vault refused the next names ({reason}).", {
                              reason: reasonText(t_i18n, laterProblem.reason, laterProblem.description),
                            })}
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                ) : (
                  <p className="rounded-sm bg-elevation-hover px-3 py-2 content-compact text-default-secondary" data-testid="vault-picker-type-path">
                    {listingProblem
                      ? t_i18n("The vault refused the listing ({reason}). Type the path of the secret.", {
                          reason: reasonText(t_i18n, listingProblem.reason, listingProblem.description),
                        })
                      : t_i18n("This vault does not let you list its secrets: type the path of the secret.")}
                  </p>
                )}

                {recent.length > 0 && (
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="flex items-center gap-1 content-caption text-default-secondary">
                      <Clock className="h-3 w-3" aria-hidden /> {t_i18n("Recent")}
                    </span>
                    {recent.map((entry) => (
                      <button
                        key={entry}
                        type="button"
                        onClick={() => {
                          choosePath(entry);
                          setPrefix(parentPrefix(entry));
                        }}
                        className="rounded-sm border border-elevation-subtle px-2 py-0.5 font-mono-plex content-caption hover:bg-elevation-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                      >
                        {entry}
                      </button>
                    ))}
                  </div>
                )}

                <div className="grid gap-3 sm:grid-cols-[2fr_1fr]">
                  <Input
                    label={t_i18n("Path")}
                    value={path}
                    onChange={(event) => choosePath(event.target.value, false)}
                    onBlur={settleTypedPath}
                    onKeyDown={(event) => {
                      if (event.key !== "Enter") return;
                      event.preventDefault();
                      settleTypedPath();
                    }}
                    placeholder={chosen.type === "onepassword" ? t_i18n("vault id/item id") : "secret/team/service"}
                    className="font-mono-plex"
                  />
                  <div className="space-y-1">
                    <label className="content-compact text-default-secondary" htmlFor="vault-picker-key">
                      {t_i18n("Key")}
                    </label>
                    {keys.data && !keys.data.unavailable ? (
                      <SearchableSelect
                        id="vault-picker-key"
                        aria-label={t_i18n("Key")}
                        searchable="auto"
                        sort={false}
                        options={keyOptions}
                        value={keyName}
                        onValueChange={(value) => {
                          setKeyName(value);
                          check.reset();
                        }}
                      />
                    ) : (
                      <Input
                        id="vault-picker-key"
                        aria-label={t_i18n("Key")}
                        value={keyName}
                        onChange={(event) => {
                          setKeyName(event.target.value);
                          check.reset();
                        }}
                        placeholder={t_i18n("Whole value")}
                        className="font-mono-plex"
                      />
                    )}
                  </div>
                </div>
                {outsideGrant && (
                  <p className="content-caption text-feedback-error-primary">
                    {t_i18n("This path is outside the paths you may use on this vault.")}
                  </p>
                )}
                {trimmedPath && !referenceValid && !outsideGrant && (
                  <p className="content-caption text-feedback-error-primary">{t_i18n("This is not a valid vault reference.")}</p>
                )}

                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={!referenceValid || outsideGrant || check.isPending}
                    aria-busy={check.isPending || undefined}
                    onClick={() => {
                      settleTypedPath();
                      check.mutate(referenceText);
                    }}
                  >
                    {check.isPending && <Spinner size="sm" tone="inherit" />}
                    {t_i18n("Check")}
                  </Button>
                  {check.data?.ok && (
                    <span className="flex items-center gap-1 content-compact text-feedback-success-primary" role="status">
                      <CheckCircle2 className="h-4 w-4" aria-hidden /> {t_i18n("The secret can be read.")}
                    </span>
                  )}
                  {check.data && !check.data.ok && (
                    <span className="flex items-center gap-1 content-compact text-feedback-error-primary" role="status">
                      <XCircle className="h-4 w-4" aria-hidden />
                      {t_i18n("The secret cannot be read: {reason}", {
                        reason: reasonText(t_i18n, check.data.reason, check.data.description),
                      })}
                    </span>
                  )}
                  {check.isError && (
                    <span className="content-compact text-feedback-error-primary" role="status">
                      {check.error.message}
                    </span>
                  )}
                </div>
              </>
            )}

            <div className="border-t border-elevation-subtle pt-3">
              {pasting ? (
                <div className="flex items-end gap-2">
                  <Input
                    label={t_i18n("Reference")}
                    value={pasted}
                    onChange={(event) => setPasted(event.target.value)}
                    placeholder="vault://provider/path#key"
                    className="flex-1 font-mono-plex"
                    error={pasted.trim() !== "" && !parseVaultReference(pasted) ? t_i18n("This is not a valid vault reference.") : undefined}
                  />
                  <Button type="button" variant="outline" size="sm" onClick={applyPasted} disabled={!parseVaultReference(pasted)}>
                    {t_i18n("Use")}
                  </Button>
                </div>
              ) : (
                <button
                  type="button"
                  className="content-caption text-default-secondary hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus rounded-sm"
                  onClick={() => setPasting(true)}
                >
                  {t_i18n("Paste a reference instead")}
                </button>
              )}
            </div>
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
            {t_i18n("Cancel")}
          </Button>
          <Button type="button" onClick={confirm} disabled={!referenceValid || outsideGrant}>
            {t_i18n("Use this secret")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
