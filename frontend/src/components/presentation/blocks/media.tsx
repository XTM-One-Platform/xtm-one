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

/** Media blocks: image (platform file), video (platform MP4 / Video Studio render) and diagram (lazy Mermaid). Logos are `logos.tsx`. */
import { lazy, Suspense, useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";

import { useFormatter } from "@/i18n";
import type { DeckDiagramBlock, DeckImageBlock, DeckImageFrame, DeckVideoBlock } from "@/types";

import { videoLink } from "@/components/video-studio/video-links";

import { videoEmbed, videoEmbedPage, videoEmbedSrc } from "./video-embed";

import { useDeckAssetSrc } from "../asset-context";
import { useBlockEditAttrs } from "../edit-context";
import { FigureBoundary } from "../FigureBoundary";
import { useGiveUp } from "../figure-wait";
import { InlineMarkup } from "../inline-markup";
import { markupToPlainText } from "../markup";
import { DiagramError, DiagramPlaceholder } from "./diagram-states";
import { alignAttr } from "./helpers";
import { SlideImage } from "./shared";
import { showcaseRoomStyle, useShowcaseRoom } from "./showcase";

// Mermaid only ships to browsers that open a deck containing a diagram.
const Diagram = lazy(() => import("./Diagram"));

export function ImageBlockView({ block }: { block: DeckImageBlock }) {
  const alt = block.alt ?? markupToPlainText(block.caption);
  const frame: DeckImageFrame = block.frame ?? "none";
  const framed = frame !== "none";
  const roomRef = useRef<HTMLDivElement>(null);
  // The window takes the picture's shape: the room it may fill and the
  // picture's ratio are both published on the showcase box for the stylesheet.
  const room = useShowcaseRoom(roomRef, framed && !!block.fill);
  const [ratio, setRatio] = useState<number | null>(null);
  const picture = (
    <SlideImage
      fileId={block.file_id}
      alt={alt}
      className="deck-image"
      fit={block.fit ?? "contain"}
      onRatio={framed ? setRatio : undefined}
      brand
    />
  );
  return (
    <figure
      className="deck-figure"
      data-size={block.size ?? "md"}
      data-fill={block.fill ? "true" : undefined}
      data-align={alignAttr(block.align ?? "center")}
      // Inner padding: a picture on a card is never glued to its edge.
      data-padding={block.padding && block.padding !== "none" ? block.padding : undefined}
      // Only a frame that draws something is written: an unframed image keeps its original DOM.
      data-frame={framed ? frame : undefined}
      {...useBlockEditAttrs()}
    >
      {framed ? (
        <div ref={roomRef} className="deck-showcase-room">
          <Showcase frame={frame} ratio={ratio} style={showcaseRoomStyle(room)}>
            {picture}
          </Showcase>
        </div>
      ) : (
        picture
      )}
      {block.caption ? (
        <figcaption>
          <InlineMarkup value={block.caption} field="caption" />
        </figcaption>
      ) : null}
    </figure>
  );
}

/** Share of the block that must be on screen for an autoplaying video to play. */
const VIDEO_VISIBLE_RATIO = 0.5;

/** `#t=start,end`: the media fragment that clips the playback window. */
export function videoFragment(start: number | null | undefined, end: number | null | undefined): string {
  const from = typeof start === "number" && start > 0 ? start : null;
  const to = typeof end === "number" && end > 0 ? end : null;
  if (from === null && to === null) return "";
  return `#t=${from ?? 0}${to !== null ? `,${to}` : ""}`;
}

/**
 * A video that plays inside the slide. The poster shows first, the bytes load
 * only once the block is on screen (`armed`), so a deck of many videos does not
 * download them all. `autoplay` plays the video while at least half of it is on
 * screen - the active slide in present mode - and pauses it when the slide
 * leaves. The static render behind the PDF and PPTX export (`data-deck-static`)
 * keeps the poster with a play badge: the poster is what the PDF prints and the
 * picture the editable PPTX turns into a movie (`data-export-video`).
 */
export function VideoBlockView({ block }: { block: DeckVideoBlock }) {
  const { t_i18n } = useFormatter();
  const alt = block.alt ?? markupToPlainText(block.caption ?? undefined);
  const frame: DeckImageFrame = block.frame ?? "none";
  const framed = frame !== "none";
  const hostRef = useRef<HTMLElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const roomRef = useRef<HTMLDivElement>(null);
  const room = useShowcaseRoom(roomRef, framed && !!block.fill);
  const [ratio, setRatio] = useState<number | null>(null);
  const [armed, setArmed] = useState(false);
  const [visible, setVisible] = useState(false);
  const [isStatic, setIsStatic] = useState(false);
  const autoplay = !!block.autoplay;
  const muted = block.muted ?? autoplay;
  const src = useDeckAssetSrc(armed ? block.file_id : null);
  const captions = useDeckAssetSrc(armed && block.captions !== false ? block.captions_file_id : null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    if (host.closest('[data-deck-static="true"]')) {
      setIsStatic(true);
      return;
    }
    if (typeof IntersectionObserver === "undefined") {
      setArmed(true);
      setVisible(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        const entry = entries[entries.length - 1];
        const shown = !!entry && entry.isIntersecting && entry.intersectionRatio >= VIDEO_VISIBLE_RATIO;
        setVisible(shown);
        if (entry?.isIntersecting) setArmed(true);
      },
      { threshold: [0, VIDEO_VISIBLE_RATIO] },
    );
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    if (!visible) {
      if (!video.paused) video.pause();
      return;
    }
    if (autoplay && video.paused) {
      // A refused autoplay (sound without a gesture) leaves the poster and the controls.
      void video.play()?.catch(() => undefined);
    }
  }, [visible, autoplay, src]);

  const poster = useDeckAssetSrc(block.poster_file_id);
  // A YouTube / Vimeo link plays in the provider's player, mounted only while
  // the block is on screen: leaving the slide unloads it, which stops it.
  const embed = block.file_id ? null : videoEmbed(block.url);
  // At `full` or with `fill`, an unframed player takes the largest 16:9 box the
  // room holds: the room's width, or its height times 16/9, whichever binds
  // (the cell's width alone pushed the control bar below the slide body).
  const playerRoomRef = useRef<HTMLDivElement>(null);
  const sizedByRoom = !framed && !!embed && (!!block.fill || block.size === "full");
  const playerRoom = useShowcaseRoom(playerRoomRef, sizedByRoom);
  const playable = !isStatic && armed && !!src;
  // The PDF keeps a link to the video under its poster (`data-link-href`, read by the export).
  const pageLink = block.video_id ? videoLink(block.video_id) : embed ? videoEmbedPage(embed) : undefined;
  const href = isStatic ? pageLink : undefined;
  const media = embed && !isStatic && visible ? (
    <iframe
      className="deck-image deck-video deck-video-embed"
      src={videoEmbedSrc(embed, {
        autoplay,
        muted,
        loop: !!block.loop,
        controls: block.controls !== false,
        start: block.start,
        end: block.end,
      })}
      title={alt || t_i18n("Video")}
      allow="autoplay; encrypted-media; fullscreen; picture-in-picture"
      allowFullScreen
      referrerPolicy="strict-origin-when-cross-origin"
      loading="lazy"
    />
  ) : playable ? (
    <video
      ref={videoRef}
      key={src}
      className="deck-image deck-video"
      src={`${src}${videoFragment(block.start, block.end)}`}
      poster={poster ?? undefined}
      controls={block.controls !== false}
      muted={muted}
      loop={!!block.loop}
      playsInline
      preload="metadata"
      aria-label={alt || t_i18n("Video")}
      onLoadedMetadata={(event) => {
        const { videoWidth, videoHeight } = event.currentTarget;
        if (videoWidth > 0 && videoHeight > 0) setRatio(videoWidth / videoHeight);
      }}
    >
      {captions ? <track kind="captions" src={captions} default /> : null}
    </video>
  ) : (
    <span className="deck-video-poster" data-export-video={block.file_id ?? undefined}>
      {href ? <a className="deck-video-link" href={href} data-link-href={href} aria-label={alt || t_i18n("Video")} /> : null}
      {block.poster_file_id ? (
        <SlideImage fileId={block.poster_file_id} alt={alt || t_i18n("Video")} className="deck-image" onRatio={setRatio} />
      ) : (
        <span className="deck-image deck-video-empty" />
      )}
      <span className="deck-video-badge" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="24" height="24">
          <path d="M8 5.5v13l11-6.5z" fill="currentColor" />
        </svg>
      </span>
    </span>
  );
  return (
    <figure
      ref={hostRef}
      className="deck-figure"
      data-kind="video"
      data-size={block.size ?? "md"}
      data-fill={block.fill ? "true" : undefined}
      data-align={alignAttr(block.align ?? "center")}
      data-frame={framed ? frame : undefined}
      {...useBlockEditAttrs()}
    >
      {framed ? (
        <div ref={roomRef} className="deck-showcase-room">
          <Showcase frame={frame} ratio={ratio} style={showcaseRoomStyle(room)}>
            {media}
          </Showcase>
        </div>
      ) : sizedByRoom ? (
        <div ref={playerRoomRef} className="deck-video-room" style={showcaseRoomStyle(playerRoom)}>
          {media}
        </div>
      ) : (
        media
      )}
      {block.caption ? (
        <figcaption>
          <InlineMarkup value={block.caption} field="caption" />
        </figcaption>
      ) : null}
    </figure>
  );
}

