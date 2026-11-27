// A browser profile whose application did not answer: the card says the session could not be
// checked, never that it is signed out, and a sign-in whose last check got no answer is not a failed sign-in.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import type { BrowserProfile, BrowserSignInState } from "@/types";
import { BrowserProfileCard } from "./BrowserProfileCard";

const { check, signIn, testSignIn } = vi.hoisted(() => ({
  check: vi.fn(),
  signIn: vi.fn(),
  testSignIn: vi.fn(),
}));

vi.mock("@/hooks/api/useBrowserProfilesApi", () => ({
  useCheckBrowserProfileSession: () => ({ mutateAsync: check, isPending: false }),
  useSignInBrowserProfile: () => ({ mutateAsync: signIn, isPending: false }),
  useTestBrowserProfileSignIn: () => ({ mutateAsync: testSignIn, isPending: false }),
  useSignOutBrowserProfile: () => ({ mutate: vi.fn(), isPending: false }),
  useSubmitMfaCode: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useMfaScreenshot: () => null,
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { id: "u-1" } }) }));

const REASON =
  "start_url answered HTTP 503: the application is unavailable or limits the rate of requests, so this check says " +
  "nothing of the session (check again later)";

function profile(overrides: Partial<BrowserProfile> = {}): BrowserProfile {
  return {
    id: "bp-1",
    user_id: "u-1",
    name: "Demo",
    description: null,
    start_url: "https://app.example.test/",
    login_url: null,
    allowed_hosts: [],
    apps: [],
    login_config: {},
    browser_options: {},
    username_hint: null,
    has_username: true,
    has_password: true,
    has_totp: false,
    totp_secret_status: "none",
    vault_credentials: [],
    vault_secrets: null,
    renews_by_itself: false,
    session_status: "unverified",
    session_saved_at: new Date().toISOString(),
    session_checked_at: new Date().toISOString(),
    last_probe_at: new Date().toISOString(),
    last_probe_result: {
      authenticated: false,
      url: "https://app.example.test/",
      reason: REASON,
      checked_at: new Date().toISOString(),
      http_status: 503,
    },
    last_sign_in_message: null,
    mfa_challenge: null,
    keepalive_minutes: 0,
    sign_in_timeout_seconds: 120,
    session_save_timeout_seconds: 30,
    mfa_timeout_seconds: 300,
    is_company_managed: false,
    shared_group_ids: [],
    shared_user_ids: [],
    can_manage: true,
    created_at: null,
    updated_at: null,
    ...overrides,
  };
}

function unverified(message: string): BrowserSignInState {
  return {
    status: "unverified",
    message,
    challenge_id: "c-1",
    kind: null,
    number: null,
    screenshot_file_id: null,
    expires_at: null,
    profile_id: "bp-1",
    stage: null,
    last_url: "https://app.example.test/",
    probe: null,
    trace: null,
  };
}

function card(value: BrowserProfile = profile()) {
  renderWithProviders(<BrowserProfileCard profile={value} onEdit={() => {}} onDelete={() => {}} />);
}

beforeEach(() => {
  check.mockReset();
  signIn.mockReset();
  testSignIn.mockReset();
});

describe("BrowserProfileCard on an application that did not answer", () => {
  it("says the application did not answer, not where the start URL lands", () => {
    card();
    expect(screen.getByText(`Checked just now: the application did not answer (${REASON})`)).toBeInTheDocument();
    expect(screen.queryByText(/the start URL lands on/)).toBeNull();
  });

  it("reads a check the application did not answer as not checked, never as signed out", async () => {
    check.mockResolvedValue(profile());
    card();
    fireEvent.click(screen.getByRole("button", { name: "Check the session" }));
    expect(await screen.findByText("The session could not be checked")).toBeInTheDocument();
    expect(screen.getByText(REASON)).toBeInTheDocument();
    expect(screen.queryByText("Not signed in")).toBeNull();
    expect(check).toHaveBeenCalledWith("bp-1");
  });

  it("reads a sign-in whose saved session got no answer as signed in, not checked, never as failed", async () => {
    signIn.mockResolvedValue(unverified("Signed in. The session is saved, but it could not be checked."));
    card();
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Signed in, not checked")).toBeInTheDocument();
    expect(screen.getByText("Signed in. The session is saved, but it could not be checked.")).toBeInTheDocument();
    expect(screen.queryByText("Sign-in failed")).toBeNull();
  });

  it("reads such a dry run as signed in, not checked, never as unfinished", async () => {
    testSignIn.mockResolvedValue(unverified("Dry run reached the application signed in, but it could not be checked."));
    card();
    fireEvent.click(screen.getByRole("button", { name: "Test sign-in" }));
    await waitFor(() => expect(screen.getAllByText("Dry run: signed in, not checked")).toHaveLength(2));
    expect(screen.queryByText("Dry run did not finish")).toBeNull();
    expect(screen.queryByText("Dry run: did not finish")).toBeNull();
  });
});

describe("BrowserProfileCard of a profile that signs in other applications", () => {
  it("names the applications on one line, without their URLs on the card face", () => {
    card(
      profile({
        apps: [
          { name: "Risk", url: "https://risk.example.test/" },
          { name: "Intel", url: "https://intel.example.test/" },
          { name: "", url: "https://third.example.test/path" },
        ],
      }),
    );
    const line = screen.getByTestId("browser-profile-apps");
    expect(line).toHaveTextContent("Also signs in: Risk, Intel, +1");
    expect(line).not.toHaveTextContent("https://");
  });

  it("shows no application line when the profile declares none", () => {
    card(profile({ apps: [] }));
    expect(screen.queryByTestId("browser-profile-apps")).toBeNull();
  });
});
