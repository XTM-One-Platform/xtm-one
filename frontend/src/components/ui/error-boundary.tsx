// SPDX-License-Identifier: Apache-2.0

import { Component, useContext, type ErrorInfo, type ReactNode } from "react";
import { IntlContext, type IntlShape } from "react-intl";
import { AlertCircle, RefreshCw, WifiOff, ServerOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { BackendUnavailableError } from "@/lib/api";
import { useBackendRecovery } from "@/lib/backend-recovery";

interface ErrorBoundaryProps {
  children: ReactNode;
  fallback?: ReactNode;
  /**
   * When true, renders a full-viewport centered error state instead of an
   * inline card.  Use this at the layout level (wrapping AppLayout) so the
   * error replaces the entire page — sidebar, header, and content.
   */
  fullPage?: boolean;
  /**
   * When this value changes, a sticky error state is cleared so the next
   * page can render. Used by the route-level `Page` wrapper with the
   * current pathname: unlike remounting via `key={pathname}` (the previous
   * approach), the boundary — and the Suspense boundary inside it —
   * survives navigation, so React Router's startTransition keeps the
   * previous page visible while the next route chunk loads instead of
   * flashing the Suspense fallback on every navigation.
   */
  resetKey?: string | number;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
  prevResetKey?: string | number;
}

/** True when the error indicates the backend / network is unreachable. */
function isConnectionError(error: Error | null): boolean {
  if (!error) return false;
  if (error instanceof BackendUnavailableError) return true; // explicit backend-down signal
  if (error instanceof TypeError) return true; // Failed to fetch, NetworkError
  return /fetch|network|ECONNREFUSED/i.test(error.message);
}

/**
 * The fallback's strings, translated when a translation provider is mounted.
 * The boundary can render above `AppIntlProvider` (or in a test without one),
 * so the English id is shown there instead of throwing from `useIntl()`.
 */
function useBoundaryText(): (id: string) => string {
  // Typed as always present, `undefined` outside an `IntlProvider`.
  const intl = useContext(IntlContext) as IntlShape | undefined;
  return (id: string) => (intl ? intl.formatMessage({ id, defaultMessage: id }) : id);
}

/**
 * The raw error text (a backend `detail`, a stack-derived message) is for the
 * developer, never the user: shown in a dev build only, the console
 * keeps it in every build (`componentDidCatch`).
 */
const SHOW_ERROR_DETAIL = import.meta.env.DEV;

function ErrorFallback({ error, fullPage, onRetry }: { error: Error | null; fullPage?: boolean; onRetry: () => void }) {
  const t_i18n = useBoundaryText();
  const isConnection = isConnectionError(error);
  const isFullPage = fullPage;
  // An unreachable backend (a restart, a deploy) comes back by itself: the
  // state recovers once the liveness route answers, no click needed.
  useBackendRecovery(isConnection, onRetry);

  const content = (
    <Card className={isConnection ? "border-feedback-alert-primary/20 bg-feedback-alert-primary/10" : "border-feedback-error-primary/20 bg-feedback-error-primary/10"}>
      <CardContent className="flex flex-col items-center justify-center py-16 text-center">
        {isConnection ? (
          <>
            <div className="relative mb-6">
              <div className="absolute inset-0 rounded-full bg-feedback-alert-primary/10 blur-xl" />
              <div className="relative flex h-20 w-20 items-center justify-center rounded-full bg-feedback-alert-primary/10 ring-1 ring-feedback-alert-primary/20">
                {isFullPage ? (
                  <ServerOff className="h-10 w-10 text-feedback-warning-primary" />
                ) : (
                  <WifiOff className="h-10 w-10 text-feedback-warning-primary" />
                )}
              </div>
            </div>
            <h2 className="text-xl font-semibold text-default-primary mb-2">
              {isFullPage ? t_i18n("Unable to reach the server") : t_i18n("Cannot connect to the server")}
            </h2>
            <p className="content-base text-default-secondary max-w-md mb-1">
              {isFullPage
                ? t_i18n("The backend is currently unreachable. This usually means the server is starting up, restarting after a deployment, or temporarily unavailable.")
                : t_i18n("The backend service is currently unreachable. This could mean the server is starting up, restarting after a deployment, or temporarily unavailable.")}
            </p>
            {isFullPage && (
              <p className="content-compact text-default-secondary max-w-sm mb-2">
                {t_i18n("Check that the backend is running, then click the button below to retry.")}
              </p>
            )}
            <p className="content-compact text-default-secondary max-w-sm mb-2">
              {t_i18n("This page reconnects by itself as soon as the server answers.")}
            </p>
            <p className="content-caption text-feedback-warning-primary font-mono-plex mb-6 max-w-md truncate">
              {error?.message}
            </p>
          </>
        ) : (
          <>
            <div className="relative mb-6">
              <div className="absolute inset-0 rounded-full bg-feedback-error-primary/10 blur-xl" />
              <div className="relative flex h-20 w-20 items-center justify-center rounded-full bg-feedback-error-primary/10 ring-1 ring-feedback-error-primary/20">
                {isFullPage ? (
                  <ServerOff className="h-10 w-10 text-feedback-error-primary" />
                ) : (
                  <AlertCircle className="h-10 w-10 text-feedback-error-primary" />
                )}
              </div>
            </div>
            <h2 className="text-xl font-semibold text-default-primary mb-2">
              {isFullPage ? t_i18n("Something went wrong") : t_i18n("Failed to load this section")}
            </h2>
            <p className="content-base text-default-secondary max-w-md mb-1">
              {isFullPage
                ? t_i18n("The backend returned an error. This usually means the server is experiencing issues or is still starting up.")
                : t_i18n("An unexpected error occurred while loading this page.")}
            </p>
            {SHOW_ERROR_DETAIL && error?.message ? (
              <p className="text-[11px] text-feedback-error-primary/60 font-mono-plex mb-6 max-w-md truncate">
                {error.message}
              </p>
            ) : (
              <div className="mb-6" />
            )}
          </>
        )}
        <Button variant="secondary" className="gap-2" onClick={onRetry}>
          <RefreshCw className="h-4 w-4" />
          {isConnection ? t_i18n("Retry connection") : t_i18n("Try again")}
        </Button>
      </CardContent>
    </Card>
  );

  // Full-page mode: center the error card in the entire viewport
  if (isFullPage) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-elevation-default p-6">
        <div className="w-full max-w-lg animate-fade-in">
          {content}
        </div>
      </div>
    );
  }

  return content;
}

