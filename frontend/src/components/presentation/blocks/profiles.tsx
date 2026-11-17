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
 * The `profiles` block: people on a grid - founders, speakers, a
 * leadership team, up to twelve (three rows of four), the columns balanced
 * when the block sets none and every row as tall as the tallest card. One
 * card per person: a circular portrait (a `people_photo` template asset by
 * file id, the initials without one), the name and title on one line each,
 * then ONE row per line with a 32 px tile - a logo by file id, else a Lucide
 * icon, else the tone dot - and the fact on a single line.
 *
 * A line, the name or the title that does not fit its row shrinks one type
 * step (`data-fit="tight"`, measured after layout) and is then cut with an
 * ellipsis, which the layout audit reports as `clipped` on `profile-line`,
 * `profile-name` or `profile-title` with the text - so the agent shortens
 * the text, never a picture. This is the native form of the slide an
 * agent used to build as a browser-rendered PNG, where one logo or one padding
 * meant a full rebuild; here each is a one-field edit.
 *
 * Styling is plain CSS in `presentation.css`, keyed by `data-*` attributes.
 */
import { type RefObject, useLayoutEffect, useRef } from "react";

import type { DeckProfile, DeckProfileLine, DeckProfilesBlock } from "@/types";

import { DeckIcon } from "../DeckIcon";
import { useBlockEditAttrs } from "../edit-context";
import { InlineMarkup } from "../inline-markup";
import { markupToPlainText } from "../markup";
import {
  PROFILE_LINE_TEXT_CLASS,
  PROFILE_NAME_CLASS,
  PROFILE_TITLE_CLASS,
  fitProfileLines,
  profileColumns,
  profileInitials,
} from "./profiles-fit";
import { IconText, SlideImage } from "./shared";

function useFitProfileLines(ref: RefObject<HTMLDivElement | null>, deps: readonly unknown[]) {
  useLayoutEffect(() => {
    const root = ref.current;
    if (!root) return;
    let cancelled = false;
    fitProfileLines(root);
    // The deck fonts may land AFTER the first pass, and a glyph-width change
    // moves no box the observer below watches (a nowrap row keeps its
    // height), so the pass is run again once they are ready - the export
    // page and the layout audit then measure the line in the font it is
    // drawn in, not in the fallback's metrics.
    const fonts = typeof document !== "undefined" ? document.fonts : undefined;
    if (fonts?.ready) {
      void fonts.ready.then(() => {
        if (!cancelled) fitProfileLines(root);
      });
    }
    if (typeof ResizeObserver === "undefined") {
      return () => {
        cancelled = true;
      };
    }
    const observer = new ResizeObserver(() => fitProfileLines(root));
    observer.observe(root);
    return () => {
      cancelled = true;
      observer.disconnect();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the lines are the dependency, re-fit when they change
  }, deps);
}

function ProfileTile({ line }: { line: DeckProfileLine }) {
  if (line.logo_file_id) {
    return (
      <span className="deck-profile-tile" data-kind="logo">
        <SlideImage fileId={line.logo_file_id} alt="" className="deck-profile-logo" fit="contain" brand />
      </span>
    );
  }
  if (line.icon) {
    return (
      <span className="deck-profile-tile" data-kind="icon">
        <DeckIcon name={line.icon} />
      </span>
    );
  }
  return <span className="deck-profile-tile" data-kind="dot" aria-hidden="true" />;
}

function ProfileCard({ profile, field }: { profile: DeckProfile; field: string }) {
  const initials = profileInitials(profile.name);
  return (
    <div className="deck-profile" data-tone={profile.tone ?? "accent"}>
      <div className="deck-profile-head">
        <span className="deck-profile-photo" data-kind={profile.photo_file_id ? "photo" : "initials"}>
          {profile.photo_file_id ? (
            <SlideImage
              fileId={profile.photo_file_id}
              alt={markupToPlainText(profile.name)}
              className="deck-profile-portrait"
              fit="cover"
            />
          ) : (
            <span className="deck-profile-initials" aria-hidden="true">
              {initials}
            </span>
          )}
        </span>
        <div className="deck-profile-identity">
          <InlineMarkup
            as="div"
            className={PROFILE_NAME_CLASS}
            value={profile.name}
            field={`${field}.name`}
            singleLine
          />
          {profile.title ? (
            <InlineMarkup
              as="div"
              className={PROFILE_TITLE_CLASS}
              value={profile.title}
              field={`${field}.title`}
              singleLine
            />
          ) : null}
        </div>
      </div>
      <ul className="deck-profile-lines">
        {profile.lines.map((line, index) => (
          // A line is the shared icon + text row: the plate is the
          // anchor its one line centres on.
          <IconText as="li" className="deck-profile-line" key={index} icon={<ProfileTile line={line} />}>
            {/* One run per line: the fit pass measures THIS element, the
                stylesheet ellipsizes it, the audit reads its cut. A line
                break would defeat all three (a <br> breaks a nowrap row), so
                the validator refuses one and the editor never offers one. */}
            <InlineMarkup
              as="span"
              className={PROFILE_LINE_TEXT_CLASS}
              value={line.text}
              field={`${field}.lines.${index}.text`}
              singleLine
            />
          </IconText>
        ))}
      </ul>
    </div>
  );
}

export function ProfilesBlockView({ block }: { block: DeckProfilesBlock }) {
  const rootRef = useRef<HTMLDivElement>(null);
  useFitProfileLines(rootRef, [block.items, block.columns, block.density, block.photo_size]);
  return (
    <div
      ref={rootRef}
      className="deck-profiles"
      data-columns={block.columns ?? profileColumns(block.items.length)}
      data-density={block.density ?? "normal"}
      data-photo-size={block.photo_size ?? "md"}
      {...useBlockEditAttrs()}
    >
      {block.items.map((profile, index) => (
        <ProfileCard profile={profile} field={`items.${index}`} key={index} />
      ))}
    </div>
  );
}
