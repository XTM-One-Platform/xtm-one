# Frontend – Developer Guide

Read before changing `frontend/`. Detail lives in `dev-docs/reference/`.

## Tech Stack

- **React 19** with functional components only
- **TypeScript** with strict mode
- **Vite 8** for development and builds
- **`@filigran/design-system`**, the UI's single source of truth: read `node_modules/@filigran/design-system/skills/consumer/design-system-usage/SKILL.md` before any UI change (exact version; on a bump re-run `generate-safelist.mjs`)
- **Tailwind CSS 4**, configured in `src/index.css` only (no `tailwind.config.ts`); its default palette is disabled, so `text-red-400` / `bg-white` do not resolve
- **Radix UI** reached through the design system; a raw Radix import in product code is a smell
- **TanStack Query 5** for server state management
- **React Router DOM 7** for routing
- **`node --test`** for unit tests, **vitest + happy-dom + Testing Library** for component tests (see "Unit and component tests" below)
- **`@filigran/chatbot`**, the chat panel (`dev-docs/chat-panel.md`). Its root, the
  panel bundle, loads only in `XtmChatPanel.tsx` and `ChatPanelPage.tsx`, lazily,
  unprefetched (or `LazyXtmChatPanel`); else `import type`, `/markdown`, `/workspaces`.

## Where to look

| Need | Read |
|---|---|
| File-by-file map of `src/` | `dev-docs/reference/frontend-directory-map.md` |
| Shared helpers and components (one rule, one module) | `dev-docs/reference/frontend-shared-utilities.md` |
| Add a page, the navigation model, page → API hooks | `dev-docs/reference/frontend-pages.md` |
| Full-stack feature (API → hook → page) | `dev-docs/reference/full-stack-feature.md` |
| Global search (Ctrl K), `>` commands, `@` sections | `dev-docs/reference/frontend-command-palette.md` |
| Design tokens, light / dark theming, editions | `dev-docs/reference/frontend-design-system.md` |
| Records tables | `dev-docs/reference/data-tables.md` |
| Common pitfalls (every ALWAYS / NEVER rule; shared components, hooks, chat panel host) | `dev-docs/reference/frontend-common-pitfalls.md` |

## Rules that apply everywhere

- **Navigation is data**: a page is added in `src/components/layout/nav-model.ts` (two levels maximum), never by hand in the sidebar or the search field, never an entry derived from user data (an integration's own pages are tabs of its detail page); a group's hub page lists its resources itself (`ResourceHub`).
- **The shell is OpenCTI's and OpenAEV's**: product switcher on top, "Made by Filigran" at the foot, About and XTM One Desktop in the profile menu; a rail flyout's open state is transient and `onOpenChange(next)` is applied as `next`, never toggled.
- **Commands live in `<Screen>.commands.ts` manifests** (static data); `when` hides, `requires` badges; `yarn validate:commands` checks them.
- **Design-system tokens only**: `bg-elevation-*` / `layer-N`, `text-default-*`, `text-accent`; never `white/N` / `black/N`, a literal colour, a reimplemented library component or an edited library token.
- **Pickers over runtime data are `SearchableSelect`** (`ModelSelect` for models); the plain `Select` is for a short list fixed at build time.
- **A library the server pages** uses `useLibraryParams()` + `useClampPage()` with `Pagination` above and below; the server filters and searches (`services/library_filters.py`).
- **One licence gate**: every AI affordance gates on `useAgenticAIEnabled()`; Community Edition copy lives once in `components/ui/enterprise-upsell.tsx`.
- **i18n**: every `t_i18n` key in all four `src/i18n/lang/*.json` catalogs (`yarn validate:i18n`).
- Feature flags: `isFeatureEnabled()` + `KNOWN_FEATURE_FLAGS`; agent-authored HTML: `sanitizeRichText()` before `dangerouslySetInnerHTML`.
- Tailwind classes, never inline styles; `cn()` for conditional classes.
- **No dead code**: `yarn knip` fails on an unused file, export or dependency (`frontend-common-pitfalls.md`, "Dead code").
- **Read the pitfalls first** when touching shared components, hooks, or the chat panel host.

## How to Add an API Hook

In the appropriate file under `src/hooks/api/` (e.g. `useAgentApi.ts`, `useAssignmentApi.ts`, etc.):

```tsx
// Query (GET):
export function useMyData() {
  return useQuery<MyType[]>({
    queryKey: ["my-data"],
    queryFn: () => api.get("/my-endpoint"),
  });
}

// Mutation (POST/PUT/DELETE):
export function useCreateMyData() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: CreateMyDataInput) => api.post("/my-endpoint", data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["my-data"] }),
  });
}
```

## Unit and component tests

`yarn test:unit` (in `yarn check` and CI's unit shards) runs two runners, each on its own files:

- `src/**/*.test.ts` run on `node --test`, no DOM, no bundler: pure modules (`src/lib/*.ts`, the logic split out of a page) imported by relative path with their `.ts` extension. Most tests belong here.
- `src/**/*.test.tsx` run on vitest (`vitest.config.ts`, `yarn test:dom` alone) in happy-dom with `@testing-library/react`: a component or a hook as a user meets it - what it shows, sends and refuses. `@/` imports resolve as in the app. Render through `src/testing/providers.tsx` (`renderWithProviders`, `withProviders` for `renderHook`: a QueryClient that never retries, the English catalog, the toaster) and mock `@/lib/api` with `vi.mock`, never the network. Assert on text, roles and alt text, never on snapshots or class names (gate: `src/no-class-name-assertions.test.ts`).

A `.test.tsx` file is never picked up by `node --test`, and `vitest.config.ts` includes nothing else, so neither runner sees the other's files.

A design-system `console.warn` fails the vitest test that rendered it: fix the component, never the test (every `Input` named, every `DialogContent` with its own `DialogTitle`; `frontend-design-system.md`).

End-to-end: `make e2e` (repo root) runs the Playwright `core` suite against the running dev stack (`make start` first); one spec with `ARGS="tests_e2e/x.spec.ts"`.
