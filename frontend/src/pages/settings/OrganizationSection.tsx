// SPDX-License-Identifier: Apache-2.0

/**
 * Settings > Organization (#8987): the organization profile every agent and
 * fleet run reads (get_organization_context, the "Organization context" block
 * of a fleet run). An administrator fills it once; each save is a new
 * version. The overview names how complete it is and the fields not filled,
 * the same list the agents report instead of guessing; a missing field opens
 * the form on it. The primary assets sync from OpenCRQ as an Inventory source
 * (CTEM > Inventory > Sources, #1754): the card names that source and its last
 * sync, and has no OpenCRQ button. Administrators
 * only. Built on the settings form kit (#9173, S1c). Five real tabs (#12329):
 * every card sits in one tab (lib/organization-profile-tabs.ts), the tab is a
 * path segment (/settings/organization/profile/<tab>), only the active panel is rendered, and the whole-profile
 * form spans the tabs: each tab carries its completeness and its unsaved,
 * drafted and invalid markers.
 */
import { type ReactNode, useEffect, useId, useMemo, useRef, useState } from "react";
import { landOnCard } from "@/lib/land-on-card";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Bot, Building2, Coins, Import, Link2, Lock, MessageSquare, Package, Pencil, Plus, RefreshCw, Server, Shield, Sparkles, Ticket } from "lucide-react";
import { TagInput } from "@/components/ui/tag-input";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CountryFlag } from "@/components/ui/country-flag";
import { ExpandableChipList } from "@/components/ui/expandable-chip-list";
import { SECTION_BAR_ANCHOR_CLASS } from "@/components/ui/section-index";
import { InfoTip } from "@/components/ui/info-tip";
import { SettingsReadField, SettingsReadGrid } from "@/components/ui/settings-read-view";
import { DataTable, type DataTableColumn, type DataTableFilter, type SortValue } from "@/components/ui/data-table";
import { Input } from "@/components/ui/input";
import { MultiSelect } from "@/components/ui/multi-select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Skeleton } from "@/components/ui/skeleton";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  SettingsCard,
  type SettingsCardFold,
  SettingsField,
  SettingsFieldGrid,
  SettingsGroupTitle,
  SettingsSaveBar,
  settingsFieldHintId,
  settingsFieldLabelId,
} from "@/components/ui/settings-form";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { ISO_3166_NUMERIC } from "@/components/presentation/blocks/chart-map-countries";
import { useIntegrationElements, useIntegrations } from "@/hooks/api/useIntegrationApi";
import { useToast } from "@/components/ui/toast";
import { AgenticGateDialogControlled } from "@/components/ui/license-gate-dialog";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { useAgenticAIEnabled, useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { OrganizationAiDraftDialog } from "./OrganizationAiDraftDialog";
import { OrganizationAssetsImportDialog } from "./OrganizationAssetsImportDialog";
import { OrganizationFillAgentDialog } from "./OrganizationFillAgentDialog";
import { OrganizationImportDialog, PROFILE_IMPORT_SECTIONS } from "./OrganizationImportDialog";
import { EXPOSURE_SECTIONS, type ExposureCard, type ExposureIssue, OrganizationExposureCards, exposureIssueFromRefusal, exposureIssues } from "./OrganizationExposureCards";
import { primeOrganizationOnboarding } from "@/lib/organization-onboarding";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import { TruncatedText } from "@/components/ui/truncated-text";
import {
  type OrganizationDraftResponse,
  type OrganizationProfile,
  type ProfileSource,
  useDraftOrganizationContext,
  useOrganizationContext,
  useOrganizationPirs,
  usePatchOrganizationContext,
  useSaveOrganizationContext,
} from "@/hooks/api/useOrganizationContextApi";
import { useAssetGroups, useCanonicalAssets } from "@/hooks/api/useOrganizationAssetsApi";
import { useFormatter } from "@/i18n";
import {
  type AmountField,
  CRITICALITY_LABELS,
  CURRENCIES,
  FIELD_HELP,
  FIELD_LABELS,
  IMPORT_SOURCE_NAMES,
  IMPORT_SOURCE_PROVIDER,
  PLATFORM_NAMES,
  type ProfileForm,
  SIZE_BANDS,
  TICKETING_PROJECT_ELEMENT,
  formatAmount,
  isSupportedRecordType,
  fromForm,
  toForm,
  unreadablePlatform,
} from "@/lib/organization-profile";
import { distinctName, OrganizationPirsView, pirLabels, PirsUnreadable, usePirText } from "./OrganizationPirs";
import { OrganizationTicketingSync } from "./OrganizationTicketingSync";
import { type BulkField, type ListEditColumn, ListSaveBar, OrganizationListEditor } from "./OrganizationListEditor";
import { OrganizationListPasteDialog } from "./OrganizationListPasteDialog";
import { addRow, addRows, attentionCount, changeCounts, committedValues, type EditRow, fieldSuggestions, isRepository, LIST_ISSUES, LIST_RULES, rowIssues, startRows } from "@/lib/organization-list-edit";
import type { PasteSpec } from "@/lib/organization-list-paste";
import { TrackedOrganizations } from "./TrackedOrganizations";
import { OrganizationSourcesSyncs } from "./OrganizationSourcesSyncs";
import { useSyncedRemoval } from "./OrganizationSyncedRemoval";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { columnEditable, LOCAL_FIELDS, SOURCE_KIND_LABELS, sourceKind, sourcePlatform, type Provenance } from "@/lib/organization-provenance";
import { OrganizationContextInjectionCard } from "./OrganizationContextInjectionCard";
import { OrganizationCrownJewelsRestoreDialog } from "./OrganizationCrownJewelsRestoreDialog";
import { OrganizationProposalsCard } from "./OrganizationProposalsCard";
import { OrganizationEvidenceHealthCard } from "./OrganizationEvidenceHealthCard";
import { OrganizationOutsideLensCard } from "./OrganizationOutsideLensCard";
import {
  PROFILE_CARD_PARAM,
  PROFILE_CARD_TAB,
  PROFILE_TABS,
  PROFILE_TAB_LABELS,
  type ProfileCard,
  type ProfileTab,
  changedTabs,
  isInventoryTab,
  profileFieldHref,
  groupByTab,
  parseProfileCard,
  profileTabOfPath,
  profileTabPath,
  tabOfField,
  tabsOfFields,
} from "@/lib/organization-profile-tabs";
import { Icon, Spinner } from "@filigran/design-system";
import { BackButton } from "@/components/ui/back-button";

const NO_SIZE = "none";
const NO_VALUE = "none";

/** Every ISO 3166-1 alpha-2 code (the list the backend validates against). */
const COUNTRY_CODES = Object.keys(ISO_3166_NUMERIC);

/** The lists edited in their own card (#12256). */
const LIST_KEYS = ["crown_jewels", "shipped_products", "tech_stack"] as const;
/** @i18n-keys */
const LIST_TITLES = {
  crown_jewels: "Primary assets and business services",
  shipped_products: "Shipped products",
  tech_stack: "Software",
} as const;
type ListKey = (typeof LIST_KEYS)[number];
/** The exposure tables edited in their own card (#12256 slice 2). @i18n-keys */
const EXPOSURE_TITLES = {
  assets_exposure: "Assets and exposure",
  asset_groups: "Asset groups",
  prioritization_policy: "Prioritization policy",
  accepted_risks: "Accepted risks",
  enterprise_knowledge: "Enterprise knowledge",
} as const;
type ListEdits = { [K in ListKey]?: { rows: EditRow<ProfileForm[K][number]>[]; open: string | null } };

/** The element a missing field opens, by contract path: a field id suffix, or the card of a list. */
const MISSING_TARGET: Record<string, string> = {
  "risk_appetite.annual_loss_threshold": "annual_loss_threshold",
  "risk_appetite.approval_threshold": "approval_threshold",
  ticketing: "ticketing-integration",
};

/**
 * The fixed columns of the primary assets table, read and edit (UX r2 B1 and B2 on #1174). Below a
 * 72rem card (1080) Assets and Source step aside, so Business service and Owner keep about 150 px
 * each, their headers whole and their inputs usable on a synced row. Business service and Owner
 * share what is left; OpenCRQ holds its header and the sort mark.
 */
export const JEWEL_WIDTHS = { select: 32, name: 240, criticality: 120, assets: 200, opencrq: 112, source: 112, actions: 96 } as const;
/** The card width below which Assets and Source of the primary assets table go to the Columns pill. */
const JEWEL_STEP_ASIDE = "6xl" as const;

/** The values of the primary assets' OpenCRQ filter. @i18n-keys */
const OPENCRQ_LINK_LABELS = { linked: "Linked to OpenCRQ", not_linked: "Not linked to OpenCRQ" } as const;
/**
 * An amount field: the room for its unit goes on the inner input element (the
 * library's className lands on the wrapper), so the field keeps the column
 * width and the unit sits inside its border (UX round 2 B1).
 */
const AMOUNT_INPUT_CLASS = "[&_input]:pr-36";

/**
 * The cards of the profile one page mounts alone (#1178, Sam 22 Nov 2026): CTEM > Inventory edits the software inventory,
 * the assets and exposure, the asset groups, the policy and the outside lens with the same cards,
 * the same per-card edit and the same Save as Settings, nothing stored twice. Without it the whole profile page.
 */
export type OrganizationEmbed = {
  cards: readonly ProfileCard[];
  /** A reader's view (#1511): the cards at rest, no Edit (the outside lens and evidence health cards gate theirs too). */
  readOnly?: boolean;
};

export function OrganizationSection({ embed }: { embed?: OrganizationEmbed } = {}) {
  const { t_i18n, locale, intl } = useFormatter();
  // The whole page, or only the cards an embedding page names.
  const full = !embed;
  const show = (card: ProfileCard) => !embed || embed.cards.includes(card);
  // On CTEM > Inventory the card edits the hand-declared software only, while the Software list also counts the
  // software of the assets: its empty text says so instead of claiming nothing is matched (UX round 1 on #1254, B4).
  const techEmptyLabel = embed
    ? t_i18n("No software declared by hand. The Software list also counts the software of your assets.")
    : t_i18n("No product yet: vulnerabilities cannot be matched to what you run.");
  const fieldId = useId();
  const nf = useNumberFormat();
  const { toast } = useToast();
  // A reader (embed.readOnly) never reads an administrators-only route: a 403 would reach the page's error
  // boundary and replace the whole Inventory page (UX r1 B1 on #1540).
  const reader = embed?.readOnly === true;
  const synced = useSyncedRemoval(!reader);
  const { data, isLoading } = useOrganizationContext();
  // In the reader's locale, as every date next to a DataTable (data-table-locale-dates gate).
  const savedAt = data?.created_at ? intl.formatDate(new Date(data.created_at), { dateStyle: "medium", timeStyle: "short" }) : "";
  const save = useSaveOrganizationContext();
  // #1752: a card saves its own fields alone (PATCH); the asset rows never travel with them.
  const patch = usePatchOrganizationContext();
  // The names a primary asset links to, read on the row APIs (#1752 slice 3b), never from the profile.
  const linkGroups = useAssetGroups({ throwOnError: false });
  const linkAssets = useCanonicalAssets({ offset: 0, limit: 200 });
  // Primary assets are an Inventory source (#1754): their sources are configured and run in CTEM > Inventory > Sources
  // like every other source, nothing is pre-provisioned and the card names none of them (Sam, 24 Nov 2026, #1883).
  const drafting = useDraftOrganizationContext();
  const [form, setForm] = useState<ProfileForm | null>(null);
  // The draft the form was opened from: the save is then recorded with source "draft".
  const [draft, setDraft] = useState<OrganizationDraftResponse | null>(null);
  const [draftFailed, setDraftFailed] = useState(false);
  // "Restore from a past version" on the primary-asset card (#1558).
  const [restoreOpen, setRestoreOpen] = useState(false);
  // The amount field the last Save refused: marked aria-invalid (the Input adapter draws the error border) until edited.
  const [invalidField, setInvalidField] = useState<AmountField | null>(null);
  // The exposure rows are checked live once a Save was refused for them (UX B3), and the API's refusal is drawn on its row.
  const [checkExposure, setCheckExposure] = useState(false);
  const [refusedIssue, setRefusedIssue] = useState<ExposureIssue | null>(null);
  // A score typed out of range blocks Save until fixed (UX B4).
  const [scoreInvalid, setScoreInvalid] = useState(false);
  // The element a missing-field chip asked for, focused once the form has rendered it enabled.
  const [focusTarget, setFocusTarget] = useState<string | null>(null);
  // Where the focus goes once a card's edit or the paste dialog has closed (UX r1 B3 on #12271): the
  // first element found, never the page body.
  const [focusAfter, setFocusAfter] = useState<{ targets: (() => HTMLElement | null | undefined)[] } | null>(null);
  // The list cards folded away in read mode (#12256 slice 2): a long page reads as its headers and counts.
  const [folded, setFolded] = useState<ReadonlySet<string>>(() => new Set());
  // The first-login setup (#9173, S3): "Set up now" lands here with ?setup=1; the form opens with the Draft menu.
  const [searchParams, setSearchParams] = useSearchParams();
  const setupRequested = searchParams.get("setup") === "1";
  const [setupGuide, setSetupGuide] = useState(false);
  // The active tab is a segment of the path (#12329, path routes #10203): a link opens it, Back
  // returns to the previous one; the query (?setup=1) is kept.
  const location = useLocation();
  const navigate = useNavigate();
  // Settings draws its own tabs only: a tab whose cards live on CTEM > Inventory reads as an unknown tab, the first one.
  const pathTab = profileTabOfPath(location.pathname);
  const tab = embed ? PROFILE_CARD_TAB[embed.cards[0]] : isInventoryTab(pathTab) ? PROFILE_TABS[0] : pathTab;
  const [focusRetry, setFocusRetry] = useState(0);
  // The card landing in progress (landOnCard), ended by the next landing or when the page unmounts.
  const endCardLanding = useRef<(() => void) | null>(null);
  // A tab whose cards live on CTEM > Inventory is never opened from Settings (#1690): the form stays where it is.
  const goToTab = (next: ProfileTab | null) => {
    if (embed || !next || next === tab || isInventoryTab(next)) return;
    navigate({ pathname: profileTabPath(next), search: location.search });
  };
  // Copilot mode keeps the Identity card alone (#1690, Sam 24 Nov 2026): no risk appetite, ticketing or case sync,
  // no fields-not-filled banner; the stored values stay untouched.
  const xtmOne = useIsXtmOne();

  const editing = form !== null;
  // The list cards in edit (#12256): one card alone from its own Edit, or the three while the whole
  // form is open (setup, draft, a missing field). Rows are taken from the profile at the first change.
  const [listEdits, setListEdits] = useState<ListEdits>({});
  // One exposure card in edit alone (#12256 slice 2): a copy of the profile where only that card changes.
  const [exposureEdit, setExposureEdit] = useState<{ card: ExposureCard; form: ProfileForm } | null>(null);
  // The list whose "Paste a list" dialog is open (slice 2).
  const [pasteFor, setPasteFor] = useState<ListKey | null>(null);
  const saved = data ? toForm(data.profile) : null;
  const current = form ?? saved;
  const listCard = LIST_KEYS.find((key) => listEdits[key] !== undefined && !editing) ?? null;
  // The card in edit alone, a list card or an exposure card (#12256 slice 2): every other Edit waits for it.
  const cardOpen: string | null = listCard ?? (editing ? null : (exposureEdit?.card ?? null));
  const listOpen = (key: ListKey) => editing || listEdits[key] !== undefined;
  // The rows of a list in edit: its edit, else the open form's list as it stands.
  const listRows = <K extends ListKey>(key: K): EditRow<ProfileForm[K][number]>[] =>
    (listEdits[key]?.rows as EditRow<ProfileForm[K][number]>[] | undefined) ?? startRows<ProfileForm[K][number]>(((form ?? saved)?.[key] ?? []) as ProfileForm[K][number][]);
  const setListRows = <K extends ListKey>(key: K, rows: EditRow<ProfileForm[K][number]>[]) => {
    setRefusedIssue(null);
    setListEdits((prev) => ({ ...prev, [key]: { rows, open: prev[key]?.open ?? null } }));
  };
  // One row of fields on the page at a time: opening a row closes the open row of the other lists.
  const setListOpen = (key: ListKey, open: string | null) =>
    setListEdits((prev) => {
      const next: ListEdits = { ...prev, [key]: { rows: prev[key]?.rows ?? listRows(key), open } };
      if (open) for (const other of LIST_KEYS) if (other !== key && next[other]?.open) next[other] = { ...next[other]!, open: null } as never;
      return next;
    });
  // A shipped product kept with a repository that is not owner/name: the API refuses it, so nothing is sent;
  // the row opens with its field marked and focused, the card in view, never a toast (UX r1 B1 on #1483).
  const refuseBadRepositories = (): boolean => {
    const bad = listRows("shipped_products").find((row) => !row.removed && row.value.product.trim() && !row.value.repositories.every(isRepository));
    if (!bad) return false;
    goToTab(tabOfField("shipped_products"));
    setListOpen("shipped_products", bad.key);
    setFocusTarget(`${fieldId}-shipped_products-repositories`);
    return true;
  };
  /** The open form with each list as its editor holds it. */
  const withLists = (base: ProfileForm): ProfileForm =>
    LIST_KEYS.reduce((next, key) => (listEdits[key] ? { ...next, [key]: committedValues(listEdits[key]!.rows as EditRow<unknown>[]) } : next), base);

  // The typed pickers (UX B1, B2): countries by name with a flag, OpenCTI PIRs by name, ticketing integrations and their projects.
  const countryOptions = useMemo(() => {
    let names: Intl.DisplayNames | null = null;
    try {
      names = new Intl.DisplayNames([locale, "en"], { type: "region" });
    } catch {
      names = null;
    }
    // The picker's options are text (the library's chip takes a string): the name and the code; the flag is drawn in the read view.
    return COUNTRY_CODES.map((code) => ({ value: code, label: `${names?.of(code) ?? code} (${code})` }));
  }, [locale]);
  const regionNames = useMemo(() => {
    try {
      return new Intl.DisplayNames([locale, "en"], { type: "region" });
    } catch {
      return null;
    }
  }, [locale]);
  const countryName = (code: string) => regionNames?.of(code) ?? code;
  // Read in view mode too (#9615): a saved PIR is named, never shown by its id; cached 5 minutes.
  const pirs = useOrganizationPirs(!reader);
  const pirText = usePirText();
  const savedPirs = useMemo(
    () => pirLabels(current?.pir_ids ?? [], current?.pir_names ?? {}, pirs.data, pirs.isLoading),
    [current?.pir_ids, current?.pir_names, pirs.data, pirs.isLoading],
  );
  // A saved PIR without a name while OpenCTI's list is on its way: a skeleton, never placeholder text (#11589).
  const pirsLoading = savedPirs.some((pir) => pir.state === "loading");
  const savedPirIds = current?.pir_ids;
  const missingPirs = useMemo(
    () => savedPirs.filter((pir) => pir.state === "missing" && savedPirIds?.includes(pir.id)),
    [savedPirs, savedPirIds],
  );
  const pirOptions = useMemo(() => {
    const known = new Map((pirs.data?.pirs ?? []).map((pir) => [pir.id, pir.name]));
    // A saved PIR OpenCTI does not list stays a choice under its last known name and state, never its id.
    for (const pir of savedPirs) if (!known.has(pir.id)) known.set(pir.id, pirText(pir));
    // One choice per id; two PIRs of the same name are told apart by the start of their id (#11589).
    const rows = [...known].map(([id, name]) => ({ id, name }));
    return rows.map((row) => ({ value: row.id, label: distinctName(row, rows) }));
  }, [pirs.data, savedPirs, pirText]);
  // OpenCTI was asked and did not answer, or answered an error: the names shown are the saved ones.
  const pirsUnreadable = pirs.isError || pirs.data?.unavailable === "unreadable";
  const { data: integrations = [] } = useIntegrations();
  const ticketingIntegrations = integrations.filter((integration) => integration.provider in TICKETING_PROJECT_ELEMENT);
  const ticketingIntegration = ticketingIntegrations.find(
    (integration) => integration.id === current?.ticketing.integration || integration.provider === current?.ticketing.integration,
  );
  const projectElement = ticketingIntegration ? TICKETING_PROJECT_ELEMENT[ticketingIntegration.provider] : "";
  // Read in view mode too (#9615): a saved project is shown by its name, not its id.
  const projects = useIntegrationElements(ticketingIntegration && (editing || current?.ticketing.project) ? ticketingIntegration.id : "", projectElement);
  const projectOptions = projects.data?.elements ?? [];
  const currencyOptions = useMemo(
    () => (current?.currency && !(CURRENCIES as readonly string[]).includes(current.currency) ? [current.currency, ...CURRENCIES] : [...CURRENCIES]),
    [current?.currency],
  );
  const currencySuffix = current?.currency || t_i18n("currency");
  // An empty list is "none in OpenCTI" only when OpenCTI answered (UX round 2 B4).
  const pirsEmptyLabel =
    pirs.data?.unavailable === "not_connected"
      ? t_i18n("The PIRs could not be listed: OpenCTI is not connected.")
      : pirs.data?.unavailable
        ? t_i18n("The PIRs could not be listed: OpenCTI did not answer.")
        : t_i18n("No PIR in OpenCTI");
  const dirty = editing && (draft !== null || JSON.stringify(withLists(form)) !== JSON.stringify(saved));
  // The open form as a profile, for "With AI" from the form: its proposals are reviewed against what is typed.
  const formProfile = useMemo(() => {
    if (!form) return null;
    const parsed = fromForm(withLists(form));
    return "invalid" in parsed ? null : (parsed.profile as OrganizationProfile);
    // withLists reads listEdits, the one other input.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form, listEdits]);

  useEffect(() => {
    if (!focusTarget) return undefined;
    const element = document.getElementById(focusTarget);
    // Its tab may still be opening (#12329): the panel mounts a frame after the tab changes, so look again then.
    if (!element) {
      if (focusRetry > 10) {
        setFocusTarget(null);
        setFocusRetry(0);
        return undefined;
      }
      const frame = requestAnimationFrame(() => setFocusRetry((count) => count + 1));
      return () => cancelAnimationFrame(frame);
    }
    setFocusRetry(0);
    // A card lands its title below the header and the tab bar (its scroll margin), kept there while the
    // panel grows (UX r1 B1 on #108: the last card of a tab was left at the bottom); a field mid-screen.
    endCardLanding.current?.();
    endCardLanding.current = null;
    if (element.dataset.anchor === "card") endCardLanding.current = landOnCard(element);
    else element.scrollIntoView?.({ block: "center" });
    element.focus({ preventScroll: true });
    // A panel that just opened can take the focus back on its first frame: focus it again then.
    requestAnimationFrame(() => {
      if (element.isConnected && document.activeElement !== element) element.focus({ preventScroll: true });
    });
    setFocusTarget(null);
    return undefined;
  }, [focusTarget, editing, tab, focusRetry]);
  useEffect(() => () => endCardLanding.current?.(), []);
  useEffect(() => {
    if (!focusAfter) return;
    // After the closed dialog's own focus return, which runs on the next tick (never cancelled by the reset below).
    const { targets } = focusAfter;
    window.setTimeout(() => targets.map((target) => target()).find((element) => element?.isConnected)?.focus());
    setFocusAfter(null);
  }, [focusAfter]);
  const byTestId = (testId: string) => () => document.querySelector<HTMLElement>(`[data-testid="${testId}"]`);
  /** A card's edit closed: the focus goes back to its Edit button. */
  const focusCardEdit = (card: string | null | undefined) => card && setFocusAfter({ targets: [byTestId(`settings.organization.edit-${card}`)] });

  const startEdit = () => {
    if (!data || cardOpen) return;
    setListEdits({});
    setForm(toForm(data.profile));
  };
  // One exposure card in edit (#12256 slice 2): the other cards stay as they read; the form is not opened.
  const startExposureEdit = (card: ExposureCard) => {
    if (!data || cardOpen) return;
    setCheckExposure(false);
    setRefusedIssue(null);
    setExposureEdit({ card, form: toForm(data.profile) });
  };
  const stopExposureEdit = () => {
    focusCardEdit(exposureEdit?.card);
    setExposureEdit(null);
    setCheckExposure(false);
    setRefusedIssue(null);
    setScoreInvalid(false);
  };
  /** The saved profile with the exposure card in edit alone changed (whole-profile PUT). */
  const exposureSaved = (edit: { card: ExposureCard; form: ProfileForm }, base: ProfileForm): ProfileForm =>
    edit.card === "accepted_risks"
      ? edit.form.prioritization_policy
        ? { ...base, prioritization_policy: { ...(base.prioritization_policy ?? edit.form.prioritization_policy), accepted_risks: edit.form.prioritization_policy.accepted_risks } }
        : base
      : { ...base, [edit.card]: edit.form[edit.card] };
  const exposureDirty = (edit: { card: ExposureCard; form: ProfileForm }) =>
    saved !== null && JSON.stringify(exposureSaved(edit, saved)) !== JSON.stringify(saved);
  const saveExposure = () => {
    if (!data || !exposureEdit) return;
    const next = exposureSaved(exposureEdit, toForm(data.profile));
    // The rows the API would refuse are marked on their row, nothing is sent (UX B3).
    if (exposureIssues(next).length > 0) {
      setCheckExposure(true);
      return;
    }
    const parsed = fromForm(next);
    if ("invalid" in parsed) return;
    const field = exposureEdit.card === "accepted_risks" ? "prioritization_policy" : exposureEdit.card;
    const options = {
      onSuccess: () => {
        stopExposureEdit();
        toast({ title: t_i18n("Organization profile saved"), variant: "success" });
      },
      onError: (err: Error) => {
        const issue = exposureIssueFromRefusal(next, err.message);
        if (issue) setRefusedIssue(issue);
        else toast({ title: t_i18n("The organization profile was not saved"), description: err.message, variant: "error" });
      },
    };
    // The asset and group rows are written through their own row APIs on CTEM > Inventory (#1752 slice 3b): never here.
    if (field === "assets_exposure" || field === "asset_groups") return;
    patch.mutate({ fields: { [field]: parsed.profile[field] }, based_on_version: data.version ?? null }, options);
  };
  // One list card in edit (#12256): the other cards stay as they read; the form is not opened.
  const startListEdit = (key: ListKey) => {
    if (!data) return;
    setListEdits({ [key]: { rows: startRows(toForm(data.profile)[key] as unknown[]), open: null } } as ListEdits);
  };
  const stopListEdit = () => {
    focusCardEdit(listCard);
    setListEdits({});
    synced.reset();
  };
  // A list card's Save writes the saved profile with that list alone changed (whole-profile PUT).
  const saveList = (key: ListKey) => {
    if (!data || !listEdits[key]) return;
    if (key === "shipped_products" && refuseBadRepositories()) return;
    const parsed = fromForm({ ...toForm(data.profile), [key]: committedValues(listEdits[key]!.rows as EditRow<unknown>[]) });
    if ("invalid" in parsed) return;
    const kept = committedValues(listEdits[key]!.rows as EditRow<Provenance>[]);
    patch.mutate({ fields: { [key]: parsed.profile[key] }, based_on_version: data.version ?? null }, {
      onSuccess: () => {
        // The synced rows removed with "Exclude from this sync" go on their sync's exclusions (#1134).
        synced.commit(kept);
        stopListEdit();
        toast({ title: t_i18n("Organization profile saved"), variant: "success" });
      },
      onError: (err) => toast({ title: t_i18n("The organization profile was not saved"), description: err.message, variant: "error" }),
    });
  };
  // A row added from a card header: first, open, focused; a closed card opens in edit first.
  const addListRow = <K extends ListKey>(key: K, value: ProfileForm[K][number]) => {
    const added = addRow(listRows(key), value);
    setListEdits((prev) => ({ ...(editing ? prev : {}), [key]: { rows: added.rows, open: added.key } }));
  };
  const update = (patch: Partial<ProfileForm>) => {
    setRefusedIssue(null);
    setForm((prev) => (prev ? { ...prev, ...patch } : prev));
  };
  const updateExposure = (patch: Partial<ProfileForm>) => {
    setRefusedIssue(null);
    setExposureEdit((prev) => (prev ? { ...prev, form: { ...prev.form, ...patch } } : prev));
  };
  // A PIR OpenCTI no longer has, taken out from the read view: the form opens without it, Save writes it.
  const removePir = (id: string) => {
    if (!data) return;
    const base = form ?? toForm(data.profile);
    setForm({ ...base, pir_ids: base.pir_ids.filter((pir) => pir !== id) });
  };
  const stopEditing = () => {
    setFocusAfter({ targets: [byTestId("settings.organization.edit")] });
    setForm(null);
    setListEdits({});
    setExposureEdit(null);
    setDraft(null);
    setInvalidField(null);
    setCheckExposure(false);
    setRefusedIssue(null);
    setScoreInvalid(false);
    setSetupGuide(false);
  };
  // Leaving the page with an unsaved edit asks first (#12329); a tab switch never loses it.
  const leaveGuard = editing || LIST_KEYS.some((key) => listEdits[key] !== undefined) || exposureEdit !== null;
  useEffect(() => {
    if (!leaveGuard) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [leaveGuard]);
  // Opened once from the setup proposal: the parameter leaves the URL, so a reload shows the page as it is.
  useEffect(() => {
    if (!setupRequested || !data) return;
    setSetupGuide(true);
    setForm((prev) => prev ?? toForm(data.profile));
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete("setup");
        return next;
      },
      { replace: true },
    );
  }, [setupRequested, data, setSearchParams]);
  // A link to one card (`?card=<key>`, profileFieldHref): its tab opens, the page lands on the card, and the
  // parameter leaves the URL in place, so Back returns to the page the link was on.
  const requestedCard = parseProfileCard(searchParams.get(PROFILE_CARD_PARAM));
  useEffect(() => {
    if (!requestedCard || !data || !show(requestedCard)) return;
    const cardTab = PROFILE_CARD_TAB[requestedCard];
    // A card Settings does not draw reads as an unknown card: the parameter is dropped, nothing opens.
    if (!embed && isInventoryTab(cardTab)) {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete(PROFILE_CARD_PARAM);
          return next;
        },
        { replace: true },
      );
      return;
    }
    if (!embed && cardTab !== tab) {
      navigate({ pathname: profileTabPath(cardTab), search: location.search }, { replace: true });
      return;
    }
    setFolded((prev) => {
      if (!prev.has(requestedCard)) return prev;
      const next = new Set(prev);
      next.delete(requestedCard);
      return next;
    });
    setFocusTarget(`${fieldId}-${requestedCard}`);
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete(PROFILE_CARD_PARAM);
        return next;
      },
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps -- show() reads embed, fixed for the page's life
  }, [requestedCard, data, tab, fieldId, navigate, location.search, setSearchParams]);
  // A missing field opens its tab first, then the form on the field (#12329).
  const jumpTo = (path: string) => {
    // A field whose card moved to CTEM > Inventory opens it there (#1178).
    const fieldTab = tabOfField(path);
    if (!embed && fieldTab && isInventoryTab(fieldTab)) {
      navigate(profileFieldHref(path));
      return;
    }
    setFolded(new Set());
    goToTab(tabOfField(path));
    if (!editing) startEdit();
    setFocusTarget(`${fieldId}-${MISSING_TARGET[path] ?? path}`);
  };
  // Opens the form on the profile proposed by the platforms; nothing is saved until Save.
  const startDraft = () =>
    drafting.mutate(undefined, {
      onSuccess: (result) => {
        setInvalidField(null);
        setDraft(result);
        setListEdits({});
        setForm(toForm(result.profile));
        // The form opens on the first Settings tab the platforms filled.
        goToTab(tabsOfFields(result.filled.map((row) => row.field))[0] ?? null);
      },
      // Inline with Retry (section 6.1), a catalogued sentence, never the backend's text.
      onError: () => setDraftFailed(true),
      onSettled: (_result, error) => !error && setDraftFailed(false),
    });

  // "With AI" (#9484): one metered model call on the administrator's click, never at page load.
  const agenticEnabled = useAgenticAIEnabled();
  const aiActionDisabled = useAiActionDisabled();
  const aiAvailable = agenticEnabled && !aiActionDisabled;
  const [aiDraftOpen, setAiDraftOpen] = useState(false);
  const [aiGateOpen, setAiGateOpen] = useState(false);
  const [assetsImportOpen, setAssetsImportOpen] = useState(false);
  // "From the platforms" (#11780): the import stepper; its "Quick setup" runs today's one-click draft.
  const [platformImportOpen, setPlatformImportOpen] = useState(false);
  // "From a connected integration" (#11888): an agent reads it and previews the assets in a chat.
  const [fillAgentOpen, setFillAgentOpen] = useState(false);
  // The chat onboarding (#11224): a new chat with the default assistant interviews for the fields left.
  const startOnboarding = () => {
    if (!aiAvailable) {
      setAiGateOpen(true);
      return;
    }
    primeOrganizationOnboarding(t_i18n);
    navigate("/chat");
  };
  const connectedPlatforms = [...new Set(integrations.map((integration) => integration.provider).filter((provider) => provider in PLATFORM_NAMES))];
  const draftMenuItems: OverflowMenuItem[] = [
    { id: "draft-platforms", label: t_i18n("From the platforms"), icon: Import, onSelect: () => setPlatformImportOpen(true) },
    {
      id: "draft-ai",
      label: t_i18n("With AI"),
      icon: aiAvailable ? Sparkles : Lock,
      onSelect: () => (aiAvailable ? setAiDraftOpen(true) : setAiGateOpen(true)),
    },
    {
      id: "draft-integration",
      label: t_i18n("From a connected integration"),
      icon: aiAvailable ? Bot : Lock,
      onSelect: () => (aiAvailable ? setFillAgentOpen(true) : setAiGateOpen(true)),
    },
  ];

  const submit = () => {
    if (!form) return;
    // The names OpenCTI gives now are saved with the PIRs, so the profile names them when OpenCTI cannot (#9615).
    const live = Object.fromEntries((pirs.data?.pirs ?? []).map((pir) => [pir.id, pir.name]));
    const parsed = fromForm({ ...withLists(form), pir_names: { ...(form.pir_names ?? {}), ...live } });
    if ("invalid" in parsed) {
      // The error is inline under the field (UX B4), announced with it: no toast on a refused Save.
      // Its tab opens first, so the field is rendered before it is focused (#12329).
      setInvalidField(parsed.field);
      goToTab(tabOfField(parsed.field));
      setFocusTarget(`${fieldId}-${parsed.field}`);
      return;
    }
    setInvalidField(null);
    if (scoreInvalid) return;
    if (refuseBadRepositories()) return;
    // A ServiceNow table the CTEM case sync cannot write to is shown inline on the Record type
    // field (#923 UX round 3 B1): nothing is sent, the field is focused, never a toast.
    if (form.ticketing.sync && !isSupportedRecordType(ticketingIntegration?.provider ?? "", form.ticketing.sync.record_type)) {
      goToTab(tabOfField("ticketing"));
      setFocusTarget(`${fieldId}-ticketing-sync-record-type`);
      return;
    }
    // The exposure rows the API would refuse are marked on their row, nothing is sent (UX B3).
    if (exposureIssues(form).length > 0) {
      setCheckExposure(true);
      goToTab("exposure");
      return;
    }
    const source: ProfileSource = draft ? "draft" : "form";
    save.mutate({ profile: parsed.profile as OrganizationProfile, source }, {
      onSuccess: () => {
        stopEditing();
        toast({ title: t_i18n("Organization profile saved"), variant: "success" });
      },
      onError: (err) => {
        // A refusal naming an exposure row or field is drawn on it, in the catalog's words; any other stays a toast.
        const issue = exposureIssueFromRefusal(form, err.message);
        if (issue) {
          setRefusedIssue(issue);
          goToTab("exposure");
        } else toast({ title: t_i18n("The organization profile was not saved"), description: err.message, variant: "error" });
      },
    });
  };

  /** The helper and the bubble of a field, from the field contract (UX B3). */
  const help = (key: keyof typeof FIELD_HELP) => ({ helper: t_i18n(FIELD_HELP[key].helper), info: t_i18n(FIELD_HELP[key].info) });
  const text = (key: "name" | "sector" | "sub_sector", title: string, placeholder?: string) => {
    const id = `${fieldId}-${key}`;
    return (
      <SettingsField id={id} label={t_i18n(title)} {...help(key)}>
        <Input
          id={id}
          aria-labelledby={settingsFieldLabelId(id)}
          aria-describedby={settingsFieldHintId(id)}
          placeholder={placeholder}
          value={current?.[key] ?? ""}
          onChange={(e) => update({ [key]: e.target.value } as Partial<ProfileForm>)}
        />
      </SettingsField>
    );
  };
  /** An amount: thousands separators while read, the unit as a suffix, the error inline (UX B2, B4). */
  const amount = (key: AmountField, title: string, unit: string, placeholder: string, preview?: ReactNode) => {
    const id = `${fieldId}-${key}`;
    const value = current?.[key] ?? "";
    const invalid = invalidField === key;
    return (
      <SettingsField
        id={id}
        label={t_i18n(title)}
        {...help(key)}
        helper={preview && !invalid ? preview : t_i18n(FIELD_HELP[key].helper)}
        error={invalid ? t_i18n("Not a valid amount: type a number, without letters, 0 or more.") : undefined}
      >
        <div className="relative">
          <Input
            id={id}
            inputMode="decimal"
            aria-labelledby={settingsFieldLabelId(id)}
            aria-describedby={settingsFieldHintId(id)}
            placeholder={placeholder}
            className={AMOUNT_INPUT_CLASS}
            value={value}
            aria-invalid={invalid ? true : undefined}
            onChange={(e) => {
              if (invalid) setInvalidField(null);
              update({ [key]: e.target.value } as Partial<ProfileForm>);
            }}
          />
          <span aria-hidden className="pointer-events-none absolute inset-y-0 right-3 flex items-center content-compact text-default-secondary">
            {unit}
          </span>
        </div>
      </SettingsField>
    );
  };
  /** An amount at rest (#10855): the figure with its unit and currency as text, what it does under it. */
  const readAmount = (key: AmountField, title: string, per: "hour" | "year", preview?: ReactNode) => {
    const value = current?.[key]?.trim() ?? "";
    const shown = value ? formatAmount(value, locale) : "";
    const currency = current?.currency ?? "";
    const text =
      per === "hour"
        ? currency
          ? t_i18n("{amount} {currency} per hour", { amount: shown, currency })
          : t_i18n("{amount} per hour, currency not set", { amount: shown })
        : currency
          ? t_i18n("{amount} {currency} per year", { amount: shown, currency })
          : t_i18n("{amount} per year, currency not set", { amount: shown });
    return (
      <SettingsReadField
        label={t_i18n(title)}
        info={help(key).info}
        data-testid={`settings.organization.read.${key}`}
        value={
          shown && (
            <>
              <span className="font-mono-plex">{text}</span>
              {preview && <span className="mt-1 block content-compact text-default-secondary">{preview}</span>}
            </>
          )
        }
      />
    );
  };
  /**
   * The two lists (primary assets, software inventory) read on the shared DataTable (data
   * tables slice 9): search, filters, sort, CSV and the pager. In edit they keep all of it on the
   * list editor (#12256, OrganizationListEditor): one row open at a time, the order frozen.
   */
  const optionLabel = (labels: Record<string, string>, value: string) => t_i18n(labels[value] ?? value);
  // Read mode draws a value as text, cut with the shared tooltip, and an empty optional value
  // as a muted "Not set" (UX r1 B1 and B2 on #10804): a disabled input could not be read
  // whole and showed its placeholder as if it were a value.
  // "Not set" takes the table's compact text so every row keeps one height (UX r2 B1 on #10804).
  const readCell = (value: string) =>
    value.trim() ? <TruncatedText text={value} /> : <span className="content-compact text-default-secondary">{t_i18n("Not set")}</span>;
  /** Where a row came from (#1134): the platform's logo and name, or Typed, CSV, API, Agent. */
  const sourceLabel = (value: Provenance) => {
    const kind = sourceKind(value.source);
    return kind === "platform" ? (IMPORT_SOURCE_NAMES[sourcePlatform(value.source)] ?? value.source ?? "") : t_i18n(SOURCE_KIND_LABELS[kind]);
  };
  const sourceCell = (value: Provenance) => {
    const platform = sourcePlatform(value.source);
    return (
      <span className="flex min-w-0 items-center gap-2">
        {platform && <ProviderLogo provider={IMPORT_SOURCE_PROVIDER[platform] ?? platform} decorative className="h-4 w-4 shrink-0" />}
        <TruncatedText text={sourceLabel(value)} />
      </span>
    );
  };
  // Source steps aside below a 72rem card (UX r1 B1 and B2 on #1174): at 1080 the fixed 128 px cut
  // Business service and Owner to nothing. The lock tip and the Source filter still name it there.
  const SOURCE_WIDTH = JEWEL_WIDTHS.source;
  const sourceColumn = <R extends { value: Provenance }>(): DataTableColumn<R> => ({
    key: "source",
    header: "Source",
    hideable: true,
    hideBelow: JEWEL_STEP_ASIDE,
    width: SOURCE_WIDTH,
    cell: ({ value }) => sourceCell(value),
    interactive: true,
    sortValue: ({ value }) => sourceLabel(value).toLowerCase(),
    firstSort: "asc",
    csv: ({ value }) => value.source ?? "manual",
  });
  const sourceFilter = <R extends { value: Provenance }>(): DataTableFilter<R> => ({
    key: "source",
    label: "Source",
    values: ({ value }) => [sourceLabel(value)],
  });
  const sourceEdit = <V extends Provenance>(): ListEditColumn<V> => ({
    key: "source",
    header: "Source",
    hideable: true,
    hideBelow: JEWEL_STEP_ASIDE,
    width: SOURCE_WIDTH,
    read: (v) => sourceCell(v),
    sortValue: (v) => sourceLabel(v).toLowerCase(),
    firstSort: "asc",
  });
  const lockedFor = (key: keyof typeof LOCAL_FIELDS) => ({
    tip: (value: Provenance) => synced.lockTip(value),
    column: (value: Provenance, column: string) => !columnEditable(value.source, column, LOCAL_FIELDS[key]),
  });
  // Read mode lays the columns out fixed (names 240, enums 120, OpenCRQ 96, the rest share what
  // is left) so a long name is cut with its tooltip inside the card, never scrolled (UX r2 B2 on #10804).
  const readWidth = (px: number) => px;
  const tableLayout = "fixed";
  const sortable = <R,>(value: (row: R) => SortValue) => value;
  /** `ordered` keeps the labels' order for an ordinal scale (criticality, exposure); every other filter is alphabetical. */
  const optionFilter = <R,>(key: string, label: string, labels: Record<string, string>, values: (row: R) => string, ordered = false): DataTableFilter<R> => ({
    key,
    label,
    values: (row) => [values(row)],
    options: Object.entries(labels).map(([value, name]) => ({ value, label: t_i18n(name) })),
    ordered,
  });
  type JewelRow = { index: number; value: ProfileForm["crown_jewels"][number] };
  type TechRow = { index: number; value: ProfileForm["tech_stack"][number] };
  const jewelColumns: DataTableColumn<JewelRow>[] = [
    {
      key: "name",
      header: "Primary asset",
      width: readWidth(JEWEL_WIDTHS.name),
      cell: ({ value }) => readCell(value.name),
      interactive: true,
      sortValue: sortable<JewelRow>(({ value }) => value.name.toLowerCase()),
      firstSort: "asc",
      csv: ({ value }) => value.name,
    },
    {
      key: "business_service",
      header: "Business service",
      hideable: true,
      cell: ({ value }) => readCell(value.business_service),
      interactive: true,
      sortValue: sortable<JewelRow>(({ value }) => value.business_service.toLowerCase()),
      firstSort: "asc",
      csv: ({ value }) => value.business_service,
    },
    {
      key: "owner",
      header: "Owner",
      hideable: true,
      cell: ({ value }) => readCell(value.owner),
      interactive: true,
      sortValue: sortable<JewelRow>(({ value }) => value.owner.toLowerCase()),
      firstSort: "asc",
      csv: ({ value }) => value.owner,
    },
    {
      key: "criticality",
      header: "Criticality",
      hideable: true,
      headerClassName: "w-44",
      width: readWidth(JEWEL_WIDTHS.criticality),
      cell: ({ value }) => readCell(value.criticality ? optionLabel(CRITICALITY_LABELS, value.criticality) : ""),
      interactive: true,
      sortValue: sortable<JewelRow>(({ value }) => Object.keys(CRITICALITY_LABELS).indexOf(value.criticality)),
      firstSort: "desc",
      csv: ({ value }) => value.criticality,
    },
    {
      // The primary asset > asset link is edited on the primary asset row (#11543 S3, placement rule).
      key: "assets",
      header: "Assets",
      hideable: true,
      // Steps aside at 1080 so Business service reads whole (UX r2 B2 on #1174); the Columns pill brings it back.
      hideBelow: JEWEL_STEP_ASIDE,
      cell: ({ value }) => readCell((value.asset_names ?? []).join(", ")),
      interactive: true,
      csv: ({ value }) => (value.asset_names ?? []).join("; "),
    },
    {
      key: "opencrq",
      header: "OpenCRQ",
      hideable: true,
      // 112 px: the header and its sort mark read whole (96 cut it, UX r2 B2 on #1174).
      headerClassName: "w-28",
      width: readWidth(JEWEL_WIDTHS.opencrq),
      cell: ({ value }) =>
        value.opencrq_asset_id ? (
          <Tip content={t_i18n("Linked to OpenCRQ")}>
            <span tabIndex={0} role="img" aria-label={t_i18n("Linked to OpenCRQ")} className={`inline-flex ${TIP_WRAP_FOCUS_RING}`}>
              <Link2 className="h-4 w-4 text-feedback-success-primary" />
            </span>
          </Tip>
        ) : null,
      interactive: true,
      sortValue: sortable<JewelRow>(({ value }) => !!value.opencrq_asset_id),
      csv: ({ value }) => value.opencrq_asset_id,
    },
    sourceColumn<JewelRow>(),
  ];
  const jewelFilters: DataTableFilter<JewelRow>[] = [
    // Ordinal scale: low to critical, never alphabetical (#12289).
    optionFilter<JewelRow>("criticality", "Criticality", CRITICALITY_LABELS, ({ value }) => value.criticality, true),
    optionFilter<JewelRow>("opencrq", "OpenCRQ", OPENCRQ_LINK_LABELS, ({ value }) => (value.opencrq_asset_id ? "linked" : "not_linked")),
    sourceFilter<JewelRow>(),
  ];
  const techColumns: DataTableColumn<TechRow>[] = [
    {
      key: "product",
      header: "Product",
      width: readWidth(240),
      cell: ({ value }) => readCell(value.product),
      interactive: true,
      sortValue: sortable<TechRow>(({ value }) => value.product.toLowerCase()),
      firstSort: "asc",
      csv: ({ value }) => value.product,
    },
    {
      key: "vendor",
      header: "Vendor",
      hideable: true,
      width: readWidth(200),
      cell: ({ value }) => readCell(value.vendor),
      interactive: true,
      sortValue: sortable<TechRow>(({ value }) => value.vendor.toLowerCase()),
      firstSort: "asc",
      csv: ({ value }) => value.vendor,
    },
    {
      key: "cpe",
      header: "CPE (optional)",
      hideable: true,
      cell: ({ value }) => readCell(value.cpe),
      interactive: true,
      sortValue: sortable<TechRow>(({ value }) => value.cpe.toLowerCase()),
      firstSort: "asc",
      csv: ({ value }) => value.cpe,
    },
    {
      // The version the organization runs (CTEM-11, #928): a case then names the version to reach.
      key: "version",
      header: "Version (optional)",
      hideable: true,
      width: readWidth(180),
      cell: ({ value }) => readCell(value.version ?? ""),
      interactive: true,
      sortValue: sortable<TechRow>(({ value }) => (value.version ?? "").toLowerCase()),
      firstSort: "asc",
      csv: ({ value }) => value.version ?? "",
    },
    sourceColumn<TechRow>(),
  ];
  type ShippedRow = { index: number; value: ProfileForm["shipped_products"][number] };
  const shippedColumns: DataTableColumn<ShippedRow>[] = [
    { key: "product", header: "Product", width: readWidth(240), cell: ({ value }) => readCell(value.product), interactive: true, sortValue: sortable<ShippedRow>(({ value }) => value.product.toLowerCase()), firstSort: "asc", csv: ({ value }) => value.product },
    { key: "vendor", header: "Vendor", hideable: true, width: readWidth(200), cell: ({ value }) => readCell(value.vendor), interactive: true, sortValue: sortable<ShippedRow>(({ value }) => value.vendor.toLowerCase()), firstSort: "asc", csv: ({ value }) => value.vendor },
    { key: "cpe", header: "CPE (optional)", hideable: true, width: readWidth(240), cell: ({ value }) => readCell(value.cpe), interactive: true, sortValue: sortable<ShippedRow>(({ value }) => value.cpe.toLowerCase()), firstSort: "asc", csv: ({ value }) => value.cpe },
    { key: "repositories", header: "Source repositories", hideable: true, cell: ({ value }) => readCell(value.repositories.join(", ")), interactive: true, sortValue: sortable<ShippedRow>(({ value }) => value.repositories.join(",").toLowerCase()), firstSort: "asc", csv: ({ value }) => value.repositories.join(" ") },
  ];
  const techFilters: DataTableFilter<TechRow>[] = [
    { key: "vendor", label: "Vendor", values: ({ value }) => [value.vendor.trim()] },
    sourceFilter<TechRow>(),
  ];
  // The list editors (#12256): the same columns, a closed row read as text, the open one as fields.
  type Jewel = ProfileForm["crown_jewels"][number];
  type Tech = ProfileForm["tech_stack"][number];
  type Shipped = ProfileForm["shipped_products"][number];
  const pick = (labels: Record<string, string>, value: string, label: string, onChange: (value: string) => void) => (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className="w-full min-w-0" aria-label={t_i18n(label)}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {Object.entries(labels).map(([key, name]) => (
          <SelectItem key={key} value={key}>
            {t_i18n(name)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
  const editText = (label: string, value: string, onChange: (value: string) => void, invalid = false, placeholder = label, list?: string) => (
    <Input aria-label={t_i18n(label)} aria-invalid={invalid || undefined} placeholder={placeholder === label ? t_i18n(label) : placeholder} value={value} onChange={(e) => onChange(e.target.value)} list={list} />
  );
  // The values a free-text field already holds in its list, offered as it is typed (slice 2): 150 rows stay consistent.
  const suggestId = (key: ListKey, field: string) => `${fieldId}-${key}-${field}-suggestions`;
  const jewelEdit: ListEditColumn<Jewel>[] = [
    { key: "name", header: "Primary asset", width: readWidth(JEWEL_WIDTHS.name), read: (v) => readCell(v.name), edit: (v, set, bad) => editText("Primary asset", v.name, (name) => set({ name }), bad), sortValue: (v) => v.name.toLowerCase(), firstSort: "asc", issues: [LIST_ISSUES.emptyName, LIST_ISSUES.duplicate] },
    { key: "business_service", header: "Business service", hideable: true, read: (v) => readCell(v.business_service), edit: (v, set) => editText("Business service", v.business_service, (business_service) => set({ business_service })), sortValue: (v) => v.business_service.toLowerCase(), firstSort: "asc" },
    { key: "owner", header: "Owner", hideable: true, read: (v) => readCell(v.owner), edit: (v, set) => editText("Owner", v.owner, (owner) => set({ owner }), false, "Owner", suggestId("crown_jewels", "owner")), sortValue: (v) => v.owner.toLowerCase(), firstSort: "asc" },
    { key: "criticality", header: "Criticality", hideable: true, headerClassName: "w-44", width: readWidth(120), read: (v) => readCell(v.criticality ? optionLabel(CRITICALITY_LABELS, v.criticality) : ""), edit: (v, set) => pick(CRITICALITY_LABELS, v.criticality, "Criticality", (criticality) => set({ criticality })), sortValue: (v) => Object.keys(CRITICALITY_LABELS).indexOf(v.criticality), firstSort: "desc" },
    {
      key: "assets",
      header: "Assets",
      hideable: true,
      // 200 px so "Link assets" and the chevron fit; Business service and Owner share the rest (UX r2 B1 on #12261).
      // Below 72rem it steps aside, else the two local fields of a synced row kept 61 px (UX r2 B1 on #1174).
      hideBelow: JEWEL_STEP_ASIDE,
      width: readWidth(JEWEL_WIDTHS.assets),
      read: (v) => readCell((v.asset_names ?? []).join(", ")),
      edit: (v, set) => {
        const names = v.asset_names ?? [];
        // The groups and assets come from their row APIs (#1752 slice 3b): the profile answers no row.
        const options = [...(linkGroups.data?.items ?? []), ...(linkAssets.data?.items ?? [])].filter((asset) => asset.name.trim()).map((asset) => ({ value: asset.name, label: asset.name }));
        return (
          <MultiSelect
            aria-label={t_i18n("Assets")}
            className="w-full min-w-0 [&_input]:min-w-0 [&_input]:w-0"
            // One line inside the 200 px cell, the rest in a +N chip: wrapped chips ran past the cell into Actions (UX r3 B1 on #12261).
            maxVisibleChips="fit"
            options={options}
            selected={names}
            onToggle={(name) => set({ asset_names: names.includes(name) ? names.filter((n) => n !== name) : [...names, name] })}
            placeholder={t_i18n("Link assets")}
            searchPlaceholder={t_i18n("Search...")}
            // The rows live in Inventory > Assets (UX r1 B1 on #1892); no empty hint while the row APIs still load (A1).
            emptyOptionsLabel={linkGroups.isLoading || linkAssets.isLoading ? t_i18n("Loading...") : t_i18n("No asset yet: add them in Inventory > Assets.")}
            noMatchLabel={t_i18n("No match")}
          />
        );
      },
    },
    sourceEdit<Jewel>(),
  ];
  // Edit mode: the CPE input takes what is left, so the fixed columns give it 100 px more at 1080,
  // Vendor scale (UX round 3 B2 on #931: 240/200/180 left CPE 62 px, header and input clipped).
  const techEdit: ListEditColumn<Tech>[] = [
    { key: "product", header: "Product", width: readWidth(200), read: (v) => readCell(v.product), edit: (v, set, bad) => editText("Product", v.product, (product) => set({ product }), bad), sortValue: (v) => v.product.toLowerCase(), firstSort: "asc", issues: [LIST_ISSUES.emptyName, LIST_ISSUES.duplicate] },
    { key: "vendor", header: "Vendor", hideable: true, width: readWidth(160), read: (v) => readCell(v.vendor), edit: (v, set, bad) => editText("Vendor", v.vendor, (vendor) => set({ vendor }), bad, "Vendor", suggestId("tech_stack", "vendor")), sortValue: (v) => v.vendor.toLowerCase(), firstSort: "asc", issues: [LIST_ISSUES.vendor] },
    { key: "cpe", header: "CPE (optional)", hideable: true, read: (v) => readCell(v.cpe), edit: (v, set, bad) => editText("CPE (optional)", v.cpe, (cpe) => set({ cpe }), bad, "cpe:2.3:a:vendor:product:*"), sortValue: (v) => v.cpe.toLowerCase(), firstSort: "asc", issues: [LIST_ISSUES.cpe] },
    { key: "version", header: "Version (optional)", hideable: true, width: readWidth(160), read: (v) => readCell(v.version ?? ""), edit: (v, set) => editText("Version (optional)", v.version ?? "", (version) => set({ version }), false, "1.25.3"), sortValue: (v) => (v.version ?? "").toLowerCase(), firstSort: "asc" },
    sourceEdit<Tech>(),
  ];
  // What the organization ships (#1432): a vulnerability on one of these resolves to its repositories in CTEM.
  const shippedEdit: ListEditColumn<Shipped>[] = [
    { key: "product", header: "Product", width: readWidth(200), read: (v) => readCell(v.product), edit: (v, set, bad) => editText("Product", v.product, (product) => set({ product }), bad), sortValue: (v) => v.product.toLowerCase(), firstSort: "asc", issues: [LIST_ISSUES.emptyName, LIST_ISSUES.duplicate] },
    { key: "vendor", header: "Vendor", hideable: true, width: readWidth(160), read: (v) => readCell(v.vendor), edit: (v, set) => editText("Vendor", v.vendor, (vendor) => set({ vendor }), false, "Vendor", suggestId("shipped_products", "vendor")), sortValue: (v) => v.vendor.toLowerCase(), firstSort: "asc" },
    // Below 72rem CPE steps aside to the Columns pill with Source, so the repositories column keeps at least
    // 240 px for its tag input (UX r1 B2 on #1483: at 1080 it had 58 px, header and chips cut).
    { key: "cpe", header: "CPE (optional)", hideable: true, hideBelow: JEWEL_STEP_ASIDE, width: readWidth(240), read: (v) => readCell(v.cpe), edit: (v, set, bad) => editText("CPE (optional)", v.cpe, (cpe) => set({ cpe }), bad, "cpe:2.3:a:vendor:product:*"), sortValue: (v) => v.cpe.toLowerCase(), firstSort: "asc", issues: [LIST_ISSUES.cpe] },
    {
      key: "repositories",
      header: "Source repositories",
      hideable: true,
      // No fixed width: the column takes what Product, Vendor and the stepped-aside columns leave.
      read: (v) => readCell(v.repositories.join(", ")),
      edit: (v, set, bad) => (
        <TagInput
          id={`${fieldId}-shipped_products-repositories`}
          value={v.repositories}
          onChange={(repositories) => set({ repositories })}
          preserveCase
          suggestFromPlatformTags={false}
          placeholder={t_i18n("owner/name, Enter to add")}
          aria-label={t_i18n("Source repositories")}
          aria-invalid={bad || undefined}
          invalidTags={v.repositories.filter((repo) => !isRepository(repo))}
        />
      ),
      sortValue: (v) => v.repositories.join(",").toLowerCase(),
      firstSort: "asc",
      issues: [LIST_ISSUES.repository],
    },
  ];
  /** Per list (slice 2): the fields the selection toolbar sets, the free-text fields suggested from the list, and "Paste a list". */
  const LIST_BULK = {
    crown_jewels: [
      { key: "owner", label: "Owner", suggestions: suggestId("crown_jewels", "owner") },
      { key: "criticality", label: "Criticality", options: CRITICALITY_LABELS },
      { key: "business_service", label: "Business service" },
    ],
    tech_stack: [{ key: "vendor", label: "Vendor", suggestions: suggestId("tech_stack", "vendor") }],
    shipped_products: [{ key: "vendor", label: "Vendor", suggestions: suggestId("shipped_products", "vendor") }],
  } as const;
  const LIST_SUGGESTED: { [K in ListKey]: readonly (keyof ProfileForm[K][number] & string)[] } = { crown_jewels: ["owner"], shipped_products: ["vendor"], tech_stack: ["vendor"] };
  const choiceWords = (labels: Record<string, string>) => Object.fromEntries(Object.entries(labels).map(([key, label]) => [key, [label, t_i18n(label)]]));
  const header = (label: string) => [label, t_i18n(label)];
  const PASTE = {
    crown_jewels: {
      title: "Paste primary assets",
      example: "Customer portal, Online sales, Jane Doe, critical",
      fieldLabels: { name: "Primary asset", business_service: "Business service", owner: "Owner", criticality: "Criticality" },
      spec: {
        fields: ["name", "business_service", "owner", "criticality"],
        empty: { name: "", business_service: "", owner: "", criticality: "high", opencrq_asset_id: "" },
        name: "name",
        headers: { name: header("Primary asset"), business_service: header("Business service"), owner: header("Owner"), criticality: header("Criticality"), opencrq_asset_id: header("OpenCRQ") },
        choices: { criticality: choiceWords(CRITICALITY_LABELS) },
        identity: LIST_RULES.crown_jewels.identity,
      } as PasteSpec<Jewel>,
    },
    tech_stack: {
      title: "Paste products",
      example: "Jira, Atlassian",
      fieldLabels: { product: "Product", vendor: "Vendor", cpe: "CPE (optional)", version: "Version (optional)" },
      spec: {
        fields: ["product", "vendor", "cpe", "version"],
        empty: { product: "", vendor: "", cpe: "", version: "" },
        name: "product",
        headers: { product: header("Product"), vendor: header("Vendor"), cpe: header("CPE (optional)"), version: [...header("Version (optional)"), "Version"] },
        identity: LIST_RULES.tech_stack.identity,
      } as PasteSpec<Tech>,
    },
    // The repositories are added on the row once pasted: a line holds one value per field.
    shipped_products: {
      title: "Paste shipped products",
      example: "Acme Server, Acme, cpe:2.3:a:acme:server:*:*:*:*:*:*:*:*",
      fieldLabels: { product: "Product", vendor: "Vendor", cpe: "CPE (optional)" },
      spec: {
        fields: ["product", "vendor", "cpe"],
        empty: { product: "", vendor: "", cpe: "", repositories: [] },
        name: "product",
        headers: { product: header("Product"), vendor: header("Vendor"), cpe: header("CPE (optional)") },
        identity: LIST_RULES.shipped_products.identity,
      } as PasteSpec<Shipped>,
    },
  };
  /** `ordered` as optionFilter: an ordinal scale keeps its order. */
  const valueFilter = <V,>(key: string, label: string, labels: Record<string, string>, values: (value: V) => string, ordered = false) => ({
    key,
    label,
    values: (value: V) => [values(value)],
    options: Object.entries(labels).map(([value, name]) => ({ value, label: t_i18n(name) })),
    ordered,
  });
  /** The editor of a list card in edit, or null when the card reads. */
  const listEditor = <K extends ListKey>(
    key: K,
    props: Pick<Parameters<typeof OrganizationListEditor<ProfileForm[K][number]>>[0], "columns" | "filters" | "searchFields" | "rowLabel" | "defaultSort" | "storageKey" | "rowTestId" | "emptyLabel">,
  ) => {
    if (!listOpen(key)) return null;
    const rows = listRows(key);
    const rules = LIST_RULES[key] as unknown as { check: (value: ProfileForm[K][number]) => ReturnType<typeof LIST_RULES.tech_stack.check>; identity: (value: ProfileForm[K][number]) => string };
    return (
      <>
        {LIST_SUGGESTED[key].map((field) => (
          <datalist key={field} id={suggestId(key, field)}>
            {fieldSuggestions(rows, (value) => String(value[field] ?? "")).map((text) => (
              <option key={text} value={text} />
            ))}
          </datalist>
        ))}
        <OrganizationListEditor
          {...props}
          bulkFields={LIST_BULK[key] as unknown as BulkField<ProfileForm[K][number]>[]}
          locked={key in LOCAL_FIELDS ? lockedFor(key as keyof typeof LOCAL_FIELDS) : undefined}
          confirmRemove={synced.confirmRemove}
          rows={rows}
          onRows={(next) => setListRows(key, next)}
          issues={rowIssues(rows, (value) => rules.check(value), rules.identity)}
          openKey={listEdits[key]?.open ?? null}
          onOpen={(open) => setListOpen(key, open)}
        />
        {listCard === key && <ListSaveBar counts={changeCounts(rows)} saving={save.isPending} onSave={() => saveList(key)} onCancel={stopListEdit} />}
      </>
    );
  };
  /** "Paste a list" on a list card in edit (slice 2): its rows are added like rows added by hand. */
  const pasteAction = (key: ListKey) =>
    listOpen(key) ? (
      <Button variant="secondary" className="gap-1.5" onClick={() => setPasteFor(key)} data-testid={`settings.organization.paste-${key}`}>
        <Icon name="clipboard-list" size={16} aria-hidden /> {t_i18n("Paste a list")}
      </Button>
    ) : null;
  const pasteDialog = <K extends ListKey>(key: K) => {
    const paste = PASTE[key] as unknown as { title: string; example: string; fieldLabels: Record<string, string>; spec: PasteSpec<ProfileForm[K][number]> };
    const rows = listRows(key);
    return (
      <OrganizationListPasteDialog<ProfileForm[K][number]>
        open
        onOpenChange={(open) => {
          if (open) return;
          setPasteFor(null);
          // The dialog leaves the page: the focus goes back to Paste a list, unless Add placed it on a row.
          setFocusAfter((prev) => prev ?? { targets: [byTestId(`settings.organization.paste-${key}`)] });
        }}
        title={t_i18n(paste.title)}
        example={t_i18n(paste.example)}
        spec={paste.spec}
        existing={committedValues(rows)}
        rowLabel={(value) => String((value as Record<string, unknown>)[paste.spec.name] ?? "")}
        fieldLabels={Object.fromEntries(Object.entries(paste.fieldLabels).map(([field, label]) => [field, t_i18n(label)]))}
        onAdd={(values) => {
          const next = addRows(rows, values);
          const known = new Set(rows.map((row) => row.key));
          // The added rows are pinned first: the top one takes the focus, else the list (UX r1 B3 on #12271).
          const top = next.find((row) => !known.has(row.key))?.key;
          setListEdits((prev) => ({ ...(editing ? prev : {}), [key]: { rows: next, open: null } }));
          setFocusAfter({
            targets: [
              () => (top ? document.querySelector(`[data-edit-key="${top}"]`)?.closest<HTMLElement>("tr") : null),
              byTestId(`settings.organization.paste-${key}`),
            ],
          });
        }}
      />
    );
  };
  /** The card's Edit (read mode), or nothing while this card or the whole form is open. */
  const listEditAction = (key: ListKey) =>
    !reader && !editing && listEdits[key] === undefined ? (
      <Tip content={cardOpen ? t_i18n("Save or cancel the edit first.") : ""} wrap="auto">
        <Button
          variant="secondary"
          className="gap-1.5"
          disabled={!data || cardOpen !== null || drafting.isPending}
          onClick={() => startListEdit(key)}
          aria-label={t_i18n("Edit {name}", { name: t_i18n(LIST_TITLES[key]) })}
          data-testid={`settings.organization.edit-${key}`}
        >
          <Pencil className="h-3.5 w-3.5" /> {t_i18n("Edit")}
        </Button>
      </Tip>
    ) : null;
  /** An exposure card's Edit (read mode, #12256 slice 2), or nothing while the whole form or this card is open. */
  const exposureEditAction = (card: ExposureCard) =>
    !embed?.readOnly && !editing && exposureEdit?.card !== card ? (
      <Tip content={cardOpen ? t_i18n("Save or cancel the edit first.") : ""} wrap="auto">
        <Button
          variant="secondary"
          className="gap-1.5"
          disabled={!data || cardOpen !== null || drafting.isPending}
          onClick={() => startExposureEdit(card)}
          aria-label={t_i18n("Edit {name}", { name: t_i18n(EXPOSURE_TITLES[card]) })}
          data-testid={`settings.organization.edit-${card}`}
        >
          <Pencil className="h-3.5 w-3.5" /> {t_i18n("Edit")}
        </Button>
      </Tip>
    ) : null;
  /** "4 need attention", on the card's header in read mode. */
  const attention = (key: ListKey) => {
    if (!saved || listOpen(key)) return null;
    const rules = LIST_RULES[key] as unknown as { check: (value: unknown) => ReturnType<typeof LIST_RULES.tech_stack.check>; identity: (value: unknown) => string };
    const count = attentionCount(saved[key] as unknown[], rules.check, rules.identity);
    return count ? (
      <Badge variant="warning" data-testid={`settings.organization.attention-${key}`}>
        {t_i18n("{count, plural, one {# needs attention} other {# need attention}}", { count })}
      </Badge>
    ) : null;
  };
  /**
   * A list card's count in its header and its fold (#12256 slice 2, read mode only: a card being
   * edited never folds). The count is the saved rows'; the attention badge stays in the actions.
   */
  const cardFold = (section: string, count: number | null): SettingsCardFold => {
    const open =
      editing ||
      ((LIST_KEYS as readonly string[]).includes(section) && listEdits[section as ListKey] !== undefined) ||
      (exposureEdit !== null && (exposureEdit.card === section || (exposureEdit.card === "accepted_risks" && section === "prioritization_policy")));
    // On CTEM > Inventory the cards never fold (#1690, Sam 24 Nov 2026): a static header, always expanded.
    if (open || embed) return { meta: count === null ? undefined : nf.count(count) };
    return {
      meta: count === null ? undefined : nf.count(count),
      collapsed: folded.has(section),
      onCollapsedChange: (next) =>
        setFolded((prev) => {
          const set = new Set(prev);
          if (next) set.add(section);
          else set.delete(section);
          return set;
        }),
    };
  };
  const rate = Number((current?.loaded_hourly_rate ?? "").replace(/\s/g, ""));
  const money = (value: number) => {
    try {
      if (current?.currency) return new Intl.NumberFormat(locale, { style: "currency", currency: current.currency, maximumFractionDigits: 2 }).format(value);
    } catch {
      // A stored code Intl does not know: the number alone, then the code.
    }
    return `${formatAmount(String(value), locale)} ${current?.currency ?? ""}`.trim();
  };
  const ratePreview =
    current?.loaded_hourly_rate.trim() && Number.isFinite(rate)
      ? t_i18n("1 hour saved = {amount} in the ROI figures", { amount: money(rate) })
      : undefined;
  /** What a saved threshold does, in words (#9173 S2): read mode only, no model call. */
  const thresholdPreview = (key: "annual_loss_threshold" | "approval_threshold") => {
    const raw = current?.[key] ?? "";
    const value = Number(raw.replace(/\s/g, ""));
    if (editing || !raw.trim() || !Number.isFinite(value)) return undefined;
    return key === "annual_loss_threshold"
      ? t_i18n("Scenarios above {amount} per year are ranked first", { amount: money(value) })
      : t_i18n("Scenarios above {amount} per year are flagged for approval", { amount: money(value) });
  };

  // The fields not filled that Settings still draws (#1690): the Business, Technology and Exposure fields are
  // reported on CTEM > Inventory, and Copilot mode reports none (Sam: "we don't care").
  const missing = xtmOne ? (data?.missing_fields ?? []).filter((path) => { const owner = tabOfField(path); return owner !== null && !isInventoryTab(owner); }) : [];
  /** The exposure cards a tab draws (#12329): the exposure tab three, the knowledge tab one. */
  const exposureCards = (cards: readonly (typeof EXPOSURE_SECTIONS)[number][]) =>
    current && (
      <OrganizationExposureCards
        cards={cards}
        readOnly={reader}
        current={!editing && exposureEdit ? exposureEdit.form : current}
        editing={editing}
        fieldId={fieldId}
        update={editing ? update : updateExposure}
        onImport={() => setAssetsImportOpen(true)}
        issues={[
          ...(editing && checkExposure ? exposureIssues(current) : []),
          ...(!editing && exposureEdit && checkExposure ? exposureIssues(exposureEdit.form) : []),
          ...(refusedIssue ? [refusedIssue] : []),
        ]}
        onScoreValidity={setScoreInvalid}
        cardEdit={editing ? null : (exposureEdit?.card ?? null)}
        editAction={exposureEditAction}
        saveBar={(card) =>
          !editing && exposureEdit?.card === card ? (
            <SettingsSaveBar
              dirty={exposureDirty(exposureEdit)}
              saving={save.isPending}
              saveDisabled={!exposureDirty(exposureEdit) || scoreInvalid}
              onSave={saveExposure}
              onCancel={stopExposureEdit}
            />
          ) : null
        }
        fold={(section) =>
          cardFold(
            section,
            // The profile API answers counts, never the rows (#1752 slice 3b).
            section === "assets_exposure"
              ? (data?.row_counts?.assets_exposure ?? 0)
              : section === "asset_groups"
                ? (data?.row_counts?.asset_groups ?? 0)
                : section === "enterprise_knowledge"
                  ? (current.enterprise_knowledge ?? []).length
                  : null,
          )
        }
      />
    );
  /** The fields not filled in a tab, at the top of its panel: each opens the form on it. */
  const missingIn = (key: ProfileTab) => {
    // An embedding page shows the cards alone: the missing fields open the whole form, on Settings.
    if (embed) return null;
    const paths = missing.filter((path) => tabOfField(path) === key);
    if (paths.length === 0) return null;
    return (
      <div data-testid="settings.organization.missing" className="space-y-2 rounded-sm border border-elevation-subtle p-3 content-base">
        <p>{t_i18n("Fields not filled, reported by every agent that needs them:")}</p>
        <div className="flex flex-wrap gap-2">
          {paths.map((path) => (
            <Button key={path} variant="secondary" className="gap-1.5" disabled={drafting.isPending || cardOpen !== null} onClick={() => jumpTo(path)}>
              <Icon name="plus" size={16} aria-hidden />
              {t_i18n((FIELD_LABELS as Record<string, string>)[path] ?? path)}
            </Button>
          ))}
        </div>
      </div>
    );
  };

  return (
    <div data-testid="settings.organization" className="space-y-4">
      {/* The Knowledge and sources page draws its own header, like its siblings (Inventory sources, the asset page):
          the back link first, then its title; the profile's Draft, Edit and fields banner stay on the profile
          (UX round 1 on #1476, B1). */}
      {full && tab === "knowledge" && (
        <div className="space-y-2" data-testid="settings.organization.knowledge-header">
          <BackButton to={profileTabPath("identity")} label="Back to Organization Profile" />
          <h3 className="flex items-center gap-2 title-xs">
            {t_i18n(PROFILE_TAB_LABELS.knowledge)}
            <InfoTip
              label={t_i18n("About the knowledge and sources")}
              content={t_i18n("The procedures and reference documents agents search, the organization context they preload, and the sources that feed the primary assets and PIRs.")}
            />
          </h3>
        </div>
      )}
      {/* A short header (#12329): the title with its explanation in a bubble, the version line, Draft and Edit. */}
      {full && tab !== "knowledge" && (
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 max-w-3xl space-y-1">
          <h3 className="flex items-center gap-2 title-xs">
            <Building2 className="h-4 w-4 text-accent" /> {t_i18n("Organization profile")}
            <InfoTip
              label={t_i18n("About the organization profile")}
              content={
                <div className="max-w-xs space-y-1 content-compact">
                  <p>
                    {t_i18n(
                      "What every agent and fleet reads before it ranks, scopes or estimates: your sector, regions, primary assets, software, intelligence requirements, risk appetite and costs. Agents cite the fields they used and name the ones not filled, never guess them. Each save is a new version.",
                    )}
                  </p>
                  <p data-testid="settings.organization.shared">{t_i18n("Shared context: the whole profile is visible to every user and every agent of this XTM One platform.")}</p>
                </div>
              }
            />
          </h3>
          {data?.version != null && data.created_at && (
            <p className="content-compact text-default-secondary">
              {/* The source the API returns for the saved version: a draft of the platforms or the form; another keeps the plain line. */}
              {data.source === "draft"
                ? t_i18n("Version {version}, saved {date} from a draft of the platforms", { version: data.version, date: savedAt })
                : data.source === "form"
                  ? t_i18n("Version {version}, saved {date} from the form", { version: data.version, date: savedAt })
                  : t_i18n("Version {version}, saved {date}", { version: data.version, date: savedAt })}
            </p>
          )}
        </div>
        {(!editing || setupGuide) && (
          <div className="flex shrink-0 items-center gap-2">
            {/* Knowledge and sources is a page of its own, opened from here (#1367, point 7: a button, not a tab). */}
            {full && (
              <Button variant="secondary" className="gap-1.5" onClick={() => goToTab("knowledge")} data-testid="settings.organization.knowledge">
                <Icon name="database" size={16} aria-hidden /> {t_i18n(PROFILE_TAB_LABELS.knowledge)}
              </Button>
            )}
            {/* One "Draft" menu (section 6.1): the free deterministic draft and the metered AI draft. */}
            <OverflowMenu
              label={t_i18n("Draft")}
              size="md"
              busy={isLoading || !data || drafting.isPending || cardOpen !== null}
              trigger={
                <>
                  {drafting.isPending && <Spinner size="sm" tone="inherit" />}{!drafting.isPending && <Import className="h-3.5 w-3.5" />}
                  {t_i18n("Draft")}
                </>
              }
              items={draftMenuItems}
            />
            {/* Disabled while a draft is pending: its result opens the form and would replace what was typed meanwhile. */}
            {/* A card in edit keeps the page's Edit, Draft and jumps disabled: each opens the whole form
                and would drop the card's rows without a word (UX r1 B1 on #12261). */}
            {!editing && (
              <Tip content={cardOpen ? t_i18n("Save or cancel the edit first.") : ""} wrap="auto">
                <Button className="gap-1.5" onClick={startEdit} disabled={isLoading || !data || drafting.isPending || cardOpen !== null} data-testid="settings.organization.edit">
                  <Pencil className="h-3.5 w-3.5" /> {t_i18n("Edit")}
                </Button>
              </Tip>
            )}
          </div>
        )}
      </div>
      )}

      {setupGuide && editing && (
        <p data-testid="settings.organization.setup-guide" className="rounded-sm border border-elevation-subtle p-3 content-base">
          {t_i18n("Start from a draft: Draft > From the platforms reads OpenCTI, OpenAEV and OpenCRQ, Draft > With AI proposes values to review. Nothing is saved until you click Save.")}
        </p>
      )}

      {/* One compact row instead of the four figure cards (#12329): the completeness is per tab on the tab
          bar, the fields not filled sit at the top of their tab, and onboarding stays one click away. */}
      {full && missing.length > 0 && !editing && tab !== "knowledge" && (
        <div data-testid="settings.organization.overview" className="flex flex-wrap items-center justify-between gap-2 rounded-sm border border-elevation-subtle px-3 py-2 content-base">
          <p>
            {t_i18n("{count, plural, one {# field not filled, reported by every agent that needs it} other {# fields not filled, reported by every agent that needs them}}", {
              count: missing.length,
            })}
          </p>
          <Button data-testid="settings.organization.continue-onboarding" variant="secondary" className="gap-1.5" disabled={drafting.isPending} onClick={startOnboarding}>
            {aiAvailable ? <MessageSquare className="h-3.5 w-3.5" /> : <Lock className="h-3.5 w-3.5" />}
            {t_i18n("Continue onboarding in chat")}
          </Button>
        </div>
      )}

      {full && draftFailed && !editing && (
        <div role="alert" data-testid="settings.organization.draft-error" className="flex items-center justify-between gap-3 rounded-sm border border-alert-error bg-alert-error p-3 content-base">
          <p className="space-x-1">
            <span className="font-medium">{t_i18n("The draft could not be made")}</span>
            <span>{t_i18n("Try again in a moment.")}</span>
          </p>
          <Button variant="secondary" onClick={startDraft} disabled={drafting.isPending}>
            {t_i18n("Retry")}
          </Button>
        </div>
      )}

      {draft && (
        <div data-testid="settings.organization.draft" className="space-y-1.5 rounded-sm border border-elevation-subtle p-3 content-base">
          <p className="font-medium">{t_i18n("Drafted from the platforms, not saved yet. Review the fields, then save.")}</p>
          {draft.filled.length === 0 ? (
            <p className="text-default-secondary">{t_i18n("The platforms had nothing to add to the empty fields.")}</p>
          ) : (
            // Grouped by the tab that holds each field (#12329), with the number filled per tab.
            <div className="space-y-1.5">
              {groupByTab(draft.filled, (row) => row.field).map((group) => (
                <div key={group.tab ?? "other"} data-testid={`settings.organization.draft-group-${group.tab ?? "other"}`}>
                  <p className="font-medium">
                    {group.tab ? t_i18n(PROFILE_TAB_LABELS[group.tab]) : t_i18n("Other")}{" "}
                    <span className="font-normal text-default-secondary">{t_i18n("{count, plural, one {# field} other {# fields}}", { count: group.items.length })}</span>
                  </p>
                  <ul className="list-disc space-y-0.5 pl-5">
                    {group.items.map((row) => (
                      <li key={row.field}>
                        {t_i18n("{field} from {platform}", {
                          field: t_i18n((FIELD_LABELS as Record<string, string>)[row.field] ?? row.field),
                          platform: PLATFORM_NAMES[row.source] ?? row.source,
                        })}
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
          {/* A platform that could not be read links where to fix it (staging defect b); its own sentence, never the backend's text (#9102 A1). */}
          {(draft.unavailable ?? []).map((line) => {
            const platform = unreadablePlatform(line);
            return (
              <p key={line} className="text-default-secondary">
                {platform === "OpenCTI PIRs"
                  ? t_i18n("OpenCTI PIRs could not be read")
                  : platform
                    ? t_i18n("{platform} could not be read", { platform })
                    : t_i18n("A platform could not be read")}
                {" "}
                <a className="text-accent underline" href="/settings/integrations">
                  {t_i18n("Settings > Integrations")}
                </a>
              </p>
            );
          })}
        </div>
      )}

      {current && (
        // The URL holds the panel (#12329): Settings draws no tab bar since #1690 (Identity, and Knowledge
        // through its button); the Business, Technology and Exposure cards live on CTEM > Inventory.
        <div className="space-y-4">
          {tab === "identity" && (
          <div role="tabpanel" aria-label={t_i18n(PROFILE_TAB_LABELS.identity)} className="space-y-4" data-testid="settings.organization.panel-identity">
          {missingIn("identity")}
          <SettingsCard
            id={`${fieldId}-identity`}
            className={SECTION_BAR_ANCHOR_CLASS}
            data-anchor="card"
            tabIndex={-1}
            icon={Building2}
            title={t_i18n("Identity")}
            description={t_i18n("Who you are, where you operate and how large you are.")}
          >
            {!editing ? (
              <SettingsReadGrid data-testid="settings.organization.identity">
                <SettingsReadField label={t_i18n("Organization name")} info={help("name").info} value={current.name.trim()} />
                <SettingsReadField label={t_i18n("Organization size")} info={help("size").info} value={current.size ? t_i18n("{band} employees", { band: current.size }) : ""} />
                <SettingsReadField label={t_i18n("Sector")} info={help("sector").info} value={current.sector.trim()} />
                <SettingsReadField label={t_i18n("Sub-sector")} info={help("sub_sector").info} value={current.sub_sector.trim()} />
                <SettingsReadField
                  label={t_i18n("Regions of operation")}
                  info={help("regions").info}
                  wide
                  value={
                    current.regions.length > 0 && (
                      <ExpandableChipList
                        items={current.regions}
                        getKey={(code) => code}
                        ariaLabel={t_i18n("Regions of operation")}
                        renderChip={(code) => (
                          <Badge variant="secondary" className="max-w-full gap-1.5 font-normal content-base" title={countryName(code)}>
                            <CountryFlag code={code} name={countryName(code)} />
                            <span className="truncate">{countryName(code)}</span>
                          </Badge>
                        )}
                      />
                    )
                  }
                />
              </SettingsReadGrid>
            ) : (
            <SettingsFieldGrid>
              {text("name", "Organization name")}
              <SettingsField id={`${fieldId}-size`} label={t_i18n("Organization size")} {...help("size")}>
                <Select value={current.size || NO_SIZE} onValueChange={(value) => update({ size: value === NO_SIZE ? "" : value })}>
                  <SelectTrigger id={`${fieldId}-size`} aria-labelledby={settingsFieldLabelId(`${fieldId}-size`)} aria-describedby={settingsFieldHintId(`${fieldId}-size`)}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={NO_SIZE}>{t_i18n("Not set")}</SelectItem>
                    {SIZE_BANDS.map((band) => (
                      <SelectItem key={band} value={band}>
                        {t_i18n("{band} employees", { band })}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </SettingsField>
              {text("sector", "Sector", "Finance")}
              {text("sub_sector", "Sub-sector", "Retail banking")}
              <SettingsField id={`${fieldId}-regions`} label={t_i18n("Regions of operation")} wide {...help("regions")}>
                <MultiSelect
                  id={`${fieldId}-regions`}
                  aria-label={t_i18n("Regions of operation")}
                  aria-describedby={settingsFieldHintId(`${fieldId}-regions`)}
                  options={countryOptions}
                  selected={current.regions}
                  onToggle={(code) =>
                    update({ regions: current.regions.includes(code) ? current.regions.filter((c) => c !== code) : [...current.regions, code] })
                  }
                  placeholder={t_i18n("Pick the countries")}
                  searchPlaceholder={t_i18n("Search a country")}
                  emptyOptionsLabel={t_i18n("No country")}
                  noMatchLabel={t_i18n("No matching country")}
                />
              </SettingsField>
            </SettingsFieldGrid>
            )}
          </SettingsCard>

          {xtmOne && (
          <>
          <SettingsCard
            id={`${fieldId}-risk_appetite`}
            className={SECTION_BAR_ANCHOR_CLASS}
            data-anchor="card"
            tabIndex={-1}
            icon={Coins}
            title={t_i18n("Risk appetite and costs")}
            description={t_i18n("Pick the currency first: every amount below is in it. The risk quantifier compares scenarios with the thresholds; the ROI figures use the hourly rate.")}
          >
            {!editing ? (
              <SettingsReadGrid data-testid="settings.organization.risk">
                <SettingsReadField label={t_i18n(FIELD_LABELS.currency)} info={help("currency").info} mono value={current.currency} />
                {readAmount("loaded_hourly_rate", "Loaded hourly rate", "hour", ratePreview)}
                {readAmount("annual_loss_threshold", "Risk tolerance per scenario", "year", thresholdPreview("annual_loss_threshold"))}
                {readAmount("approval_threshold", "Escalation threshold", "year", thresholdPreview("approval_threshold"))}
              </SettingsReadGrid>
            ) : (
            <SettingsFieldGrid>
              <SettingsField id={`${fieldId}-currency`} label={t_i18n(FIELD_LABELS.currency)} {...help("currency")}>
                <SearchableSelect
                  id={`${fieldId}-currency`}
                  aria-labelledby={settingsFieldLabelId(`${fieldId}-currency`)}
                  aria-describedby={settingsFieldHintId(`${fieldId}-currency`)}
                  pinned={[{ value: NO_VALUE, label: t_i18n("Not set") }]}
                  options={currencyOptions.map((code) => ({ value: code, label: code }))}
                  sort={false}
                  value={current.currency || NO_VALUE}
                  onValueChange={(value) => update({ currency: value === NO_VALUE ? "" : value })}
                />
              </SettingsField>
              {amount("loaded_hourly_rate", "Loaded hourly rate", t_i18n("{currency} per hour", { currency: currencySuffix }), "85", ratePreview)}
              {amount("annual_loss_threshold", "Risk tolerance per scenario", t_i18n("{currency} per year", { currency: currencySuffix }), "250000", thresholdPreview("annual_loss_threshold"))}
              {amount("approval_threshold", "Escalation threshold", t_i18n("{currency} per year", { currency: currencySuffix }), "1000000", thresholdPreview("approval_threshold"))}
            </SettingsFieldGrid>
            )}
          </SettingsCard>

          <SettingsCard
            id={`${fieldId}-ticketing`}
            className={SECTION_BAR_ANCHOR_CLASS}
            data-anchor="card"
            tabIndex={-1}
            icon={Ticket}
            title={t_i18n("Ticketing and intelligence")}
            description={t_i18n("Where agents open tickets and which OpenCTI PIRs they follow.")}
          >
            {!editing ? (
              <SettingsReadGrid data-testid="settings.organization.ticketing">
                <SettingsReadField
                  label={t_i18n("Ticketing integration")}
                  info={help("ticketing_integration").info}
                  value={current.ticketing.integration ? (ticketingIntegration?.name ?? current.ticketing.integration) : ""}
                />
                <SettingsReadField
                  label={t_i18n("Ticketing project")}
                  info={help("ticketing_project").info}
                  value={current.ticketing.project ? (projectOptions.find((p) => p.id === current.ticketing.project)?.name ?? current.ticketing.project) : ""}
                />
                <SettingsReadField
                  label={t_i18n("Priority intelligence requirements (PIRs)")}
                  info={help("pir_ids").info}
                  wide
                  value={
                    // Read mode (#9615): the names as wrapped chips linked to OpenCTI, no search, no remove cross, no chevron.
                    <OrganizationPirsView
                      pirs={savedPirs}
                      list={pirs.data}
                      unreadable={pirsUnreadable}
                      retrying={pirs.isFetching}
                      onRetry={() => void pirs.refetch()}
                      onRemove={removePir}
                    />
                  }
                />
              </SettingsReadGrid>
            ) : (
            <SettingsFieldGrid>
              <SettingsField id={`${fieldId}-ticketing-integration`} label={t_i18n("Ticketing integration")} {...help("ticketing_integration")}>
                <SearchableSelect
                  id={`${fieldId}-ticketing-integration`}
                  aria-labelledby={settingsFieldLabelId(`${fieldId}-ticketing-integration`)}
                  aria-describedby={settingsFieldHintId(`${fieldId}-ticketing-integration`)}
                  searchable="auto"
                  pinned={[{ value: NO_VALUE, label: t_i18n("Not set") }]}
                  options={[
                    // A value saved as typed text ("jira") stays shown until another is picked.
                    ...(current.ticketing.integration && !ticketingIntegrations.some((i) => i.id === current.ticketing.integration)
                      ? [{ value: current.ticketing.integration, label: ticketingIntegration?.name ?? current.ticketing.integration }]
                      : []),
                    ...ticketingIntegrations.map((integration) => ({ value: integration.id, label: integration.name })),
                  ]}
                  value={current.ticketing.integration || NO_VALUE}
                  onValueChange={(value) => update({ ticketing: { integration: value === NO_VALUE ? "" : value, project: "" } })}
                />
              </SettingsField>
              <SettingsField id={`${fieldId}-ticketing-project`} label={t_i18n("Ticketing project")} {...help("ticketing_project")}>
                {projectOptions.length > 0 ? (
                  <SearchableSelect
                    id={`${fieldId}-ticketing-project`}
                    aria-labelledby={settingsFieldLabelId(`${fieldId}-ticketing-project`)}
                    aria-describedby={settingsFieldHintId(`${fieldId}-ticketing-project`)}
                    searchable="auto"
                    pinned={[{ value: NO_VALUE, label: t_i18n("Not set") }]}
                    options={[
                      ...(current.ticketing.project && !projectOptions.some((p) => p.id === current.ticketing.project)
                        ? [{ value: current.ticketing.project, label: current.ticketing.project }]
                        : []),
                      ...projectOptions.map((project) => ({ value: project.id, label: project.name })),
                    ]}
                    value={current.ticketing.project || NO_VALUE}
                    onValueChange={(value) => update({ ticketing: { ...current.ticketing, project: value === NO_VALUE ? "" : value } })}
                  />
                ) : (
                  <Input
                    id={`${fieldId}-ticketing-project`}
                    aria-labelledby={settingsFieldLabelId(`${fieldId}-ticketing-project`)}
                    aria-describedby={settingsFieldHintId(`${fieldId}-ticketing-project`)}
                    placeholder="SEC"
                    value={current.ticketing.project}
                    onChange={(e) => update({ ticketing: { ...current.ticketing, project: e.target.value } })}
                  />
                )}
              </SettingsField>
              <SettingsField
                id={`${fieldId}-pir_ids`}
                label={t_i18n("Priority intelligence requirements (PIRs)")}
                wide
                {...help("pir_ids")}
                helper={
                  pirs.data?.unavailable === "not_connected"
                    ? t_i18n("The PIRs of OpenCTI could not be listed: the saved ones are kept.")
                    : t_i18n(FIELD_HELP.pir_ids.helper)
                }
              >
                <div className="flex flex-col gap-1.5">
                  {pirsLoading ? (
                    // The picker waits for the names: its chips would otherwise read a placeholder (#11589).
                    <div
                      className="flex min-h-9 flex-wrap items-center gap-1.5 rounded-sm border border-elevation-subtle-soft px-2 py-1.5"
                      role="status"
                      aria-busy="true"
                      aria-label={t_i18n("Loading the PIR names")}
                      data-testid="settings.organization.pirs-loading"
                    >
                      {savedPirs.slice(0, 8).map((pir) => (
                        <Skeleton key={pir.id} className="h-6 w-32 rounded-sm" />
                      ))}
                    </div>
                  ) : (
                    <MultiSelect
                      id={`${fieldId}-pir_ids`}
                      aria-label={t_i18n("Priority intelligence requirements (PIRs)")}
                      aria-describedby={settingsFieldHintId(`${fieldId}-pir_ids`)}
                      options={pirOptions}
                      selected={current.pir_ids}
                      onToggle={(id) => update({ pir_ids: current.pir_ids.includes(id) ? current.pir_ids.filter((p) => p !== id) : [...current.pir_ids, id] })}
                      placeholder={t_i18n("Pick the PIRs")}
                      searchPlaceholder={t_i18n("Search a PIR")}
                      emptyOptionsLabel={pirsEmptyLabel}
                      noMatchLabel={t_i18n("No matching PIR")}
                    />
                  )}
                  {/* Edit mode says it too, with Retry, as read mode does (#11589). */}
                  {pirsUnreadable && <PirsUnreadable retrying={pirs.isFetching} onRetry={() => void pirs.refetch()} />}
                  {/* The picker's chips cut a long label at 250 px and take no title (library Chip):
                      a PIR OpenCTI no longer has is also written out whole here (UX round 1 B2). */}
                  {missingPirs.length > 0 && (
                    <ul className="flex flex-col gap-0.5 content-compact text-default-secondary" data-testid="settings.organization.pirs-missing" aria-label={t_i18n("PIR no longer in OpenCTI")}>
                      {missingPirs.map((pir) => (
                        <li key={pir.id} className="break-words">{pirText(pir)}</li>
                      ))}
                    </ul>
                  )}
                </div>
              </SettingsField>
            </SettingsFieldGrid>
            )}
            {/* How CTEM cases sync with the target (#917): part of the profile form, read by the next sync. */}
            <div className="space-y-3 border-t border-elevation-subtle-soft pt-4" data-testid="settings.organization.ticketing-sync-group">
              <SettingsGroupTitle icon={RefreshCw}>{t_i18n("CTEM case sync")}</SettingsGroupTitle>
              <p className="content-compact text-default-secondary">
                {current.ticketing.integration
                  ? t_i18n("How CTEM cases open, update and close their tickets. A change applies to the next sync.")
                  : t_i18n("No ticketing connected: cases stay in XTM One. Set the ticketing integration above to sync them.")}
              </p>
              <OrganizationTicketingSync
                idPrefix={`${fieldId}-ticketing-sync`}
                sync={current.ticketing.sync}
                provider={ticketingIntegration?.provider ?? ""}
                editing={editing}
                onChange={(sync) => update({ ticketing: { ...current.ticketing, sync } })}
              />
            </div>
          </SettingsCard>
          </>
          )}
          </div>
          )}

          {tab === "business" && (
          <div role="tabpanel" aria-label={t_i18n(PROFILE_TAB_LABELS.business)} className="space-y-4" data-testid="settings.organization.panel-business">
          {missingIn("business")}
          <SettingsCard
            id={`${fieldId}-crown_jewels`}
            className={SECTION_BAR_ANCHOR_CLASS}
            data-anchor="card"
            {...cardFold("crown_jewels", current.crown_jewels.length)}
            tabIndex={-1}
            data-testid="settings.organization.primary-assets"
            icon={Shield}
            title={t_i18n("Primary assets and business services")}
            description={t_i18n("What the business cannot lose, ranked by criticality.")}
            action={
              <>
                {attention("crown_jewels")}
                {!reader && !listOpen("crown_jewels") && (
                  // An icon button: a third labelled action wraps the card title at 1080 (UX round 1 B1, #1567).
                  <Tip content={editing || cardOpen ? t_i18n("Save or cancel the edit first.") : t_i18n("Restore from a past version")} wrap="auto">
                    <Button
                      variant="secondary"
                      size="icon"
                      aria-label={t_i18n("Restore from a past version")}
                      data-testid="settings.organization.primary-assets-restore"
                      disabled={editing || cardOpen !== null}
                      onClick={() => setRestoreOpen(true)}
                    >
                      <Icon name="rotate-ccw" size={16} aria-hidden />
                    </Button>
                  </Tip>
                )}
                {listEditAction("crown_jewels")}
                {pasteAction("crown_jewels")}
                {listOpen("crown_jewels") && (
                  <Button
                    variant="secondary"
                    className="gap-1.5"
                    onClick={() => addListRow("crown_jewels", { name: "", business_service: "", owner: "", criticality: "high", opencrq_asset_id: "" })}
                  >
                    <Plus className="h-3.5 w-3.5" /> {t_i18n("Add a primary asset")}
                  </Button>
                )}
              </>
            }
          >
            {listEditor("crown_jewels", {
              columns: jewelEdit,
              filters: [valueFilter("criticality", "Criticality", CRITICALITY_LABELS, (v: Jewel) => v.criticality, true)], // ordinal scale (#12289)
              searchFields: (v) => [v.name, v.business_service, v.owner],
              rowLabel: (v) => v.name,
              storageKey: "settings-organization-crown-jewels",
              rowTestId: "settings.organization.primary-asset-row",
              emptyLabel: t_i18n("No primary asset yet: agents cannot rank by business impact."),
            }) ??
              (current.crown_jewels.length === 0 ? (
                <p className="text-default-secondary">{t_i18n("No primary asset yet: agents cannot rank by business impact.")}</p>
              ) : (
                <DataTable
                  rows={current.crown_jewels.map((value, index) => ({ index, value }))}
                  columns={jewelColumns}
                  layout={tableLayout}
                  rowKey={(row) => String(row.index)}
                  searchFields={(row) => [row.value.name, row.value.business_service, row.value.owner]}
                  searchPlaceholder={t_i18n("Search...")}
                  filters={jewelFilters}
                  exportName="primary-assets"
                  urlKey="ocj_"
                  storageKey="settings-organization-primary-assets"
                  emptyLabel={t_i18n("No primary asset yet: agents cannot rank by business impact.")}
                  rowTestId="settings.organization.primary-asset-row"
                />
              ))}
            <OrganizationCrownJewelsRestoreDialog open={restoreOpen} onOpenChange={setRestoreOpen} currentVersion={data?.version ?? null} />
          </SettingsCard>

          {/* What the organization ships (#1432), apart from the software inventory it runs: CTEM resolves a vulnerability on one to its repositories. */}
          <SettingsCard
            id={`${fieldId}-shipped_products`}
            className={SECTION_BAR_ANCHOR_CLASS}
            data-anchor="card"
            {...cardFold("shipped_products", current.shipped_products.length)}
            tabIndex={-1}
            data-testid="settings.organization.shipped-products"
            icon={Package}
            title={t_i18n("Shipped products")}
            description={t_i18n("The products your organization ships, with their source repositories: a vulnerability on one resolves to its repositories in CTEM.")}
            action={
              <>
                {attention("shipped_products")}
                {listEditAction("shipped_products")}
                {pasteAction("shipped_products")}
                {listOpen("shipped_products") && (
                  <Button variant="secondary" className="gap-1.5" onClick={() => addListRow("shipped_products", { product: "", vendor: "", cpe: "", repositories: [] })}>
                    <Plus className="h-3.5 w-3.5" /> {t_i18n("Add a shipped product")}
                  </Button>
                )}
              </>
            }
          >
            {listEditor("shipped_products", {
              columns: shippedEdit,
              filters: [{ key: "vendor", label: "Vendor", values: (v: Shipped) => [v.vendor.trim()] }],
              searchFields: (v) => [v.product, v.vendor, v.cpe, ...v.repositories],
              rowLabel: (v) => v.product,
              storageKey: "settings-organization-shipped-products",
              rowTestId: "settings.organization.shipped-row",
              emptyLabel: t_i18n("No shipped product yet: a vulnerability on a product you ship stays unresolved."),
            }) ??
              (current.shipped_products.length === 0 ? (
                <p className="text-default-secondary">{t_i18n("No shipped product yet: a vulnerability on a product you ship stays unresolved.")}</p>
              ) : (
                <DataTable
                  rows={current.shipped_products.map((value, index) => ({ index, value }))}
                  columns={shippedColumns}
                  layout={tableLayout}
                  rowKey={(row) => String(row.index)}
                  searchFields={(row) => [row.value.product, row.value.vendor, row.value.cpe, ...row.value.repositories]}
                  searchPlaceholder={t_i18n("Search...")}
                  filters={[{ key: "vendor", label: "Vendor", values: ({ value }) => [value.vendor.trim()] }]}
                  exportName="shipped-products"
                  urlKey="osp_"
                  storageKey="settings-organization-shipped-products"
                  emptyLabel={t_i18n("No shipped product yet: a vulnerability on a product you ship stays unresolved.")}
                  rowTestId="settings.organization.shipped-row"
                />
              ))}
          </SettingsCard>

          {/* The tracked organizations (#9157, M2): added from this page; the first-login setup sets up the primary only. */}
          {/* The landing anchor sits on the card root, so its focus ring keeps the card's radius (UX r1 B2 on #108). */}
          <TrackedOrganizations id={`${fieldId}-tracked_organizations`} className={SECTION_BAR_ANCHOR_CLASS} data-anchor="card" tabIndex={-1} readOnly={reader} />
          </div>
          )}

          {tab === "technology" && (
          <div role="tabpanel" aria-label={t_i18n(PROFILE_TAB_LABELS.technology)} className="space-y-4" data-testid="settings.organization.panel-technology">
          {missingIn("technology")}
          {show("tech_stack") && (
          <SettingsCard
            id={`${fieldId}-tech_stack`}
            className={SECTION_BAR_ANCHOR_CLASS}
            data-anchor="card"
            {...cardFold("tech_stack", current.tech_stack.length)}
            tabIndex={-1}
            data-testid="settings.organization.tech-stack"
            icon={Server}
            title={t_i18n("Software")}
            description={
              embed
                ? t_i18n("The software you declare by hand. The Software list adds the software of your assets.")
                : t_i18n("The products you run, matched against vulnerabilities.")
            }
            action={
              <>
                {attention("tech_stack")}
                {listEditAction("tech_stack")}
                {pasteAction("tech_stack")}
                {listOpen("tech_stack") && (
                  <Button variant="secondary" className="gap-1.5" onClick={() => addListRow("tech_stack", { product: "", vendor: "", cpe: "", version: "" })}>
                    <Plus className="h-3.5 w-3.5" /> {t_i18n("Add a product")}
                  </Button>
                )}
              </>
            }
          >
            {listEditor("tech_stack", {
              columns: techEdit,
              filters: [{ key: "vendor", label: "Vendor", values: (v: Tech) => [v.vendor.trim()] }],
              searchFields: (v) => [v.product, v.vendor, v.cpe, v.version],
              rowLabel: (v) => v.product,
              storageKey: "settings-organization-tech-stack",
              rowTestId: "settings.organization.tech-row",
              emptyLabel: techEmptyLabel,
            }) ??
              (current.tech_stack.length === 0 ? (
                <p className="text-default-secondary">{techEmptyLabel}</p>
              ) : (
                <DataTable
                  rows={current.tech_stack.map((value, index) => ({ index, value }))}
                  columns={techColumns}
                  layout={tableLayout}
                  rowKey={(row) => String(row.index)}
                  searchFields={(row) => [row.value.product, row.value.vendor, row.value.cpe]}
                  searchPlaceholder={t_i18n("Search...")}
                  filters={techFilters}
                  exportName="tech-stack"
                  urlKey="ots_"
                  storageKey="settings-organization-tech-stack"
                  emptyLabel={techEmptyLabel}
                />
              ))}
          </SettingsCard>
          )}

          </div>
          )}

          {tab === "exposure" && (
          <div role="tabpanel" aria-label={t_i18n(PROFILE_TAB_LABELS.exposure)} className="space-y-4" data-testid="settings.organization.panel-exposure">
            {missingIn("exposure")}
            {/* The field changes proposed outside the form, the loop trace's scope proposal first among them (CTEM-30). */}
            {full && <OrganizationProposalsCard />}
            {/* The asset and group tables are CTEM > Inventory's, on the row APIs (#1752 slice 3b). */}
            {exposureCards((["prioritization_policy"] as const).filter(show))}
            {/* Owned domains, address ranges and the outside lens pass (CTEM-26 slice 2, #1140). */}
            {show("outside_lens") && <OrganizationOutsideLensCard id={`${fieldId}-outside_lens`} fold={cardFold("outside_lens", null)} />}
            {/* The OpenCRQ evidence health pass, on by default (#1344): the signal sources' Turn on lands here. */}
            {show("evidence_health") && (
              <OrganizationEvidenceHealthCard id={`${fieldId}-evidence_health`} fold={cardFold("evidence_health", null)} />
            )}
          </div>
          )}

          {/* Enterprise knowledge, the scheduled imports and the context agents preload: what feeds the profile and how it is read. */}
          {tab === "knowledge" && (
          <div role="tabpanel" aria-label={t_i18n(PROFILE_TAB_LABELS.knowledge)} className="space-y-4" data-testid="settings.organization.panel-knowledge">
            {missingIn("knowledge")}
            {exposureCards(["enterprise_knowledge"])}
            {/* The platform default of the organization summary cascade (#11533). */}
            <OrganizationContextInjectionCard />
            {/* Sources and syncs (#1276), the tab's last block so the table fits the window under the
                tab bar: one scroller, nothing below the list (UX r1 B1, B2 on #1294). The business profile's
                sources here, the inventory's on CTEM > Inventory > Sources (#1178). */}
            <OrganizationSourcesSyncs scope="profile" onAddSource={() => setPlatformImportOpen(true)} disabled={editing || cardOpen !== null || drafting.isPending} />
          </div>
          )}

          {/* One save bar for the whole form, on every tab, sticky at the bottom (#12329). */}
          {editing && <SettingsSaveBar dirty={dirty} saving={save.isPending} saveDisabled={scoreInvalid} onSave={submit} onCancel={stopEditing} />}
        </div>
      )}

      {data && aiDraftOpen && (
        <OrganizationAiDraftDialog
          open={aiDraftOpen}
          onOpenChange={setAiDraftOpen}
          profile={formProfile ?? data.profile}
          platforms={connectedPlatforms}
          // From the open form (the setup path): "Apply accepted (n)" fills the form, its own Save writes it.
          onApply={
            editing
              ? (merged) => {
                  // The form opens on the first tab with proposals; each tab the draft changed is marked (#12329).
                  const next = toForm(merged);
                  const before = form ? withLists(form) : next;
                  const drafted = PROFILE_TABS.filter((key) => changedTabs(before, next).has(key));
                  setListEdits({});
                  setForm(next);
                  goToTab(drafted[0] ?? null);
                }
              : undefined
          }
          // Saved from read mode: the default assistant takes over for the fields left (#11224).
          onSaved={editing ? undefined : startOnboarding}
        />
      )}
      {assetsImportOpen && <OrganizationAssetsImportDialog open={assetsImportOpen} onOpenChange={setAssetsImportOpen} />}
      {pasteFor && listOpen(pasteFor) && pasteDialog(pasteFor)}
      {synced.element}
      {fillAgentOpen && <OrganizationFillAgentDialog open={fillAgentOpen} onOpenChange={setFillAgentOpen} />}
      {platformImportOpen && (
        <OrganizationImportDialog
          open={platformImportOpen}
          onOpenChange={setPlatformImportOpen}
          // The inventory sections are imported from CTEM > Inventory > Sources only (#1367, point 5).
          sections={PROFILE_IMPORT_SECTIONS}
          quickSetupPending={drafting.isPending}
          onQuickSetup={() => {
            setPlatformImportOpen(false);
            startDraft();
          }}
        />
      )}
      <AgenticGateDialogControlled open={aiGateOpen} onOpenChange={setAiGateOpen} feature={t_i18n("Draft with AI")} />
    </div>
  );
}
