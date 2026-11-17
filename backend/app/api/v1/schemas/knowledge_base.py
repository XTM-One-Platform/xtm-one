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

"""Pydantic schemas for knowledge base endpoints."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    StrictBool,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.api.v1.schemas.column_limits import column_length_validator
from app.core.utils import normalize_github_repo
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.services.chunking import chunk_param_type_problem, chunk_params_problem
from app.services.kb_figures import MAX_DESCRIBED_PAGES, MAX_DESCRIPTION_CHARS


def _refuse_non_integer_chunk_param(value: object, info: ValidationInfo) -> object:
    problem = chunk_param_type_problem(info.field_name or "value", value)
    if problem:
        raise ValueError(problem)
    return value


# Checked before coercion: lax `int` would store `true` as 1 and "128" as 128.
ChunkParam = Annotated[int, BeforeValidator(_refuse_non_integer_chunk_param)]

#: ``kb_embedding_text.HTML_INGESTION_MODES``, spelled as a type.
HtmlIngestion = Literal["source", "rendered"]


class ManagingWorkspace(BaseModel):
    """The workspace that owns a knowledge base."""

    id: str
    name: str


class KnowledgeBaseResponse(BaseModel):
    id: str
    user_id: str | None = None
    owner_name: str | None = None
    name: str
    description: str | None = None
    embedding_model: str
    is_system_managed: bool = False
    is_builtin: bool = False
    sync_sources_customized: bool = False
    # Content lock: the collection is ground truth. Still fully searchable and
    # readable; agent writes and ad-hoc document mutation are refused. Configured
    # import sources keep running — see the model for the full contract.
    is_read_only: bool = False
    # How HTML documents are embedded: their stored markup (``source``)
    # or their rendered page text (``rendered``). See the model.
    html_ingestion: HtmlIngestion = "source"
    # Whether the figures of an uploaded PDF are described for the index
    # (``kb_figures``). See the model.
    describe_figures: bool = False
    is_company_managed: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    #: Whether the REQUESTING user may administer it - ``helpers.can_manage``,
    #: share tiers included, so the UI never offers an edit the API refuses.
    #: Built-in and system-managed KBs stay locked whatever this says.
    can_manage: bool = False
    #: Materialized by a skill package import: read-only on every write route
    #: whoever asks (``helpers.is_package_origin_knowledge_base``), so the UI
    #: never offers those writes.
    is_package_origin: bool = False
    #: Why no one may write its content - ``package_installed`` / ``workspace`` /
    #: ``builtin`` / ``read_only`` (``helpers.knowledge_base_content_lock``, the rule the
    #: write tools and agents' write scopes apply), or ``None``. The agent
    #: Knowledge section reads it to refuse a write grant the runtime strips.
    content_lock: Literal["package_installed", "workspace", "builtin", "read_only"] | None = None
    #: The workspace that owns it - its Sources or Conversations: listed
    #: in the library, changed only from the workspace, never offered for
    #: binding. ``None`` for every other knowledge base.
    managed_by_workspace: ManagingWorkspace | None = None
    tags: list[str] = []
    document_count: int
    chunk_count: int
    query_count: int = 0
    # Document status breakdown (computed from DB, not paginated docs)
    ready_count: int = 0
    pending_count: int = 0
    error_count: int = 0
    # A fixed, platform-authored sentence and its code, never raw exception
    # text (``app.services.kb_document_errors``).
    latest_error_message: str | None = None
    latest_error_code: str | None = None
    # Earliest scheduled automatic re-embed across this KB's ``error`` docs
    # (ISO timestamp). None when no failed doc has an auto-retry scheduled.
    next_auto_retry_at: str | None = None
    # How many ``error`` docs have an auto-retry scheduled. Comparing this to
    # ``error_count`` tells the UI whether ALL failures will self-heal or
    # some failed structurally and still need manual action.
    retryable_error_count: int = 0
    # Derived from stored embeddings rows against current platform embedding target.
    has_stale_embeddings: bool = False
    stale_tuple_count: int = 0
    stale_chunk_count: int = 0
    created_at: str
    updated_at: str


class CreateKnowledgeBaseRequest(BaseModel):
    name: str
    description: str | None = None
    is_read_only: bool = False
    html_ingestion: HtmlIngestion = "source"
    # Strict: a switch, never ``"yes"`` or ``1`` read as one.
    describe_figures: StrictBool = False
    is_company_managed: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    tags: list[str] | None = None
    embedding_model: str = ""
    chunk_size: ChunkParam = 512
    chunk_overlap: ChunkParam = 128

    _fit_columns = column_length_validator(KnowledgeBase, "name", "embedding_model")

    @model_validator(mode="after")
    def _chunk_params_in_range(self) -> "CreateKnowledgeBaseRequest":
        # An overlap reaching the size made chunking loop forever on the
        # worker; the 422 names the allowed range.
        problem = chunk_params_problem(self.chunk_size, self.chunk_overlap)
        if problem:
            raise ValueError(problem)
        return self


class UpdateKnowledgeBaseRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    # Toggling the content lock is itself a config change, so it goes through the
    # same manage gate as the rest of this request (and is audited).
    is_read_only: bool | None = None
    # Applies to documents processed afterwards; re-embedding the existing ones
    # is the explicit reprocess action, never a side effect of this change.
    html_ingestion: HtmlIngestion | None = None
    # Applies to PDFs uploaded afterwards: a PDF already stored keeps no copy
    # of its pages to describe, and turning it off stops inserting the
    # descriptions on the next processing of a document.
    describe_figures: StrictBool | None = None
    is_company_managed: bool | None = None
    # Owner (``user_id``) transfer — admin only. Ignored when omitted/unchanged.
    user_id: str | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    tags: list[str] | None = None

    _fit_columns = column_length_validator(KnowledgeBase, "name")


#: ``kb_figures.FIGURE_STATUSES``, spelled as a type.
FigureStatus = Literal["pending", "complete", "partial", "failed"]


class KnowledgeDocumentResponse(BaseModel):
    id: str
    filename: str
    source_type: str
    source_url: str | None = None
    status: str
    chunk_count: int
    error_message: str | None = None
    error_code: str | None = None
    # The figure pass of a PDF uploaded to a knowledge base that describes
    # figures (``kb_figures.figure_state``): ``None`` for any other document.
    # A ``ready`` document whose status is ``partial`` / ``failed`` is indexed
    # (in part) as text only, and ``figure_note`` says why, in fixed words.
    figure_status: FigureStatus | None = None
    figure_note: str | None = None
    figure_pages: int = 0
    created_at: str


class KnowledgeDocumentDetailResponse(BaseModel):
    """A single document with its full stored source text.

    The list response deliberately omits ``content`` (it would make listing a
    large knowledge base enormous); this detail shape carries the complete,
    verbatim source plus the embedding state needed to tell whether the vector
    index has caught up with it yet.
    """

    id: str
    knowledge_base_id: str
    filename: str
    source_type: str
    source_url: str | None = None
    status: str
    chunk_count: int
    error_message: str | None = None
    error_code: str | None = None
    # ``None`` means no stored source text at all, which is distinct from an
    # intentionally empty document ("").
    content: str | None = None
    content_hash: str | None = None
    content_length: int = 0
    # As on the list response: the figure pass of a PDF, ``None`` otherwise.
    figure_status: FigureStatus | None = None
    figure_note: str | None = None
    figure_pages: int = 0
    created_at: str
    updated_at: str


class UpdateKnowledgeDocumentRequest(BaseModel):
    """Replace or extend a document's stored source, triggering a re-embed."""

    content: str
    filename: str | None = None
    mode: str = "replace"  # replace | append

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        normalized = (v or "replace").strip().lower()
        if normalized not in ("replace", "append"):
            raise ValueError("mode must be 'replace' or 'append'")
        return normalized