/**
 * React error boundary that catches render errors in child components
 * and displays a friendly error UI with retry option.
 *
 * Detects network / connection errors (backend unavailable) and shows
 * a dedicated "Cannot connect to the server" message instead of a
 * generic crash screen.
 *
 * **fullPage mode** — used at the layout level to replace the entire
 * viewport with a beautiful centered error state when the API is down
 * before any page content can render.
 *
 * A connection error recovers by itself: the fallback asks the liveness
 * route every few seconds and reloads once the backend answers.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false, error: null, prevResetKey: props.resetKey };
  }

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { hasError: true, error };
  }

  /** Clear a sticky error when `resetKey` changes (navigation). Remounted
   *  queries refetch on mount, so the new page renders normally. */
  static getDerivedStateFromProps(
    props: ErrorBoundaryProps,
    state: ErrorBoundaryState,
  ): Partial<ErrorBoundaryState> | null {
    if (props.resetKey !== state.prevResetKey) {
      return { hasError: false, error: null, prevResetKey: props.resetKey };
    }
    return null;
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("ErrorBoundary caught:", error, info);
  }

  handleRetry = () => {
    // Always reload to reset the TanStack Query cache.  A simple state
    // reset would re-render the component tree, but queries are still in
    // error state and throwOnError would re-throw immediately.
    window.location.reload();
  };

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;
      return <ErrorFallback error={this.state.error} fullPage={this.props.fullPage} onRetry={this.handleRetry} />;
    }

    return this.props.children;
  }
}
