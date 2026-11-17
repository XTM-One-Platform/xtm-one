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
 * Share a deck with people and groups.
 *
 * A deck is shared like every other entity — the platform's three-tier model:
 * per-user and per-group grants with a permission tier (`PrincipalSharingCard`),
 * plus the admin-only company-managed switch that publishes it to everyone.
 * Grants are what let a colleague open the deck AND ask their own assistant to
 * edit it in place: a `config` grantee's agent resolves the same deck through
 * `generate_presentation(presentation_id=…)`, a `read_only` grantee is told to
 * duplicate instead.
 *
 * The link grants NOTHING on its own — it is the address of the deck, and the
 * backend answers 404 to anyone the grants (or the company-managed flag) do not
 * admit. The first version of this dialog put the link first, above the
 * grants, and read like a "share by link" dialog: people copied it, sent it,
 * and the recipient saw "not available". So the grants come first, the link
 * sits under an audience line that says in words who can open it — computed
 * from the DRAFT, with an "applies when you save" note while it differs from
 * what is stored — and the tiers are worded for a deck (viewer / editor) rather
 * than borrowing "can configure" from agents.
 *
 * Only the sharing fields travel in the PATCH — never the spec — so opening this
 * dialog while a collaborator's agent is mid-edit cannot clobber their slides,
 * and a sharing change never bumps the deck revision.
 *
 * The PUBLIC LINK is the one exception to "the link grants nothing",
 * and it sits apart, last, under its own heading: a separate URL anyone can
 * open read-only without an account. Its switch applies at once - it is not
 * part of the grants draft the Save button writes - and turning it off or
 * replacing it asks first, because both break a link people already hold. An
 * administrator can turn public links off for the platform; the section then
 * says so instead of offering a switch that would be refused. Under an active
 * link, `Allow PDF download` (off on every new link) lets its visitors
 * also download the slides as a PDF; it applies at once too.
 */
import { useEffect, useState } from "react";
import { Building2, Download, Link2, Lock, Share2, UsersRound } from "lucide-react";

import { Button } from "@/components/ui/button";
import { CopyIconButton } from "@/components/ui/copy-icon-button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { PrincipalSharingCard } from "@/components/ui/principal-sharing-card";
import { PublicLinkCard } from "@/components/ui/public-link-card";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import {
  useDisablePresentationPublicLink,
  useEnablePresentationPublicLink,
  usePresentationPublicLink,
  useUpdatePresentation,
  useUpdatePresentationPublicLink,
} from "@/hooks/api/usePresentationApi";
import { useAuth } from "@/hooks/useAuth";
import { useUserShareGrants } from "@/hooks/useUserShareGrants";
import { useFormatter } from "@/i18n";
import {
  PRESENTATION_SHARE_PERMISSION_DESCRIPTIONS,
  PRESENTATION_SHARE_PERMISSION_LABELS,
} from "@/lib/constants";
import type { Presentation, PresentationSummary } from "@/types";
import { absoluteUrl } from "@/lib/base-path";
import {
  sharingAudience,
  sharingChanged,
  sharingPayloadGroups,
  type SharingDraft,
} from "@/lib/sharing-draft";
import { Spinner } from "@filigran/design-system";

/** Absolute viewer link — what a collaborator pastes into a message. */
function presentationLink(id: string): string {
  return absoluteUrl(`/presentations/${id}`);
}