class GlobalKnowledgeDocumentResponse(BaseModel):
    id: str
    knowledge_base_id: str
    knowledge_base_name: str
    #: The document's KB was installed by a skill package, whose document
    #: routes (reprocess included) refuse every write.
    knowledge_base_is_package_origin: bool = False
    filename: str
    source_type: str
    source_url: str | None = None
    status: str
    chunk_count: int
    error_message: str | None = None
    error_code: str | None = None
    created_at: str


class GlobalKnowledgeDocumentsResponse(BaseModel):
    items: list[GlobalKnowledgeDocumentResponse]
    total: int
    limit: int
    offset: int


class AddTextDocumentRequest(BaseModel):
    filename: str
    content: str
    source_type: str = "markdown"  # markdown | text

    _fit_columns = column_length_validator(KnowledgeDocument, "source_type")


class CrawlURLRequest(BaseModel):
    url: str
    max_depth: int = 0  # 0 = single page, 1 = follow links one level, etc.


class SyncNotionRequest(BaseModel):
    page_ids: list[str] | None = None
    search_query: str | None = None


class SyncDriveRequest(BaseModel):
    folder_path: str = "/"
    site_id: str | None = None  # SharePoint site ID (omit for OneDrive)
    recursive: bool = False  # Whether to recurse into subfolders


