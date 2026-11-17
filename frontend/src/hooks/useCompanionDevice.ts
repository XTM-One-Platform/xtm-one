// SPDX-License-Identifier: Apache-2.0

import { isPhoneDevice } from "@/components/companion/companion-routes";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";

interface NavigatorWithClientHints extends Navigator {
  userAgentData?: { mobile?: boolean };
}

/** Read once: the device does not change while the page lives. */
const PHONE =
  typeof navigator !== "undefined" &&
  isPhoneDevice({
    clientHintMobile: (navigator as NavigatorWithClientHints).userAgentData?.mobile,
    userAgent: navigator.userAgent,
  });

/** Whether this device is a phone, where the app is the mobile companion
 *  (`components/companion`): the device decides, not the screen width.
 *  False while "MOBILE_COMPANION" is off (or not yet read): a phone then gets
 *  the web app, with no companion shell, PIN lock or phone-only layout. */
export function useCompanionDevice(): boolean {
  const companion = useFeatureFlag("MOBILE_COMPANION");
  return PHONE && companion;
}
