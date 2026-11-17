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

/**
 * ShareConversationDialog — grant read access to a conversation.
 *
 * The ask behind the issue was "let me paste the chat URL into a Teams thread
 * or a Jira ticket". The original proposal was to drop RBAC on chats entirely
 * and let UUID entropy stand in for access control. This dialog is the answer
 * that keeps RBAC: everyone who can read a shared conversation is an
 * authenticated platform account, named in the audit trail, revocable in one
 * click.
 *
 * Two ways to widen access, presented in that order because they answer
 * different questions:
 *
 *   1. **People and groups** — "Alice should see this." Explicit, shows up in
 *      the recipient's "Shared with me" list, survives them not having the URL.
 *   2. **Anyone with the link** — "whoever is in this Teams channel should be
 *      able to open it." No recipient list to maintain up front; still
 *      authenticated, still auditable, and reachable *by URL only* — it does
 *      not push the conversation into anybody's sidebar.
 *
 * Deliberately absent: an anonymous/public link. That is the one thing the
 * issue asked for that we are not building, because it would put conversation
 * content — including tool output pulled from integrations and knowledge bases
 * that are themselves under group RBAC — outside authentication entirely.
 *
 * Read-only is the only tier. Continuing someone else's thread is a fork, not
 * a permission (see the model's ``SHARE_PERMISSION_READ`` comment).
 */

import { useEffect, useMemo, useState } from "react";
import { IconButton, Spinner } from "@filigran/design-system";
import {
  AlertTriangle,
  Check,
  Copy,
  Globe,
  Lock,
  Search,
  Trash2,
  Users,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  SHARE_TARGET_MIN_QUERY_LENGTH,
  useConversationShareMutations,
  useConversationShares,
  useShareTargets,
} from "@/hooks/api/useChatApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { absoluteUrl } from "@/lib/base-path";
import type { ConversationShare, ConversationShareTarget } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";

interface ShareConversationDialogProps {
  conversationId: string | null;
  conversationTitle?: string | null;
  open: boolean;
  onClose: () => void;
}

/** The URL a recipient opens. Mirrors the ``?c=`` deep link the app already uses. */
function conversationUrl(conversationId: string): string {
  return absoluteUrl(`/chat?c=${conversationId}`);
}

