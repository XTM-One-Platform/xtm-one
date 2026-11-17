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

"""STIX_BUNDLE — the single shared STIX bundle-envelope structural check.

``stix.envelope_validity`` and the entity/relationship metrics all resolve a bundle
through :func:`extract_bundle_objects`, so a value envelope validity rejects can never be
scored by them.

The check is **shallow**: a JSON object with ``type == "bundle"`` and an ``objects`` list
of JSON objects carrying a string ``type`` and ``id``. It MUST NOT be presented as
complete STIX 2.1 validity, which the ``StixValidator`` protocol checks.

Failures raise :class:`BundleStructureError`, which callers map per side: ``scored``/0 for
a prediction, ``ConfigurationError`` for a reference (corrupt ground truth is caller
misconfiguration, never a model failure).
"""

from collections.abc import Mapping
from dataclasses import dataclass


class BundleStructureError(Exception):
    """A value is not a well-formed STIX bundle envelope.

    Carries the structured ``locations`` (JSON Pointers) needed to build a ``MetricError``.
    """

    def __init__(self, message: str, locations: list[str]) -> None:
        super().__init__(message)
        self.message = message
        self.locations = locations


@dataclass(frozen=True, slots=True)
class StixObject:
    """A structurally validated STIX object.

    ``type`` and ``id`` are guaranteed strings. ``data`` is the original mapping.
    """

    index: int
    type: str
    id: str
    data: Mapping[str, object]

    @property
    def location(self) -> str:
        """The JSON Pointer of this object within its bundle.

        Returns:
            A pointer such as ``/objects/3``.
        """
        return f"/objects/{self.index}"


def extract_bundle_objects(bundle: object) -> list[StixObject]:
    """Validate a STIX bundle envelope and return its structurally checked objects.

    Args:
        bundle: The candidate bundle (typically already canonicalized).

    Returns:
        The bundle's objects, each with a guaranteed string ``type`` and ``id``.

    Raises:
        BundleStructureError: If the value is not a JSON object, is not typed
            ``"bundle"``, has a non-list ``objects``, or holds an object that is not a
            mapping or lacks a string ``type``/``id``.
    """
    if not isinstance(bundle, Mapping):
        raise BundleStructureError("STIX bundle must be a JSON object", [])
    if bundle.get("type") != "bundle":
        raise BundleStructureError("STIX bundle 'type' must be 'bundle'", ["/type"])
    objects = bundle.get("objects")
    if not isinstance(objects, list):
        raise BundleStructureError("STIX bundle 'objects' must be a list", ["/objects"])

    result: list[StixObject] = []
    for index, obj in enumerate(objects):
        location = f"/objects/{index}"
        if not isinstance(obj, Mapping):
            raise BundleStructureError("STIX object must be a JSON object", [location])
        object_type = obj.get("type")
        if not isinstance(object_type, str):
            raise BundleStructureError(
                "STIX object must have a string 'type'", [f"{location}/type"]
            )
        object_id = obj.get("id")
        if not isinstance(object_id, str):
            raise BundleStructureError(
                "STIX object must have a string 'id'", [f"{location}/id"]
            )
        result.append(
            StixObject(index=index, type=object_type, id=object_id, data=obj)
        )
    return result


__all__ = ["BundleStructureError", "StixObject", "extract_bundle_objects"]
