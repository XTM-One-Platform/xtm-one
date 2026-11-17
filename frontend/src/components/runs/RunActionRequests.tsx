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

import { Hand } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useToast } from "@/components/ui/toast";
import { useDismissRunActionRequest, useRunActionRequests } from "@/hooks/api/useAssignmentApi";
import { useFormatter } from "@/i18n";
import { timeAgo } from "@/lib/utils";

/**
 * The open requests to act on a paused run: someone who cannot answer
 * it asked, from the card of its pasted link, the people who can. Shown to
 * those people only (`enabled`), each request dismissable.
 */
export function RunActionRequests({ runId, enabled }: { runId: string; enabled: boolean }) {
  const { t_i18n } = useFormatter();
  const { data: requests } = useRunActionRequests(runId, enabled);
  const dismiss = useDismissRunActionRequest();
  const { toast } = useToast();
  if (!enabled || !requests || requests.length === 0) return null;
  return (
    <Card className="border-feedback-warning-primary/30">
      <CardHeader>
        <CardTitle className="content-base flex items-center gap-2 text-feedback-warning-primary">
          <Hand className="h-4 w-4" />
          {t_i18n("Requests to act on this run")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {requests.map((request) => (
          <div key={request.id} className="flex items-center justify-between gap-3">
            <p className="content-compact text-default-primary">
              {t_i18n("{name} asked you to act on this run.", { name: request.requester_name })}{" "}
              <span className="text-default-secondary">{timeAgo(request.created_at)}</span>
            </p>
            <Button
              variant="ghost"
              size="sm"
              disabled={dismiss.isPending}
              onClick={() =>
                dismiss.mutate(
                  { runId, requestId: request.id },
                  {
                    onError: (err) =>
                      toast({ title: t_i18n("Could not dismiss the request"), description: err.message, variant: "error" }),
                  },
                )
              }
            >
              {t_i18n("Dismiss")}
            </Button>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
