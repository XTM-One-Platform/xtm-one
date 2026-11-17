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
 * Share a video - the deck's Share dialog (`PresentationShareDialog`)
 * for a video: per-user and per-group grants with a tier (`can view`: watch,
 * download, duplicate; `can edit`: also edit, render and share), the
 * admin-only company-managed switch, the audience line computed from the
 * DRAFT, the video's address to copy, and - apart, last - its public link.
 *
 * Only the sharing fields travel in the PATCH, never the spec, so opening
 * this dialog while the assistant edits the video cannot clobber a scene, and
 * a sharing change never bumps the revision. The public link applies at once
 * (`PublicLinkCard`) and plays the video's CURRENT render; `Allow download`
 * gives its visitors a download button for the MP4 and its captions - a
 * convenience, not a protection: the player's bytes are theirs to save.
 */
import { Download } from "lucide-react";

import { EntityShareDialog } from "@/components/ui/entity-share-dialog";
import { PublicLinkCard } from "@/components/ui/public-link-card";
import {
  useDisableVideoPublicLink,
  useEnableVideoPublicLink,
  useUpdateVideoPublicLink,
  useUpdateVideoSharing,
  useVideoPublicLink,
} from "@/hooks/api/useVideoStudioApi";
import { useFormatter } from "@/i18n";
import { VIDEO_SHARE_PERMISSION_DESCRIPTIONS, VIDEO_SHARE_PERMISSION_LABELS } from "@/lib/constants";
import type { VideoProject } from "@/types";

import { videoLink } from "./video-links";

function VideoPublicLinkSection({ video, open }: { video: VideoProject; open: boolean }) {
  const { t_i18n } = useFormatter();
  const link = useVideoPublicLink(video.id, open);
  const enable = useEnableVideoPublicLink();
  const disable = useDisableVideoPublicLink();
  const options = useUpdateVideoPublicLink();
  return (
    <div className="space-y-1.5">
      <PublicLinkCard
        state={link.data}
        isLoading={link.isLoading}
        isError={link.isError}
        busy={enable.isPending || disable.isPending || options.isPending}
        replacing={enable.isPending}
        enable={() => enable.mutateAsync({ projectId: video.id })}
        disable={() => disable.mutateAsync(video.id)}
        regenerate={() => enable.mutateAsync({ projectId: video.id, regenerate: true })}
        description={t_i18n(
          "Anyone with this link can watch the video without an account: its current render only, never its scenes, recordings or editor.",
        )}
        offConfirm={t_i18n(
          "Whoever has the link will be told the video is not available. The people and groups it is shared with keep their access.",
        )}
        adminOffKept={t_i18n(
          "An administrator has turned public links off on this platform: this video's link plays nothing until they are turned back on.",
        )}
        option={{
          label: t_i18n("Allow download"),
          description: t_i18n(
            "Visitors get a download button for the MP4 and its captions. It is a convenience, not a protection: anyone who can watch the video can save what it plays.",
          ),
          icon: Download,
          checked: !!link.data?.allow_download,
          onChange: (allowed) => options.mutateAsync({ projectId: video.id, allowDownload: allowed }),
          onToast: t_i18n("Download allowed"),
          offToast: t_i18n("Download turned off"),
          testId: "video.share.publicLink.download",
        }}
        testIdPrefix="video.share.publicLink"
      />
      {link.data?.enabled && !video.ready_render && (
        <p className="content-caption text-feedback-warning-primary">
          {t_i18n("The video has not been rendered yet: the link says so until its first render is ready.")}
        </p>
      )}
    </div>
  );
}

export function VideoShareDialog({
  video,
  open,
  onOpenChange,
}: {
  video: VideoProject;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const update = useUpdateVideoSharing();
  return (
    <EntityShareDialog
      entity={video}
      open={open}
      onOpenChange={onOpenChange}
      title={t_i18n("Share video")}
      description={t_i18n(
        "Access is granted here: the video's address opens nothing on its own, unless you turn on its public link. Everyone with access can watch, download and duplicate it; editors can also edit, render and share it, in the editor or through their own assistant.",
      )}
      entityType="video"
      permissionLabels={VIDEO_SHARE_PERMISSION_LABELS}
      permissionDescriptions={VIDEO_SHARE_PERMISSION_DESCRIPTIONS}
      companyLabel={t_i18n("Company-managed video")}
      link={videoLink(video.id)}
      linkNote={t_i18n("Anyone else who opens it is told the video is not available.")}
      save={(payload) => update.mutateAsync({ projectId: video.id, ...payload })}
      saving={update.isPending}
      testId="video.share.dialog"
    >
      {video.can_manage && <VideoPublicLinkSection video={video} open={open} />}
    </EntityShareDialog>
  );
}