/** The public link of the deck: on / off at once, copy, replace, its PDF download. Managers only. */
function PublicLinkSection({ presentationId, open }: { presentationId: string; open: boolean }) {
  const { t_i18n } = useFormatter();
  const link = usePresentationPublicLink(presentationId, open);
  const enable = useEnablePresentationPublicLink();
  const disable = useDisablePresentationPublicLink();
  const options = useUpdatePresentationPublicLink();
  return (
    <PublicLinkCard
      state={link.data}
      isLoading={link.isLoading}
      isError={link.isError}
      busy={enable.isPending || disable.isPending || options.isPending}
      replacing={enable.isPending}
      enable={() => enable.mutateAsync({ id: presentationId })}
      disable={() => disable.mutateAsync(presentationId)}
      regenerate={() => enable.mutateAsync({ id: presentationId, regenerate: true })}
      description={t_i18n(
        "Anyone with this link can view the presentation without an account: the slides only, no speaker notes, comments or editing.",
      )}
      offConfirm={t_i18n(
        "Whoever has the link will be told the presentation is not available. The people and groups it is shared with keep their access.",
      )}
      adminOffKept={t_i18n(
        "An administrator has turned public links off on this platform: this presentation's link opens nothing until they are turned back on.",
      )}
      // The PDF download is the link's, applies at once like its
      // switch, and asks nothing: turning it off breaks no address anyone holds.
      option={{
        label: t_i18n("Allow PDF download"),
        description: t_i18n("Visitors can also download the slides as a PDF, without speaker notes."),
        icon: Download,
        checked: !!link.data?.allow_pdf_download,
        onChange: (allowed) => options.mutateAsync({ id: presentationId, allowPdfDownload: allowed }),
        onToast: t_i18n("PDF download allowed"),
        offToast: t_i18n("PDF download turned off"),
        testId: "presentation.share.publicLink.pdf",
      }}
      testIdPrefix="presentation.share.publicLink"
    />
  );
}

