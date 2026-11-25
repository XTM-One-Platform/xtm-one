/**
 * Form action buttons follow the form standard (#9375, Design standards s.2)
 * - `yarn test`.
 *
 * The shared EntityConfigCard edit row, the shared confirm dialog (useConfirm)
 * and the Settings > Organization section headers (Organization profile,
 * Departments, General Assistant, Tracked organizations) and the Channels &
 * Bots card headers (agent, assignment) render their Edit, Configure, Cancel, Save
 * and confirm buttons at the default 36 px height (no size="sm", 24 px), with
 * Cancel (secondary) first and the primary action last at the right end.
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// vitest runs from frontend/ (CI and `yarn test`).
/**
 * The library Spinner's own `size="sm"` (16 px glyph) is not a button size:
 * its tags are dropped from the source these gates read, so `size="sm"` below
 * only ever means a 24 px button (spinner sweep of #10533).
 */
const withoutSpinners = (source: string) => source.replace(/<Spinner\b[^>]*\/>/g, "");
const read = (...parts: string[]) => withoutSpinners(readFileSync(join(process.cwd(), "src", ...parts), "utf8"));

/** The source slice from `start` to `end` (both markers must exist). */
const slice = (source: string, start: string, end: string) => {
  const from = source.indexOf(start);
  expect(from, `marker not found: ${start}`).toBeGreaterThanOrEqual(0);
  const to = source.indexOf(end, from + start.length);
  expect(to, `marker not found: ${end}`).toBeGreaterThan(from);
  return source.slice(from, to);
};

