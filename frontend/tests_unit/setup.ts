// jest-dom's matchers (toBeInTheDocument, toHaveClass, …) registered on
// vitest's expect.
import "@testing-library/jest-dom/vitest";

import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";
import { failOnDesignSystemWarnings } from "../src/testing/design-system-warnings";

/**
 * Testing Library unmounts between tests on its own ONLY when `afterEach` is a
 * global, and this project runs vitest without globals. Without this, every
 * render accumulates in the same document and queries start failing with
 * "found multiple elements" — which reads like a duplicated-render bug in the
 * component under test rather than a missing hook here.
 */
afterEach(cleanup);

failOnDesignSystemWarnings();
