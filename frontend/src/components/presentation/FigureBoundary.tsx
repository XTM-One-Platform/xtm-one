/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The error boundary around a deck figure's lazy drawing.
 *
 * A lazy chunk that fails to load - a network error, a build replaced under
 * an open tab - throws where the drawing renders. With no boundary of its own
 * that reached the route's, which replaced the whole deck, and the figure's
 * `data-deck-pending` never cleared, so the print never started. The figure
 * shows its own error state instead. React keeps a failed lazy import failed,
 * so the state holds for as long as the boundary is mounted.
 */
import { Component, type ReactNode } from "react";

interface FigureBoundaryProps {
  children: ReactNode;
  /** What stands in place of the drawing; nothing when the figure draws its error state itself (`onError`). */
  fallback?: (error: unknown) => ReactNode;
  onError?: (error: unknown) => void;
}

interface FigureBoundaryState {
  failed: boolean;
  error: unknown;
}

export class FigureBoundary extends Component<FigureBoundaryProps, FigureBoundaryState> {
  state: FigureBoundaryState = { failed: false, error: null };

  static getDerivedStateFromError(error: unknown): FigureBoundaryState {
    return { failed: true, error };
  }

  componentDidCatch(error: unknown) {
    this.props.onError?.(error);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return this.props.fallback ? this.props.fallback(this.state.error) : null;
  }
}
