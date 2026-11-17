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

import { X, Check } from "lucide-react";

interface ApplyPreviewProps {
  original: string;
  proposed: string;
  onApply: () => void;
  onCancel: () => void;
}

export function ApplyPreview({
  original,
  proposed,
  onApply,
  onCancel,
}: ApplyPreviewProps) {
  return (
    <div className="apply-preview-overlay">
      <div className="apply-preview" role="dialog" aria-modal="true">
        <div className="apply-preview-header">
          <span>Preview: replace selection</span>
          <button className="btn-icon" onClick={onCancel} aria-label="Close">
            <X size={16} />
          </button>
        </div>

        <div className="apply-preview-panels">
          <div className="apply-preview-panel apply-preview-original">
            <div className="apply-preview-label">Original</div>
            {original || "(empty)"}
          </div>
          <div className="apply-preview-panel apply-preview-proposed">
            <div className="apply-preview-label">Proposed</div>
            {proposed || "(empty)"}
          </div>
        </div>

        <div className="apply-preview-actions">
          <button className="btn btn-sm" onClick={onCancel}>
            Cancel
          </button>
          <button className="btn btn-sm btn-primary" onClick={onApply}>
            <Check size={14} />
            Apply
          </button>
        </div>
      </div>
    </div>
  );
}
