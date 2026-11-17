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
 * What a slide's own components need to know about the deck they are drawn
 * in, beyond the slide: the size of the canvas, for the components that
 * compose against the whole canvas rather than their own box (the tiled
 * heroes lay their tiles on a grid of the canvas), and whether the deck shows
 * its logo, for a cover that draws the logo itself (`heroHostsLogo`). Provided
 * by the two slide hosts - `StaticSlide` and the reveal deck's slide - from
 * the deck's aspect ratio and `chrome.show_logo`; 16:9 and shown anywhere else.
 */
import { createContext, useContext } from "react";

import { DECK_SIZE } from "./geometry";

export const DeckCanvasContext = createContext<{ width: number; height: number }>(DECK_SIZE["16:9"]);

export function useDeckCanvas(): { width: number; height: number } {
  return useContext(DeckCanvasContext);
}

export const DeckShowsLogoContext = createContext<boolean>(true);

export function useDeckShowsLogo(): boolean {
  return useContext(DeckShowsLogoContext);
}