export function PresentationShareDialog({
  presentation,
  open,
  onOpenChange,
}: {
  presentation: Presentation | PresentationSummary;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { isAdmin } = useAuth();
  const update = useUpdatePresentation();

  const [sharedGroupIds, setSharedGroupIds] = useState<string[] | null>(
    presentation.shared_group_ids,
  );
  const [sharedGroupPermissions, setSharedGroupPermissions] = useState<
    Record<string, string> | null
  >(presentation.shared_group_permissions);
  const [companyManaged, setCompanyManaged] = useState(presentation.is_company_managed);
  const userGrants = useUserShareGrants(presentation);

  // Re-seed from the deck each time the dialog opens: a collaborator (or the
  // agent) may have changed the sharing since the last time, and a cancelled
  // edit must not leak into the next session.
  useEffect(() => {
    if (!open) return;
    setSharedGroupIds(presentation.shared_group_ids);
    setSharedGroupPermissions(presentation.shared_group_permissions);
    setCompanyManaged(presentation.is_company_managed);
    userGrants.reset();
    // `userGrants` is a fresh object every render; `reset` is what we call.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, presentation]);

  const link = presentationLink(presentation.id);

  const draftUserIds = userGrants.props.userIds;
  const draftRestricted = userGrants.props.accessRestricted;
  const draftUserPermissions = userGrants.props.userPermissions;

  const draft: SharingDraft = {
    companyManaged,
    accessRestricted: draftRestricted,
    groupIds: sharedGroupIds,
    groupPermissions: sharedGroupPermissions,
    userIds: draftUserIds,
    userPermissions: draftUserPermissions,
  };
  // Whether saving would change anything — the audience line below says
  // "applies when you save" only then, so a freshly opened dialog reads as a
  // statement of fact and an edited one as a plan.
  const dirty = sharingChanged(draft, {
    companyManaged: presentation.is_company_managed,
    accessRestricted: presentation.access_restricted,
    groupIds: presentation.shared_group_ids,
    groupPermissions: presentation.shared_group_permissions,
    userIds: presentation.shared_user_ids,
    userPermissions: presentation.shared_user_permissions,
  });

  // Who the DRAFT lets in — the same rule `deck_permission()` applies, said in
  // words next to the link so nobody has to infer it from two toggles.
  const { kind: audienceKind, text: audience } = sharingAudience(draft, t_i18n);
  const AudienceIcon =
    audienceKind === "company" ? Building2 : audienceKind === "shared" ? UsersRound : Lock;

  const handleSave = async () => {
    try {
      await update.mutateAsync({
        id: presentation.id,
        is_company_managed: companyManaged,
        ...sharingPayloadGroups(draft),
        ...userGrants.toPayload(companyManaged),
      });
      toast({ title: t_i18n("Sharing updated"), variant: "success" });
      onOpenChange(false);
    } catch (err) {
      toast({
        title: t_i18n("Could not update sharing"),
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Share2 className="h-4 w-4 text-accent" />
            {t_i18n("Share presentation")}
          </DialogTitle>
          <DialogDescription>
            {t_i18n(
              "Access is granted here: the presentation's address opens nothing on its own, unless you turn on its public link. Everyone with access can open, present, export, duplicate and comment on it; editors can also change it, in the editor or through their own assistant.",
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <PrincipalSharingCard
            sharedGroupIds={sharedGroupIds}
            onChange={setSharedGroupIds}
            sharedGroupPermissions={sharedGroupPermissions}
            onPermissionsChange={setSharedGroupPermissions}
            userSharing={userGrants.props}
            entityType="presentation"
            isCompanyManaged={companyManaged}
            disabled={update.isPending}
            permissionLabels={PRESENTATION_SHARE_PERMISSION_LABELS}
            permissionDescriptions={PRESENTATION_SHARE_PERMISSION_DESCRIPTIONS}
          />

          {isAdmin && (
            <div className="flex items-center justify-between gap-3 rounded-sm bg-elevation-hover p-3">
              <div className="flex min-w-0 items-center gap-2">
                <Building2 className="h-4 w-4 shrink-0 text-accent" />
                <div className="min-w-0">
                  <p className="content-base font-medium">{t_i18n("Company-managed presentation")}</p>
                  <p className="content-compact text-default-secondary">
                    {t_i18n("Visible to everyone in the company. Only admins can edit.")}
                  </p>
                </div>
              </div>
              <Switch
                aria-label={t_i18n("Company-managed presentation")}
                checked={companyManaged}
                onCheckedChange={setCompanyManaged}
                disabled={update.isPending}
              />
            </div>
          )}

          {/* The link comes LAST, under the audience it is good for: it is the
              deck's address, not a grant, and a reader who copies it must know
              who it will open for. */}
          <div className="space-y-2 rounded-sm border border-elevation-subtle bg-elevation-hover p-3">
            <p className="content-caption font-medium uppercase tracking-wide text-default-secondary">
              {t_i18n("Who can open this link")}
            </p>
            <p className="flex items-start gap-2 content-compact">
              <AudienceIcon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden />
              <span className="min-w-0">
                {audience}
                {dirty && (
                  <span className="ml-1.5 text-feedback-warning-primary">{t_i18n("Applies when you save.")}</span>
                )}
              </span>
            </p>
            <div className="flex items-start gap-2 rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2">
              <Link2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-default-secondary" aria-hidden />
              {/* `break-all`, not `truncate`: the whole address stays readable
                  (an ellipsis hid the very part that tells two decks apart),
                  and a URL has no word boundaries to wrap at. */}
              <code className="min-w-0 flex-1 break-all font-mono-plex text-[11px] leading-snug text-default-secondary">
                {link}
              </code>
              <CopyIconButton value={link} label={t_i18n("Copy link")} className="-my-1 shrink-0" />
            </div>
            <p className="text-[11px] text-default-secondary">
              {t_i18n("Anyone else who opens it is told the presentation is not available.")}
            </p>
          </div>

          {presentation.can_manage && <PublicLinkSection presentationId={presentation.id} open={open} />}
        </div>

        {/* Nothing to save until something changed: a dialog opened only to
            copy the link closes, it does not "cancel", and a live Save button
            would suggest the link needs saving before it works. */}
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={update.isPending}>
            {dirty ? t_i18n("Cancel") : t_i18n("Close")}
          </Button>
          <Button
            onClick={() => void handleSave()}
            disabled={update.isPending || !dirty}
            className="gap-1.5" aria-busy={update.isPending || undefined}>
            {update.isPending && <Spinner size="sm" tone="inherit" />}
            {t_i18n("Save")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
