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
 * A spot in a widget's header, before its menu, that the widget's content may
 * put a control in (`InHeader`): OpenAEV writes its number's comparison
 * sentence behind an info sign in its title row. Without a header (an expanded
 * widget, a preview) the control stays where the content drew it.
 */
import { createContext, useContext, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

interface HeaderSlot {
  slot: HTMLElement | null;
  setSlot: (element: HTMLElement | null) => void;
}

const HeaderSlotContext = createContext<HeaderSlot | null>(null);

/** The scope of one widget: its header offers the spot, its content uses it. */
export function HeaderSlotScope({ children }: { children: ReactNode }) {
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  return <HeaderSlotContext.Provider value={{ slot, setSlot }}>{children}</HeaderSlotContext.Provider>;
}

/** The spot itself, drawn by the header. */
export function HeaderSlotTarget() {
  const scope = useContext(HeaderSlotContext);
  return scope ? <span ref={scope.setSlot} className="contents" data-header-slot="" /> : null;
}

/** *children* in the header's spot when there is one, else where they stand. */
export function InHeader({ children }: { children: ReactNode }) {
  const slot = useContext(HeaderSlotContext)?.slot;
  return slot ? createPortal(children, slot) : <>{children}</>;
}
