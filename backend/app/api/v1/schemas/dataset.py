# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Pydantic schemas for dataset endpoints."""

from pydantic import BaseModel, Field

from app.api.v1.schemas.column_limits import column_length_validator
from app.models.dataset import Dataset
from app.models.file_attachment import FileAttachment

# ---------------------------------------------------------------------------
# Dataset Entry
# ---------------------------------------------------------------------------


class DatasetEntryRequest(BaseModel):
    description: str | None = None
    input_prompt: str = ""
    expected_answer: str
    evaluation_prompt: str | None = None
    file_ids: list[str] | None = None
    output_schema: dict | None = None


class DatasetPackageEntry(DatasetEntryRequest):
    """An entry of a dataset package's manifest: the fields of a created entry.

    ``files`` names the package's files by their manifest ``id``. An entry without
    ``expected_answer`` imports with an empty one.
    """

    expected_answer: str = ""
    files: list[str] = Field(default_factory=list)


class DatasetPackageFile(BaseModel):
    """A file a dataset package carries, as its manifest lists it."""

    id: str
    path: str = ""
    filename: str = "file"
    content_type: str = "application/octet-stream"
    sha256: str | None = None
    md5: str | None = None

    _fit_columns = column_length_validator(FileAttachment, "filename", "content_type")


class DatasetEntryResponse(BaseModel):
    id: str
    dataset_id: str
    position: int
    description: str | None = None
    input_prompt: str = ""
    expected_answer: str
    evaluation_prompt: str | None = None
    file_ids: list[str] | None = None
    output_schema: dict | None = None
    files: list[dict] | None = None
    created_at: str

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


class CreateDatasetRequest(BaseModel):
    name: str = Field(..., max_length=200)
    description: str | None = None
    evaluation_instructions: str | None = None
    output_schema: dict | None = None
    # The family this dataset's entries are shaped for, and (for
    # CLASSIFICATION only) its declared label vocabulary.
    family: str | None = Field(None, max_length=32)
    label_vocabulary: list[str] | None = None
    is_company_managed: bool = False
    is_read_only: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    entries: list[DatasetEntryRequest] | None = None


class UpdateDatasetRequest(BaseModel):
    name: str | None = Field(None, max_length=200)
    description: str | None = None
    evaluation_instructions: str | None = None
    # The shape every expected answer in this dataset is written to.
    # Same wrapper as ``DatasetEntryRequest.output_schema``. Sent as ``{}`` to clear it:
    # ``None`` already means "leave it alone" on a partial update.
    output_schema: dict | None = None
    # ``family``/``label_vocabulary`` use explicit-null-clears PATCH semantics:
    # omitted = leave alone, explicit ``null`` = clear. Telling the two apart needs
    # ``model_fields_set`` at the call site; ``None`` after parsing is ambiguous.
    family: str | None = Field(None, max_length=32)
    label_vocabulary: list[str] | None = None
    is_company_managed: bool | None = None
    is_read_only: bool | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None


class DatasetFamilyConflict(BaseModel):
    """An evaluation linked to a dataset whose family no longer matches it after an
    update changed the dataset's family."""

    evaluation_id: str
    evaluation_name: str
    message: str


class DatasetResponse(BaseModel):
    id: str
    user_id: str | None = None
    name: str
    description: str | None = None
    evaluation_instructions: str | None = None
    # Returned exactly as written, never normalised or reordered: an author
    # has to be able to recognise what they submitted.
    output_schema: dict | None = None
    # The family this dataset's entries are shaped for, its declared CLASSIFICATION
    # vocabulary, and a computed, read-only flag shown wherever a dataset is.
    family: str | None = None
    label_vocabulary: list[str] | None = None
    needs_family: bool = True
    content_hash: str | None = None
    current_version: int = 0
    is_company_managed: bool = False
    is_read_only: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    evaluation_count: int = 0
    entry_count: int = 0
    created_at: str
    updated_at: str
    # Set only when an update's family change conflicts with an already-linked
    # evaluation: reported, never auto-resolved.
    family_conflicts: list[DatasetFamilyConflict] | None = None

    model_config = {"from_attributes": True}


class DatasetDetailResponse(DatasetResponse):
    entries: list[DatasetEntryResponse] = []


# ---------------------------------------------------------------------------
# Dataset Versions
# ---------------------------------------------------------------------------


class DatasetVersionResponse(BaseModel):
    id: str
    version_number: int
    content_hash: str
    entry_count: int
    created_at: str

    model_config = {"from_attributes": True}


class DatasetVersionEntryResponse(BaseModel):
    id: str
    entry_id: str | None = None
    position: int
    description: str | None = None
    input_prompt: str | None = None
    expected_answer: str
    evaluation_prompt: str | None = None
    file_ids: list[str] | None = None
    output_schema: dict | None = None

    model_config = {"from_attributes": True}


class DatasetVersionDetailResponse(DatasetVersionResponse):
    entries: list[DatasetVersionEntryResponse] = []


# ---------------------------------------------------------------------------
# HuggingFace import
# ---------------------------------------------------------------------------


class HFImportRequest(BaseModel):
    hf_dataset_id: str
    config: str | None = None
    split: str = "train"
    input_column: str
    expected_answer_column: str | None = None
    evaluation_prompt_column: str | None = None
    description_column: str | None = None
    row_limit: int | None = Field(None, ge=1, le=500_000)
    # Dataset metadata
    name: str | None = None
    description: str | None = None
    evaluation_instructions: str | None = None
    is_read_only: bool = False

    _fit_columns = column_length_validator(Dataset, "name")


# ---------------------------------------------------------------------------
# Link / Unlink
# ---------------------------------------------------------------------------


class LinkedDatasetResponse(BaseModel):
    id: str
    dataset_id: str
    dataset_name: str
    dataset_description: str | None = None
    entry_count: int = 0
    position: int = 0
    created_at: str
    # Also in the evaluation-linking picker: a family-less dataset is
    # flagged wherever it can be selected for an evaluation.
    family: str | None = None
    needs_family: bool = True
    # The conflict message when the dataset's family and the evaluation's are both
    # declared and differ (``check_family_compatibility``); ``None`` otherwise.
    family_conflict: str | None = None
