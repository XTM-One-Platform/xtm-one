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
import {
  Users, Bot, Zap, MessageSquare, BarChart3, Key, Radio, Link2,
  BookOpen, FileText, GraduationCap, Server, Wrench,
} from "lucide-react";

import { useDashboardStats } from "@/hooks/api/useMiscApi";
import { useAiAdoptionOverview } from "@/hooks/api/useAiAdoptionApi";
import { INTEGRATION_PROVIDER_NAMES, CHANNEL_TYPE_LABELS } from "@/lib/constants";
import { pluralize } from "@/lib/utils";
import { WidgetRow } from "@/components/ui/widget-row";
import {
  HorizontalBarChart,
  TimeSeriesChart,
  KpiCard,
  KpiCount,
  KpiRangePicker,
  GroupFilterWarning,
} from "./shared";
import { rangeToDays, rangeDiffLabel, useGroupFilter, type ActivityRange } from "./shared-utils";

// Self-contained KPI: Platform Active Users
const PLATFORM_USERS_LABELS: Record<string, string> = {
  all: "Monthly Active Users", "1y": "Yearly Active Users", "6m": "6M Active Users",
  "3m": "Quarterly Active Users", "1m": "Monthly Active Users", "1w": "Weekly Active Users",
};
function PlatformActiveUsersKpi() {
  const groupId = useGroupFilter();
  const [range, setRange] = useState<ActivityRange>("1m");
  const days = rangeToDays(range);
  const { data: overview } = useAiAdoptionOverview(days, groupId);
  const { data: stats } = useDashboardStats(range, "platform");
  const activeUsers = overview?.active_user_count ?? 0;
  const prevActiveUsers = overview?.prev_active_user_count ?? 0;
  return <KpiCard label={PLATFORM_USERS_LABELS[range] || "Active Users"} value={activeUsers} icon={Users} diff={range !== "all" ? prevActiveUsers : undefined} diffLabel={rangeDiffLabel(range)} subtitle={<>of <KpiCount value={stats?.user_count ?? 0} /> registered</>} headerRight={<><GroupFilterWarning /><KpiRangePicker value={range} onChange={setRange} /></>} />;
}

// Self-contained KPI: Platform Agentic Executions
function PlatformAgenticKpi() {
  const [range, setRange] = useState<ActivityRange>("1m");
  const { data: stats } = useDashboardStats(range, "platform");
  return <KpiCard label="Agentic Executions" value={stats?.runs.total ?? 0} icon={BarChart3} diff={range !== "all" ? (stats?.diffs.runs_prev_month_count ?? 0) : undefined} diffLabel={rangeDiffLabel(range)} headerRight={<><GroupFilterWarning /><KpiRangePicker value={range} onChange={setRange} /></>} />;
}

// Self-contained KPI: Platform Conversations
function PlatformConversationsKpi() {
  const [range, setRange] = useState<ActivityRange>("1m");
  const { data: stats } = useDashboardStats(range, "platform");
  return <KpiCard label="Conversations" value={stats?.conversation_count ?? 0} icon={MessageSquare} subtitle={<><KpiCount value={stats?.message_count ?? 0} /> messages</>} headerRight={<><GroupFilterWarning /><KpiRangePicker value={range} onChange={setRange} /></>} />;
}

// Self-contained KPI: Platform API Calls
function PlatformApiCallsKpi() {
  const [range, setRange] = useState<ActivityRange>("1m");
  const { data: stats } = useDashboardStats(range, "platform");
  const totalCalls = stats?.top_api_key_users?.reduce((s, u) => s + u.value, 0) ?? 0;
  return <KpiCard label="API Calls" value={totalCalls} icon={Key} subtitle={<><KpiCount value={stats?.top_api_key_users?.length ?? 0} /> users</>} headerRight={<><GroupFilterWarning /><KpiRangePicker value={range} onChange={setRange} /></>} />;
}

// Self-contained KPI: Agents
function AgentsKpi() {
  const [range, setRange] = useState<ActivityRange>("1m");
  const { data: stats } = useDashboardStats(range, "platform");
  return <KpiCard label="Agents" value={stats?.agents.active ?? 0} icon={Bot} diff={range !== "all" ? stats?.diffs.agents_prev_month : undefined} diffLabel={rangeDiffLabel(range)} subtitle={<><KpiCount value={stats?.assignments.active ?? 0} /> active {pluralize(stats?.assignments.active ?? 0, "assignment")}</>} headerRight={<><GroupFilterWarning /><KpiRangePicker value={range} onChange={setRange} /></>} />;
}

// Self-contained KPI: Knowledge Bases
function KnowledgeBasesKpi() {
  const [range, setRange] = useState<ActivityRange>("1m");
  const { data: stats } = useDashboardStats(range, "platform");
  return <KpiCard label="Knowledge Bases" value={stats?.knowledge_bases ?? 0} icon={BookOpen} subtitle={<><KpiCount value={stats?.document_count ?? 0} /> docs</>} diff={range !== "all" ? stats?.diffs.documents_prev_month : undefined} diffLabel={rangeDiffLabel(range)} headerRight={<><GroupFilterWarning /><KpiRangePicker value={range} onChange={setRange} /></>} />;
}

