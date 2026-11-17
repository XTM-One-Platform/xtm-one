"""Candidate files: the resume and other files, downloaded or uploaded."""

from __future__ import annotations

from typing import Any

from app.integrations.file_delivery import (
    deliver_file,
    fetch_file,
    file_content_type,
    read_pipe_file,
    uploaded_file_answer,
)
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

from ._common import PROVIDER, AshbyContext, definition, id_schema

#: Largest candidate file downloaded or uploaded.
MAX_FILE_BYTES = 25 * 1024 * 1024


def candidate_files(candidate: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """``(kind, file handle)`` for the resume first, then every other file."""
    handles: list[tuple[str, dict[str, Any]]] = []
    resume = candidate.get("resumeFileHandle")
    if isinstance(resume, dict) and resume.get("handle"):
        handles.append(("resume", resume))
    for handle in candidate.get("fileHandles") or []:
        if isinstance(handle, dict) and handle.get("handle"):
            handles.append(("file", handle))
    return handles


def register(ctx: AshbyContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def download_ashby_candidate_file(candidate_id: str, file_id: str = "") -> str | ToolResult:
        async with ctx.client() as client:
            candidate, err = await ctx.call(client, "candidate.info", {"id": candidate_id})
            if err:
                return err
            candidate = candidate if isinstance(candidate, dict) else {}
            files = candidate_files(candidate)
            if not files:
                return f"Error: candidate {candidate_id} has no resume or files in Ashby."
            wanted = str(file_id or "").strip()
            chosen = next((f for f in files if not wanted or str(f[1].get("id")) == wanted), None)
            if chosen is None:
                listed = ", ".join(f"{h.get('name')} (file id: {h.get('id')})" for _, h in files)
                return f"Error: no file '{wanted}' on candidate {candidate_id}. Files: {listed}."
            kind, handle = chosen
            info, err = await ctx.call(client, "file.info", {"fileHandle": handle["handle"]})
            if err:
                return err
        url = str((info or {}).get("url") or "") if isinstance(info, dict) else ""
        if not url:
            return f"Error: Ashby returned no download URL for the {kind} of candidate {candidate_id}."
        name = str(handle.get("name") or f"ashby-{kind}-{handle.get('id', '')}")
        # file.info answers a signed storage URL: fetched without the API key.
        async with ctx.client() as storage:
            fetched = await fetch_file(
                storage,
                "get",
                url,
                provider=PROVIDER,
                action=f"download the {kind} of candidate {candidate_id}",
                max_bytes=MAX_FILE_BYTES,
                follow_redirects=True,
            )
        if isinstance(fetched, str):
            return f"{fetched}\nDownload URL: {url}"
        return await deliver_file(
            data=fetched.data,
            filename=name,
            content_type=file_content_type(fetched.content_type, name),
            source="Ashby",
            persist_file_fn=ctx.persist_file_fn,
            links=[("Profile", candidate.get("profileUrl")), ("Download URL", url)],
            details=[f"The {kind} of candidate {candidate_id}; the download URL is signed and short-lived."],
        )

    async def upload_ashby_candidate_file(
        candidate_id: str,
        file_id: str,
        as_resume: bool = False,
        file_name: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        piped = await read_pipe_file(file_context, file_id, max_bytes=MAX_FILE_BYTES, target="Ashby")
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, piped_name, content_type = piped
        file_id = file_id.strip()
        filename = (file_name or "").strip() or piped_name
        endpoint, part = ("candidate.uploadResume", "resume") if as_resume else ("candidate.uploadFile", "file")
        async with ctx.client() as client:
            _, err = await ctx.post(
                client,
                endpoint,
                {"candidateId": candidate_id},
                files={part: (filename, data, content_type)},
            )
        if err:
            return err
        what = "resume" if as_resume else "file"
        return uploaded_file_answer(
            f"Uploaded file {file_id} as the {what} of candidate {candidate_id}.",
            target=PROVIDER,
            filename=filename,
        )

    executor.register("download_ashby_candidate_file", download_ashby_candidate_file)
    executor.register("upload_ashby_candidate_file", upload_ashby_candidate_file)

    candidate_id = id_schema("candidate")
    return [
        definition(
            "download_ashby_candidate_file",
            "Download a candidate's resume (default) or one of their files into the "
            "conversation: a download card, its links and a file_id to forward to any tool "
            "that takes a file; its readable content comes with the result. "
            "get_ashby_candidate lists the resume and file ids. Include the returned "
            "[[FILE:...]] marker verbatim so the user gets a download card.",
            {
                "candidate_id": candidate_id,
                "file_id": {
                    "type": "string",
                    "description": "File id from get_ashby_candidate (default: the resume)",
                    "default": "",
                },
            },
            ["candidate_id"],
            1,
        ),
        definition(
            "upload_ashby_candidate_file",
            "Upload a file of this conversation to a candidate byte-for-byte, as their "
            "resume or as a candidate file: any file attached by the user, generated, or "
            "downloaded by another tool, by its file_id.",
            {
                "candidate_id": candidate_id,
                "file_id": {"type": "string", "description": "file_id of a file of this conversation"},
                "as_resume": {
                    "type": "boolean",
                    "description": "Upload as the candidate's resume (default: a candidate file)",
                    "default": False,
                },
                "file_name": {
                    "type": "string",
                    "description": "Optional file name (default: the file's own name)",
                    "default": "",
                },
            },
            ["candidate_id", "file_id"],
            1,
        ),
    ]
