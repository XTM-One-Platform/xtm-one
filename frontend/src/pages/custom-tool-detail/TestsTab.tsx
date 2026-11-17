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

import { useState } from "react";
import { Icon, Spinner } from "@filigran/design-system";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { useRunCustomToolTests, useUpdateCustomTool } from "@/hooks/api/useCustomToolApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { formatTimeAgo } from "@/lib/relative-time";
import type { CustomTool, CustomToolTestReport, CustomToolTestResult, CustomToolTestStatus } from "@/types";
import { TEST_CASES_EXAMPLE, testCodeExample, parseTestCases } from "./test-cases";

// ---------------------------------------------------------------------------
// Status display
// ---------------------------------------------------------------------------

type ResultStatus = CustomToolTestResult["status"];

/** The badge of a run's overall status. */
export function TestStatusBadge({ status }: { status: CustomToolTestStatus | null | undefined }) {
  const { t_i18n } = useFormatter();
  if (!status) return <Badge variant="outline" className="content-caption">{t_i18n("Not run yet")}</Badge>;
  if (status === "passed") return <Badge variant="success" className="content-caption">{t_i18n("Tests passed")}</Badge>;
  if (status === "no_tests") return <Badge variant="outline" className="content-caption">{t_i18n("No tests")}</Badge>;
  return (
    <Badge variant="destructive" className="content-caption">
      {status === "failed" ? t_i18n("Tests failed") : t_i18n("Tests errored")}
    </Badge>
  );
}

function ResultIcon({ status }: { status: ResultStatus }) {
  if (status === "passed") return <Icon name="circle-check" size={16} className="text-feedback-success-primary shrink-0" />;
  if (status === "skipped") return <Icon name="skip-forward" size={16} className="text-default-secondary shrink-0" />;
  return <Icon name="circle-x" size={16} className="text-feedback-error-primary shrink-0" />;
}

