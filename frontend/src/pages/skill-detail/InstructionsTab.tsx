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

import { useMemo, useRef, useState } from "react";
import { Edit3 } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { splitSkillFrontmatter } from "@/lib/skill-content";
import { VariableInsertButton } from "@/components/ui/variable-insert-button";
import { useToast } from "@/components/ui/toast";
import { useUpdateSkill } from "@/hooks/api/useSkillApi";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { useFormatter } from "@/i18n";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import type { AgentSkill } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";

// No typography plugin here: Tailwind's preflight resets headings, lists and
// code, so each element is styled explicitly.
const SKILL_MARKDOWN_CLASSES = [
  "content-base text-default-secondary leading-relaxed break-words space-y-3",
  "[&_h1]:content-base [&_h1]:font-semibold [&_h1]:text-default-primary [&_h1]:text-lg",
  "[&_h2]:content-base [&_h2]:font-semibold [&_h2]:text-default-primary [&_h2]:text-base",
  "[&_h3]:content-base [&_h3]:font-medium [&_h3]:text-default-primary",
  "[&_h4]:content-base [&_h4]:font-medium [&_h4]:text-default-primary",
  "[&_strong]:font-semibold [&_strong]:text-default-primary",
  "[&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_li]:my-1 [&_li>ul]:mt-1 [&_li>ol]:mt-1",
  "[&_a]:text-accent [&_a]:underline",
  "[&_blockquote]:border-l-2 [&_blockquote]:border-elevation-subtle [&_blockquote]:pl-3 [&_blockquote]:italic",
  "[&_hr]:border-elevation-subtle",
  "[&_:not(pre)>code]:content-caption [&_:not(pre)>code]:bg-elevation-default [&_:not(pre)>code]:px-1 [&_:not(pre)>code]:py-0.5 [&_:not(pre)>code]:rounded-sm [&_:not(pre)>code]:font-mono-plex",
  "[&_pre]:overflow-auto [&_pre]:rounded-sm [&_pre]:bg-elevation-default [&_pre]:p-3 [&_pre]:content-caption [&_pre]:font-mono-plex",
  "[&_table]:w-full [&_table]:border-collapse [&_th]:border [&_th]:border-elevation-subtle [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_td]:border [&_td]:border-elevation-subtle [&_td]:px-2 [&_td]:py-1",
].join(" ");

export function InstructionsTab({ skill, canManage }: { skill: AgentSkill; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const update = useUpdateSkill();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  const aiOff = useAiActionDisabled();
  const { toast } = useToast();
  const [editing, setEditing] = useState(false);
  const [content, setContent] = useState(skill.content ?? "");
  const contentRef = useRef<HTMLTextAreaElement>(null);
  const rendered = useMemo(() => splitSkillFrontmatter(skill.content ?? ""), [skill.content]);

  const startEditing = () => {
    setContent(skill.content ?? "");
    setEditing(true);
  };

  const handleSave = async () => {
    // A variable referenced here reaches the skill's audience and the agents using it.
    if (!(await confirmSharing({ entity_type: "skill", entity_id: skill.id, change: { content } }))) return;
    update.mutate(
      { id: skill.id, content: content || undefined },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Instructions updated"), description: t_i18n("SKILL.md content saved successfully."), variant: "success" });
          setEditing(false);
        },
      },
    );
  };

  return (
    <Card>
      {consentElement}
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle>{t_i18n("Instructions")}</CardTitle>
        {canManage && !editing && (
          <Button variant="outline" size="sm" className="gap-1.5" onClick={startEditing}>
            <Edit3 className="h-3.5 w-3.5" />
            {t_i18n("Edit")}
          </Button>
        )}
      </CardHeader>
      <CardContent className="space-y-3">
        {editing ? (
          <>
            <div className="flex items-center justify-between">
              <label className="content-compact text-default-secondary">{t_i18n("SKILL.md content")}</label>
              <VariableInsertButton textareaRef={contentRef} value={content} onChange={setContent} />
            </div>
            <textarea
              ref={contentRef}
              className={`flex min-h-[360px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base font-mono-plex placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
              value={content}
              onChange={(e) => setContent(e.target.value)}
              placeholder={t_i18n("Skill instructions in markdown...")}
            />
            <div className="flex justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={() => setEditing(false)} disabled={update.isPending}>{t_i18n("Cancel")}</Button>
              <AiDisabledTip><Button size="sm" onClick={handleSave} disabled={update.isPending || aiOff}>{t_i18n("Save")}</Button></AiDisabledTip>
            </div>
          </>
        ) : skill.content ? (
          <div className="max-h-[65vh] space-y-3 overflow-auto rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-4">
            {rendered.frontmatter && (
              <pre className="rounded-sm border border-elevation-subtle-soft bg-elevation-default p-3 content-compact font-mono-plex text-default-secondary whitespace-pre-wrap">
                {rendered.frontmatter}
              </pre>
            )}
            {/* A skill's text is third-party content (a shared skill, a remote
                SKILL.md): no images, which would be fetched when the page opens,
                and links open apart. The chat renderer is for the assistant's own
                output. */}
            <div className={SKILL_MARKDOWN_CLASSES}>
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                disallowedElements={["img"]}
                components={{
                  a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
                }}
              >
                {rendered.body}
              </ReactMarkdown>
            </div>
          </div>
        ) : (
          <p className="content-base text-default-secondary">{t_i18n("No SKILL.md content available for this skill.")}</p>
        )}
      </CardContent>
    </Card>
  );
}
