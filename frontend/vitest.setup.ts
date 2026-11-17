// jest-dom's matchers (toBeInTheDocument, toHaveAttribute, …) registered on
// vitest's expect, for the component tests that assert with them.
import "@testing-library/jest-dom/vitest";

import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

import { failOnDesignSystemWarnings } from "./src/testing/design-system-warnings";

// Without vitest globals, Testing Library cannot register its own cleanup.
afterEach(cleanup);

failOnDesignSystemWarnings();
