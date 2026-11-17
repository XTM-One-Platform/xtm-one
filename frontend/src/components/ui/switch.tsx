// SPDX-License-Identifier: Apache-2.0

import * as React from "react";
import { Switch as FdsSwitch } from "@filigran/design-system";

/**
 * Switch — re-exported from the design system.
 *
 * A genuine drop-in: the library's Switch wraps the same
 * `@radix-ui/react-switch` primitive this app wrapped by hand, and its props are
 * that primitive's minus `children`, so all 120 call sites keep working
 * unchanged. The only reason this file still exists is the import path — 54
 * files import from `@/components/ui/switch`.
 *
 * Note for review: the library records this component as WCAG-failing
 * (Switch.meta.ts). That is inherited, not introduced, and belongs upstream —
 * patching it here would fork the component and hide the gap.
 */
const Switch = React.forwardRef<
  React.ComponentRef<typeof FdsSwitch>,
  React.ComponentPropsWithoutRef<typeof FdsSwitch>
>((props, ref) => <FdsSwitch ref={ref} {...props} />);
Switch.displayName = "Switch";

export { Switch };
