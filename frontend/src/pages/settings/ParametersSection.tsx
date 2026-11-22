// SPDX-License-Identifier: Apache-2.0

import { useEffect, useId, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import {
  AlertTriangle,
  Check,
  Copy,
  ExternalLink,
  Fingerprint,
  ImageIcon,
  KeyRound,
  Languages,
  LayoutTemplate,
  Palette,
  RotateCcw,
  ShieldCheck,
  Smartphone,
  Sparkles,
  Trash2,
  Type,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  SettingsCard,
  SettingsField,
  SettingsFieldGrid,
  SettingsGroupTitle,
  SettingsSaveBar,
  settingsFieldHintId,
  settingsFieldLabelId,
} from "@/components/ui/settings-form";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { Badge } from "@/components/ui/badge";
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from "@/components/ui/select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useFormatter, AVAILABLE_LANGUAGES, LANGUAGE_AUTO } from "@/i18n";
import { Tip } from "@/components/ui/tooltip";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { EditionBadge } from "@/components/ui/enterprise-upsell";
import { useToast } from "@/components/ui/toast";
import { FileUpload } from "@/components/ui/file-upload";
import { SettingsNotSet, SettingsReadField, SettingsReadGrid } from "@/components/ui/settings-read-view";
import { usePlatformSettings, useUpdatePlatformSettings } from "@/hooks/api/useAiAdoptionApi";
import { useResetOrganizationSetupPrompt } from "@/hooks/api/useOrganizationContextApi";
import { useAuth } from "@/hooks/useAuth";
import { useTemplateContainers } from "@/hooks/api/useTemplateContainerApi";
import { useOnIncludedAllowance } from "@/hooks/useEnterpriseUpsell";
import {
  usePlatformConfig, usePlatformName, usePlatformLoginMessageDefault, useXTMLicense, useIsXtmOne,
} from "@/hooks/usePlatformConfig";
import { formatCompactNumber, formatDate, pluralize } from "@/lib/utils";
import { COPY_FEEDBACK_DELAY, FILIGRAN_CONTACT_URL, XTM_ONE_TRIAL_URL } from "@/lib/constants";
import { AI_ENABLEMENT_ANCHOR } from "@/lib/settings-routes";
import type { PlatformSettings as PlatformSettingsT } from "@/types";
import { withBasePath } from "@/lib/base-path";
import { BrandAssetSourceCard } from "./BrandAssetSourceCard";
import { XtmHubRegistrationCard } from "./XtmHubRegistrationCard";
import { ColorField } from "./ColorField";
import { Spinner } from "@filigran/design-system";

const LOGO_MAX_BYTES = 512 * 1024;
const FAVICON_MAX_BYTES = 64 * 1024;
const OG_IMAGE_MAX_BYTES = 512 * 1024;
const FAVICON_ACCEPT = "image/png,image/jpeg,image/webp,image/x-icon,image/vnd.microsoft.icon,image/gif,.ico";
// Mirror backend caps in app/api/v1/settings.py to keep UI/API in lockstep.
const PLATFORM_NAME_MAX = 255;
const PLATFORM_DESCRIPTION_MAX = 1000;
const PLATFORM_LOGIN_MESSAGE_MAX = 5000;
const HEX_COLOR_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/;

function allowedFavicon(value: string | null | undefined): string | null {
  if (!value || value.toLowerCase().startsWith("data:image/svg+xml")) return null;
  return value;
}

interface BrandingDraft {
  platform_name: string;
  platform_logo: string | null;
  platform_description: string;
  platform_login_message: string;
  platform_accent_color: string;
  platform_background_color: string;
  platform_theme_color: string;
  platform_favicon: string | null;
  platform_og_image: string | null;
}

function draftFromSettings(s: PlatformSettingsT | undefined): BrandingDraft {
  return {
    platform_name: s?.platform_name ?? "",
    platform_logo: s?.platform_logo ?? s?.platform_logo_url ?? null,
    platform_description: s?.platform_description ?? "",
    platform_login_message: s?.platform_login_message ?? "",
    platform_accent_color: s?.platform_accent_color ?? "",
    platform_background_color: s?.platform_background_color ?? "",
    platform_theme_color: s?.platform_theme_color ?? "",
    platform_favicon: allowedFavicon(s?.platform_favicon),
    platform_og_image: s?.platform_og_image ?? null,
  };
}

export function ParametersSection() {
  // These three fields carried a bare <label> with no `for`, so they had no
  // accessible name at all — a screen reader announced "edit text" and nothing
  // else. It went unnoticed because the label happened to SIT next to the
  // input, which reads fine visually and let a positional test selector find
  // the field. The design system's Input wraps its input element, which broke that
  // adjacency and surfaced the missing association.
  const platformNameId = useId();
  const descriptionId = useId();
  const loginMessageId = useId();
  const { t_i18n } = useFormatter();
  const { data: settings, isLoading } = usePlatformSettings();
  const update = useUpdatePlatformSettings();
  const qc = useQueryClient();
  const toast = useToast();
  const platformConfig = usePlatformConfig();
  const isXtmOne = useIsXtmOne();
  const defaultName = usePlatformName();
  // Mode default (not the resolved override) so the placeholder previews the
  // fallback that applies once the field is cleared.
  const defaultLoginMessage = usePlatformLoginMessageDefault();
  const [draft, setDraft] = useState<BrandingDraft>(() => draftFromSettings(undefined));
  const [editing, setEditing] = useState(false);

  const view: BrandingDraft = editing ? draft : draftFromSettings(settings);

  const colorsValid = (["platform_theme_color", "platform_accent_color", "platform_background_color"] as const).every(
    (k) => draft[k] === "" || HEX_COLOR_RE.test(draft[k]),
  );

  const startEditing = () => {
    setDraft(draftFromSettings(settings));
    setEditing(true);
  };

  const handleSave = () => {
    update.mutate(
      {
        platform_name: draft.platform_name.trim() || null,
        platform_logo: draft.platform_logo,
        platform_description: draft.platform_description || null,
        platform_login_message: draft.platform_login_message || null,
        platform_accent_color: draft.platform_accent_color || null,
        platform_background_color: draft.platform_background_color || null,
        platform_theme_color: draft.platform_theme_color || null,
        platform_favicon: draft.platform_favicon,
        platform_og_image: draft.platform_og_image,
      },
      { onSuccess: () => setEditing(false) },
    );
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Spinner size="lg" tone="inherit" className="text-accent" label={t_i18n("Loading...")} />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <InstanceIdCard instanceId={settings?.instance_id ?? ""} />

      {isXtmOne && <XtmHubRegistrationCard />}

      {/* Enterprise License is rendered above Platform Branding so the
          licensing state (and its CTAs) is the first thing an admin sees
          when they land on the Parameters tab — it sets the context for
          every gated feature below. */}
      <LicenseManagementCard
        hasDbLicense={settings?.has_db_license ?? false}
        hasEnvLicense={settings?.has_env_license ?? false}
        onSaved={() => {
          qc.invalidateQueries({ queryKey: ["platform-settings"] });
          qc.invalidateQueries({ queryKey: ["platform-config"] });
        }}
      />

      <AiEnablementCard
        value={settings?.ai_enabled ?? null}
        deploymentDefault={settings?.ai_enabled_default ?? true}
        onSaved={() => {
          qc.invalidateQueries({ queryKey: ["platform-settings"] });
          qc.invalidateQueries({ queryKey: ["platform-config"] });
          /* And once more past the server's 5s cache. The immediate refetch can
             land on a replica whose own resolution is still warm and answer with
             the pre-toggle value, which the client would then hold — the toggle
             looks like it did nothing. Cheaper than cross-replica invalidation,
             which FR-003a explicitly does not require. */
          setTimeout(
            () => qc.refetchQueries({ queryKey: ["platform-config"] }),
            6000,
          );
        }}
      />

      <PlatformLanguageCard
        value={settings?.platform_language ?? LANGUAGE_AUTO}
        onSaved={() => {
          qc.invalidateQueries({ queryKey: ["platform-settings"] });
          qc.invalidateQueries({ queryKey: ["platform-config"] });
        }}
      />

      <DefaultPresentationTemplateCard
        value={settings?.default_presentation_template_id ?? null}
        onSaved={() => qc.invalidateQueries({ queryKey: ["platform-settings"] })}
      />

      <BrandAssetSourceCard />

      <SettingsCard
        icon={Palette}
        title={t_i18n("Platform Branding")}
        description={t_i18n("How the platform introduces itself: its name, logo, colors and the images browsers and share previews show.")}
        action={
          !editing ? (
            <Button variant="outline" onClick={startEditing}>
              {t_i18n("Edit")}
            </Button>
          ) : undefined
        }
      >
        {!editing ? (
          // At rest (#10855): the branding reads as tiles, never as disabled fields; Edit opens the form.
          <BrandingReadView
            view={view}
            defaultName={defaultName}
            defaultAccent={platformConfig?.platform_default_accent_color}
            defaultBackground={platformConfig?.platform_default_background_color}
            defaultTheme={platformConfig?.platform_theme_color}
          />
        ) : (
        <div className="space-y-6">
          {/* ── Identity ─────────────────────────────────────── */}
          <section className="space-y-4">
            <SettingsGroupTitle icon={Type}>{t_i18n("Identity")}</SettingsGroupTitle>
            <SettingsField
              id={platformNameId}
              label={t_i18n("Platform Name")}
              helper={t_i18n("Displayed in the page title and sidebar header.")}
              info={t_i18n("Replaces the product name in the browser tab and the sidebar header. Empty: the default product name is shown.")}
            >
              <Input
                id={platformNameId}
                aria-labelledby={settingsFieldLabelId(platformNameId)}
                aria-describedby={settingsFieldHintId(platformNameId)}
                value={view.platform_name}
                onChange={(e) => setDraft((d) => ({ ...d, platform_name: e.target.value }))}
                placeholder={defaultName}
                maxLength={PLATFORM_NAME_MAX}
              />
            </SettingsField>

            <FileUpload
              name="platform_logo"
              label={t_i18n("Logo")}
              help={t_i18n("Displayed in the sidebar and chat headers.")}
              info={t_i18n("Shown in the sidebar and the chat headers. PNG, JPEG, WebP or GIF up to 512 KB; a wide image about 32 px high reads best. When empty, the product logo is shown.")}
              value={view.platform_logo}
              maxBytes={LOGO_MAX_BYTES}
              previewHeight={32}
              onChange={(v) => setDraft((d) => ({ ...d, platform_logo: v }))}
              onError={(msg) => toast.toast({ title: t_i18n("Logo rejected"), description: msg, variant: "error" })}
            />

            <SettingsField
              id={descriptionId}
              label={t_i18n("Description")}
              helper={t_i18n("Used in browser tab description and social share previews.")}
              info={t_i18n("One sentence about the platform, used as the page description and in the preview a chat or social app draws when a link is shared. Empty: the default description is used.")}
            >
              <Input
                id={descriptionId}
                aria-labelledby={settingsFieldLabelId(descriptionId)}
                aria-describedby={settingsFieldHintId(descriptionId)}
                value={view.platform_description}
                onChange={(e) => setDraft((d) => ({ ...d, platform_description: e.target.value }))}
                placeholder={t_i18n("Next-gen enterprise agentic AI platform")}
                maxLength={PLATFORM_DESCRIPTION_MAX}
              />
            </SettingsField>

            <SettingsField
              id={loginMessageId}
              label={t_i18n("Login Message")}
              helper={t_i18n("Subtitle shown under the logo on the login screen. Supports Markdown (links, bold, lists) so you can point users to a policy or help page.")}
              info={t_i18n("Shown to everyone before they sign in, so never put internal details here. Empty: the default welcome line is shown.")}
            >
              <textarea
                id={loginMessageId}
                aria-describedby={settingsFieldHintId(loginMessageId)}
                value={view.platform_login_message}
                onChange={(e) => setDraft((d) => ({ ...d, platform_login_message: e.target.value }))}
                placeholder={defaultLoginMessage}
                maxLength={PLATFORM_LOGIN_MESSAGE_MAX}
                rows={3}
                className={`w-full rounded-sm bg-elevation-default border border-elevation-subtle px-3 py-2 content-base placeholder:text-default-secondary ${FIELD_FOCUS_RING} disabled:cursor-not-allowed disabled:opacity-50 resize-y`}
              />
            </SettingsField>
          </section>

          {/* ── In-App Colors ─────────────────────────────────── */}
          <section className="space-y-4">
            <SettingsGroupTitle icon={Palette}>{t_i18n("In-App Colors")}</SettingsGroupTitle>
            <SettingsFieldGrid>
              <ColorField
                label={t_i18n("Accent Color")}
                help={t_i18n("Buttons, links, active states, and highlights.")}
                info={t_i18n("The color of buttons, links, active states and highlights. Format #rrggbb (or #rgb, #rrggbbaa), for example #0fbcff. When empty, the product default is used.")}
                value={view.platform_accent_color}
                defaultValue={platformConfig?.platform_default_accent_color}
                onChange={(v) => setDraft((d) => ({ ...d, platform_accent_color: v }))}
              />
              <ColorField
                label={t_i18n("Background Color")}
                help={t_i18n("App surface color. Card and panel shades are derived automatically.")}
                info={t_i18n("The color of the app surface; card and panel shades are derived from it. Format #rrggbb (or #rgb, #rrggbbaa), for example #070d19. When empty, the product default is used.")}
                value={view.platform_background_color}
                defaultValue={platformConfig?.platform_default_background_color}
                onChange={(v) => setDraft((d) => ({ ...d, platform_background_color: v }))}
              />
            </SettingsFieldGrid>
          </section>

          {/* ── Browser & PWA ─────────────────────────────────── */}
          <section className="space-y-4">
            <SettingsGroupTitle icon={Smartphone}>{t_i18n("Browser & PWA")}</SettingsGroupTitle>
            <SettingsFieldGrid>
              <ColorField
                label={t_i18n("Theme Color")}
                help={t_i18n("Mobile browser address bar and PWA splash screen color.")}
                info={t_i18n("The color mobile browsers give the address bar and the installed app's splash screen. Format #rrggbb (or #rgb, #rrggbbaa), for example #001bda. When empty, the product default is used.")}
                value={view.platform_theme_color}
                defaultValue={platformConfig?.platform_theme_color}
                onChange={(v) => setDraft((d) => ({ ...d, platform_theme_color: v }))}
              />
            </SettingsFieldGrid>
          </section>

          {/* ── Media Assets ──────────────────────────────────── */}
          <section className="space-y-4">
            <SettingsGroupTitle icon={ImageIcon}>{t_i18n("Media Assets")}</SettingsGroupTitle>
            <FileUpload
              name="platform_favicon"
              label={t_i18n("Favicon")}
              help={t_i18n("Browser tab icon. Use .ico or a small raster image.")}
              info={t_i18n("The icon of the browser tab and of bookmarks. An .ico file or a small square PNG, up to 64 KB. When empty, the product icon is shown.")}
              value={view.platform_favicon}
              maxBytes={FAVICON_MAX_BYTES}
              previewHeight={24}
              accept={FAVICON_ACCEPT}
              onChange={(v) => setDraft((d) => ({ ...d, platform_favicon: v }))}
              onError={(msg) => toast.toast({ title: t_i18n("Favicon rejected"), description: msg, variant: "error" })}
            />

            <FileUpload
              name="platform_og_image"
              label={t_i18n("Social Preview Image (OG Image)")}
              help={t_i18n("Shown when the app URL is shared on social media.")}
              info={t_i18n("The image a chat or social app draws when a link to the platform is shared. About 1200 x 630 px reads best, up to 512 KB. When empty, the product image is used.")}
              value={view.platform_og_image}
              maxBytes={OG_IMAGE_MAX_BYTES}
              previewHeight={60}
              onChange={(v) => setDraft((d) => ({ ...d, platform_og_image: v }))}
              onError={(msg) => toast.toast({ title: t_i18n("OG image rejected"), description: msg, variant: "error" })}
            />
          </section>

          {/* The bar is a child of the whole form, never of a wrapper as tall as
              itself, so `sticky bottom-0` keeps Cancel and Save on screen. */}
          {editing && (
            <SettingsSaveBar
              dirty={JSON.stringify(draft) !== JSON.stringify(draftFromSettings(settings))}
              saving={update.isPending}
              saveDisabled={!colorsValid}
              onSave={handleSave}
              onCancel={() => setEditing(false)}
            />
          )}
        </div>
        )}
      </SettingsCard>

      {isXtmOne && <ResetOnboardingCard />}
    </div>
  );
}

/**
 * Reset onboarding: the guided setup is proposed again to administrators, as
 * after a fresh install (XTM One mode, administrators). Only the proposal's
 * answer and step progress are cleared; profile, integrations and fleets stay.
 */
function ResetOnboardingCard() {
  const { t_i18n } = useFormatter();
  const { user } = useAuth();
  const toast = useToast();
  const reset = useResetOrganizationSetupPrompt();
  const [ConfirmEl, confirm] = useConfirm();

  if (user?.role !== "admin") return null;

  const handleReset = async () => {
    const ok = await confirm({
      title: t_i18n("Reset onboarding?"),
      description: t_i18n(
        "The guided setup is proposed again to administrators. Your organization profile, integrations and fleets are not changed.",
      ),
      confirmLabel: t_i18n("Reset onboarding"),
      variant: "default",
    });
    if (!ok) return;
    reset.mutate(undefined, {
      onSuccess: () => toast.toast({ title: t_i18n("Onboarding reset"), variant: "success" }),
    });
  };

  return (
    <>
      <SettingsCard
        icon={RotateCcw}
        title={t_i18n("Onboarding")}
        description={t_i18n("Show the guided setup again now, as after a fresh install.")}
        action={
          <Button variant="outline" onClick={handleReset} disabled={reset.isPending}>
            {t_i18n("Reset onboarding")}
          </Button>
        }
      />
      {ConfirmEl}
    </>
  );
}

/** A colour at rest: its swatch and its hex, or the product default named as such. */
function ColorRead({ value, defaultValue }: { value: string; defaultValue?: string }) {
  const { t_i18n } = useFormatter();
  const shown = value || defaultValue || "";
  if (!shown) return null;
  return (
    <span className="inline-flex items-center gap-2">
      <span aria-hidden className="h-4 w-4 shrink-0 rounded-sm border border-elevation-subtle" style={{ backgroundColor: shown }} />
      <span className="font-mono-plex">{shown.toLowerCase()}</span>
      {!value && <SettingsNotSet label={t_i18n("Product default")} />}
    </span>
  );
}

/** An uploaded image at rest: a small preview, or nothing (the tile then reads the product default). */
function ImageRead({ src, alt, height }: { src: string | null; alt: string; height: number }) {
  if (!src) return null;
  return <img src={src} alt={alt} style={{ maxHeight: height }} className="max-w-full rounded-sm object-contain" />;
}

/** The Platform Branding card at rest (#10855). */
function BrandingReadView({
  view, defaultName, defaultAccent, defaultBackground, defaultTheme,
}: {
  view: BrandingDraft;
  defaultName: string;
  defaultAccent?: string;
  defaultBackground?: string;
  defaultTheme?: string;
}) {
  const { t_i18n } = useFormatter();
  // Every empty value reads muted, the way a colour does: the effective default
  // when there is one to show, then "Product default" (UX round 1 on #10870, B1).
  const productDefault = <SettingsNotSet label={t_i18n("Product default")} />;
  const namedDefault = (text: string) => (
    <span className="inline-flex flex-wrap items-baseline gap-2">
      <span>{text}</span>
      {productDefault}
    </span>
  );
  return (
    <SettingsReadGrid data-testid="settings.branding.read">
      <SettingsReadField label={t_i18n("Platform Name")} info={t_i18n("Replaces the product name in the browser tab and the sidebar header. Empty: the default product name is shown.")} value={view.platform_name.trim() || namedDefault(defaultName)} />
      <SettingsReadField label={t_i18n("Logo")} info={t_i18n("Shown in the sidebar and the chat headers. PNG, JPEG, WebP or GIF up to 512 KB; a wide image about 32 px high reads best. When empty, the product logo is shown.")} value={view.platform_logo ? <ImageRead src={view.platform_logo} alt={t_i18n("Logo")} height={32} /> : productDefault} />
      <SettingsReadField label={t_i18n("Description")} wide info={t_i18n("One sentence about the platform, used as the page description and in the preview a chat or social app draws when a link is shared. Empty: the default description is used.")} value={view.platform_description.trim() || productDefault} />
      <SettingsReadField label={t_i18n("Login Message")} wide info={t_i18n("Shown to everyone before they sign in, so never put internal details here. Empty: the default welcome line is shown.")} value={view.platform_login_message.trim() ? <span className="whitespace-pre-wrap">{view.platform_login_message}</span> : productDefault} />
      <SettingsReadField label={t_i18n("Accent Color")} value={<ColorRead value={view.platform_accent_color} defaultValue={defaultAccent} />} />
      <SettingsReadField label={t_i18n("Background Color")} value={<ColorRead value={view.platform_background_color} defaultValue={defaultBackground} />} />
      <SettingsReadField label={t_i18n("Theme Color")} value={<ColorRead value={view.platform_theme_color} defaultValue={defaultTheme} />} />
      <SettingsReadField label={t_i18n("Favicon")} value={view.platform_favicon ? <ImageRead src={view.platform_favicon} alt={t_i18n("Favicon")} height={24} /> : productDefault} />
      <SettingsReadField label={t_i18n("Social Preview Image (OG Image)")} wide value={view.platform_og_image ? <ImageRead src={view.platform_og_image} alt={t_i18n("Social Preview Image (OG Image)")} height={60} /> : productDefault} />
    </SettingsReadGrid>
  );
}

/** The three values the API accepts, as the Select's option keys. */
const AI_ON = "on";
const AI_OFF = "off";
const AI_FOLLOW = "follow";

function aiChoiceOf(value: boolean | null): string {
  if (value === null) return AI_FOLLOW;
  return value ? AI_ON : AI_OFF;
}

/**
 * Turning AI on or off on a running platform (US2).
 *
 * Three options rather than a switch plus a reset button: "follow the
 * deployment default" is a distinct state, not an absence of one, and a
 * two-position switch has nowhere to put it. It is also the shape that makes
 * the contract's warned-about mistake unrepresentable — a client cannot send
 * `false` meaning "reset" if "reset" is its own option.
 */
function AiEnablementCard({
  value,
  deploymentDefault,
  onSaved,
}: {
  value: boolean | null;
  deploymentDefault: boolean;
  onSaved: () => void;
}) {
  const { t_i18n } = useFormatter();
  const update = useUpdatePlatformSettings();
  const toast = useToast();
  const [ConfirmEl, confirm] = useConfirm();

  const selectId = useId();
  const current = aiChoiceOf(value);
  const resolved = value ?? deploymentDefault;

  // "Turn AI on" (AiLockedFooter) links to this card by its hash: bring it
  // into view and give it the focus once the page has rendered.
  const anchorRef = useRef<HTMLDivElement>(null);
  const { hash } = useLocation();
  useEffect(() => {
    if (hash !== `#${AI_ENABLEMENT_ANCHOR}`) return;
    const node = anchorRef.current;
    if (!node) return;
    node.scrollIntoView({ behavior: "auto", block: "start" });
    node.focus({ preventScroll: true });
  }, [hash]);

  const handleChange = async (choice: string) => {
    if (choice === current) return;
    const next = choice === AI_FOLLOW ? null : choice === AI_ON;
    const willBeOff = !(next ?? deploymentDefault);

    // Only on the way off, and stating what "off" actually does. Work already
    // running keeps going to its own timeout, which for a few minutes is
    // indistinguishable from the switch not having worked unless we say so.
    if (willBeOff && resolved) {
      const ok = await confirm({
        title: t_i18n("Turn off AI on this platform?"),
        description: t_i18n(
          "New AI work stops immediately and AI actions are refused. Runs already in progress are left to finish under their own time limit rather than being cancelled, so you may see activity for a few more minutes. Nothing is deleted, and turning AI back on restores it.",
        ),
        confirmLabel: t_i18n("Turn off AI"),
        variant: "warning",
      });
      if (!ok) return;
    }

    update.mutate(
      { ai_enabled: next },
      {
        onSuccess: () => {
          toast.toast({
            title: next === false || (next === null && !deploymentDefault)
              ? t_i18n("AI disabled")
              : t_i18n("AI enabled"),
            // Enabling provisions the built-in content before the request
            // returns, so by the time this shows, the platform is populated.
            variant: "success",
          });
          onSaved();
        },
        onError: () => {
          // The state change may already be committed with only the
          // provisioning behind it having failed, so the one thing we must not
          // do is leave the card showing the old value as if nothing happened.
          toast.toast({
            title: t_i18n("Could not change the AI setting"),
            description: t_i18n("Reload the page to see the current state."),
            variant: "error",
          });
          onSaved();
        },
      },
    );
  };

  return (
    <div
      id={AI_ENABLEMENT_ANCHOR}
      ref={anchorRef}
      tabIndex={-1}
      // A scroll target, not a control: clear the sticky app header (published
      // by the shell as --fds-header-height) plus the 16 px gap, and paint no
      // focus frame (the browser default outline is not the product ring).
      className="scroll-mt-[calc(var(--fds-header-height,68px)_+_1rem)] outline-none"
    >
      {ConfirmEl}
      <SettingsCard
        icon={Sparkles}
        title={t_i18n("Artificial Intelligence")}
        description={t_i18n("Whether this platform runs AI at all: agents, assistants, knowledge-base indexing and every model or embedding provider. Turning it on provisions the built-in content, which can take a moment. Existing data is always kept and stays readable either way.")}
      >
        <SettingsField
          id={selectId}
          label={t_i18n("AI on this platform")}
          helper={
            value !== null
              ? t_i18n("This is an explicit choice made here, overriding the deployment configuration.")
              : t_i18n("Follows the deployment configuration until a choice is made here.")
          }
          info={t_i18n("Enabled or Disabled is a choice stored on this platform and wins over the deployment configuration. Deployment default follows the value the platform was installed with. Turning AI off stops new AI work at once; nothing is deleted.")}
        >
        <div className="flex items-center gap-3">
          <Select value={current} onValueChange={handleChange} disabled={update.isPending}>
            <SelectTrigger
              id={selectId}
              className="max-w-md"
              aria-labelledby={settingsFieldLabelId(selectId)}
              aria-describedby={settingsFieldHintId(selectId)}
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={AI_ON}>{t_i18n("Enabled")}</SelectItem>
              <SelectItem value={AI_OFF}>{t_i18n("Disabled")}</SelectItem>
              <SelectItem value={AI_FOLLOW}>
                {deploymentDefault
                  ? t_i18n("Deployment default (enabled)")
                  : t_i18n("Deployment default (disabled)")}
              </SelectItem>
            </SelectContent>
          </Select>
          {update.isPending && <Spinner size="sm" tone="inherit" className="text-accent" />}
        </div>
        </SettingsField>
      </SettingsCard>
    </div>
  );
}

/**
 * Platform-wide UI language card — same behavior as OpenCTI's
 * "Platform language" setting: "Automatic" (default) resolves each user's
 * language from their browser; a concrete language applies to all users
 * unless they set a personal override in their profile.
 */
function PlatformLanguageCard({
  value,
  onSaved,
}: {
  value: string;
  onSaved: () => void;
}) {
  const { t_i18n } = useFormatter();
  const update = useUpdatePlatformSettings();
  const toast = useToast();
  const selectId = useId();

  const handleChange = (lang: string) => {
    if (lang === value) return;
    update.mutate(
      { platform_language: lang },
      {
        onSuccess: () => {
          toast.toast({ title: t_i18n("Platform language updated"), variant: "success" });
          onSaved();
        },
      },
    );
  };

  return (
    <SettingsCard
      icon={Languages}
      title={t_i18n("Platform Language")}
      description={t_i18n("The language the interface opens in for every user who has not chosen one.")}
    >
      <SettingsField
        id={selectId}
        label={t_i18n("Default language")}
        helper={t_i18n("Users can still pick their own language in their profile.")}
        info={t_i18n("Automatic reads each person's browser language. A language chosen here applies to every user, except those who set their own in their profile.")}
      >
        <Select value={value} onValueChange={handleChange} disabled={update.isPending}>
          <SelectTrigger
            id={selectId}
            className="max-w-md"
            aria-labelledby={settingsFieldLabelId(selectId)}
            aria-describedby={settingsFieldHintId(selectId)}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={LANGUAGE_AUTO}>
              {t_i18n("Automatic")}
            </SelectItem>
            {AVAILABLE_LANGUAGES.map(({ value: lang, label }) => (
              <SelectItem key={lang} value={lang}>{label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </SettingsField>
    </SettingsCard>
  );
}

/** Sentinel for "use the seeded brand default" — an empty string is not a valid
 *  Radix SelectItem value, and the persisted value for it is `null`. */
const BRAND_DEFAULT_TEMPLATE = "__brand_default__";

function DefaultPresentationTemplateCard({
  value,
  onSaved,
}: {
  value: string | null;
  onSaved: () => void;
}) {
  const { t_i18n } = useFormatter();
  const update = useUpdatePlatformSettings();
  const toast = useToast();
  const { data: templates = [], isLoading } = useTemplateContainers({ kind: "presentation" });
  const selectId = useId();
  // Only a company-managed template is a valid platform default: a deck is
  // themed with the template resolved at generation time and re-checked against
  // the READER's visibility, so a private one would leave every other user's
  // deck unthemed. The API refuses anything else with a 400 — this just hides
  // the options that would fail.
  const eligible = templates.filter((t) => t.is_company_managed);
  // A stale id (template deleted, or made private since) would otherwise pin
  // the select to a blank value; show it as the brand default it resolves to.
  const selected = value && eligible.some((t) => t.id === value) ? value : BRAND_DEFAULT_TEMPLATE;

  const handleChange = (next: string) => {
    if (next === selected) return;
    update.mutate(
      { default_presentation_template_id: next === BRAND_DEFAULT_TEMPLATE ? null : next },
      {
        onSuccess: () => {
          toast.toast({ title: t_i18n("Default presentation template updated"), variant: "success" });
          onSaved();
        },
      },
    );
  };

  return (
    <SettingsCard
      icon={LayoutTemplate}
      title={t_i18n("Default Presentation Template")}
      description={t_i18n("The template a presentation uses when nobody chose one.")}
      action={
        <Button variant="outline" asChild>
          <Link to="/templates">{t_i18n("Manage templates")}</Link>
        </Button>
      }
    >
      <SettingsField
        id={selectId}
        label={t_i18n("Template")}
        helper={t_i18n("Only company-managed presentation templates are listed.")}
        info={t_i18n("Used when a deck is generated without a template: by automations, and in chats where the assistant cannot ask. In a chat, a user who can see several templates is asked which one to use. When set to the seeded brand template, decks wear the product brand.")}
      >
        <SearchableSelect
          id={selectId}
          aria-labelledby={settingsFieldLabelId(selectId)}
          aria-describedby={settingsFieldHintId(selectId)}
          className="max-w-md"
          options={eligible.map((tpl) => ({
            value: tpl.id,
            label: tpl.name,
            description: tpl.is_builtin ? t_i18n("Built-in") : undefined,
          }))}
          pinned={[{ value: BRAND_DEFAULT_TEMPLATE, label: t_i18n("Seeded brand template") }]}
          value={selected}
          onValueChange={handleChange}
          disabled={update.isPending || isLoading}
        />
      </SettingsField>
    </SettingsCard>
  );
}

function InstanceIdCard({ instanceId }: { instanceId: string }) {
  const { t_i18n } = useFormatter();
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    if (!instanceId) return;
    navigator.clipboard.writeText(instanceId);
    setCopied(true);
    setTimeout(() => setCopied(false), COPY_FEEDBACK_DELAY);
  };

  return (
    <SettingsCard
      icon={Fingerprint}
      title={t_i18n("Instance ID")}
      description={t_i18n("Unique identifier for this platform instance. Use this when generating a license certificate.")}
    >
        <div className="flex items-center gap-2">
          <code className="flex-1 rounded-sm bg-elevation-default border border-elevation-subtle px-3 py-2 content-base font-mono-plex select-all truncate">
            {instanceId || "—"}
          </code>
          <Tip content={copied ? t_i18n("Copied!") : t_i18n("Copy to clipboard")}>
            <Button
              size="icon"
              variant="outline"
              className="shrink-0"
              onClick={handleCopy}
              disabled={!instanceId}
              aria-label={t_i18n("Copy to clipboard")}
            >
              {copied ? <Check className="h-4 w-4 text-feedback-success-primary" /> : <Copy className="h-4 w-4" />}
            </Button>
          </Tip>
        </div>
    </SettingsCard>
  );
}

const SUB_LICENSE_PRODUCTS: { key: "opencti_ids" | "openaev_ids" | "opencrq_ids"; name: string; logo: string }[] = [
  { key: "opencti_ids", name: "OpenCTI", logo: withBasePath("/logos/opencti.svg") },
  { key: "openaev_ids", name: "OpenAEV", logo: withBasePath("/logos/openaev.svg") },
  { key: "opencrq_ids", name: "OpenCRQ", logo: withBasePath("/logos/opencrq.svg") },
];

function SubLicensesDisplay({ subLicenses }: { subLicenses?: { opencti_ids: string[]; openaev_ids: string[]; opencrq_ids: string[] } }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-3">
      <SettingsGroupTitle>{t_i18n("Sub-Licenses")}</SettingsGroupTitle>
      <div className="space-y-1.5">
        {SUB_LICENSE_PRODUCTS.map(({ key, name, logo }) => {
          const ids = subLicenses?.[key] ?? [];
          const licensed = ids.length > 0;
          const isGlobal = ids.includes("global");
          return (
            <div key={key} className="flex items-center gap-2.5 rounded-sm border px-3 py-2 bg-elevation-hover border-elevation-subtle-soft">
              <img src={logo} alt={name} className={`h-5 w-5 ${licensed ? "" : "opacity-40"}`} onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }} />
              <span className={`content-base font-medium flex-1 ${licensed ? "" : "text-default-secondary"}`}>{name}</span>
              {licensed ? (
                isGlobal ? (
                  <Badge variant="outline" className="content-caption px-1.5 py-0 h-4 border-accent/30 text-accent">{t_i18n("Global")}</Badge>
                ) : (
                  <Tip content={ids.join(", ")}>
                    <Badge variant="outline" className="content-caption px-1.5 py-0 h-4">
                      {ids.length} {t_i18n(pluralize(ids.length, "instance"))}
                    </Badge>
                  </Tip>
                )
              ) : (
                <span className="content-compact text-default-secondary">{t_i18n("Not included")}</span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function LicenseManagementCard({
  hasDbLicense,
  hasEnvLicense,
  onSaved,
}: {
  hasDbLicense: boolean;
  hasEnvLicense: boolean;
  onSaved: () => void;
}) {
  const { t_i18n } = useFormatter();
  const pc = usePlatformConfig();
  const xtmLicense = useXTMLicense();
  const update = useUpdatePlatformSettings();
  const [showInstall, setShowInstall] = useState(false);
  const [pem, setPem] = useState("");
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const pemId = useId();
  const [ConfirmEl, confirm] = useConfirm();

  const hasLicense = xtmLicense?.valid;
  const source = xtmLicense?.license_source ?? "none";
  const isFromEnv = source === "env_var";
  // Editions are an XTM One concept — Copilot mode keeps the license-status
  // badge and copy (its edition is always "").
  const isXtmOne = useIsXtmOne();
  // Enterprise Edition inherited from a connected EE platform, no Filigran license
  // of its own: fully activated, on the included allowance. The card then
  // invites a license for capacity, never for capability.
  const onIncludedAllowance = useOnIncludedAllowance();

  const handleInstall = () => {
    const val = textareaRef.current?.value || pem;
    if (!val.trim()) {
      // Required field: an empty submit shows the inline error and keeps
      // the focus in the textarea, never a silent no-op.
      setError(t_i18n("Paste the license certificate."));
      textareaRef.current?.focus();
      return;
    }
    setError(null);
    update.mutate(
      { enterprise_license: val } as Record<string, unknown>,
      {
        onSuccess: () => {
          setShowInstall(false);
          setPem("");
          setError(null);
          onSaved();
        },
        onError: (err: Error) => {
          setError(err.message || t_i18n("Failed to save license"));
          textareaRef.current?.focus();
        },
      },
    );
  };

  const handleRemove = async () => {
    const ok = await confirm({
      title: t_i18n("Remove Enterprise License"),
      description: t_i18n("This will remove the database-stored license and its agentic capacity. Unless a license is set via environment variable, the deployment keeps Enterprise Edition only through a connected Enterprise Edition platform (with the included allowance), and otherwise falls back to Community Edition with AI off. Are you sure?"),
      confirmLabel: t_i18n("Remove License"),
      variant: "danger",
    });
    if (!ok) return;
    update.mutate(
      { enterprise_license: "" } as Record<string, unknown>,
      {
        onSuccess: () => {
          onSaved();
        },
      },
    );
  };

  return (
    <>
      <SettingsCard
        icon={KeyRound}
        title={t_i18n("Enterprise License")}
        description={t_i18n("The Filigran license of this platform: its edition, its agentic capacity and the products it covers.")}
        action={
          <>
            {isXtmOne ? (
              <EditionBadge />
            ) : hasLicense ? (
              <Badge className="bg-feedback-success-primary/20 text-feedback-success-primary border-feedback-success-primary/30">
                {xtmLicense.license_type === "trial" ? t_i18n("Trial") : t_i18n("Active")}
              </Badge>
            ) : (
              <Badge variant="warning">{t_i18n("Not Activated")}</Badge>
            )}
          </>
        }
      >
        <div className="space-y-5">
          {hasLicense && xtmLicense ? (
            <>
              {/* Three-column balanced layout */}
              <div className="grid md:grid-cols-2 gap-6">
                {/* License details */}
                <div className="space-y-3">
                  <SettingsGroupTitle>{t_i18n("License Details")}</SettingsGroupTitle>
                  <div className="space-y-2 content-base">
                    {xtmLicense.customer && (
                      <div>
                        <span className="text-default-secondary content-compact">{t_i18n("Organization")}</span>
                        <p className="font-medium">{xtmLicense.customer}</p>
                      </div>
                    )}
                    <div>
                      <span className="text-default-secondary content-compact">{t_i18n("Type")}</span>
                      <p className="font-medium capitalize">{xtmLicense.license_type}</p>
                    </div>
                    {xtmLicense.expiration && (
                      <div>
                        <span className="text-default-secondary content-compact">{t_i18n("Expires")}</span>
                        <p className={`font-medium ${xtmLicense.expiration_prevention ? "text-feedback-warning-primary" : ""}`}>
                          {formatDate(xtmLicense.expiration)}
                          {xtmLicense.expiration_prevention && (
                            <Tip content={t_i18n("License expires within 3 months")}>
                              <AlertTriangle className="inline h-3 w-3 ml-1 text-feedback-warning-primary" />
                            </Tip>
                          )}
                        </p>
                      </div>
                    )}
                    <div>
                      <span className="text-default-secondary content-compact">{t_i18n("Source")}</span>
                      <p className="font-medium">
                        {isFromEnv ? (
                          <Tip content={t_i18n("License is set via the ENTERPRISE_LICENSE environment variable")}>
                            <span className="flex items-center gap-1.5">
                              {t_i18n("Environment variable")}
                              <Badge variant="outline" className="content-caption px-1.5 py-0 h-4">ENV</Badge>
                            </span>
                          </Tip>
                        ) : (
                          t_i18n("Database")
                        )}
                      </p>
                    </div>
                    <div>
                      <span className="text-default-secondary content-compact">{t_i18n("Agentic capacity")}</span>
                      <p className="font-medium tabular-nums">
                        {[
                          { value: xtmLicense.agentic_quotas.daily, unit: t_i18n("day") },
                          { value: xtmLicense.agentic_quotas.monthly, unit: t_i18n("month") },
                          { value: xtmLicense.agentic_quotas.yearly, unit: t_i18n("year") },
                        ]
                          .filter((q): q is { value: number; unit: string } => q.value != null)
                          .map((q) => `${formatCompactNumber(q.value)} / ${q.unit}`)
                          .join(" · ") || t_i18n("Unlimited")}
                      </p>
                    </div>
                  </div>
                </div>

                {/* Sub-licenses */}
                <SubLicensesDisplay subLicenses={xtmLicense.sub_licenses} />
              </div>

              {isFromEnv && (
                <div className="rounded-sm bg-feedback-alert-primary/[0.06] border border-feedback-alert-primary/20 p-3 flex items-start gap-2.5">
                  <AlertTriangle className="h-4 w-4 text-feedback-warning-primary shrink-0 mt-0.5" />
                  <div className="content-compact text-default-secondary">
                    <p className="font-medium text-feedback-warning-primary mb-1">{t_i18n("Environment variable override active")}</p>
                    <p>
                      {t_i18n("The")} <code className="text-feedback-warning-primary">ENTERPRISE_LICENSE</code> {t_i18n("environment variable takes priority over any license stored in the database. To manage the license from this UI, remove the environment variable and restart.")}
                    </p>
                  </div>
                </div>
              )}

              {!isFromEnv && (
                <div className="flex items-center gap-2 pt-1">
                  <Button
                    variant="outline"
                    className="gap-1.5"
                    onClick={() => { setPem(""); setError(null); setShowInstall(true); }}
                  >
                    <KeyRound className="h-4 w-4" />
                    {t_i18n("Update License")}
                  </Button>
                  {hasDbLicense && (
                    <Button
                      variant="outline"
                      className="gap-1.5 text-feedback-error-primary hover:text-feedback-error-primary hover:bg-feedback-error-primary/10 border-feedback-error-primary/30"
                      onClick={handleRemove}
                      disabled={update.isPending}
                    >
                      <Trash2 className="h-4 w-4" />
                      {t_i18n("Remove License")}
                    </Button>
                  )}
                </div>
              )}
            </>
          ) : (
            <>
              {onIncludedAllowance ? (
                <div className="space-y-2 content-base text-default-secondary">
                  <p>
                    {t_i18n("Enterprise Edition is active through your connected Enterprise Edition platform: every capability is on, under the included agentic allowance ({daily} per day, {monthly} per month, {yearly} per year, shared across the deployment).", {
                      daily: pc?.agentic_quotas.daily ?? 0,
                      monthly: pc?.agentic_quotas.monthly ?? 0,
                      yearly: pc?.agentic_quotas.yearly ?? 0,
                    })}
                  </p>
                  <p>
                    {t_i18n("Install a Filigran license to carry your own agentic capacity, sized to your usage.")}
                  </p>
                </div>
              ) : isXtmOne ? (
                <div className="space-y-2 content-base text-default-secondary">
                  <p>
                    {t_i18n("Community Edition: AI is not activated on this deployment.")}
                  </p>
                  <p>
                    {t_i18n("Everything is already installed. Connect an Enterprise Edition OpenCTI, OpenAEV or OpenCRQ platform, or install a Filigran license, to unlock agents, chat and automations - no redeploy.")}
                  </p>
                </div>
              ) : (
                <p className="content-base text-default-secondary">
                  {t_i18n("No license detected. Install a Filigran license to use this platform.")}
                </p>
              )}
              {hasEnvLicense && (
                <div className="rounded-sm bg-feedback-alert-primary/[0.06] border border-feedback-alert-primary/20 p-3 flex items-start gap-2.5">
                  <AlertTriangle className="h-4 w-4 text-feedback-warning-primary shrink-0 mt-0.5" />
                  <p className="content-compact text-default-secondary">
                    {t_i18n("An")} <code className="text-feedback-warning-primary">ENTERPRISE_LICENSE</code> {t_i18n("environment variable is set but the license failed validation. Check that it contains a valid PEM certificate.")}
                  </p>
                </div>
              )}
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  className="gap-1.5"
                  onClick={() => { setPem(""); setError(null); setShowInstall(true); }}
                >
                  <KeyRound className="h-4 w-4" />
                  {t_i18n("Install License")}
                </Button>
                {isXtmOne && !onIncludedAllowance && (
                  <Button
                    variant="outline"
                    className="gap-1.5"
                    onClick={() => window.open(XTM_ONE_TRIAL_URL, "_blank", "noopener,noreferrer")}
                  >
                    <Sparkles className="h-4 w-4" />
                    {t_i18n("Start free trial")}
                  </Button>
                )}
                <Button
                  variant="outline"
                  className="gap-1.5"
                  onClick={() => window.open(FILIGRAN_CONTACT_URL, "_blank", "noopener,noreferrer")}
                >
                  <ExternalLink className="h-4 w-4" />
                  {t_i18n("Contact Filigran")}
                </Button>
              </div>
            </>
          )}
        </div>
      </SettingsCard>

      {showInstall && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-scrim backdrop-blur-sm animate-fade-in">
          <Card className="w-full max-w-lg mx-4 shadow-[0_0_60px_color-mix(in_srgb,_var(--color-accent)_calc(0.1_*_100%),_transparent)]">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <KeyRound className="h-4 w-4 text-accent" />
                {hasLicense ? t_i18n("Update Enterprise License") : t_i18n("Install Enterprise License")}
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {hasEnvLicense && (
                <div className="rounded-sm bg-feedback-alert-primary/[0.06] border border-feedback-alert-primary/20 p-2.5 flex items-start gap-2">
                  <AlertTriangle className="h-3.5 w-3.5 text-feedback-warning-primary shrink-0 mt-0.5" />
                  <p className="content-compact text-default-secondary">
                    {t_i18n("The")} <code className="text-feedback-warning-primary">ENTERPRISE_LICENSE</code> {t_i18n("environment variable is set and will take priority. The database license will only be used if the environment variable is removed.")}
                  </p>
                </div>
              )}
              <SettingsField
                id={pemId}
                label={t_i18n("License certificate (PEM)")}
                helper={t_i18n("The whole certificate Filigran sent you, from the BEGIN CERTIFICATE line to the END CERTIFICATE line.")}
                info={t_i18n("The certificate is checked when you save. An invalid or expired certificate is refused and the current license stays in place.")}
                error={error}
                required
              >
                <textarea
                  id={pemId}
                  ref={textareaRef}
                  defaultValue={pem}
                  onChange={(e) => setPem(e.target.value)}
                  aria-describedby={settingsFieldHintId(pemId)}
                  placeholder={`-----BEGIN CERTIFICATE-----\n${t_i18n("Paste your Filigran license PEM here...")}\n-----END CERTIFICATE-----`}
                  className={`w-full h-40 rounded-sm bg-elevation-default border border-elevation-subtle px-3 py-2 content-base font-mono-plex placeholder:text-default-secondary ${FIELD_FOCUS_RING} resize-none aria-[invalid=true]:border-feedback-error-primary`}
                />
              </SettingsField>
              <div className="rounded-sm bg-accent/5 border border-accent/10 p-3 space-y-1.5">
                <p className="content-compact text-default-secondary">
                  {t_i18n("Don't have a license?")}{" "}
                  <a href={FILIGRAN_CONTACT_URL} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                    {t_i18n("Contact Filigran")}
                  </a>{" "}
                  {t_i18n("to purchase one.")}
                </p>
                <p className="content-compact text-default-secondary">
                  {t_i18n("You can also set the")} <code className="text-accent">ENTERPRISE_LICENSE</code>{" "}
                  {t_i18n("environment variable and restart the platform.")}
                </p>
              </div>
              <div className="flex gap-2 justify-end">
                <Button variant="ghost" onClick={() => setShowInstall(false)}>
                  {t_i18n("Cancel")}
                </Button>
                <Button
                  onClick={handleInstall}
                  disabled={update.isPending} aria-busy={update.isPending || undefined}>
                  {update.isPending && <Spinner size="sm" tone="inherit" />}{!update.isPending && <ShieldCheck className="h-4 w-4 mr-1.5" />}
                  {update.isPending ? t_i18n("Validating...") : hasLicense ? t_i18n("Update License") : t_i18n("Install License")}
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {ConfirmEl}
    </>
  );
}
