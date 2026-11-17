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
 * Opening / divider / closing layouts: title, section, closing.
 *
 * The title and closing slides carry a HERO: `data-hero` on
 * `.deck-title` / `.deck-closing` names the variant the stylesheet draws, and
 * `HeroBackdrop` paints it under the content - `plain` paints nothing and
 * adds nothing to the DOM the layouts always produced but that one attribute.
 *
 * A cover whose hero hosts the logo (`cascade`, `horizon`: `heroHostsLogo`)
 * draws the brand logo itself, large in its top corner, and the chrome leaves
 * its own out. The title slide's speakers and footer line sit in one
 * `.deck-title-meta` group, which every other cover lays out as before (the
 * group is `display: contents`) and the `cascade` draws as one row under a rule.
 */
import type { ClosingSlide, DeckContactKind, DeckCoverSpeaker, DeckSpeaker, SectionSlide, TitleSlide } from "@/types";

import { SlideImage } from "../blocks/shared";
import { useDeckShowsLogo } from "../canvas-context";
import { DeckIcon } from "../DeckIcon";
import { useDeckDesign } from "../design-context";
import { heroHostsLogo, slideHero, slideHeroTone, slideOverlay } from "../hero";
import { HeroBackdrop } from "../HeroBackdrop";
import { InlineMarkup, PlainText } from "../inline-markup";
import { useSlideScheme } from "../theme";

