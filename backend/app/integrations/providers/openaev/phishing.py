# SPDX-License-Identifier: Apache-2.0

"""OpenAEV phishing component tools.

``manage_openaev_phishing`` drives the two admin CRUD surfaces that back the
Components > Phishing screens:

* **Landing pages** (``/api/phishing/landing-pages``) - reusable, publicly
  served credential-capture / lure pages. Creating one auto-synthesizes a
  ``openaev_phishing`` InjectorContract (contract id == landing page id, label
  ``Phishing: <name>``) so a phishing campaign is just a normal inject targeting
  a Team/Player - discover it through ``search_openaev_injector_contracts``.
* **Email templates** (``/api/phishing/email-templates``) - reusable lure emails
  (subject/body, optional sender override, tracking-pixel toggle).

Both surfaces are ADMIN-only on OpenAEV (``isUserHasAccess`` = ``isAdmin``) and
gated by the phishing capabilities: read/search needs ``ACCESS_PHISHING``,
create/update/duplicate needs ``MANAGE_PHISHING``, delete needs
``DELETE_PHISHING``. The plain (non-tenant) routes are used; RBAC is enforced by
OpenAEV per calling user, so a caller without the capability gets a clean 403.

Both PUT endpoints are FULL-REPLACE (the controller applies EVERY input field,
nulling anything omitted), so ``update`` reads the current entity first and
merges the caller's changes over it - a partial update never wipes the HTML/CSS
or subject/body it did not touch.
"""

from __future__ import annotations

import logging

from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _api, _search_body

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"

_LANDING_URI = "/api/phishing/landing-pages"
_EMAIL_URI = "/api/phishing/email-templates"

#: Per-resource contract: REST path, id/name JSON keys, the writable field map
#: (tool param -> OpenAEV JSON key), and the fields mandatory on create. The
#: writable keys are identical between the entity output and the create/update
#: input, so ``update`` can round-trip a fetched entity straight back as the
#: full-replace body.
_RESOURCES: dict[str, dict] = {
    "landing_page": {
        "path": _LANDING_URI,
        "id_key": "phishing_landing_page_id",
        "name_key": "phishing_landing_page_name",
        "label": "phishing landing page",
        "writable": {
            "name": "phishing_landing_page_name",
            "description": "phishing_landing_page_description",
            "html": "phishing_landing_page_html",
            "css": "phishing_landing_page_css",
            "capture_submitted_data": "phishing_landing_page_capture_submitted_data",
            "capture_passwords": "phishing_landing_page_capture_passwords",
            "redirect_url": "phishing_landing_page_redirect_url",
            "primary_color_dark": "phishing_landing_page_primary_color_dark",
            "primary_color_light": "phishing_landing_page_primary_color_light",
            "custom_domain_id": "phishing_landing_page_custom_domain",
        },
        "required_create": ["name"],
    },
    "email_template": {
        "path": _EMAIL_URI,
        "id_key": "phishing_email_template_id",
        "name_key": "phishing_email_template_name",
        "label": "phishing email template",
        "writable": {
            "name": "phishing_email_template_name",
            "description": "phishing_email_template_description",
            "subject": "phishing_email_template_subject",
            "html_body": "phishing_email_template_html_body",
            "text_body": "phishing_email_template_text_body",
            "from_name": "phishing_email_template_from_name",
            "from_email": "phishing_email_template_from_email",
            "add_tracking_pixel": "phishing_email_template_add_tracking_pixel",
        },
        "required_create": ["name", "subject"],
    },
}


def _fmt_landing_page(d: dict) -> str:
    lines = [f"[PhishingLandingPage] {d.get('phishing_landing_page_name', '?')}"]
    lines.append(f"  id: {d.get('phishing_landing_page_id', 'N/A')}")
    if d.get("phishing_landing_page_description"):
        lines.append(f"  Description: {d['phishing_landing_page_description']}")
    lines.append(
        "  Captures: submitted_data="
        f"{bool(d.get('phishing_landing_page_capture_submitted_data'))}, "
        f"passwords={bool(d.get('phishing_landing_page_capture_passwords'))}"
    )
    if d.get("phishing_landing_page_redirect_url"):
        lines.append(f"  Redirect URL: {d['phishing_landing_page_redirect_url']}")
    if d.get("phishing_landing_page_custom_domain"):
        lines.append(f"  Custom domain: {d['phishing_landing_page_custom_domain']}")
    # The auto-synthesized phishing InjectorContract shares this id; surface the
    # handoff so the agent can wire a human phishing step against it.
    lines.append(
        "  Phishing InjectorContract id (use as a human inject target): "
        f"{d.get('phishing_landing_page_id', 'N/A')}"
    )
    return "\n".join(lines)


