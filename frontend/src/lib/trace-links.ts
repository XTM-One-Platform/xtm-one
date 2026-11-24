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
 * Deep links into the Trace Explorer, scoped to what is being investigated.
 * The query keys are the ones `pages/Traces.tsx` reads back and `GET /traces`
 * filters on, so a shortcut never spells them by hand.
 */
export const ASSIGNMENT_TRACE_PARAM = "assignment_id";
export const ASSIGNMENT_RUN_TRACE_PARAM = "assignment_run_id";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Is this scope value an id at all? It comes from a URL anyone can craft, so
 * it is checked before being spliced into an API path (`../users/…` would
 * otherwise send the admin's session somewhere else). `GET /traces` validates
 * it on its own and answers 422.
 */
export function isTraceScopeId(value: string): boolean {
  return UUID_RE.test(value);
}

function tracesPath(key: string, value: string): string {
  return `/observability/traces?${new URLSearchParams({ [key]: value }).toString()}`;
}

/** Every trace recorded by any run of the assignment. */
export function assignmentTracesPath(assignmentId: string): string {
  return tracesPath(ASSIGNMENT_TRACE_PARAM, assignmentId);
}

/** The traces recorded by one assignment run. */
export function assignmentRunTracesPath(runId: string): string {
  return tracesPath(ASSIGNMENT_RUN_TRACE_PARAM, runId);
}
