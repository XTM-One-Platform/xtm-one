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
 * Share to the app - where the Android share sheet lands (`/share`).
 *
 * Text and links arrive as query parameters, files through the service worker
 * (`lib/shared-inbox.ts`). Nothing is sent: the content is put in a new
 * conversation's composer and attachment tray, and the person reads, adds a
 * question and sends it.
 */
import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Share2 } from "lucide-react";

import { useAskAssistant } from "@/lib/ask-assistant";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useFormatter } from "@/i18n";
import { handFilesToChat, takeSharedContent } from "@/lib/shared-inbox";

export function SharePage() {
  const { t_i18n } = useFormatter();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const ask = useAskAssistant();
  const [empty, setEmpty] = useState(false);

  useEffect(() => {
    let live = true;
    (async () => {
      const fromParams = ["title", "text", "url"]
        .map((key) => params.get(key)?.trim())
        .filter((value): value is string => !!value)
        .join("\n");
      const stashed = params.get("pending") ? await takeSharedContent() : null;
      if (!live) return;
      const text = [fromParams, stashed?.text ?? ""].filter(Boolean).join("\n").slice(0, 4000);
      const files = stashed?.files ?? [];
      if (!text && files.length === 0) {
        setEmpty(true);
        return;
      }
      handFilesToChat(files);
      ask(text);
    })();
    return () => {
      live = false;
    };
    // Once per arrival: the parameters are the share itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!empty) return null;
  return (
    <Card className="mt-8 flex flex-col items-center gap-4 border-dashed px-6 py-10 text-center">
      <Share2 className="h-10 w-10 text-accent" aria-hidden="true" />
      <p className="content-base text-default-primary">{t_i18n("Nothing was shared.")}</p>
      <Button className="min-h-11" onClick={() => navigate("/chat")}>
        {t_i18n("Open the assistant")}
      </Button>
    </Card>
  );
}