def _fmt_email_template(d: dict) -> str:
    lines = [f"[PhishingEmailTemplate] {d.get('phishing_email_template_name', '?')}"]
    lines.append(f"  id: {d.get('phishing_email_template_id', 'N/A')}")
    if d.get("phishing_email_template_description"):
        lines.append(f"  Description: {d['phishing_email_template_description']}")
    if d.get("phishing_email_template_subject"):
        lines.append(f"  Subject: {d['phishing_email_template_subject']}")
    from_name = d.get("phishing_email_template_from_name")
    from_email = d.get("phishing_email_template_from_email")
    if from_name and from_email:
        lines.append(f"  From: {from_name} <{from_email}>")
    elif from_name or from_email:
        lines.append(f"  From: {from_name or from_email}")
    lines.append(
        f"  Tracking pixel: {bool(d.get('phishing_email_template_add_tracking_pixel'))}"
    )
    return "\n".join(lines)


_FORMATTERS = {"landing_page": _fmt_landing_page, "email_template": _fmt_email_template}


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def manage_openaev_phishing(
        action: str = "",
        resource: str = "landing_page",
        resource_id: str = "",
        name: str = "",
        description: str = "",
        html: str = "",
        css: str = "",
        capture_submitted_data: bool | None = None,
        capture_passwords: bool | None = None,
        redirect_url: str = "",
        primary_color_dark: str = "",
        primary_color_light: str = "",
        custom_domain_id: str = "",
        subject: str = "",
        html_body: str = "",
        text_body: str = "",
        from_name: str = "",
        from_email: str = "",
        add_tracking_pixel: bool | None = None,
        search: str = "",
        limit: int = 30,
    ) -> str:
        action = (action or "").strip().lower()
        resource = (resource or "").strip().lower()
        spec = _RESOURCES.get(resource)
        if spec is None:
            return (
                "resource must be one of: landing_page, email_template "
                f"(got '{resource}')."
            )
        path = spec["path"]
        label = spec["label"]
        fmt = _FORMATTERS[resource]

        # Collect only the writable fields the caller actually set. Empty strings
        # are treated as "not provided" (a full clear is a rare need and would
        # otherwise make every optional string a footgun); booleans are provided
        # only when not None, so omitting one lets the OpenAEV default stand.
        supplied_values = {
            "name": name,
            "description": description,
            "html": html,
            "css": css,
            "capture_submitted_data": capture_submitted_data,
            "capture_passwords": capture_passwords,
            "redirect_url": redirect_url,
            "primary_color_dark": primary_color_dark,
            "primary_color_light": primary_color_light,
            "custom_domain_id": custom_domain_id,
            "subject": subject,
            "html_body": html_body,
            "text_body": text_body,
            "from_name": from_name,
            "from_email": from_email,
            "add_tracking_pixel": add_tracking_pixel,
        }
        provided: dict[str, object] = {}
        for param, json_key in spec["writable"].items():
            value = supplied_values.get(param)
            if isinstance(value, bool) or (isinstance(value, str) and value.strip()):
                provided[json_key] = value

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", f"{path}/search", f"search {label}s",
                json=_search_body(size=min(limit, 100), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            total = data.get("totalElements") if isinstance(data, dict) else None
            return format_entity_list(
                items,
                fmt,
                noun=label,
                total=total,
                empty_msg=f"No {label}s found{' for ' + repr(search) if search else ''}.",
            )

        if action == "get":
            if not resource_id:
                return "resource_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"{path}/{resource_id}", f"get {label}",
            )
            if err:
                return err
            data = resp.json()
            return fmt(data) if data else f"{label} '{resource_id}' not found."

        if action == "create":
            missing = [f for f in spec["required_create"] if not provided.get(spec["writable"][f])]
            if missing:
                return f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} required to create a {label}."
            resp, err = await _api(
                base_url, headers, "post", path, f"create {label}", json=provided,
            )
            if err:
                return err
            data = resp.json()
            return f"Created {label}.\n" + fmt(data)

        if action == "update":
            if not resource_id:
                return "resource_id is required for action 'update'."
            if not provided:
                return (
                    f"Provide at least one field to update on the {label} "
                    f"(e.g. name, description, {'html/css' if resource == 'landing_page' else 'subject/html_body'})."
                )
            # Full-replace PUT: fetch current, merge the caller's changes over the
            # existing writable fields, so untouched fields are preserved.
            current_resp, err = await _api(
                base_url, headers, "get", f"{path}/{resource_id}", f"get {label}",
            )
            if err:
                return err
            current = current_resp.json() or {}
            body = {
                json_key: current.get(json_key)
                for json_key in spec["writable"].values()
                if current.get(json_key) is not None
            }
            body.update(provided)
            resp, err = await _api(
                base_url, headers, "put", f"{path}/{resource_id}", f"update {label}",
                json=body,
            )
            if err:
                return err
            data = resp.json()
            return f"Updated {label}.\n" + fmt(data)

        if action == "duplicate":
            if not resource_id:
                return "resource_id is required for action 'duplicate'."
            resp, err = await _api(
                base_url, headers, "post", f"{path}/{resource_id}/duplicate",
                f"duplicate {label}",
            )
            if err:
                return err
            data = resp.json()
            return f"Duplicated {label}.\n" + fmt(data)

        if action == "delete":
            if not resource_id:
                return "resource_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"{path}/{resource_id}", f"delete {label}",
            )
            if err:
                return err
            return f"Deleted {label} {resource_id}."

        return (
            "action must be one of: list, get, create, update, duplicate, delete "
            f"(got '{action}')."
        )

    executor.register("manage_openaev_phishing", manage_openaev_phishing)

    return [
        ToolDefinition(
            name="manage_openaev_phishing",
            priority=2,
            description=(
                "Manage OpenAEV phishing components (admin + phishing capability "
                "required). resource='landing_page' manages reusable credential-capture "
                "/ lure pages; resource='email_template' manages reusable lure emails. "
                "Actions: 'list' (search), 'get' (resource_id), 'create', 'update' "
                "(resource_id + changed fields; a full-replace PUT is merged over the "
                "current entity so untouched fields are kept), 'duplicate' (resource_id), "
                "'delete' (resource_id). Creating a landing page auto-creates a phishing "
                "InjectorContract whose id equals the landing page id - use it as the "
                "target of a human phishing inject (find it via "
                "search_openaev_injector_contracts)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get", "create", "update", "duplicate", "delete"],
                    },
                    "resource": {
                        "type": "string",
                        "enum": ["landing_page", "email_template"],
                        "default": "landing_page",
                    },
                    "resource_id": {
                        "type": "string",
                        "description": "Landing page / email template id (get/update/duplicate/delete).",
                    },
                    "name": {"type": "string", "description": "Name (required on create)."},
                    "description": {"type": "string"},
                    "html": {"type": "string", "description": "landing_page: page HTML."},
                    "css": {"type": "string", "description": "landing_page: page CSS."},
                    "capture_submitted_data": {
                        "type": "boolean",
                        "description": "landing_page: capture posted form data as findings (default true).",
                    },
                    "capture_passwords": {
                        "type": "boolean",
                        "description": "landing_page: capture submitted passwords (default true).",
                    },
                    "redirect_url": {
                        "type": "string",
                        "description": "landing_page: URL to redirect the victim to after submit.",
                    },
                    "primary_color_dark": {"type": "string", "description": "landing_page: dark-theme accent hex."},
                    "primary_color_light": {"type": "string", "description": "landing_page: light-theme accent hex."},
                    "custom_domain_id": {
                        "type": "string",
                        "description": "landing_page: id of a verified custom domain to serve the page on.",
                    },
                    "subject": {"type": "string", "description": "email_template: subject (required on create)."},
                    "html_body": {"type": "string", "description": "email_template: HTML body."},
                    "text_body": {"type": "string", "description": "email_template: plain-text body."},
                    "from_name": {"type": "string", "description": "email_template: sender display name override."},
                    "from_email": {"type": "string", "description": "email_template: sender email override."},
                    "add_tracking_pixel": {
                        "type": "boolean",
                        "description": "email_template: embed an open-tracking pixel (default true).",
                    },
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 30},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
