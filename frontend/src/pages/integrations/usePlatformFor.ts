import {
  usePlatformRegistrations,
  type PlatformRegistration,
} from "@/hooks/api/usePlatformApi";

/**
 * The registration behind an integration, when that integration is a connected
 * platform's.
 *
 * Joined on ``integration_id`` so the list, the row and the detail page all
 * read one record and cannot disagree about a platform's approval posture.
 * Undefined for an ordinary integration, which is what makes every caller here
 * render nothing rather than an empty state.
 */
export function usePlatformFor(
  integrationId?: string,
): PlatformRegistration | undefined {
  const { data } = usePlatformRegistrations();
  if (!integrationId) return undefined;
  return (data ?? []).find((reg) => reg.integration_id === integrationId);
}
