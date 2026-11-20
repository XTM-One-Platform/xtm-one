// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from "react";
import { Eye, EyeOff, Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import { useToast } from "@/components/ui/toast";
import { EntityConfigCard } from "@/components/ui/entity-config-card";
import { RelayRouteField } from "@/components/ui/relay-route-field";
import { serviceUrlOf } from "@/lib/relay-destinations";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { useUserShareGrants } from "@/hooks/useUserShareGrants";
import { useUpdateIntegration } from "@/hooks/api/useIntegrationApi";
import { useAuth } from "@/hooks/useAuth";
import { extraConfigPatch, hasInvalidExtraField, isExtraFieldVisible, isSecretExtraKey, restoreEmptiedExtraFields, withoutHiddenExtraFields } from "@/lib/extra-fields";
import { ExtraFieldInputs } from "@/components/ui/extra-field-inputs";
import { Label } from "@/components/ui/label";
import { SecretField } from "@/components/ui/secret-field";
import { otherVariantFieldKeys, resolveVariant, variantValue } from "@/lib/catalog-variants";
import { INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import {
  canManageEntity,
  credentialShareChoice,
  isConnectedAccountWithheld,
  isPerUserIntegration,
  isSharedOauthIntegration,
  isSharedTokenUnacknowledged,
  pluralize,
} from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { PlatformGateControl } from "@/pages/integrations/PlatformGate";
import { HitlIcon } from "@/components/ui/hitl-icon";
import { hitlState } from "@/lib/hitl";
import { useToolGateCatalog } from "@/hooks/api/useHitlWhitelist";
import { useApprovalsInUse } from "@/hooks/api/useToolApprovals";
import type { Integration, ProviderCatalogItem } from "@/types";
import { RegistrationEditableFields } from "./RegistrationEditableFields";
import { usesAdminOAuthApp } from "./admin-oauth-app";

/**
 * `canManage` gates editing with every lock applied; `isRegistrationOnly`
 * says which of those locks is the platform registration's, so the card can
 * explain it instead of presenting it as a missing permission.
 */
export function ConfigurationCard({ integration, catalogEntry, canManage, isRegistrationOnly = false, a2aDirection, hiddenExtraKeys }: { integration: Integration; catalogEntry?: ProviderCatalogItem; canManage: boolean; isRegistrationOnly?: boolean; a2aDirection?: "inbound" | "outbound"; hiddenExtraKeys?: string[] }) {
  const { t_i18n } = useFormatter();
  const update = useUpdateIntegration();
  const { toast } = useToast();
  const { user, isAdmin } = useAuth();
  // Sharing an OAuth integration shares its connected account. Not
  // sharing the account is a choice about the owner's credentials, so only
  // the owner or an admin makes it; a config-tier member edits the rest.
  const isOwner = integration.user_id === user?.id;
  const isTokenOwner = !!user?.id && integration.oauth_token_owner_id === user.id;
  const canChooseCredentialSharing = isAdmin || isOwner || isTokenOwner;
  const [editing, setEditing] = useState(false);
  const [showSecrets, setShowSecrets] = useState(false);
  const [confirmElement, confirmSharing] = useCredentialSharingConfirm();

  const [editName, setEditName] = useState(integration.name);
  const [editDescription, setEditDescription] = useState(integration.description || "");
  const [editTags, setEditTags] = useState<string[]>(integration.tags || []);
  const [editClientId, setEditClientId] = useState("");
  const [editClientSecret, setEditClientSecret] = useState("");
  const [editApiKey, setEditApiKey] = useState("");
  // OAuth2 password grant: the user's login, stored as "username:password".
  const [editUsername, setEditUsername] = useState("");
  const [editPassword, setEditPassword] = useState("");
  const [editExtra, setEditExtra] = useState<Record<string, unknown>>(integration.extra_config ? { ...integration.extra_config } : {});
  // The entry as it reads for this registration's variant (its direction,
  // for A2A). A variant's fields replace the base map rather than merging,
  // or inbound-only pins would show on the outbound form.
  const variant = variantValue(catalogEntry, integration.extra_config, a2aDirection);
  const editableExtraFields = useMemo<Record<string, string>>(
    () => (catalogEntry ? resolveVariant(catalogEntry, variant).extra_fields ?? {} : {}),
    [catalogEntry, variant],
  );

  const [editAuthType, setEditAuthType] = useState(integration.auth_type);
  // The backend's rule (#8386): supported_auth_types, else exactly auth_type.
  const supportedAuthTypes = catalogEntry?.supported_auth_types?.length
    ? catalogEntry.supported_auth_types
    : catalogEntry?.auth_type ? [catalogEntry.auth_type] : [];
  const authModeChoices = supportedAuthTypes.length && !supportedAuthTypes.includes(integration.auth_type)
    ? [integration.auth_type, ...supportedAuthTypes]
    : supportedAuthTypes;
  const [editCompanyManaged, setEditCompanyManaged] = useState(integration.is_company_managed);
  const [editSharedGroupIds, setEditSharedGroupIds] = useState<string[] | null>(integration.shared_group_ids ?? null);
  const [editSharedGroupPermissions, setEditSharedGroupPermissions] = useState<Record<string, string> | null>(integration.shared_group_permissions ?? null);
  // Per-user grants + access restriction.
  const shareGrants = useUserShareGrants(integration);
  // The opt-out: shared, without the owner's account. Off unless the owner
  // already chose it.
  const [editWithholdAccount, setEditWithholdAccount] = useState(() => isConnectedAccountWithheld(integration));
  const [editRequiresApproval, setEditRequiresApproval] = useState(integration.requires_approval);
  const [editRelayGroupId, setEditRelayGroupId] = useState<string | null>(integration.relay_group_id ?? null);

  // The keys a registration leaves to the deployment (the OpenAEV tenant):
  // edited on their own while the rest of the registered config stays locked.
  const registrationEditableKeys = isRegistrationOnly ? (catalogEntry?.registration_editable_extra_fields ?? []) : [];
  const canEditRegistered = canManageEntity(integration, user?.id, isAdmin);

  // A registered Suite platform decides approval once for the whole platform,
  // so this integration's own toggle is inert there and the backend
  // refuses to write it. Point at the control that actually decides rather
  // than offering one whose effect would be nil.
  const approvalSetByPlatform = integration.approval_managed_by_platform;

  /* Whether this provider actually has exempt reads, rather than asserting it
     from the switch alone. An administrator can re-gate them — per tool, or in
     bulk from the tools page — and a provider may contribute no read tools at
     all, and in both cases the claim below was false while the table on this
     same page said otherwise. */
  const { data: gateCatalog } = useToolGateCatalog();
  const approvalsInUse = useApprovalsInUse();
  const providerTools = (gateCatalog?.tools ?? []).filter(
    (t) => t.provider === integration.provider,
  );
  const hasExemptReads =
    providerTools.length > 0 && providerTools.some((t) => !t.gates);

  // Custom bundle schemas declare typed settings; secrets (type: "secret")
  // with a non-obvious key (e.g. "auth") must be masked too — the key name
  // regex alone only covers conventional names — and booleans must keep their
  // JSON type through the edit form instead of degrading to strings.
  const schemaTypeByKey = new Map(
    (catalogEntry?.settings_schema ?? []).map((s) => [s.key, s.type]),
  );
  // Shared with the creation form; mirrors what the API redacts.
  const isSecretKey = (key: string) => isSecretExtraKey(key, schemaTypeByKey.get(key));

  const startEditing = () => {
    setEditing(true);
    setEditName(integration.name);
    setEditDescription(integration.description || "");
    setEditTags(integration.tags || []);
    setEditClientId("");
    setEditClientSecret("");
    setEditApiKey("");
    setEditUsername("");
    setEditPassword("");
    setEditExtra(integration.extra_config ? { ...integration.extra_config } : {});
    setEditAuthType(integration.auth_type);
    setEditCompanyManaged(integration.is_company_managed);
    setEditSharedGroupIds(integration.shared_group_ids ?? null);
    setEditSharedGroupPermissions(integration.shared_group_permissions ?? null);
    shareGrants.reset();
    setEditWithholdAccount(isConnectedAccountWithheld(integration));
    setEditRequiresApproval(integration.requires_approval);
    setEditRelayGroupId(integration.relay_group_id ?? null);
    setShowSecrets(false);
  };

  // Every kind of share - company-wide, a group, a person - hands the
  // integration's connected account to people who are not its owner.
  const willShareOauth = isSharedOauthIntegration({
    auth_type: editAuthType,
    is_company_managed: editCompanyManaged,
    shared_group_ids: editSharedGroupIds,
    shared_user_ids: shareGrants.props.userIds,
  });

  const handleSave = async () => {
    const payload: { id: string; name?: string; description?: string; tags?: string[]; is_company_managed?: boolean; shared_group_ids?: string[] | null; client_id?: string; client_secret?: string; api_key?: string; extra_config?: Record<string, unknown>; keep_oauth_credentials_on_share?: boolean; requires_approval?: boolean } = {
      id: integration.id,
      name: editName,
      description: editDescription,
      tags: editTags,
    };
    // Only when it changed, and never for a platform-governed provider: the
    // backend answers 409 there, so sending it would fail an unrelated rename.
    if (!approvalSetByPlatform && editRequiresApproval !== integration.requires_approval) {
      payload.requires_approval = editRequiresApproval;
    }
    // The sharing choice travels with every save of an integration that ends
    // up shared over OAuth, so an unrelated edit (a rename) keeps what the
    // owner chose: an opted-out integration stays opted out.
    const shareChoice = credentialShareChoice({
      authType: editAuthType,
      willBeShared: willShareOauth,
      withholdConnectedAccount: editWithholdAccount,
      mayConsent: canChooseCredentialSharing,
      acknowledged: integration.oauth_share_acknowledged,
    });
    if (shareChoice !== undefined) {
      payload.keep_oauth_credentials_on_share = shareChoice;
    }
    if (isAdmin && editCompanyManaged !== integration.is_company_managed) {
      payload.is_company_managed = editCompanyManaged;
    }
    // Include shared_group_ids (send null to clear, or array of IDs)
    const groupsChanged = JSON.stringify(editSharedGroupIds) !== JSON.stringify(integration.shared_group_ids ?? null);
    if (groupsChanged) {
      payload.shared_group_ids = editCompanyManaged ? null : (editSharedGroupIds && editSharedGroupIds.length > 0 ? editSharedGroupIds : null);
    }
    const permsChanged = JSON.stringify(editSharedGroupPermissions) !== JSON.stringify(integration.shared_group_permissions ?? null);
    if (permsChanged) {
      (payload as Record<string, unknown>).shared_group_permissions = editCompanyManaged ? null : (editSharedGroupPermissions && Object.keys(editSharedGroupPermissions).length > 0 ? editSharedGroupPermissions : null);
    }
    // Per-user grants + access restriction. A user grant is a share
    // like any other: it shares the connected account unless the owner turned
    // on "Don't share my connected account" above.
    Object.assign(payload, shareGrants.toPayload(editCompanyManaged));
    if (editClientId.trim()) payload.client_id = editClientId;
    if (editClientSecret.trim()) payload.client_secret = editClientSecret;
    if (editApiKey.trim()) payload.api_key = editApiKey;
    if (editAuthType === "oauth_password" && (editUsername.trim() || editPassword)) {
      payload.api_key = `${editUsername.trim()}:${editPassword}`;
    }
    // Hidden first, then restore what was emptied, then drop what is still
    // empty. The update merges key by key (a key left out is kept), so every
    // stored key the form no longer holds is sent as null below: that is what
    // clears an abandoned credential.
    // Drop the keys other variants declare (e.g. ``credential_mode``,
    // outbound-only): editing starts from the stored config, so they would
    // otherwise be preserved forever. Only the entry's own cross-variant keys,
    // never "everything not in the current map": extra_config legitimately
    // holds values no catalog field declares.
    const otherVariantKeys = otherVariantFieldKeys(catalogEntry, variant);
    for (const key of Object.keys(editableExtraFields)) otherVariantKeys.delete(key);

    const currentDirectionExtra = Object.fromEntries(
      Object.entries(editExtra).filter(([key]) => !otherVariantKeys.has(key)),
    );
    const applicableExtra = withoutHiddenExtraFields(
      currentDirectionExtra,
      catalogEntry?.extra_field_visible_when,
      catalogEntry?.extra_field_options,
    );
    // An emptied field keeps its stored value; left out of the payload it
    // would be deleted. Only visible fields, so a hidden one still clears.
    const visibleEditableKeys = Object.keys(editableExtraFields).filter((key) =>
      isExtraFieldVisible(key, currentDirectionExtra, catalogEntry?.extra_field_visible_when, catalogEntry?.extra_field_options),
    );
    const restoredExtra = restoreEmptiedExtraFields(applicableExtra, integration.extra_config, visibleEditableKeys);
    const filteredExtra = Object.fromEntries(
      Object.entries(restoredExtra).filter(([, v]) =>
        typeof v === "string" ? v.trim().length > 0 : v !== null && v !== undefined,
      ),
    );
    const extraPatch = extraConfigPatch(filteredExtra, integration.extra_config);
    // Omit extra_config entirely when nothing changes hands.
    if (Object.keys(extraPatch).length > 0) {
      payload.extra_config = extraPatch;
    }
    if (editAuthType !== integration.auth_type) {
      (payload as Record<string, unknown>).auth_type = editAuthType;
    }
    if (editRelayGroupId !== (integration.relay_group_id ?? null)) {
      (payload as Record<string, unknown>).relay_group_id = editRelayGroupId;
    }
    // One confirmation before a save that lends this integration's credential
    // to more people. A co-editor who may not share the connected
    // account cannot lend it on a share: the save shares the integration
    // without it, and says so.
    const sharing = payload as Record<string, unknown>;
    const sharingChanged = ["is_company_managed", "shared_group_ids", "shared_user_ids", "access_restricted"].some((key) => key in sharing);
    if (sharingChanged) {
      const stripsAccount =
        editAuthType === "oauth" && willShareOauth && integration.has_access_token
        && !integration.oauth_share_acknowledged && shareChoice === undefined;
      const confirmed = await confirmSharing(
        {
          entity_type: "integration",
          entity_id: integration.id,
          ...(("is_company_managed" in sharing) ? { is_company_managed: sharing.is_company_managed as boolean } : {}),
          ...(("shared_group_ids" in sharing) ? { shared_group_ids: sharing.shared_group_ids as string[] | null } : {}),
          ...(("shared_user_ids" in sharing) ? { shared_user_ids: sharing.shared_user_ids as string[] | null } : {}),
          ...(("access_restricted" in sharing) ? { access_restricted: sharing.access_restricted as boolean } : {}),
          withhold_connected_account: shareChoice === false,
        },
        {
          confirmLabel: t_i18n("Save and share"),
          unshareConfirmLabel: t_i18n("Save and stop sharing"),
          note: stripsAccount
            ? t_i18n("Saving does not share the owner's connected account: only its owner or an admin can share it. Everyone else it is shared with will authorize with their own account.")
            : undefined,
        },
      );
      if (!confirmed) return;
    }
    const hadAccessToken = integration.has_access_token;
    // The token can disappear for two distinct reasons: the auth type changed
    // away from "oauth" (which always clears it, sharing or not), or a new
    // client ID, whose app the token was not issued to — the toast
    // must say which. Not sharing the connected account keeps it.
    const switchedAwayFromOauth = integration.auth_type === "oauth" && editAuthType !== "oauth";
    const replacedClientId = payload.client_id !== undefined;
    const wasWithheld = isPerUserIntegration(integration);
    update.mutate(payload, {
      onSuccess: (updated) => {
        if (!wasWithheld && isPerUserIntegration(updated)) {
          // The opt-out keeps the owner's account theirs.
          toast({
            title: t_i18n("Connected account not shared"),
            description: t_i18n("Everyone this integration is shared with now authorizes with their own account. Yours stays yours, and authorizing again does not share it."),
            variant: "info",
          });
        } else if (hadAccessToken && !updated.has_access_token && (switchedAwayFromOauth || replacedClientId)) {
          toast({
            title: t_i18n("OAuth credentials removed"),
            description: switchedAwayFromOauth
              ? t_i18n("Your stored OAuth credentials were removed because the auth type changed. Reconnect via OAuth if you switch back.")
              : t_i18n("Your stored OAuth credentials were removed because the client ID changed: they were issued to the previous OAuth app. Authorize again to connect through the new one."),
            variant: "info",
          });
        } else {
          toast({ title: t_i18n("Integration updated"), description: t_i18n("Changes saved successfully."), variant: "success" });
        }
        setEditing(false);
      },
    });
  };

  const oauthCredentialsShared =
    isSharedOauthIntegration(integration) && integration.oauth_share_acknowledged;
  const sharedGroupCount = integration.shared_group_ids?.length ?? 0;
  const sharedUserCount = integration.shared_user_ids?.length ?? 0;
  // A shared OAuth integration holding a token nobody consented to share
  // (left from before sharing recorded the consent): other members cannot
  // pick it in a chat or test it, while what it is bound to keeps using
  // it (nothing is withheld at run time). Saving the configuration shares it;
  // say so where it can be fixed.
  const oauthCredentialsWithheld = isSharedTokenUnacknowledged(integration);

  return (
    <>
    {confirmElement}
    <EntityConfigCard
      entityLabel="integration"
      isCompanyManaged={integration.is_company_managed}
      isBuiltin={integration.is_builtin}
      userId={integration.user_id}
      sharedGroupIds={integration.shared_group_ids}
      ownerName={integration.owner_name}
      canManage={canManage}
      hasManageRights={canManageEntity(integration, user?.id, isAdmin)}
      editing={editing}
      onStartEditing={startEditing}
      onCancel={() => setEditing(false)}
      onSave={handleSave}
      canSave={!(catalogEntry?.requires_relay && !editRelayGroupId) && !hasInvalidExtraField(editExtra, catalogEntry?.extra_field_inputs)}
      isSaving={update.isPending}
      editCompanyManaged={editCompanyManaged}
      onCompanyManagedChange={setEditCompanyManaged}
      editSharedGroupIds={editSharedGroupIds}
      onSharedGroupIdsChange={setEditSharedGroupIds}
      editSharedGroupPermissions={editSharedGroupPermissions}
      userSharing={shareGrants.props}
      onSharedGroupPermissionsChange={setEditSharedGroupPermissions}
      onTransferOwner={async (userId) => {
        const confirmed = await confirmSharing(
          { entity_type: "integration", entity_id: integration.id, new_owner_id: userId },
          { confirmLabel: t_i18n("Transfer") },
        );
        if (!confirmed) return;
        update.mutate(
          { id: integration.id, user_id: userId },
          { onSuccess: () => toast({ title: t_i18n("Owner updated"), variant: "success" }) },
        );
      }}
      companyManagedDescription={
        editCompanyManaged
          ? t_i18n("Shared with all users. Toggling off will make it private and unbind it from other users' agents.")
          : t_i18n("Private to you. Toggle on to share with all users.")
      }
      sharingNotice={
        willShareOauth ? (
          <div className="rounded-sm bg-feedback-alert-primary/10 border border-feedback-alert-primary/20 p-3 space-y-2">
            <p className="content-caption text-feedback-warning-primary dark:text-feedback-warning-primary">
              {isOwner
                ? t_i18n("This is an OAuth connection. Once shared, everyone it is shared with acts using your connected account. To share it without your account, turn on the switch below: your account stays yours, and everyone it is shared with authorizes with their own.")
                : t_i18n("This is an OAuth connection. Once shared, everyone it is shared with acts using its owner's connected account. Only the owner or an admin can choose not to share that account; everyone it is shared with then authorizes with their own.")}
            </p>
            <div className="flex items-center justify-between">
              <label htmlFor="withhold-oauth-account" className="content-compact font-medium">
                {isOwner ? t_i18n("Don't share my connected account") : t_i18n("Don't share the owner's connected account")}
              </label>
              <Switch
                id="withhold-oauth-account"
                checked={editWithholdAccount}
                disabled={!canChooseCredentialSharing}
                onCheckedChange={setEditWithholdAccount}
              />
            </div>
            {/* Lending the account again disconnects everyone's own. */}
            {isPerUserIntegration(integration) && !editWithholdAccount && (integration.connected_user_count ?? 0) > 0 && (
              <p className="content-caption text-feedback-warning-primary dark:text-feedback-warning-primary">
                {t_i18n(
                  "{count, plural, one {Saving shares the connected account and disconnects the # person who connected their own.} other {Saving shares the connected account and disconnects the # people who connected their own.}}",
                  { count: integration.connected_user_count ?? 0 },
                )}
              </p>
            )}
          </div>
        ) : undefined
      }
      editHeaderExtra={
        <Button
          variant="ghost" size="sm" className="h-6 px-2 content-compact"
          onClick={() => setShowSecrets(!showSecrets)}
        >
          {showSecrets ? <EyeOff className="h-3 w-3 mr-1" /> : <Eye className="h-3 w-3 mr-1" />}
          {showSecrets ? t_i18n("Hide") : t_i18n("Show")}
        </Button>
      }
      readView={
        <>
          <div>
            <div className="content-compact text-default-secondary mb-1">{t_i18n("Name")}</div>
            <p className="content-base font-medium">{integration.name}</p>
          </div>
          {integration.description && (
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Description")}</div>
              <p className="content-base text-default-secondary">{integration.description}</p>
            </div>
          )}
          <div>
            <div className="content-compact text-default-secondary mb-1.5">{t_i18n("Provider")}</div>
            <p className="content-base">{INTEGRATION_PROVIDER_NAMES[integration.provider] ?? integration.provider}</p>
          </div>
          <RelayRouteField
            readOnly
            value={integration.relay_group_id}
            onChange={() => undefined}
            destination={serviceUrlOf(integration.extra_config)}
          />
          {/*
            Shown to everyone who can see the integration, not only to whoever
            can edit it: "do agents act on this system unattended" is something
            a reader needs when judging what an agent did, and until now the
            only way to find out was to read the database. Only while approvals
            are in use: with no agent asking, every tool runs without asking and
            this would describe a behaviour nobody gets.
          */}
          {approvalsInUse && (
          <div className="rounded-sm bg-elevation-hover p-3 flex items-start gap-2.5">
            <HitlIcon
              state={hitlState(integration.requires_approval)}
              className="mt-0.5"
            />
            <div className="min-w-0">
              {/* Reads run unattended whichever kind of connection this is:
                  a registered platform's by classification, and
                  an ordinary integration's by the same derivation. The flat
                  "its tools ask" claim survived here for the second case and
                  told a reader every call pauses while most of the table
                  below it does not. The per-tool answer is on that table;
                  this is the summary. */}
              <p className="content-compact font-medium">
                {!integration.requires_approval
                  ? t_i18n("Its tools run without asking.")
                  : !hasExemptReads
                  ? t_i18n("Its tools ask for approval before running.")
                  : approvalSetByPlatform
                  ? t_i18n(
                      "Anything that changes this platform asks for approval; its read tools run without asking.",
                    )
                  : t_i18n(
                      "Anything that changes it asks for approval; its read tools run without asking.",
                    )}
              </p>
              {approvalSetByPlatform && (
                <p className="content-compact text-default-secondary mt-0.5">
                  {t_i18n("Set once for the whole platform, in Settings → Access and security → Tool approvals.")}
                </p>
              )}
            </div>
          </div>
          )}
          {/* The chatbot's own half, for a connection that is a registered
              platform: whether it can ask at all, and the policy for when it
              cannot — changeable here rather than two screens away (FR-047).
              Renders nothing for an ordinary integration. */}
          {approvalsInUse && <PlatformGateControl integrationId={integration.id} />}
          {oauthCredentialsShared && (
            <div className="rounded-sm bg-feedback-alert-primary/5 p-3 flex items-start gap-2.5">
              <Users className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0 mt-0.5" />
              <p className="content-compact text-feedback-warning-primary leading-relaxed">
                {integration.is_company_managed
                  ? t_i18n("Personal OAuth credentials are shared company-wide: every user can act using this token.")
                  : sharedUserCount === 0
                    ? t_i18n("Personal OAuth credentials are shared with {count} {unit}: its members can act using this token.", {
                        count: sharedGroupCount,
                        unit: t_i18n(pluralize(sharedGroupCount, "group")),
                      })
                    : sharedGroupCount === 0
                      ? t_i18n("{count, plural, one {Personal OAuth credentials are shared with # person, who can act using this token.} other {Personal OAuth credentials are shared with # people, who can act using this token.}}", { count: sharedUserCount })
                      : t_i18n("Personal OAuth credentials are shared with {groups, plural, one {# group} other {# groups}} and {people, plural, one {# person} other {# people}}, who can act using this token.", { groups: sharedGroupCount, people: sharedUserCount })}
              </p>
            </div>
          )}
          {isSharedOauthIntegration(integration) && isPerUserIntegration(integration) && (
            <div className="rounded-sm bg-elevation-highlight p-3 flex items-start gap-2.5">
              <Users className="h-3.5 w-3.5 text-default-secondary shrink-0 mt-0.5" />
              <p className="content-compact text-default-secondary leading-relaxed">
                {t_i18n("Shared without its owner's connected account: everyone it is shared with authorizes with their own account, and the owner's stays theirs.")}
              </p>
            </div>
          )}
          {oauthCredentialsWithheld && (
            <div className="rounded-sm bg-feedback-alert-primary/5 p-3 flex items-start gap-2.5">
              <Users className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0 mt-0.5" />
              <p className="content-compact text-feedback-warning-primary leading-relaxed">
                {canChooseCredentialSharing
                  ? t_i18n("These personal OAuth credentials were never shared: other members cannot pick this integration in a chat or test it, but anything it is already bound to still uses them. To share them with everyone this integration is shared with, edit this configuration and save it.")
                  : t_i18n("These personal OAuth credentials are not shared: you cannot pick this integration in a chat or test it. Ask its owner or an admin to share them, or connect your own integration.")}
              </p>
            </div>
          )}
          {integration.oauth_connected_by_other && (
            <div className="rounded-sm bg-feedback-alert-primary/5 p-3 flex items-start gap-2.5">
              <Users className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0 mt-0.5" />
              <p className="content-compact text-feedback-warning-primary leading-relaxed">
                {integration.oauth_token_owner_name
                  ? t_i18n("This integration is connected with {name}'s account, not its owner's: it kept the previous owner's connection when it changed hands. Reconnect it to use the owner's own account.", { name: integration.oauth_token_owner_name })
                  : t_i18n("This integration is connected with someone else's account, not its owner's: it kept the previous owner's connection when it changed hands. Reconnect it to use the owner's own account.")}
              </p>
            </div>
          )}
          {integration.auth_type === "platform_managed" && (
            <div className="rounded-sm border border-alert-info bg-alert-info px-3 py-2">
              <p className="content-compact text-default-primary">{t_i18n("Managed by the platform: No credentials stored. Authentication is handled automatically at runtime.")}</p>
            </div>
          )}
          {/* Non-sensitive extra config values (e.g. base_url), above the registration
              notice: it says "the fields below", which must be only the editable ones. */}
          {integration.extra_config && Object.entries(integration.extra_config)
            .filter(([key]) => !isSecretKey(key) && !(hiddenExtraKeys ?? []).includes(key) && !registrationEditableKeys.includes(key))
            .map(([key, value]) => (
              <div key={key}>
                <div className="content-compact text-default-secondary mb-1">{editableExtraFields[key] || key}</div>
                {/* extra_config values can be non-strings (booleans from bundle
                    toggles, arrays like Jira sites) — serialize instead of
                    rendering "[object Object]" */}
                <p className="content-base font-mono-plex">{typeof value === "string" ? value : JSON.stringify(value)}</p>
              </div>
            ))
          }
          {isRegistrationOnly && (
            <div className="rounded-sm bg-accent/5 p-3">
              <p className="content-compact text-accent">
                {registrationEditableKeys.length > 0 && canEditRegistered
                  ? t_i18n("This integration is created and kept up to date by its platform's registration: only the fields below can be edited here.")
                  : t_i18n("This integration is created and kept up to date by its platform's registration, so its configuration cannot be edited here.")}
              </p>
            </div>
          )}
          {registrationEditableKeys.length > 0 && (
            <RegistrationEditableFields
              integration={integration}
              fieldKeys={registrationEditableKeys}
              labels={editableExtraFields}
              canEdit={canEditRegistered}
              options={catalogEntry?.extra_field_options}
            />
          )}
        </>
      }
      editView={
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <Label className="block text-default-primary">{t_i18n("Name")}</Label>
              <Input aria-label={t_i18n("Name")} value={editName} onChange={(e) => setEditName(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label className="block text-default-primary">{t_i18n("Tags")}</Label>
              <TagInput value={editTags} onChange={setEditTags} placeholder={t_i18n("Add tags…")} />
            </div>
          </div>

          <div className="space-y-1">
            <Label className="block text-default-primary">{t_i18n("Description")} <span className="font-normal text-default-secondary">{t_i18n("(passed to agents as tool context)")}</span></Label>
            <textarea
              value={editDescription}
              onChange={(e) => setEditDescription(e.target.value)}
              placeholder={t_i18n("Describe what this integration gives access to (e.g. 'Filigran company Notion workspace with all internal docs, events, and projects')…")}
              rows={2}
              className="w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base ring-offset-focus placeholder:text-default-disabled focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2 resize-none"
            />
          </div>

          <RelayRouteField
            value={editRelayGroupId}
            onChange={setEditRelayGroupId}
            destination={serviceUrlOf(editExtra)}
            required={!!catalogEntry?.requires_relay}
          />

          {/*
            One answer for the whole connection. Per-tool curation across
            ~2,350 integration tools is deferred, so the operator's unit of trust
            is the integration — and the alternative to this control was a curl
            against the API, which is not an interface anyone should need.
          */}
          {approvalSetByPlatform ? (
            <div className="rounded-sm border border-alert-info bg-alert-info px-3 py-2">
              <p className="content-compact text-default-primary">
                {t_i18n("Approval for a connected platform is set once for the platform, in Settings → Access and security → Tool approvals.")}
              </p>
            </div>
          ) : (
            <div className="space-y-2">
              <div className="flex items-center justify-between rounded-sm bg-elevation-hover px-3 py-2">
                <Label htmlFor="requires-approval" className="text-default-primary">
                  {t_i18n("Ask for approval before running its tools")}
                </Label>
                <Switch
                  id="requires-approval"
                  checked={editRequiresApproval}
                  onCheckedChange={setEditRequiresApproval}
                />
              </div>
              {!approvalsInUse && (
                <p className="content-compact text-default-secondary">
                  {t_i18n("No agent asks before running tools yet, so this changes nothing until one does.")}
                </p>
              )}
              {/* Warned before saving, not after: the point is to inform the
                  decision, and this one weakens a gate everywhere at once. */}
              {!editRequiresApproval && (
                <div className="rounded-sm bg-feedback-alert-primary/10 border border-feedback-alert-primary/20 p-3">
                  <p className="content-caption text-feedback-warning-primary dark:text-feedback-warning-primary">
                    {t_i18n("Every tool this integration provides will then run unattended: agents will act on {provider} without stopping to ask. Tools from other sources are unaffected.", {
                      provider: INTEGRATION_PROVIDER_NAMES[integration.provider] ?? integration.provider,
                    })}
                  </p>
                </div>
              )}
            </div>
          )}

          {/* Auth mode selector — when provider supports multiple auth types.
              Not for a spec-imported provider (OpenAPI): its credential type
              follows the spec's authentication option. */}
          {/* Also for an integration left on a type its provider does not
              support (#8386): it lists that type and the supported ones, so the
              row can be switched back. */}
          {!catalogEntry?.spec_import && authModeChoices.length > 1 && (
            <div className="space-y-1">
              <Label className="block text-default-primary">{t_i18n("Authentication mode")}</Label>
              <Select value={editAuthType} onValueChange={setEditAuthType}>
                <SelectTrigger aria-label={t_i18n("Authentication mode")}><SelectValue /></SelectTrigger>
                <SelectContent>
                  {authModeChoices.map((at) => (
                    <SelectItem key={at} value={at}>
                      {at === "api_key" ? t_i18n("API key") : at === "platform_managed" ? t_i18n("Managed by the platform") : at === "oauth" ? t_i18n("OAuth") : at}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}

          {editAuthType === "platform_managed" && (
            <div className="rounded-sm border border-alert-info bg-alert-info px-3 py-2">
              <p className="content-compact text-default-primary">{t_i18n("Managed by the platform: No credentials needed. Authentication is handled automatically at runtime.")}</p>
            </div>
          )}

          {/* Stored credentials never come back: the placeholder says whether
              one is stored, typing replaces it, leaving it keeps it. */}
          {(editAuthType === "oauth" || editAuthType === "client_credentials" || editAuthType === "oauth_password") && (
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label className="block text-default-primary">{t_i18n("Client ID")}</Label>
                <Input aria-label={t_i18n("Client ID")} type={showSecrets ? "text" : "password"} value={editClientId} onChange={(e) => setEditClientId(e.target.value)} placeholder={integration.has_client_id ? t_i18n("Stored: type to replace") : t_i18n("Paste new client ID...")} />
              </div>
              <SecretField
                label={t_i18n("Client Secret")}
                value={editClientSecret}
                onChange={setEditClientSecret}
                hasStoredValue={integration.has_client_secret}
                stored={integration.vault_secrets?.locations?.client_secret?.[0] ?? null}
                placeholder={t_i18n("Paste new client secret...")}
              />
            </div>
          )}

          {editAuthType === "oauth_password" && (
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <Label className="block text-default-primary">{t_i18n("Username")}</Label>
                <Input aria-label={t_i18n("Username")} value={editUsername} onChange={(e) => setEditUsername(e.target.value)} placeholder={integration.has_api_key ? t_i18n("Stored: type to replace") : ""} autoComplete="off" />
              </div>
              <div className="space-y-1">
                <Label className="block text-default-primary">{t_i18n("Password")}</Label>
                <Input aria-label={t_i18n("Password")} type={showSecrets ? "text" : "password"} value={editPassword} onChange={(e) => setEditPassword(e.target.value)} placeholder={integration.has_api_key ? t_i18n("Stored: type to replace") : ""} autoComplete="new-password" />
              </div>
            </div>
          )}

          {(editAuthType === "api_key" || editAuthType === "bot_token") && (
            <SecretField
              label={editAuthType === "bot_token" ? t_i18n("Bot token") : t_i18n("API key")}
              value={editApiKey}
              onChange={setEditApiKey}
              hasStoredValue={integration.has_api_key}
              stored={integration.vault_secrets?.locations?.api_key?.[0] ?? null}
              placeholder={t_i18n("Paste new key...")}
            />
          )}

          <ExtraFieldInputs
            mode="edit"
            fields={editableExtraFields}
            omitKeys={usesAdminOAuthApp(integration, catalogEntry, { clientId: editClientId, authType: editAuthType }) ? ["webhook_secret"] : undefined}
            values={editExtra}
            onChange={setEditExtra}
            options={catalogEntry?.extra_field_options}
            visibleWhen={catalogEntry?.extra_field_visible_when}
            inputs={catalogEntry?.extra_field_inputs}
            schemaTypeByKey={schemaTypeByKey}
            showSecrets={showSecrets}
          />
        </>
      }
    />
    </>
  );
}