function ResultRow({ result }: { result: CustomToolTestResult }) {
  const { t_i18n } = useFormatter();
  const [open, setOpen] = useState(result.status === "failed" || result.status === "error");
  const detail = [result.message, result.output && result.output !== result.message ? result.output : null]
    .filter(Boolean)
    .join("\n\n");
  return (
    <li className="rounded-sm border border-elevation-subtle-soft">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-elevation-hover"
        onClick={() => setOpen(!open)}
        aria-expanded={detail ? open : undefined}
        disabled={!detail}
      >
        <ResultIcon status={result.status} />
        <span className="content-compact font-medium truncate">{result.name}</span>
        <Badge variant="outline" className="content-caption">
          {result.kind === "case" ? t_i18n("Case") : t_i18n("Test code")}
        </Badge>
        {result.dry_run && <Badge variant="cyan" className="content-caption">{t_i18n("Dry run")}</Badge>}
        {result.duration_ms !== undefined && (
          <span className="ml-auto content-caption text-default-secondary tabular-nums">{result.duration_ms} ms</span>
        )}
        {detail && <Icon name={open ? "chevron-down" : "chevron-right"} size={16} className="text-default-secondary" />}
      </button>
      {detail && open && (
        <pre className="border-t border-elevation-subtle-soft px-3 py-2 content-compact font-mono-plex text-default-secondary whitespace-pre-wrap break-words max-h-[240px] overflow-y-auto">
          {detail}
        </pre>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Run card
// ---------------------------------------------------------------------------

function RunCard({ tool, canManage }: { tool: CustomTool; canManage: boolean }) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const run = useRunCustomToolTests();
  const [report, setReport] = useState<CustomToolTestReport | null>(null);
  const shown = report ?? tool.last_test_results ?? null;
  const cases = tool.test_cases ?? [];
  const hasCode = !!tool.test_code?.trim();
  const hasTests = cases.length > 0 || hasCode;

  const start = (options: { case_names?: string[]; include_code_tests?: boolean; dry_run_only?: boolean }) => {
    run.mutate(
      { id: tool.id, ...options },
      {
        onSuccess: (data) => setReport(data),
        onError: (err) => toast({ title: t_i18n("Tests could not run"), description: err.message, variant: "error" }),
      },
    );
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <CardTitle className="content-base flex items-center gap-2">
            <Icon name="flask-conical" size={16} className="text-accent" /> {t_i18n("Tests")}
            <TestStatusBadge status={shown?.status ?? tool.last_test_status} />
          </CardTitle>
          {canManage && hasTests && (
            <div className="flex items-center gap-2">
              <Button
                variant="secondary"
                size="sm"
                className="gap-1.5"
                disabled={run.isPending}
                onClick={() => start({ dry_run_only: true })}
              >
                <Icon name="shield-check" size={16} /> {t_i18n("Dry run only")}
              </Button>
              <Button size="sm" className="gap-1.5" disabled={run.isPending} aria-busy={run.isPending || undefined} onClick={() => start({})}>
                {run.isPending && <Spinner size="sm" tone="inherit" />}
                {!run.isPending && <Icon name="play" size={16} />}
                {run.isPending ? t_i18n("Running...") : t_i18n("Run all tests")}
              </Button>
            </div>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="content-compact text-default-secondary">
          {t_i18n(
            "{cases, plural, =0 {No data case} one {# data case} other {# data cases}} · {code, select, yes {test code declared} other {no test code}}",
            { cases: cases.length, code: hasCode ? "yes" : "no" },
          )}
          {shown && (
            <>
              {" · "}
              {t_i18n("last run {when}", { when: formatTimeAgo(intl, shown.ran_at) })}
              {shown.dry_run_only && ` · ${t_i18n("dry run only")}`}
            </>
          )}
        </p>
        {!hasTests && (
          <p className="rounded-sm bg-elevation-hover p-3 content-compact text-default-secondary">
            {t_i18n("This tool declares no tests yet. Add data cases or test code below, then run them here.")}
          </p>
        )}
        {canManage && cases.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {cases.map((c) => (
              <Button
                key={c.name}
                variant="outline"
                size="sm"
                className="gap-1.5 h-7 content-compact"
                disabled={run.isPending}
                onClick={() => start({ case_names: [c.name], include_code_tests: false })}
              >
                <Icon name="play" size={16} /> {c.name}
                {c.dry_run && <Badge variant="cyan" className="content-caption">{t_i18n("Dry run")}</Badge>}
              </Button>
            ))}
          </div>
        )}
        {shown && shown.results.length > 0 && (
          <div className="space-y-2">
            <p className="content-compact text-default-secondary">
              {t_i18n("{passed} passed · {failed} failed · {error, plural, one {# error} other {# errors}} · {skipped} skipped", shown.summary)}
            </p>
            <ul className="space-y-1.5">
              {shown.results.map((r, i) => (
                <ResultRow key={`${r.kind}-${r.name}-${i}`} result={r} />
              ))}
            </ul>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Declaration card
// ---------------------------------------------------------------------------

function DeclarationCard({ tool, editable }: { tool: CustomTool; editable: boolean }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateCustomTool();
  const [editing, setEditing] = useState(false);
  const [casesText, setCasesText] = useState("");
  const [codeText, setCodeText] = useState("");
  const [parseError, setParseError] = useState<string | null>(null);
  const python = tool.language === "python";

  const startEditing = () => {
    setCasesText(tool.test_cases?.length ? JSON.stringify(tool.test_cases, null, 2) : "");
    setCodeText(tool.test_code ?? "");
    setParseError(null);
    setEditing(true);
  };

  const save = () => {
    const parsed = parseTestCases(casesText);
    if (!parsed.ok) {
      setParseError(parsed.error);
      return;
    }
    update.mutate(
      { id: tool.id, test_cases: parsed.cases, test_code: codeText },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Tests saved"), variant: "success" });
          setEditing(false);
        },
      },
    );
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="content-base flex items-center gap-2">
            <Icon name="code-xml" size={16} className="text-accent" /> {t_i18n("Declared tests")}
          </CardTitle>
          {editable && !editing && (
            <Button variant="secondary" size="sm" onClick={startEditing}>{t_i18n("Edit")}</Button>
          )}
          {editing && (
            <div className="flex items-center gap-2">
              <Button variant="ghost" size="sm" onClick={() => setEditing(false)}>{t_i18n("Cancel")}</Button>
              <Button size="sm" onClick={save} disabled={update.isPending}>{t_i18n("Save")}</Button>
            </div>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="rounded-sm bg-elevation-hover p-3 space-y-1.5 content-compact text-default-secondary">
          <p>
            {t_i18n(
              "Every run of the tool receives {variable}: false when an agent calls it, true for a dry-run case. A tool that writes must check it and skip its changes: the sandbox cannot do it for the tool.",
              { variable: python ? "dry_run" : "$dry_run" },
            )}
          </p>
          <p>
            {python
              ? t_i18n("Test code: test_* functions or unittest classes, calling run_tool(inputs, dry_run=True). It returns what the tool printed and raises ToolError when the tool fails.")
              : t_i18n("Test code: test(name, fn) with assert (node:assert), calling await runTool(inputs, {options}). It returns what the tool printed and throws ToolError when the tool fails.", { options: "{ dryRun: true }" })}
          </p>
        </div>

        {editing ? (
          <>
            <div className="space-y-1">
              <label htmlFor="tool-test-cases" className="content-compact text-default-secondary">{t_i18n("Data cases (JSON)")}</label>
              <textarea
                id="tool-test-cases"
                className={cn(
                  `flex min-h-[180px] w-full rounded-sm border bg-elevation-default px-3 py-2 content-compact font-mono-plex placeholder:text-default-secondary ${FIELD_FOCUS_RING}`,
                  parseError ? "border-feedback-error-primary/50" : "border-elevation-subtle",
                )}
                value={casesText}
                placeholder={TEST_CASES_EXAMPLE}
                spellCheck={false}
                onChange={(e) => { setCasesText(e.target.value); setParseError(null); }}
              />
              {parseError && <p className="text-[11px] text-feedback-error-primary">{t_i18n(parseError)}</p>}
            </div>
            <div className="space-y-1">
              <label htmlFor="tool-test-code" className="content-compact text-default-secondary">
                {python ? t_i18n("Test code (Python)") : t_i18n("Test code (JavaScript)")}
              </label>
              <textarea
                id="tool-test-code"
                className={`flex min-h-[180px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-compact font-mono-plex placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
                value={codeText}
                placeholder={testCodeExample(python)}
                spellCheck={false}
                onChange={(e) => setCodeText(e.target.value)}
              />
            </div>
          </>
        ) : (
          <>
            <div className="space-y-1">
              <div className="content-compact text-default-secondary">{t_i18n("Data cases")}</div>
              <pre className="rounded-sm bg-elevation-hover px-3 py-2 content-compact font-mono-plex whitespace-pre-wrap break-words max-h-[300px] overflow-y-auto">
                {tool.test_cases?.length ? JSON.stringify(tool.test_cases, null, 2) : t_i18n("None")}
              </pre>
            </div>
            <div className="space-y-1">
              <div className="content-compact text-default-secondary">{t_i18n("Test code")}</div>
              <pre className="rounded-sm bg-elevation-hover px-3 py-2 content-compact font-mono-plex whitespace-pre-wrap break-words max-h-[300px] overflow-y-auto">
                {tool.test_code?.trim() ? tool.test_code : t_i18n("None")}
              </pre>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Tab
// ---------------------------------------------------------------------------

export function TestsTab({ tool, canManage }: { tool: CustomTool; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const synced = !!tool.source_id;
  return (
    <div className="space-y-6">
      {synced && (
        <p className="rounded-sm bg-elevation-hover p-3 content-compact text-default-secondary">
          {t_i18n("These tests are declared in the repository ({path}): edit them there. A new version of the tool is activated only when they pass.", { path: tool.source_path || "/" })}
        </p>
      )}
      <RunCard tool={tool} canManage={canManage} />
      <DeclarationCard tool={tool} editable={canManage && !synced && !tool.is_package_origin} />
    </div>
  );
}