class SyncGoogleDriveRequest(BaseModel):
    folder_id: str = "root"


class SyncGitHubRequest(BaseModel):
    repo: str  # e.g. "owner/repo-name"
    branch: str = "main"
    path: str = ""  # Optional subdirectory

    @field_validator("repo")
    @classmethod
    def normalize_repo(cls, v: str) -> str:
        """Accept full GitHub URLs and extract owner/repo."""
        return normalize_github_repo(v)


class SyncResponse(BaseModel):
    documents_synced: int
    document_ids: list[str]


class KBExportKnowledgeBase(BaseModel):
    name: str
    description: str | None = None
    tags: list[str] = []
    embedding_model: str = ""
    chunk_size: ChunkParam = 512
    chunk_overlap: ChunkParam = 128
    # Defaulted so a package exported before the setting existed imports as
    # ``source``, exactly what its knowledge base embedded.
    html_ingestion: HtmlIngestion = "source"
    describe_figures: bool = False


class KBExportFigurePage(BaseModel):
    """One page description of a document's figure reading (``kb_figures``).

    Read from a package someone else may have written: bounded here, and the
    text is cleaned again (``kb_figures.clean_description``) on import.
    """

    page: int = Field(ge=1, le=100_000)
    offset: int = Field(ge=0)
    mode: Literal["figures", "page"] = "figures"
    text: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS + 100)


class KBExportDocument(BaseModel):
    filename: str
    source_type: str
    source_url: str | None = None
    archive_path: str | None = None
    # The figure reading of a PDF whose knowledge base described figures, so
    # an imported knowledge base embeds the same text without the PDF (which
    # is not exported). ``None`` for every other document and every package
    # written before the setting existed.
    figure_status: Literal["complete", "partial", "failed"] | None = None
    figure_pages: list[KBExportFigurePage] | None = Field(default=None, max_length=MAX_DESCRIBED_PAGES)


class KBExportSyncSource(BaseModel):
    source_type: str
    config: dict = {}
    sync_interval: str = "manual"


class KBExportManifest(BaseModel):
    format: str
    version: int
    exported_at: str
    export_id: str
    part: int
    total_parts: int
    knowledge_base: KBExportKnowledgeBase
    documents: list[KBExportDocument] = []
    sync_sources: list[KBExportSyncSource] = []


class KBExportPackageResponse(BaseModel):
    id: str
    export_id: str
    part_number: int
    total_parts: int
    filename: str
    file_size: int
    expires_at: str
    download_url: str


class KBExportResponse(BaseModel):
    export_id: str
    packages: list[KBExportPackageResponse]


class SyncSourceResponse(BaseModel):
    id: str
    source_type: str
    config: dict
    sync_interval: str
    enabled: bool
    integration_id: str | None = None
    integration_name: str | None = None
    last_synced_at: str | None = None
    last_sync_status: str | None = None
    last_sync_error: str | None = None
    last_sync_documents: int = 0
    created_at: str
    updated_at: str


class CreateSyncSourceRequest(BaseModel):
    source_type: str  # notion | microsoft-drive | google-drive | github
    config: dict  # source-specific config (repo, branch, path, folder_id, optional timeout_secs, etc.)
    sync_interval: str = "manual"  # manual | 6h | 12h | daily | weekly
    integration_id: str | None = None  # explicit integration to use for syncing


class UpdateSyncSourceRequest(BaseModel):
    config: dict | None = None  # supports optional timeout_secs override
    sync_interval: str | None = None
    enabled: bool | None = None
    integration_id: str | None = None  # explicit integration to use (pass "" to clear)


class TestSearchRequest(BaseModel):
    query: str
    top_k: int = 5


class TestSearchResultItem(BaseModel):
    content: str
    source_type: str
    source_id: str | None = None
    score: float
    created_at: str


class TestSearchResponse(BaseModel):
    query: str
    results: list[TestSearchResultItem]
    total: int


class GlobalTestSearchRequest(BaseModel):
    query: str
    top_k: int = 10
    kb_ids: list[str] | None = None


class GlobalTestSearchResultItem(BaseModel):
    knowledge_base_id: str
    knowledge_base_name: str
    content: str
    source_type: str
    source_id: str | None = None
    score: float
    created_at: str


class GlobalTestSearchResponse(BaseModel):
    query: str
    results: list[GlobalTestSearchResultItem]
    total: int


class KBActivityLogResponse(BaseModel):
    id: str
    event: str
    details: str | None = None
    source_type: str | None = None
    document_count: int | None = None
    created_at: str
