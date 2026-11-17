# Global search field and commands (Ctrl K)

The top bar search field, its prefixes, and the `*.commands.ts` manifest contract.

There is no dialog: the field is inline in the top bar
(`components/layout/GlobalSearch.tsx`, design-system `SearchInput`), in the
OpenAEV and OpenCTI style (#11201). **Ctrl K** (Cmd K) and **`/`** focus it from
anywhere (`/` is ignored while typing in another field); Escape closes the list
under it. What the former command palette did stays in the field, chosen by a
prefix:

| Prefix | Mode | Source |
|---|---|---|
| *(none)* | Search the platform | `GET /search` (every entity type, ranked, RBAC server side) and `nav-model`, the same tree the sidebar renders |
| `>` | Run a command | every screen's `*.commands.ts` manifest |
| `@` | Jump inside this page | sections the mounted screen registered |

Without a prefix, typing (debounced, 2 characters minimum) lists the top hits
grouped by entity type, the matching pages, then **See all results**. Enter with
no row picked, or See all results, opens the results page `/search?q=&type=`
(`pages/Search.tsx`): a rail of entity types with match counts and the hits in
the shared `DataTable`, server paged and sorted. Query and type live in the URL,
so refresh, a shared link and back all work. `lib/search-types.ts` maps each
entity type to its label, icon and route: a new searchable type is added there
and in `backend/app/services/global_search.py`, never in the field.

### Adding commands to a screen

Create `<Screen>.commands.ts` next to the page. `import.meta.glob` picks it up at
build time: there is no registry to edit and no generated file to regenerate.

```ts
import { Plus } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "prompts.create",          // dot-namespaced, globally unique
    category: "Prompts",           // i18n key, rendered as "Prompts: Create a prompt"
    title: "Create a prompt",      // i18n key
    keywords: ["new"],             // extra search terms (also matched in English)
    icon: Plus,
    when: ["isAdmin"],             // context keys, all must be true, else hidden
    requires: "agenticAI",         // licence gate, shown with an EE badge
    navigate: "/prompts/new",
  },
]);
```

**`when` hides, `requires` badges.** They are not interchangeable. `when` is
availability (wrong role, wrong platform mode, feature switched off) and the
command simply is not there. `requires` is a licence gate: the feature exists and
the user has not bought it, so the `>` list shows it with an EE badge and answers
with the upsell dialog, exactly as the buttons on the list pages do. Gating the
add-on with `when` makes the search field the one surface where a customer cannot
discover what the add-on unlocks; the validator rejects it.

The split that matters:

- **Manifests are static data.** No imports of the screen, no closures, otherwise
  listing a command would drag that page's chunk into the entry bundle, and the
  validator (which parses files) could not read them. This is also why the
  catalog can offer a screen's commands while that screen is unmounted.
- **Behaviour is registered at runtime** by the mounted screen:
  `useCommandHandler("agents.import", fn)` for actions, `useCommandSections([...])`
  for `@` targets. Most screens need no work there: `DetailTabs` publishes its
  own tabs, so every tabbed page answers `@` for free, and registration is
  additive so a nested section bar contributes too. Register manually only for
  jump targets that are not tabs. A command with both `navigate` and `handler`
  opens the route first and fires the handler once the screen registers it (5 s
  window): that is how "Agents: Import an agent from a file" works from anywhere.
- **A parent with `children` is not listed by `>`**: the field lists leaf
  commands only. Its sub-entries stay reachable as the destination's own tabs
  (`@` once there) or as pages of the navigation (see `Settings.commands.ts`,
  whose children mirror the tab bar).

Run `yarn validate:commands` (also part of `yarn check`). It enforces unique ids,
the naming shape, known `when` keys, i18n keys that exist in `en.json`, and that
every command actually does something. Add new context keys in **three** places:
`CONTEXT_KEYS` in `lib/commands/types.ts`, the `context` memo in
`CommandsProvider`, and the copy in `scripts/validate-commands.mjs`.
