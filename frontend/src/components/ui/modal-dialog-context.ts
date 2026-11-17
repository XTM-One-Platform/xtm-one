// SPDX-License-Identifier: Apache-2.0

import { createContext, useContext } from "react";

/** True inside a dialog's content, set by `DialogContent`: a popover portalled from there sits outside its scroll lock. */
export const InModalDialogContext = createContext(false);

export function useInModalDialog(): boolean {
  return useContext(InModalDialogContext);
}