describe("form action buttons at 36 px, Cancel then primary", () => {
  it("EntityConfigCard: Edit at default size, Cancel then Save changes at the right end", () => {
    const src = read("components", "ui", "entity-config-card.tsx");
    const edit = slice(src, "{showEditButton && (", "{showReadOnlyBadge");
    expect(edit).not.toMatch(/size="sm"|h-7/);
    const row = slice(src, '<div className="flex justify-end gap-2 pt-1" data-testid="entity-config-card.actions"', "</div>");
    expect(row).not.toContain('size="sm"');
    expect(row.indexOf('t_i18n("Cancel")')).toBeLessThan(row.indexOf('t_i18n("Save changes")'));
    expect(src).not.toMatch(/text-\[1[01]px\]/);
  });

  it("confirm dialog (useConfirm): Cancel and confirm at default size", () => {
    const src = read("components", "ui", "confirm-dialog.tsx");
    const row = slice(src, '<div className="flex justify-end gap-2 pt-2">', "</DialogContent>");
    expect(row).not.toContain('size="sm"');
    expect(row.indexOf("cancelLabel")).toBeLessThan(row.indexOf("confirmLabel"));
  });

  it.each([
    ["Organization profile", ["pages", "settings", "OrganizationSection.tsx"], "{!editing && (", "</Tip>"],
    ["Departments", ["pages", "settings", "DepartmentsSection.tsx"], '<div className="ml-4 flex shrink-0 gap-2">', "</div>"],
    ["General Assistant", ["pages", "settings", "GeneralAssistantSection.tsx"], "{!gaEditing ? (", ")}\n"],
  ])("%s section header buttons at default size", (_name, path, start, end) => {
    const header = slice(read(...path), start, end);
    const buttons = header.match(/<Button\b[^>]*>/g) ?? [];
    expect(buttons.length).toBeGreaterThan(0);
    for (const button of buttons) expect(button).not.toContain('size="sm"');
  });

  it("Organization profile Draft button matches the 36 px Edit beside it (UX r1 B1 on #10088, one Draft button since #1991)", () => {
    const src = read("pages", "settings", "OrganizationSection.tsx");
    expect(src).not.toContain("<OverflowMenu");
    const draft = slice(src, "<Button", 'data-testid="settings.organization.draft-open"');
    const button = draft.slice(draft.lastIndexOf("<Button"));
    expect(button).not.toMatch(/size="sm"|h-7/);
  });

  it("General Assistant edit labels at body size, help lines at 12 px (UX r1 B2 on #10088)", () => {
    const src = read("pages", "settings", "GeneralAssistantSection.tsx");
    const form = slice(src, "{/* Link to Agent selector */}", "{/* Integration bindings */}");
    expect(form).not.toMatch(/<label\b/);
    // Help and status lines; the linked-agent Badge keeps its caption size.
    expect(form).not.toMatch(/<p className="content-caption/);
    for (const name of ["Link to Agent", "Chat Model", "Max Iterations", "System Prompt"]) {
      expect(form).toMatch(new RegExp(`<Label[^>]*>[\\s\\S]{0,80}t_i18n\\("${name}"\\)`));
    }
  });

  it("Departments description at 12 px and add buttons at default size (UX r1 B3 on #10088)", () => {
    const src = read("pages", "settings", "DepartmentsSection.tsx");
    expect(src).not.toMatch(/text-\[1[01]px\]/);
    for (const label of [
      "Add a department",
      "Add a group rule",
      "Create from the org-unit groups",
      // UX r2 B1 on #10088
      "Import",
      "Apply the mapping to history",
    ]) {
      const at = src.indexOf(`t_i18n("${label}")`);
      expect(at, label).toBeGreaterThan(0);
      const open = src.lastIndexOf("<Button", at);
      expect(src.slice(open, at), label).not.toContain('size="sm"');
    }
  });

  it("General Assistant edit labels tied to their control and help line (UX r2 B2 on #10088)", () => {
    const src = read("pages", "settings", "GeneralAssistantSection.tsx");
    for (const id of ["ga-linked-agent", "ga-chat-model", "ga-max-iterations"]) {
      expect(src, id).toContain(`htmlFor="${id}"`);
      expect(src, id).toContain(`id="${id}"`);
      expect(src, id).toContain(`aria-describedby="${id}-help"`);
      expect(src, id).toContain(`<p id="${id}-help" className="content-compact`);
    }
  });

  it("General Assistant skills and handover help lines at 12 px (UX r2 B2 on #10088)", () => {
    for (const file of ["GeneralAssistantSkills.tsx", "GAHandoverPicker.tsx"]) {
      expect(read("pages", "settings", file), file).not.toMatch(/<p className="content-caption text-default-secondary">\s*\{t_i18n\(\s*"(Instructions the General|Choose the agents)/);
    }
  });

  it("Assignment Channels & Bots card header uses the shared CardEditActions (UX r1 B2 of #10976)", () => {
    const header = slice(read("pages", "assignment-detail", "ChannelsTab.tsx"), 't_i18n("Channels & Bots")', "</CardHeader>");
    expect(header).toContain("<CardEditActions");
    expect(header).not.toMatch(/<Button\b|size="sm"|h-7/);
  });

  it.each([
    ["Agent Channels & Bots", ["pages", "agent-detail", "config", "ChannelsSection.tsx"], "{!editingChannels ? (", "</CardHeader>"],
  ])("%s card header: Edit, Cancel and Save at default size, Cancel first", (_name, path, start, end) => {
    const header = slice(read(...path), start, end);
    expect(header).not.toMatch(/size="sm"|h-7|content-compact/);
    expect(header.indexOf('t_i18n("Cancel")')).toBeGreaterThan(0);
    expect(header.indexOf('t_i18n("Save")')).toBeGreaterThan(header.indexOf('t_i18n("Cancel")'));
  });

  it.each([
    ["Organization profile", ["pages", "settings", "OrganizationSection.tsx"]],
    ["Tracked organizations", ["pages", "settings", "TrackedOrganizations.tsx"]],
  ])("%s buttons at default size (no size=\"sm\")", (_name, path) => {
    expect(read(...path)).not.toContain('size="sm"');
  });

  it("CardEditActions: secondary Edit and Cancel, Cancel first, Save last, all at default size", () => {
    const src = read("components", "ui", "card-edit-actions.tsx");
    expect(src).not.toMatch(/size="sm"|h-7|content-compact|variant="ghost"/);
    const edit = slice(src, "if (!editing) {", "const save =");
    expect(edit).toContain('variant="secondary"');
    const row = slice(src, 'data-testid="card-edit-actions"', "</div>");
    expect(row).toContain('variant="secondary" onClick={onCancel}');
    expect(row.indexOf("onCancel")).toBeLessThan(row.indexOf("save"));
  });

  // UX r1 drift of #10120: every card header with a read / edit switch uses the
  // shared CardEditActions, never a hand-made 28 px ghost Edit / Save / Cancel.
  it.each([
    ["pages", "agent-detail", "config", "GeneralSection.tsx"],
    ["pages", "agent-detail", "config", "ToolingSection.tsx"],
    ["pages", "agent-detail", "config", "KnowledgeSection.tsx"],
    ["pages", "agent-detail", "config", "PromptsCard.tsx"],
    ["pages", "agent-detail", "config", "SkillsCard.tsx"],
    ["pages", "agent-detail", "config", "RunnerSelectorCard.tsx"],
    ["pages", "agent-detail", "config", "BrowserProfileSelectorCard.tsx"],
    ["pages", "assignment-detail", "BuiltinScopeCard.tsx"],
    ["pages", "settings", "GeneralAssistantSection.tsx"],
  ])("%s/%s card headers use CardEditActions", (...path) => {
    const src = read(...path);
    expect(src).toContain("<CardEditActions");
    // No hand-made header Edit / Save / Cancel left at 28 px.
    for (const label of ["Edit", "Save", "Cancel"]) {
      let at = src.indexOf(`t_i18n("${label}")`);
      while (at >= 0) {
        const open = src.lastIndexOf("<Button", at);
        const tag = src.slice(open, src.indexOf(">", open) + 1);
        expect(tag, `${path.join("/")} ${label}`).not.toMatch(/h-7/);
        at = src.indexOf(`t_i18n("${label}")`, at + 1);
      }
    }
  });

  it("Group information Edit, Cancel and Save changes at default size", () => {
    const src = read("pages", "GroupDetail.tsx");
    const card = slice(src, 't_i18n("Group Information")', 't_i18n("Save changes")');
    for (const button of card.match(/<Button\b[^>]*>/g) ?? []) {
      expect(button).not.toMatch(/size="sm"|h-7|variant="ghost"/);
    }
  });

  it("Profile: Edit Profile, password Change and the password form actions at default size, Cancel first", () => {
    const src = read("pages", "Profile.tsx");
    const header = slice(src, 'tab === "overview" && (', 't_i18n("Save")');
    for (const button of header.match(/<Button\b[^>]*>/g) ?? []) expect(button).not.toMatch(/size="sm"|h-7|variant="ghost"/);
    expect(header.indexOf('t_i18n("Cancel")')).toBeGreaterThan(0);
    const change = slice(src, 'data-testid="profile.password-change"', 't_i18n("Change")');
    expect(change).not.toMatch(/size="sm"|h-7/);
    const card = slice(src, 'htmlFor="profile-current-password"', 't_i18n("Update Password")');
    expect(card).not.toMatch(/content-caption|h-8/);
    const row = slice(src, 'data-testid="profile.password-actions"', 't_i18n("Update Password")');
    expect(row).not.toMatch(/size="sm"|h-7/);
    expect(row).toContain('t_i18n("Cancel")');
  });

  it("New evaluation inline dataset and entry forms: Cancel then Create / Add Entry at default size", () => {
    const src = read("pages", "NewEvaluation.tsx");
    for (const end of ['onClick={handleCreateDataset}', 'onClick={handleAddEntry}']) {
      const at = src.indexOf(end);
      expect(at).toBeGreaterThan(0);
      const row = src.slice(src.lastIndexOf('<div className="flex justify-end gap-2">', at), at);
      expect(row).toContain('t_i18n("Cancel")');
      expect(row).not.toMatch(/size="sm"|h-7|variant="ghost"/);
    }
  });

  it("New evaluation inline forms: labels tied at body size, textareas at body size (UX r1 B1-B2 of #10145)", () => {
    const src = read("pages", "NewEvaluation.tsx");
    const form = slice(src, 't_i18n("Create New Dataset")', 'onClick={handleCreateDataset}');
    const entry = slice(src, 't_i18n("New Entry")', 'onClick={handleAddEntry}');
    for (const block of [form, entry]) {
      expect(block).not.toMatch(/<label\b/);
      expect(block).not.toMatch(/text-\[11px\]/);
      for (const textarea of block.match(/<textarea\b[\s\S]*?\/>/g) ?? []) {
        expect(textarea).toMatch(/\sid="/);
        expect(textarea).toContain("content-base");
      }
      for (const label of block.match(/<Label\b[^>]*>/g) ?? []) expect(label).toMatch(/htmlFor="/);
    }
  });

  it("Profile overview edit: every field label tied at body size, controls at default height (UX r1 B3-B4 of #10145)", () => {
    const src = read("pages", "Profile.tsx");
    const edit = slice(src, '{editing ? (\n                  <div className="px-5 py-4 space-y-3">', '<InfoRow icon={Fingerprint}');
    expect(edit).not.toMatch(/content-caption|h-7|h-8|size="sm"/);
    for (const id of ["profile-full-name", "profile-title", "profile-email", "profile-timezone", "profile-theme", "profile-language"]) {
      expect(edit).toContain(`id="${id}"`);
      expect(edit).toContain(`aria-labelledby="${id}-label"`);
    }
  });

  it("Group detail members: the role filter is a pill of the shared DataTable toolbar (UX r1 B5 of #10145, #11940)", () => {
    const src = read("pages", "GroupDetail.tsx");
    const members = slice(src, "function MembersSection(", "function ManagersSection(");
    expect(members).toContain("<DataTable<GroupMemberItem>");
    expect(members).toMatch(/key: "role",\s+label: "Role"/);
    expect(members).not.toContain("<SelectTrigger");
  });

  it("Profile account card: one label style for every row (UX r2 B1 of #10145)", () => {
    const profile = read("pages", "Profile.tsx");
    const infoRow = slice(profile, "function InfoRow(", "function ApiKeysSection(");
    expect(infoRow).not.toMatch(/uppercase|content-compact|content-caption/);
    const pw = profile.indexOf('data-testid="profile.password-label"');
    expect(pw).toBeGreaterThan(0);
    const pwTag = profile.slice(profile.lastIndexOf("<span", pw), profile.indexOf(">", pw) + 1);
    expect(pwTag).toContain("content-base");
    expect(pwTag).not.toMatch(/uppercase/);
    const twoFactor = read("pages", "profile", "TwoFactorRow.tsx");
    const at = twoFactor.indexOf('data-testid="profile.two-factor-label"');
    expect(at).toBeGreaterThan(0);
    const tag = twoFactor.slice(twoFactor.lastIndexOf("<span", at), twoFactor.indexOf(">", at) + 1);
    expect(tag).toContain("content-base");
    expect(tag).not.toMatch(/uppercase/);
    const actions = slice(twoFactor, 'data-testid="profile.two-factor-actions"', "</div>");
    for (const button of actions.match(/<Button\b[^>]*>/g) ?? []) expect(button).not.toMatch(/size="sm"|h-7/);
  });

  it("Group detail Add Member is the design-system picker above the members table, out of its toolbar, no hand overlay (#9375 iconOverlay, #11940, UX r1 B2 on #11941)", () => {
    const src = read("pages", "GroupDetail.tsx");
    const at = src.indexOf('data-testid="group-detail.add-member"');
    expect(at).toBeGreaterThan(0);
    const picker = src.slice(at, src.indexOf("</div>", at));
    expect(picker).toContain("<SearchableSelect");
    expect(picker).not.toMatch(/<SearchInput\b|shadow-overlay|absolute/);
    const members = slice(src, "function MembersSection(", "function ManagersSection(");
    expect(members).not.toContain("toolbar={addMember}");
    expect(members.indexOf("{addMember}")).toBeLessThan(members.indexOf("<DataTable<GroupMemberItem>"));
  });

  it.each([
    ["Users", "Users.tsx", /totalAll=\{countedAll\?\.count\}/, /useUsersCount\(\{\}\)/],
    ["Groups", "Groups.tsx", /totalAll=\{countedAll\?\.count\}/, /useGroupsCount\(\{\}\)/],
    ["Group members", "GroupDetail.tsx", /totalAll=\{allMembersData\?\.total\}/, /useGroupMembers\(groupId, \{ limit: BACKEND_FETCH_LIMIT, offset: 0 \}\)/],
  ])("%s: a filtered total reads 'n of N' from the unfiltered count (UX r1 B1 on #11941)", (_name, file, prop, unfiltered) => {
    const src = read("pages", file);
    expect(src).toMatch(prop);
    expect(src).toMatch(unfiltered);
  });

  // The server-mode lists of the #11939 retest sweep: each passes its unfiltered count (#12039).
  it.each([
    ["Audit logs", ["components", "audit-logs", "AuditLogList.tsx"], /totalAll=\{everything\?\.total\}/, /useAuditLogs\(\{ user_id: userId, limit: 1, offset: 0 \}\)/],
    ["KB documents", ["pages", "KnowledgeDocuments.tsx"], /totalAll=\{everything\?\.total\}/, /useGlobalKnowledgeDocuments\(\{ limit: 1, offset: 0 \}\)/],
    ["Traces", ["pages", "Traces.tsx"], /totalAll=\{everyTraceQuery\.data\?\.total\}/, /useTraces\(\{ limit: 1 \}\)/],
    ["Trace sessions", ["pages", "TraceSessions.tsx"], /totalAll=\{everySessionQuery\.data\?\.total\}/, /useTraceSessions\(\{ limit: 1 \}\)/],
    ["Agent ratings", ["pages", "agent-detail", "FeedbackTab.tsx"], /totalAll=\{summary\?\.total\}/, /useAgentAnnotationSummary\(agent\.id/],
    ["Agent memory", ["pages", "agent-detail", "MemoryTab.tsx"], /totalAll=\{totalCount\}/, /useAgentMemories\(agent\.id, \{ limit: 1 \}/],
    ["ROI records", ["components", "roi", "RoiRecordsList.tsx"], /totalAll=\{countAll\.data\?\.total\}/, /useRoiRecordCount\(\{ \.\.\.query, review: "all" \}\)/],
    ["Improvements inbox", ["components", "health", "ImprovementsInbox.tsx"], /totalAll=\{everything\}/, /useImprovements\(\{ \.\.\.fixed, limit: 1, offset: 0 \}\)/],
  ])("%s: a filtered server total reads 'n of N' from the unfiltered count (#12039)", (_name, parts, prop, unfiltered) => {
    const src = read(...parts);
    expect(src).toMatch(prop);
    expect(src).toMatch(unfiltered);
  });

  it.each([
    ["UserDetail Reset two-factor", ["pages", "UserDetail.tsx"], 'data-testid="user-detail.reset-mfa"'],
    ["NewEvaluation New Dataset", ["pages", "NewEvaluation.tsx"], 'data-testid="new-evaluation.new-dataset"'],
    ["NewEvaluation Add Entry", ["pages", "NewEvaluation.tsx"], 'data-testid="new-evaluation.add-entry-open"'],
    ["NewEvaluation Attach File", ["pages", "NewEvaluation.tsx"], 'data-testid="new-evaluation.attach-file"'],
    ["Profile timezone Detect", ["pages", "Profile.tsx"], 'data-testid="profile.timezone-detect"'],
    ["Profile two-factor Set up", ["pages", "profile", "TwoFactorRow.tsx"], 'data-testid="profile.two-factor-setup"'],
    // A3 of #10145 r3: the other Profile sections.
    ["Profile memory Purge All", ["pages", "Profile.tsx"], 'data-testid="profile.memory-purge-all"'],
    ["Profile cross-agent profile Clear", ["pages", "Profile.tsx"], 'data-testid="profile.memory-clear-profile"'],
    ["Profile agent memory Clear", ["pages", "Profile.tsx"], 'data-testid="profile.memory-clear-agent"'],
    ["Profile Reset to defaults", ["pages", "Profile.tsx"], 'data-testid="profile.reset-to-defaults"'],
    ["Profile MCP copy URL", ["pages", "Profile.tsx"], 'data-testid="profile.mcp-copy-url"'],
    ["Profile platform MCP copy URL", ["pages", "Profile.tsx"], 'data-testid="profile.xtm-mcp-copy-url"'],
    ["Profile platform MCP copy config", ["pages", "Profile.tsx"], 'data-testid="profile.xtm-mcp-copy-config"'],
    ["Profile Cursor snippet Copy", ["pages", "Profile.tsx"], 'data-testid="profile.snippet-copy-cursor"'],
    ["Profile Claude Desktop snippet Copy", ["pages", "Profile.tsx"], 'data-testid="profile.snippet-copy-claude"'],
    // UX r1 B3 of #10191: Platform MCP servers (OpenCTI, OpenAEV) row.
    ["Platform MCP server copy URL", ["components", "ui", "platform-mcp-server-list.tsx"], 'data-testid="platform-mcp-copy-url"'],
    ["Platform MCP server Config", ["components", "ui", "platform-mcp-server-list.tsx"], 'data-testid="platform-mcp-copy-config"'],
  ])("%s at default size", (_name, path, marker) => {
    const src = read(...path);
    const at = src.indexOf(marker);
    expect(at).toBeGreaterThan(0);
    const tag = src.slice(src.lastIndexOf("<Button", at), src.indexOf(">", at) + 1);
    expect(tag).not.toMatch(/size="sm"|h-[67]\b|text-\[1[01]px\]|content-caption/);
  });

  it("New evaluation: footer Cancel secondary then Create, entry help texts at 12 px (A1-A2 of #10145 r3)", () => {
    const src = read("pages", "NewEvaluation.tsx");
    const footer = slice(src, 'data-testid="new-evaluation.footer-actions"', "onClick={handleSubmit}");
    expect(footer).toContain('t_i18n("Cancel")');
    expect(footer).toContain('variant="secondary"');
    expect(footer).not.toMatch(/variant="ghost"|size="sm"/);
    expect(src).not.toMatch(/text-\[1[01]px\]/);
  });

  it("New evaluation Settings and Scoring cards: labels at body size, sentence case, no 10/11 px help (UX r1 B1 of #10191)", () => {
    for (const file of ["evaluation-settings-card.tsx", "evaluation-scoring-card.tsx"]) {
      const src = read("components", "evaluation", file);
      expect(src).not.toMatch(/uppercase tracking-wider/);
      expect(src).not.toMatch(/text-\[1[01]px\]/);
      expect(src).toMatch(/<Label htmlFor=/);
    }
    const settings = read("components", "evaluation", "evaluation-settings-card.tsx");
    for (const id of ["ids.name", "ids.description", "ids.executionMode", "ids.runMode", "ids.timeout", "ids.judgeMaxTokens", "ids.schedule"]) {
      expect(settings).toContain(`id={${id}}`);
    }
  });

  it("New evaluation sub-score weights: default 36 px Input tied to its label and help (UX r1 B2 of #10191)", () => {
    const src = read("components", "evaluation", "evaluation-scoring-card.tsx");
    const row = slice(src, "EVALUATION_SUB_SCORES.map((s) => (", "</div>\n                    ))}");
    expect(row).toContain("<Input");
    expect(row).toContain("aria-describedby={weightsHelpId}");
    expect(row).toMatch(/<Label id=\{`\$\{fieldId\}-weight-\$\{s\.key\}-label`\} htmlFor=\{`\$\{fieldId\}-weight-/);
    expect(row).toContain("aria-labelledby={`${fieldId}-weight-${s.key}-label`}");
    expect(row).not.toMatch(/h-7|<input/);
  });

  it("Profile MCP: platform server descriptions and the Platform Tools MCP card at 12 px, 36 px buttons (UX r2 B1-B2 of #10191)", () => {
    const list = read("components", "ui", "platform-mcp-server-list.tsx");
    expect(list).not.toMatch(/text-\[1[01]px\]/);
    const download = read("components", "ui", "xtm-mcp-package-download.tsx");
    // size="sm" is refused on the buttons; the library Spinner's own sm size is not a button size.
    expect(download).not.toMatch(/text-\[1[01]px\]|content-caption|(?<!<Spinner )size="sm"/);
    expect(download).toContain('size="md"');
    const segmented = read("components", "ui", "segmented-filter.tsx");
    expect(segmented).toContain('size === "md" ? "px-3 h-8 content-compact"');
  });

  it("section headers in edit mode put Cancel before Save", () => {
    for (const path of [
      ["pages", "settings", "DepartmentsSection.tsx"],
      ["pages", "settings", "GeneralAssistantSection.tsx"],
    ]) {
      const src = read(...path);
      const cancel = src.indexOf('t_i18n("Cancel")');
      const save = src.indexOf('t_i18n("Save")', cancel);
      expect(cancel).toBeGreaterThan(0);
      expect(save).toBeGreaterThan(cancel);
    }
  });

  it.each([
    ["ApplyPresetDialog.tsx"],
    ["FleetBulkDialog.tsx"],
    ["ForkFleetDialog.tsx"],
    ["NewFleetDialog.tsx"],
    ["OrganizationScopeDialog.tsx"],
  ])("fleet dialog %s: footer Cancel secondary at default size (A2 of #10406 r2)", (file) => {
    const src = read("components", "fleets", file);
    const at = src.indexOf('{t_i18n("Cancel")}');
    expect(at).toBeGreaterThan(0);
    const tag = src.slice(src.lastIndexOf("<Button", at), at);
    expect(tag).toContain('variant="secondary"');
    expect(tag).not.toContain('size="sm"');
  });

  it("deck Edit panel: close at the icon size, Add slide at 36 px (A2/A4 of #10488 r1)", () => {
    const src = read("components", "presentation", "PresentationEditorPanel.tsx");
    const close = src.match(/<Button\b[^>]*onClick=\{onClose\}[^>]*>/)?.[0] ?? "";
    expect(close).toContain('size="icon"');
    // A flex child beside the description paragraph: without shrink-0 the
    // 36x36 icon button is squeezed to 23 px wide at 1024 (#10505 UX r1 B1).
    expect(close).toMatch(/className="[^"]*\bshrink-0\b/);
    const add = slice(src, "items={addSlideItems}", "header=");
    expect(add).toContain("h-9");
    expect(add).not.toContain("h-7");
  });

  it("deck inspector row buttons at the 32 px toolbar floor (A3 of #10488 r1)", () => {
    const src = read("components", "presentation", "SchemaFieldControls.tsx");
    const rows = slice(src, "function RowButtons(", "\n}\n");
    expect(rows).not.toMatch(/h-7|w-6/);
    expect(rows.match(/h-8 w-8/g)?.length).toBe(3);
  });

  it("new agent wizard: no text under 12 px, Discard draft at 36 px, every field label on the shared Label (UX note on #10245 r2, #10571 r1 B1)", () => {
    const src = read("pages", "NewAgentWizard.tsx");
    expect(src).not.toMatch(/text-\[(9|10|11)px\]|content-caption/);
    const discard = src.match(/<Button\b[^>]*onClick=\{discardDraft\}[^>]*>/)?.[0] ?? "";
    expect(discard).not.toMatch(/size="sm"|h-6|h-7/);
    expect(discard).toContain('variant="secondary"');
    expect(src).not.toMatch(/<label\b/);
    expect(src).toMatch(/<Label>\{t_i18n\("Prompt library"\)\}/);
    expect(src).toMatch(/<Label>\{t_i18n\("Persona \/ System Prompt"\)\}/);
  });
});