// Self-contained KPI: Prompts
function PromptsKpi() {
  const [range, setRange] = useState<ActivityRange>("1m");
  const { data: stats } = useDashboardStats(range, "platform");
  return <KpiCard label="Prompts" value={stats?.prompt_count ?? 0} icon={FileText} diff={range !== "all" ? stats?.diffs.prompts_prev_month : undefined} diffLabel={rangeDiffLabel(range)} headerRight={<><GroupFilterWarning /><KpiRangePicker value={range} onChange={setRange} /></>} />;
}

// Self-contained KPI: Skills
function SkillsKpi() {
  const [range, setRange] = useState<ActivityRange>("1m");
  const { data: stats } = useDashboardStats(range, "platform");
  return <KpiCard label="Skills" value={stats?.skill_count ?? 0} icon={GraduationCap} diff={range !== "all" ? stats?.diffs.skills_prev_month : undefined} diffLabel={rangeDiffLabel(range)} headerRight={<><GroupFilterWarning /><KpiRangePicker value={range} onChange={setRange} /></>} />;
}

// Self-contained KPI: MCP Servers
function MCPServersKpi() {
  const [range, setRange] = useState<ActivityRange>("1m");
  const { data: stats } = useDashboardStats(range, "platform");
  return <KpiCard label="MCP Servers" value={stats?.mcp_server_count ?? 0} icon={Server} diff={range !== "all" ? stats?.diffs.mcp_servers_prev_month : undefined} diffLabel={rangeDiffLabel(range)} headerRight={<><GroupFilterWarning /><KpiRangePicker value={range} onChange={setRange} /></>} />;
}

// Self-contained KPI: Custom Tools
function CustomToolsKpi() {
  const [range, setRange] = useState<ActivityRange>("1m");
  const { data: stats } = useDashboardStats(range, "platform");
  return <KpiCard label="Custom Tools" value={stats?.custom_tool_count ?? 0} icon={Wrench} diff={range !== "all" ? stats?.diffs.custom_tools_prev_month : undefined} diffLabel={rangeDiffLabel(range)} headerRight={<><GroupFilterWarning /><KpiRangePicker value={range} onChange={setRange} /></>} />;
}

// ===================================================================
// OVERVIEW: agentic AI adoption on the platform
// ===================================================================
export default function OverviewTab() {
  return (
    <div className="space-y-6">
      {/* Line 1: Active Users / Agentic Executions / Conversations / API Calls.
          Two per line below xl: four cards with a range picker each cut their
          labels and wrap the picker at 1080 px (#11477, same rule as Gaps). */}
      <WidgetRow className="sm:grid-cols-2 xl:grid-cols-4 stagger-children" data-testid="overview-kpi-row">
        <PlatformActiveUsersKpi />
        <PlatformAgenticKpi />
        <PlatformConversationsKpi />
        <PlatformApiCallsKpi />
      </WidgetRow>

      {/* Line 2: Agents / Knowledge Bases / Prompts */}
      <WidgetRow className="sm:grid-cols-2 lg:grid-cols-3 stagger-children">
        <AgentsKpi />
        <KnowledgeBasesKpi />
        <PromptsKpi />
      </WidgetRow>

      {/* Line 3: Skills / MCP Servers / Custom Tools */}
      <WidgetRow className="sm:grid-cols-2 lg:grid-cols-3 stagger-children">
        <SkillsKpi />
        <MCPServersKpi />
        <CustomToolsKpi />
      </WidgetRow>

      {/* Line 4: Top Active Users / Top Active Agents / Top Active Integrations */}
      <WidgetRow className="sm:grid-cols-1 lg:grid-cols-3 stagger-children">
        <HorizontalBarChart dataKey="top_users" icon={Users} title="Top Active Users" color="bg-accent" />
        <HorizontalBarChart dataKey="top_agents" icon={Bot} title="Top Active Agents" color="bg-feedback-success-primary" />
        <HorizontalBarChart dataKey="top_integrations" icon={Link2} title="Top Active Integrations" color="bg-feedback-error-primary" nameMapper={INTEGRATION_PROVIDER_NAMES} />
      </WidgetRow>

      {/* Line 5: Top API Key Users / Top Active Channels / Top Active Assignments */}
      <WidgetRow className="sm:grid-cols-1 lg:grid-cols-3 stagger-children">
        <HorizontalBarChart dataKey="top_api_key_users" icon={Key} title="Top API Key Users" color="bg-feedback-alert-primary" />
        <HorizontalBarChart dataKey="top_channels" icon={Radio} title="Top Active Channels" color="bg-feedback-info-primary" nameMapper={CHANNEL_TYPE_LABELS} />
        <HorizontalBarChart dataKey="top_assignments" icon={Zap} title="Top Active Assignments" color="bg-filigran-ia-primary" />
      </WidgetRow>

      {/* Line 6: Agents (Cumulative) / Agentic Executions */}
      <WidgetRow className="lg:grid-cols-2 stagger-children">
        <TimeSeriesChart dataKey="agents_over_time" icon={Bot} title="Agents (Cumulative)" color="violet" />
        <TimeSeriesChart dataKey="executions_over_time" icon={Zap} title="Agentic Executions" color="emerald" />
      </WidgetRow>

      {/* Line 7: Chat Messages / API Key Calls */}
      <WidgetRow className="lg:grid-cols-2 stagger-children">
        <TimeSeriesChart dataKey="messages_over_time" icon={MessageSquare} title="Chat Messages" color="cyan" />
        <TimeSeriesChart dataKey="api_key_calls_over_time" icon={Key} title="API Key Calls" color="emerald" />
      </WidgetRow>
    </div>
  );
}
