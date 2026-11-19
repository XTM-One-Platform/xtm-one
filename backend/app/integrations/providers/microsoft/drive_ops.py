"""Drive file upkeep: rename, move, delete and export to PDF a OneDrive / SharePoint file.

Every tool addresses the item as the comment tools do: by ``url`` (a sharing
link or the browser URL) or by ``item_path`` relative to the drive root, with
``site_id`` for a SharePoint library. Each write is read back from Graph before
it is reported, and a Graph refusal (403, 423 locked, a checked-out file) is
answered in Graph's own words.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.integrations.file_content import keep_file, kept_file_line, kilobytes
from app.integrations.file_delivery import untrusted_file_facts
from app.integrations.http import AutoRefreshClient
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

from ._common import _BASE_URL, _MS_MAX_FILE_BYTES, _PROVIDER, MicrosoftContext
from ._drive import _encode_drive_filename, _encode_drive_path, _is_folder, _resolve_drive_url
from .sharepoint import _site_drive_base

#: What the upkeep tools read of a driveItem.
_ITEM_SELECT = "id,name,size,file,folder,eTag,webUrl,parentReference,lastModifiedDateTime,lastModifiedBy,publication"
#: Extensions Graph converts with ``/content?format=pdf``.
PDF_EXPORTABLE = frozenset(
    {
        "doc",
        "docx",
        "dot",
        "dotx",
        "odt",
        "rtf",
        "ppt",
        "pptx",
        "pps",
        "ppsx",
        "pot",
        "potx",
        "odp",
        "xls",
        "xlsx",
        "xlsm",
        "ods",
    }
)
_PDF_MIME = "application/pdf"
_SOURCE = "Microsoft drive"

_LOCATION_PROPS = {
    "url": {
        "type": "string",
        "default": "",
        "description": "The file's SharePoint / OneDrive link or browser URL (or give item_path)",
    },
    "item_path": {
        "type": "string",
        "default": "",
        "description": "Path from the drive or library root, e.g. 'Legal/Terms.docx' (or give url)",
    },
    "site_id": {
        "type": "string",
        "default": "",
        "description": "With item_path: the SharePoint site (id, 'hostname:/sites/name' or URL); empty = OneDrive",
    },
}


@dataclass(frozen=True)
class _Item:
    """A located driveItem: its drive, its own base URL and what Graph said of it."""

    drive_base: str
    item_base: str
    meta: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.meta.get("name") or "")

    @property
    def parent_id(self) -> str:
        return str((self.meta.get("parentReference") or {}).get("id") or "")


def _path_of(meta: dict[str, Any]) -> str:
    """The item's path in its drive as Graph words it (``/drive/root:/A/b.docx`` -> ``A/b.docx``)."""
    parent = str((meta.get("parentReference") or {}).get("path") or "")
    folder = parent.split("root:", 1)[1].strip("/") if "root:" in parent else ""
    name = str(meta.get("name") or "")
    return f"{folder}/{name}" if folder else name


def _modified_lines(meta: dict[str, Any]) -> list[str]:
    who = ((meta.get("lastModifiedBy") or {}).get("user") or {}).get("displayName") or "unknown"
    return [
        f"Path: {_path_of(meta)}",
        f"Last modified: {meta.get('lastModifiedDateTime') or 'unknown'}",
        f"Modified by: {who}",
    ]


def _facts(meta: dict[str, Any], *, with_path: bool = True) -> str:
    """The item's own words (name, path, links) in one quarantine block."""
    return untrusted_file_facts(
        source=_SOURCE,
        filename=str(meta.get("name") or ""),
        details=_modified_lines(meta) if with_path else (),
        links=[("Web URL", meta.get("webUrl"))],
    )


def _clean_name(value: str) -> tuple[str, str | None]:
    name = (value or "").strip()
    if not name:
        return "", "Error: give the new name."
    if "/" in name or "\\" in name:
        return "", "Error: a name holds no '/' or '\\'; to change the folder use move_microsoft_drive_file."
    return name, None