export function ShareConversationDialog({
  conversationId,
  conversationTitle,
  open,
  onClose,
}: ShareConversationDialogProps) {
  const { t_i18n } = useFormatter();
  const [query, setQuery] = useState("");
  const [copied, setCopied] = useState(false);

  const shares = useConversationShares(conversationId, { enabled: open });
  const targets = useShareTargets(query);
  const { addShare, removeShare, setLinkAccess } =
    useConversationShareMutations(conversationId);

  // Reset the transient bits on every open: a stale search term from the last
  // conversation would show results that have nothing to do with this one.
  useEffect(() => {
    if (open) {
      setQuery("");
      setCopied(false);
    }
  }, [open, conversationId]);

  // Memoised so ``alreadyShared`` below has a stable dependency: a fresh
  // ``[]`` on every render would rebuild the Set (and the suggestion filter)
  // on every keystroke.
  const existing = useMemo(() => shares.data?.shares ?? [], [shares.data]);
  const alreadyShared = useMemo(
    () => new Set(existing.map((s) => `${s.principal_type}:${s.principal_id}`)),
    [existing],
  );

  const suggestions: ConversationShareTarget[] = useMemo(() => {
    if (!targets.data) return [];
    return [...targets.data.users, ...targets.data.groups].filter(
      (t) => !alreadyShared.has(`${t.type}:${t.id}`),
    );
  }, [targets.data, alreadyShared]);

  const isOrgReadable = shares.data?.link_access === "org_read";
  const aiOff = useAiActionDisabled();
  const mutating =
    addShare.isPending || removeShare.isPending || setLinkAccess.isPending || aiOff;
  const mutationError =
    addShare.error?.message ??
    removeShare.error?.message ??
    setLinkAccess.error?.message ??
    null;

  const handleCopy = async () => {
    if (!conversationId) return;
    await navigator.clipboard.writeText(conversationUrl(conversationId));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="text-base">{t_i18n("Share conversation")}</DialogTitle>
          <DialogDescription className="content-base leading-relaxed">
            {conversationTitle
              ? t_i18n(
                  'People you add can read "{title}" and download its files. They cannot reply in it, rename it or delete it.',
                  { title: conversationTitle },
                )
              : t_i18n(
                  "People you add can read this conversation and download its files. They cannot reply in it, rename it or delete it.",
                )}
          </DialogDescription>
        </DialogHeader>

        {/* ---------- People and groups ---------- */}
        <div className="space-y-2">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-default-secondary" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t_i18n("Search people and groups…")}
              className="pl-8"
              aria-label={t_i18n("Search people and groups")}
            />
          </div>

          {query.trim().length > 0 &&
            query.trim().length < SHARE_TARGET_MIN_QUERY_LENGTH && (
              <p className="px-1 content-compact text-default-secondary">
                {t_i18n("Keep typing to search.")}
              </p>
            )}

          {targets.isFetching && (
            <p className="flex items-center gap-2 px-1 content-compact text-default-secondary">
              <Spinner size="sm" tone="inherit" />
              {t_i18n("Searching…")}
            </p>
          )}

          {targets.data && suggestions.length === 0 && !targets.isFetching && (
            <p className="px-1 content-compact text-default-secondary">
              {t_i18n("No matches.")}
            </p>
          )}

          {suggestions.length > 0 && (
            <ul className="max-h-44 overflow-y-auto rounded-sm border border-elevation-subtle-soft">
              {suggestions.map((t) => (
                <li key={`${t.type}:${t.id}`}>
                  <button
                    type="button"
                    disabled={mutating}
                    onClick={() => {
                      addShare.mutate(
                        t.type === "user"
                          ? { userId: t.id }
                          : { groupId: t.id },
                      );
                      setQuery("");
                    }}
                    className="flex w-full items-center gap-2.5 px-3 py-2 text-left content-base transition-colors hover:bg-elevation-hover disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {t.type === "group" ? (
                      <Users className="h-4 w-4 shrink-0 text-default-secondary" />
                    ) : (
                      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-accent/15 content-caption font-medium text-accent">
                        {t.name.slice(0, 1).toUpperCase()}
                      </span>
                    )}
                    <span className="min-w-0 flex-1 truncate">{t.name}</span>
                    {t.email && (
                      <span className="shrink-0 truncate content-compact text-default-secondary">
                        {t.email}
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* ---------- Current grants ---------- */}
        {shares.isLoading ? (
          <p className="flex items-center gap-2 content-base text-default-secondary">
            <Spinner size="sm" tone="inherit" />
            {t_i18n("Loading access…")}
          </p>
        ) : existing.length > 0 ? (
          <ul className="space-y-1">
            {existing.map((s: ConversationShare) => (
              <li
                key={s.id}
                className="flex items-center gap-2.5 rounded-sm px-1 py-1.5 content-base"
              >
                {s.principal_type === "group" ? (
                  <Users className="h-4 w-4 shrink-0 text-default-secondary" />
                ) : (
                  <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-accent/15 content-caption font-medium text-accent">
                    {s.principal_name.slice(0, 1).toUpperCase()}
                  </span>
                )}
                <span className="min-w-0 flex-1 truncate">
                  {s.principal_name}
                  {s.principal_email && (
                    <span className="ml-1.5 content-compact text-default-secondary">
                      {s.principal_email}
                    </span>
                  )}
                </span>
                <span className="shrink-0 content-compact text-default-secondary">
                  {t_i18n("Can view")}
                </span>
                <IconButton variant="default" priority="tertiary" aria-label={t_i18n("Remove access for {name}", {
                    name: s.principal_name,
                  })} icon={<Trash2 className="h-3.5 w-3.5" />} className="h-7 w-7 shrink-0 text-default-secondary hover:text-feedback-error-primary" disabled={mutating} onClick={() => removeShare.mutate(s.id)} />
              </li>
            ))}
          </ul>
        ) : (
          <p className="content-base text-default-secondary">
            {t_i18n("Only you have access.")}
          </p>
        )}

        {/* ---------- Link tier ---------- */}
        <div className="space-y-3 rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3">
          <div className="flex items-start gap-2.5">
            {isOrgReadable ? (
              <Globe className="mt-0.5 h-4 w-4 shrink-0 text-accent" />
            ) : (
              <Lock className="mt-0.5 h-4 w-4 shrink-0 text-default-secondary" />
            )}
            <div className="min-w-0 flex-1">
              <p className="content-base font-medium">
                {isOrgReadable
                  ? t_i18n("Anyone on the platform with the link can view")
                  : t_i18n("Only people you add can view")}
              </p>
              <p className="mt-0.5 content-compact leading-relaxed text-default-secondary">
                {isOrgReadable
                  ? t_i18n(
                      "Signing in is still required, and every view is recorded. The conversation does not appear in anyone's sidebar: it is reachable through the link only.",
                    )
                  : t_i18n(
                      "Turn this on to paste the link into a channel or a ticket without listing recipients up front.",
                    )}
              </p>
            </div>
            <Switch
              checked={isOrgReadable}
              disabled={mutating || shares.isLoading}
              aria-label={t_i18n("Anyone on the platform with the link can view")}
              onCheckedChange={(next) =>
                setLinkAccess.mutate(next ? "org_read" : "private")
              }
            />
          </div>

          <Button
            variant="outline"
            size="sm"
            className="w-full"
            disabled={!conversationId}
            onClick={handleCopy}
          >
            {copied ? (
              <>
                <Check className="mr-2 h-3.5 w-3.5" />
                {t_i18n("Link copied")}
              </>
            ) : (
              <>
                <Copy className="mr-2 h-3.5 w-3.5" />
                {t_i18n("Copy link")}
              </>
            )}
          </Button>
          {/* The link is always copyable, including while the conversation is
              private — the recipient simply gets "not found" until they are
              granted access. Hiding the button would suggest the URL is the
              thing being granted, when the grant is what makes the URL work. */}
        </div>

        {mutationError && (
          <div
            role="alert"
            className="flex items-start gap-2 rounded-sm border border-feedback-error-primary/30 bg-feedback-error-primary/10 p-2 content-base text-feedback-error-primary"
          >
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span className="leading-relaxed">{mutationError}</span>
          </div>
        )}

        <p
          className={cn(
            "flex items-start gap-2 content-compact leading-relaxed text-default-secondary",
          )}
        >
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" />
          {t_i18n(
            "This conversation may contain results the agent retrieved from integrations or knowledge bases. Sharing it shares those results too, whether or not the recipient has access to the source.",
          )}
        </p>

        <div className="flex justify-end pt-1">
          <Button variant="outline" onClick={onClose}>
            {t_i18n("Done")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
