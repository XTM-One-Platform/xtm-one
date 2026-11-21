// SPDX-License-Identifier: Apache-2.0

/**
 * The page of an address no route serves (#797). Every URI follows the left
 * menu and its tabs, with no alias and no redirect of a former address: a
 * path the router does not know, an old one included, lands here inside the
 * app chrome, never on the home page.
 */

import { useNavigate } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { NotFoundState } from "@/components/ui/not-found-state";

/** The design-system compass, sized by NotFoundState's class like the other not-found icons. */
function NotFoundIcon({ className }: { className?: string }) {
  return <Icon name="compass" className={className} aria-hidden />;
}

export default function RouteNotFound() {
  const navigate = useNavigate();
  return (
    <NotFoundState
      icon={NotFoundIcon}
      title="Page not found"
      description="No page answers this address. The menu on the left lists every page."
      actionLabel="Go to Home"
      onAction={() => navigate("/")}
      backTo="/"
      backLabel="Back to Home"
    />
  );
}