/**
 * The showcase window around a picture: a glow in the brand gradient
 * under a window whose bar is the frame's - three dots and an address pill
 * for `browser`, three small dots on a bar a third as tall for
 * `browser_slim`, a camera dot on a bezel for `device`. The glow and the bar
 * are decoration (`aria-hidden`), drawn as real elements with backgrounds so
 * the editable PPTX carries them as a picture and shapes; the glow is captured
 * isolated (`data-export-backdrop`) so the window is never baked into it.
 */
function Showcase({
  frame,
  ratio,
  style,
  children,
}: {
  frame: Exclude<DeckImageFrame, "none">;
  ratio: number | null;
  style: CSSProperties | undefined;
  children: ReactNode;
}) {
  // The one inline style besides the theme variables: both values are
  // measured at runtime (the room, the picture), so neither can be a class.
  const sized = ratio !== null && ratio > 0;
  const showcaseStyle = sized ? ({ ...style, "--deck-image-ratio": String(ratio) } as CSSProperties) : style;
  return (
    <div className="deck-showcase" data-sized={sized ? "true" : undefined} style={showcaseStyle}>
      <span className="deck-showcase-glow" aria-hidden="true" data-export-backdrop="true" />
      <div className="deck-showcase-window">
        <div className="deck-showcase-bar" aria-hidden="true">
          {frame === "browser" ? (
            <>
              <span className="deck-showcase-dot" />
              <span className="deck-showcase-dot" />
              <span className="deck-showcase-dot" />
              <span className="deck-showcase-address" />
            </>
          ) : frame === "browser_slim" ? (
            <>
              <span className="deck-showcase-dot" />
              <span className="deck-showcase-dot" />
              <span className="deck-showcase-dot" />
            </>
          ) : (
            <span className="deck-showcase-camera" />
          )}
        </div>
        {children}
      </div>
    </div>
  );
}

