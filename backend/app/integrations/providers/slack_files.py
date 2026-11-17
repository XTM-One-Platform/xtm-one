# SPDX-License-Identifier: Apache-2.0

"""Slack files for the Slack integration and the Slack bot.

* :func:`file_links` / :func:`message_files_suffix`: every file a tool meets
  is shown with its id, its permalink and its ``url_private_download``.
* :func:`download_file_bytes`: ``url_private_download`` needs the token as a
  bearer header, so the bytes are fetched server-side, only from a Slack
  file host (the token never leaves Slack).
* :func:`upload_file_bytes`: ``files.upload`` was retired by Slack on
  2025-11-12; uploads go through ``files.getUploadURLExternal``, a POST of the
  bytes to the returned URL and ``files.completeUploadExternal``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.egress import egress_client
from app.integrations.file_delivery import Link
from app.integrations.http import safe_request

SLACK_API = "https://slack.com/api"

#: Largest file the Slack tools download or upload.
MAX_SLACK_FILE_BYTES = 100 * 1024 * 1024

_SLACK_FILE_HOST_SUFFIXES = ("slack.com", "slack-gov.com")


def is_slack_file_host(url: str) -> bool:
    """True for an HTTPS URL on a Slack host, the only place the token is sent."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and any(
        host == suffix or host.endswith("." + suffix) for suffix in _SLACK_FILE_HOST_SUFFIXES
    )


def download_url(file: dict) -> str:
    return str(file.get("url_private_download") or file.get("url_private") or "")


def file_links(file: dict) -> list[Link]:
    return [("Web URL", file.get("permalink")), ("Download URL", download_url(file))]


def describe_file(file: dict) -> str:
    """One line for a Slack file: name, id, type, size and both links."""
    name = file.get("name") or file.get("title") or "untitled"
    kind = file.get("mimetype") or file.get("filetype") or "?"
    parts = [f"{name} (ID: {file.get('id', '?')}, {kind}, {file.get('size', 0)} bytes)"]
    if file.get("permalink"):
        parts.append(str(file["permalink"]))
    if url := download_url(file):
        parts.append(f"download: {url}")
    return " - ".join(parts)


def message_files_suffix(message: dict) -> str:
    """The files attached to a Slack message, one per line; ``""`` when none."""
    files = [f for f in message.get("files") or [] if isinstance(f, dict) and f.get("id")]
    if not files:
        return ""
    return "".join(f"\n    file: {describe_file(f)}" for f in files)


def _slack_error(data: dict) -> str | None:
    if not data.get("ok"):
        return f"Slack error: {data.get('error', 'unknown')}"
    return None


async def get_file_info(
    client: httpx.AsyncClient, headers: dict[str, str], provider: str, file_id: str
) -> tuple[dict | None, str | None]:
    resp, err = await safe_request(
        client,
        "get",
        f"{SLACK_API}/files.info",
        provider,
        "get file info",
        headers=headers,
        params={"file": file_id},
    )
    if err:
        return None, err
    data = resp.json()
    if err := _slack_error(data):
        return None, err
    return data.get("file") or {}, None


async def download_file_bytes(
    headers: dict[str, str], provider: str, file: dict
) -> tuple[bytes | None, str | None]:
    """The bytes of a Slack file (``files.info`` object), or the error."""
    url = download_url(file)
    if not url:
        return None, "Error: Slack gave no download URL for this file."
    if not is_slack_file_host(url):
        return None, (
            f"Error: refusing to download from '{urlparse(url).hostname or '?'}' - the "
            "Slack token is only sent to a Slack file host."
        )
    size = int(file.get("size") or 0)
    if size > MAX_SLACK_FILE_BYTES:
        return None, (
            f"Error: file is {round(size / 1024 / 1024, 1)} MB, over the "
            f"{MAX_SLACK_FILE_BYTES // 1024 // 1024} MB download limit."
        )
    # httpx drops the Authorization header on a redirect to another origin.
    async with egress_client(timeout=120, follow_redirects=True) as client:
        resp, err = await safe_request(
            client, "get", url, provider, "download file", headers=headers
        )
    if err:
        return None, err
    content = resp.content or b""
    served = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    expected = str(file.get("mimetype") or "").lower()
    if served == "text/html" and expected != "text/html":
        return None, (
            "Error: Slack answered with a sign-in page instead of the file - the "
            "token needs the files:read scope and access to the file."
        )
    if not content:
        return None, "Error: the downloaded Slack file is empty."
    if len(content) > MAX_SLACK_FILE_BYTES:
        return None, (
            f"Error: file is {round(len(content) / 1024 / 1024, 1)} MB, over the "
            f"{MAX_SLACK_FILE_BYTES // 1024 // 1024} MB download limit."
        )
    return content, None


async def upload_file_bytes(
    headers: dict[str, str],
    provider: str,
    *,
    data: bytes,
    filename: str,
    channels: list[str],
    title: str = "",
    initial_comment: str = "",
    thread_ts: str = "",
) -> tuple[dict | None, str | None]:
    """Upload *data* and share it in *channels*; the completed file object, or the error."""
    if thread_ts and len(channels) != 1:
        # Refused before any bytes are sent: an upload never completed is
        # left behind as a private orphan file.
        return None, "Error: thread_ts shares the file in a thread of exactly one channel - pass a single channel."
    async with egress_client(timeout=120) as client:
        resp, err = await safe_request(
            client,
            "post",
            f"{SLACK_API}/files.getUploadURLExternal",
            provider,
            "get upload URL",
            headers=headers,
            data={"filename": filename, "length": str(len(data))},
        )
        if err:
            return None, err
        ticket = resp.json()
        if err := _slack_error(ticket):
            return None, err
        upload_url = str(ticket.get("upload_url") or "")
        file_id = ticket.get("file_id")
        if not file_id or not is_slack_file_host(upload_url):
            return None, "Error: Slack did not return a usable upload URL."
        _, err = await safe_request(
            client,
            "post",
            upload_url,
            provider,
            "send file bytes",
            headers=headers,
            files={"file": (filename, data)},
        )
        if err:
            return None, err
        body: dict[str, Any] = {"files": [{"id": file_id, "title": title or filename}]}
        if len(channels) == 1:
            body["channel_id"] = channels[0]
        elif channels:
            body["channels"] = ",".join(channels)
        if initial_comment:
            body["initial_comment"] = initial_comment
        if thread_ts:
            body["thread_ts"] = thread_ts
        resp, err = await safe_request(
            client,
            "post",
            f"{SLACK_API}/files.completeUploadExternal",
            provider,
            "complete upload",
            headers={**headers, "Content-Type": "application/json; charset=utf-8"},
            json=body,
        )
        if err:
            return None, err
        done = resp.json()
        if err := _slack_error(done):
            return None, err
        completed = (done.get("files") or [{}])[0] or {}
        return {"id": file_id, **completed}, None


def split_channels(value: Any) -> list[str]:
    return list(dict.fromkeys(p.strip() for p in str(value or "").split(",") if p.strip()))