function Speaker({
  speaker,
  photoFileId,
  field = "speaker",
}: {
  speaker?: DeckSpeaker;
  photoFileId?: string;
  /** The spec path of the speaker, for the in-place editor (`speakers.1` on a title slide). */
  field?: string;
}) {
  if (!speaker) return null;
  return (
    <div className="deck-speaker">
      {photoFileId ? (
        <SlideImage fileId={photoFileId} alt={speaker.name} className="deck-speaker-photo" fit="cover" />
      ) : null}
      <div className="deck-speaker-body">
        <InlineMarkup as="div" className="deck-speaker-name" value={speaker.name} field={`${field}.name`} />
        {speaker.title ? (
          <InlineMarkup as="div" className="deck-speaker-title" value={speaker.title} field={`${field}.title`} />
        ) : null}
        {speaker.credentials?.length ? (
          <div className="deck-speaker-credentials">
            {speaker.credentials.map((credential, index) => (
              <InlineMarkup as="span" key={index} value={credential} field={`${field}.credentials.${index}`} />
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** The title slide's speakers, side by side in speaking order. */
function Speakers({ speakers }: { speakers?: DeckCoverSpeaker[] }) {
  if (!speakers?.length) return null;
  return (
    <div className="deck-speakers" data-count={speakers.length}>
      {speakers.map((speaker, index) => (
        <Speaker key={index} speaker={speaker} photoFileId={speaker.photo_file_id} field={`speakers.${index}`} />
      ))}
    </div>
  );
}

/**
 * The brand logo a cover draws itself (`heroHostsLogo`): the variant of the
 * slide's own scheme, like the chrome's - the dark-ink lockup on a light
 * cover when the template ships one - and hidden with the deck's logo.
 */
function CoverLogo({ slide, brand = false }: { slide: TitleSlide | ClosingSlide; brand?: boolean }) {
  const shows = useDeckShowsLogo();
  const resolution = useSlideScheme();
  if (!shows || !(brand || heroHostsLogo(slide)) || !resolution) return null;
  const { tokens } = resolution;
  // The brand cover is always dark: the light lockup, whatever the scheme.
  const fileId =
    resolution.scheme === "light" && !brand
      ? (tokens.logo_light_file_id ?? tokens.logo_file_id)
      : tokens.logo_file_id;
  if (!fileId) return null;
  return (
    <span className="deck-cover-logo" aria-hidden="true">
      <SlideImage fileId={fileId} alt="" className="deck-cover-logo-image" fit="contain" />
    </span>
  );
}

/**
 * The brand cover backdrop (`design.cover: "brand"`, spec 3.8): part of
 * the background, never an effect on an element. The template's mark at about
 * 7% opacity, drawn large and cropped by the bottom-right corner, and
 * Sky Blue into Royal Blue rising from that corner over the dark paper.
 * Captured as one picture by the PPTX export like the `gradient` hero's wash;
 * the backdrop's `overflow: hidden` clips the mark at the slide edge.
 */
function BrandCoverBackdrop() {
  const resolution = useSlideScheme();
  const fileId = resolution?.tokens.mark_file_id;
  return (
    <div className="deck-brand-backdrop" data-export-backdrop="true" aria-hidden="true">
      {fileId ? <SlideImage fileId={fileId} alt="" className="deck-brand-emblem" fit="contain" /> : null}
    </div>
  );
}

export function TitleLayout({ slide }: { slide: TitleSlide }) {
  const brand = useDeckDesign()?.cover === "brand";
  const hero = slideHero(slide) ?? "plain";
  return (
    <div className="deck-title" data-align={slide.align ?? "start"} data-hero={hero} data-brand={brand ? "true" : undefined}>
      {brand ? (
        <BrandCoverBackdrop />
      ) : (
        <HeroBackdrop
          hero={hero}
          tone={slideHeroTone(slide)}
          imageFileId={slide.background_image_file_id}
          overlay={slideOverlay(slide)}
          seed={slide.id ?? "title"}
          composition={slide.align === "center" ? "center" : "start"}
        />
      )}
      <CoverLogo slide={slide} brand={brand} />
      <div className="deck-title-main">
        {slide.event_label ? (
          <InlineMarkup as="div" className="deck-eyebrow" value={slide.event_label} field="event_label" />
        ) : null}
        <InlineMarkup as="h1" value={slide.title} field="title" accentLastLine={slide.hero_accent_word === true} />
        {slide.subtitle ? (
          <InlineMarkup as="p" className="deck-subtitle" value={slide.subtitle} field="subtitle" />
        ) : null}
        {slide.icon_strip?.length ? (
          <div className="deck-icon-strip" aria-hidden="true">
            {slide.icon_strip.map((icon, index) => (
              <span className="deck-icon-tile" data-tone="accent" data-size="md" key={index}>
                <DeckIcon name={icon} />
              </span>
            ))}
          </div>
        ) : null}
      </div>
      {slide.speakers?.length || slide.footer ? (
        <div className="deck-title-meta">
          <Speakers speakers={slide.speakers} />
          {slide.footer ? <InlineMarkup as="p" className="deck-footer" value={slide.footer} field="footer" /> : null}
        </div>
      ) : null}
    </div>
  );
}

export function SectionLayout({ slide }: { slide: SectionSlide }) {
  // `design.section: "outline"`: the number drawn as a large outline
  // (the eyebrow is hidden by `eyebrows: false`). The slide's icon is drawn on
  // every composition, the outline one included.
  const outline = useDeckDesign()?.section === "outline";
  return (
    <div className="deck-section" data-outline={outline ? "true" : undefined}>
      {slide.number ? (
        <div className="deck-section-number" aria-hidden="true">
          {String(slide.number).padStart(2, "0")}
        </div>
      ) : null}
      <div className="deck-section-main">
        {slide.label ? <InlineMarkup as="div" className="deck-eyebrow" value={slide.label} field="label" /> : null}
        {slide.icon ? (
          <span className="deck-icon-tile" data-tone="accent" data-size="lg">
            <DeckIcon name={slide.icon} />
          </span>
        ) : null}
        <InlineMarkup as="h1" value={slide.title} field="title" />
        {slide.subtitle ? (
          <InlineMarkup as="p" className="deck-subtitle" value={slide.subtitle} field="subtitle" />
        ) : null}
      </div>
    </div>
  );
}

const CONTACT_ICON: Record<DeckContactKind, string> = {
  web: "globe",
  email: "mail",
  linkedin: "linkedin",
  x: "at-sign",
  github: "github",
  phone: "phone",
};

export function ClosingLayout({ slide }: { slide: ClosingSlide }) {
  // The brand closing: the cover's backdrop; the logo moves to the
  // bottom centre with the contacts centred under it (stylesheet order).
  const brand = useDeckDesign()?.cover === "brand";
  const hero = slideHero(slide) ?? "plain";
  return (
    <div
      className="deck-closing"
      data-align={slide.align === "start" ? "start" : undefined}
      data-hero={hero}
      data-brand={brand ? "true" : undefined}
    >
      {brand ? (
        <BrandCoverBackdrop />
      ) : (
        <HeroBackdrop
          hero={hero}
          tone={slideHeroTone(slide)}
          imageFileId={slide.background_image_file_id}
          overlay={slideOverlay(slide)}
          seed={slide.id ?? "closing"}
          composition={slide.align === "start" ? "start" : "center"}
        />
      )}
      <CoverLogo slide={slide} brand={brand} />
      <div className="deck-closing-main">
        <InlineMarkup as="h1" value={slide.title ?? "Thank you"} field="title" />
        {slide.subtitle ? (
          <InlineMarkup as="p" className="deck-subtitle" value={slide.subtitle} field="subtitle" />
        ) : null}
      </div>
      <Speaker speaker={slide.speaker} />
      {slide.contacts?.length ? (
        <ul className="deck-contacts">
          {slide.contacts.map((contact, index) => (
            <li key={index} data-kind={contact.kind}>
              <DeckIcon name={CONTACT_ICON[contact.kind] ?? "link"} />
              <span>
                {contact.label ? (
                  <InlineMarkup value={contact.label} field={`contacts.${index}.label`} />
                ) : (
                  <PlainText value={contact.value} field={`contacts.${index}.value`} />
                )}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      {slide.tagline ? (
        <InlineMarkup as="p" className="deck-closing-tagline" value={slide.tagline} field="tagline" />
      ) : null}
    </div>
  );
}