def register(ctx: MicrosoftContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the drive upkeep tools on *executor* and return their definitions."""

    async def _get_item(client: AutoRefreshClient, item_base: str) -> tuple[dict[str, Any] | None, str | None]:
        resp, err = await ctx.request(
            client, "get", item_base, _PROVIDER, "read the drive item", params={"$select": _ITEM_SELECT}
        )
        if err or resp is None:
            return None, err or "Error: Microsoft Graph returned nothing for the drive item."
        return resp.json() or {}, None

    async def _locate(
        client: AutoRefreshClient, url: str, item_path: str, site_id: str
    ) -> tuple[_Item | None, str | None]:
        link = (url or "").strip()
        path = (item_path or "").strip().strip("/")
        if bool(link) == bool(path):
            return None, (
                "Error: give the file by url (its SharePoint / OneDrive link) or by "
                "item_path (with site_id for a SharePoint library), not both."
            )
        if link:
            if not link.lower().startswith("https://"):
                return None, "Error: provide the full https:// SharePoint/OneDrive URL of the file."
            target, err = await _resolve_drive_url(ctx, client, link)
            if target is None:
                return None, err or "Error: could not resolve that URL to a drive item."
            located = target.item_base
        else:
            site = (site_id or "").strip()
            if site:
                drive, _by_address, err = await _site_drive_base(ctx, client, site)
                if err or drive is None:
                    return None, err
            else:
                drive = f"{_BASE_URL}/me/drive"
            located = f"{drive}/root:/{_encode_drive_path(path)}"
        meta, err = await _get_item(client, located)
        if err or meta is None:
            return None, err
        drive_id = (meta.get("parentReference") or {}).get("driveId")
        item_id = meta.get("id")
        if not drive_id or not item_id:
            return None, "Error: Microsoft Graph did not say which drive holds that item."
        drive_base = f"{_BASE_URL}/drives/{drive_id}"
        return _Item(drive_base, f"{drive_base}/items/{item_id}", meta), None

    async def _child(
        client: AutoRefreshClient, drive_base: str, folder_id: str, name: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """The item named *name* in folder *folder_id*, ``None`` when there is none."""
        resp, err = await ctx.request(
            client,
            "get",
            f"{drive_base}/items/{folder_id}:/{_encode_drive_filename(name)}",
            _PROVIDER,
            "check the name in the folder",
            params={"$select": "id,name"},
            expected_statuses={404},
        )
        if err:
            if resp is not None and resp.status_code == 404:
                return None, None
            return None, err
        if resp is None:
            return None, "Error: Microsoft Graph returned nothing for the folder check."
        return resp.json() or {}, None

    async def _patch(
        client: AutoRefreshClient, item: _Item, body: dict[str, Any], *, overwrite: bool, action: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        _resp, err = await ctx.request(
            client,
            "patch",
            item.item_base,
            _PROVIDER,
            action,
            params={"@microsoft.graph.conflictBehavior": "replace" if overwrite else "fail"},
            json=body,
        )
        if err:
            return None, err
        # Read back what Graph now holds, not what the PATCH echoed.
        return await _get_item(client, item.item_base)

    async def _taken(client: AutoRefreshClient, item: _Item, folder_id: str, name: str, overwrite: bool) -> str | None:
        """The refusal when *name* is taken in *folder_id* by another item and *overwrite* is off."""
        existing, err = await _child(client, item.drive_base, folder_id, name)
        if err:
            return err
        if existing and existing.get("id") != item.meta.get("id") and not overwrite:
            return (
                "Error: that folder already holds an item with the requested name, so nothing was "
                "changed. Pass overwrite=true to replace it, or choose another name."
            )
        return None

    async def rename_microsoft_drive_file(
        new_name: str, url: str = "", item_path: str = "", site_id: str = "", overwrite: bool = False
    ) -> str:
        """Rename a drive item in place."""
        name, err = _clean_name(new_name)
        if err:
            return err
        async with ctx.client() as client:
            item, err = await _locate(client, url, item_path, site_id)
            if err or item is None:
                return err or "Error: item not found."
            if item.name == name:
                return "The item already has that name; nothing was changed.\n" + _facts(item.meta)
            if err := await _taken(client, item, item.parent_id, name, overwrite):
                return err
            after, err = await _patch(client, item, {"name": name}, overwrite=overwrite, action="rename the drive item")
            if err or after is None:
                return err or "Error: the rename was not read back."
        if after.get("name") != name:
            return "Error: Microsoft Graph accepted the rename but the item still reads under its old name.\n" + _facts(
                after
            )
        return "Renamed the item; read back from Microsoft Graph under its new name.\n" + _facts(after)

    async def move_microsoft_drive_file(
        folder: str,
        url: str = "",
        item_path: str = "",
        site_id: str = "",
        new_name: str = "",
        overwrite: bool = False,
    ) -> str:
        """Move a drive item to another folder of the same drive or library."""
        name = ""
        if (new_name or "").strip():
            name, err = _clean_name(new_name)
            if err:
                return err
        async with ctx.client() as client:
            item, err = await _locate(client, url, item_path, site_id)
            if err or item is None:
                return err or "Error: item not found."
            target_path = (folder or "").strip().strip("/")
            target_url = (
                f"{item.drive_base}/root:/{_encode_drive_path(target_path)}"
                if target_path
                else f"{item.drive_base}/root"
            )
            resp, err = await ctx.request(
                client,
                "get",
                target_url,
                _PROVIDER,
                "open the destination folder",
                params={"$select": "id,name,folder"},
                expected_statuses={404},
            )
            if err:
                if resp is not None and resp.status_code == 404:
                    return (
                        f"Error: the drive holding the item has no folder '{target_path}', so nothing was moved. "
                        "The destination is a folder path of the same drive or library."
                    )
                return err
            if resp is None:
                return "Error: Microsoft Graph returned nothing for the destination folder."
            target = resp.json() or {}
            if not _is_folder(target):
                return f"Error: '{target_path}' is a file, not a folder, so nothing was moved."
            final_name = name or item.name
            if target.get("id") == item.parent_id and final_name == item.name:
                return "The item is already in that folder; nothing was changed.\n" + _facts(item.meta)
            if err := await _taken(client, item, str(target.get("id")), final_name, overwrite):
                return err
            body: dict[str, Any] = {"parentReference": {"id": target.get("id")}}
            if name:
                body["name"] = name
            after, err = await _patch(client, item, body, overwrite=overwrite, action="move the drive item")
            if err or after is None:
                return err or "Error: the move was not read back."
        if (after.get("parentReference") or {}).get("id") != target.get("id"):
            return "Error: Microsoft Graph accepted the move but the item still reads in its old folder.\n" + _facts(
                after
            )
        return f"Moved the item to folder '{target_path or '(root)'}'; read back from Microsoft Graph.\n" + _facts(
            after
        )

    async def delete_microsoft_drive_file(
        url: str = "", item_path: str = "", site_id: str = "", recursive: bool = False
    ) -> str:
        """Move a drive item to the recycle bin."""
        async with ctx.client() as client:
            item, err = await _locate(client, url, item_path, site_id)
            if err or item is None:
                return err or "Error: item not found."
            if _is_folder(item.meta) and not recursive:
                count = (item.meta.get("folder") or {}).get("childCount", "unknown")
                return (
                    f"Error: that item is a folder holding {count} item(s), so nothing was deleted. "
                    "Pass recursive=true to send the folder and everything in it to the recycle bin.\n"
                    + _facts(item.meta)
                )
            _resp, err = await ctx.request(client, "delete", item.item_base, _PROVIDER, "delete the drive item")
            if err:
                return err
            gone, _gone_err = await ctx.request(
                client,
                "get",
                item.item_base,
                _PROVIDER,
                "confirm the deletion",
                params={"$select": "id"},
                expected_statuses={404},
            )
        if gone is not None and gone.status_code != 404:
            return "Error: Microsoft Graph accepted the delete but the item still reads.\n" + _facts(item.meta)
        kind = "folder and its content" if _is_folder(item.meta) else "file"
        return (
            f"Moved the {kind} to the recycle bin (restorable from there); read back: the item no longer opens.\n"
            + _facts(item.meta)
        )

    async def convert_microsoft_drive_file_to_pdf(
        url: str = "",
        item_path: str = "",
        site_id: str = "",
        upload: bool = False,
        pdf_name: str = "",
        overwrite: bool = False,
        file_context: FileContext | None = None,
    ) -> ToolResult | str:
        """Convert an Office file to PDF with Graph and keep the PDF; optionally upload it beside the source."""
        async with ctx.client() as client:
            item, err = await _locate(client, url, item_path, site_id)
            if err or item is None:
                return err or "Error: item not found."
            if _is_folder(item.meta):
                return "Error: that item is a folder; only an Office file is exported to PDF."
            stem, dot, ext = item.name.rpartition(".")
            if not dot or ext.lower() not in PDF_EXPORTABLE:
                return (
                    "Error: Microsoft Graph converts only Word, PowerPoint and Excel files "
                    f"({', '.join(sorted(PDF_EXPORTABLE))}) to PDF.\n" + _facts(item.meta, with_path=False)
                )
            target_name = (pdf_name or "").strip() or f"{stem}.pdf"
            if not target_name.lower().endswith(".pdf"):
                target_name += ".pdf"
            if "/" in target_name or "\\" in target_name:
                return "Error: pdf_name is a file name, not a path; the PDF is written beside the source."
            resp, err = await ctx.request(
                client,
                "get",
                f"{item.item_base}/content",
                _PROVIDER,
                "export the file to PDF",
                params={"format": "pdf"},
                follow_redirects=True,
            )
            if err or resp is None:
                return err or "Error: Microsoft Graph returned no PDF for that file."
            data = resp.content or b""
            if not data.startswith(b"%PDF"):
                return "Error: Microsoft Graph returned no PDF for that file.\n" + _facts(item.meta, with_path=False)
            if len(data) > _MS_MAX_FILE_BYTES:
                return (
                    f"Error: the PDF is {kilobytes(len(data))}, over the {_MS_MAX_FILE_BYTES // 1024 // 1024} MB limit."
                )
            file_ref = await keep_file(ctx.persist_file_fn, file_context, target_name, data, _PDF_MIME)
            lines = [f"Exported the file to PDF ({kilobytes(len(data))}).", kept_file_line(file_ref)]
            uploaded: dict[str, Any] | None = None
            if upload:
                if err := await _taken(client, item, item.parent_id, target_name, overwrite):
                    return "\n".join([*lines, err.replace("nothing was changed", "the PDF was not uploaded")])
                put, err = await ctx.request(
                    client,
                    "put",
                    f"{item.drive_base}/items/{item.parent_id}:/{_encode_drive_filename(target_name)}:/content",
                    _PROVIDER,
                    "upload the PDF",
                    params={"@microsoft.graph.conflictBehavior": "replace" if overwrite else "fail"},
                    headers={"Content-Type": _PDF_MIME},
                    content=data,
                )
                if err or put is None:
                    return "\n".join([*lines, f"The PDF was not uploaded: {err or 'no answer from Microsoft Graph'}"])
                uploaded, err = await _get_item(client, f"{item.drive_base}/items/{(put.json() or {}).get('id')}")
                if err or uploaded is None:
                    return "\n".join([*lines, f"The PDF upload was not read back: {err}"])
                if int(uploaded.get("size") or 0) != len(data):
                    return "\n".join([*lines, "Error: the uploaded PDF reads back with another size than the export."])
                lines.append("Uploaded the PDF beside the source; read back from Microsoft Graph with the same size.")
        facts = _facts(uploaded) if uploaded else _facts(item.meta, with_path=False)
        return ToolResult(tool_call_id="", content="\n".join([*lines, facts]), files=[file_ref])

    executor.register("rename_microsoft_drive_file", rename_microsoft_drive_file)
    executor.register("move_microsoft_drive_file", move_microsoft_drive_file)
    executor.register("delete_microsoft_drive_file", delete_microsoft_drive_file)
    executor.register("convert_microsoft_drive_file_to_pdf", convert_microsoft_drive_file_to_pdf)

    overwrite_prop = {
        "type": "boolean",
        "default": False,
        "description": "Replace an item that already holds the name (default: refuse)",
    }
    return [
        ToolDefinition(
            name="rename_microsoft_drive_file",
            description=(
                "Rename a OneDrive / SharePoint file or folder in place, by url or item_path. "
                "Refuses a name already taken in its folder unless overwrite=true; read back."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "new_name": {"type": "string", "description": "The new name, extension included"},
                    **_LOCATION_PROPS,
                    "overwrite": overwrite_prop,
                },
                "required": ["new_name"],
            },
            source="integration:microsoft",
            priority=2,
        ),
        ToolDefinition(
            name="move_microsoft_drive_file",
            description=(
                "Move a OneDrive / SharePoint file or folder to another folder of the same drive "
                "or library, optionally renaming it. Refuses a taken name unless overwrite=true; read back."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "Destination folder path from the same drive or library root (empty = root)",
                    },
                    **_LOCATION_PROPS,
                    "new_name": {
                        "type": "string",
                        "default": "",
                        "description": "Optional new name at the destination",
                    },
                    "overwrite": overwrite_prop,
                },
                "required": ["folder"],
            },
            source="integration:microsoft",
            priority=2,
        ),
        ToolDefinition(
            name="delete_microsoft_drive_file",
            description=(
                "Move a OneDrive / SharePoint file to the recycle bin (restorable), by url or item_path; "
                "answers its name, path, last modified and modified by. A folder needs recursive=true."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_LOCATION_PROPS,
                    "recursive": {
                        "type": "boolean",
                        "default": False,
                        "description": "Required to delete a folder with everything in it",
                    },
                },
            },
            source="integration:microsoft",
            priority=2,
        ),
        ToolDefinition(
            name="convert_microsoft_drive_file_to_pdf",
            description=(
                "Export a Word, PowerPoint or Excel file of OneDrive / SharePoint to PDF (Graph conversion), "
                "kept as a conversation file; upload=true also writes the PDF beside the source, read back."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_LOCATION_PROPS,
                    "upload": {
                        "type": "boolean",
                        "default": False,
                        "description": "Also upload the PDF into the source's folder",
                    },
                    "pdf_name": {
                        "type": "string",
                        "default": "",
                        "description": "PDF file name (default: the source's name with .pdf)",
                    },
                    "overwrite": overwrite_prop,
                },
            },
            source="integration:microsoft",
            priority=2,
        ),
    ]
