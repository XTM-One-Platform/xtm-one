// SPDX-License-Identifier: Apache-2.0

/// <reference types="@testing-library/jest-dom/vitest" />

// Registers jest-dom's matchers (toBeInTheDocument, toHaveAttribute, …) on
// vitest's `expect` for the TYPE checker. Importing the setup file at runtime is
// not enough on its own: without this reference the matchers exist at runtime and
// fail `tsc`, which is a confusing place to discover a missing type.