type EditAttrs = Record<string, string | undefined>;

/** The diagram while its chunk loads: pending, until it gives up after `FIGURE_WAIT_MS`. */
function DiagramLoading({ block, editAttrs }: { block: DeckDiagramBlock; editAttrs: EditAttrs }) {
  const hostRef = useRef<HTMLElement>(null);
  const gaveUp = useGiveUp(hostRef, "chunk");
  return (
    <figure
      ref={hostRef}
      className="deck-diagram"
      data-size={block.size ?? "md"}
      data-state="pending"
      data-deck-pending={gaveUp ? undefined : "true"}
      data-deck-unfinished={gaveUp ? "true" : undefined}
      {...editAttrs}
    >
      <DiagramPlaceholder />
    </figure>
  );
}

/** The diagram whose chunk could not load: the block's error state, never the route's. */
function DiagramUnavailable({ block, editAttrs, error }: { block: DeckDiagramBlock; editAttrs: EditAttrs; error: unknown }) {
  return (
    <figure
      className="deck-diagram"
      data-size={block.size ?? "md"}
      data-fill={block.fill ? "true" : undefined}
      data-state="error"
      data-deck-unfinished="true"
      {...editAttrs}
    >
      <DiagramError message={error instanceof Error ? error.message : String(error)} source={block.source} />
      {block.caption ? (
        <figcaption>
          <InlineMarkup value={block.caption} field="caption" />
        </figcaption>
      ) : null}
    </figure>
  );
}

export function DiagramBlockView({ block }: { block: DeckDiagramBlock }) {
  const editAttrs = useBlockEditAttrs();
  return (
    <FigureBoundary fallback={(error) => <DiagramUnavailable block={block} editAttrs={editAttrs} error={error} />}>
      <Suspense fallback={<DiagramLoading block={block} editAttrs={editAttrs} />}>
        <Diagram block={block} editAttrs={editAttrs} />
      </Suspense>
    </FigureBoundary>
  );
}
