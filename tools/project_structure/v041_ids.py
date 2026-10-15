"""Shared grammar and safe metadata keys for Dungeon logical object IDs.

Object IDs are logical identities. In particular, a slash in a declared ID
separates SKLT declaration scopes; it does not make the ID a filesystem path.
Call :func:`metadata_key` when an ID must identify a metadata filename.
"""

from __future__ import annotations

import hashlib
import re


MAX_OBJECT_ID_LENGTH = 1024

# Preserve the established canonical identity grammar. A colon is valid in a
# logical ID; filesystem output paths are validated separately and IDs are
# mapped through metadata_key instead of being used as path components.
_LEGACY_OBJECT_ID = r"[A-Za-z0-9][A-Za-z0-9_.:-]*"

# These name characters mirror DECLARATION_RE in v04_sklt.py.
_DECLARATION_NAME = r"[A-Za-z_][A-Za-z0-9_.:-]*"
_ANY_DECLARATION = rf"(?:project|module|capability|command|action):{_DECLARATION_NAME}"
_SCOPE_DECLARATION = rf"(?:project|module):{_DECLARATION_NAME}"
_NON_SCOPE_ROOT = rf"(?:capability|command|action):{_DECLARATION_NAME}"

# v04_sklt only nests declarations under project/module scopes. A top-level
# capability/command/action is valid, but cannot have a nested child ID.
_DECLARED_OBJECT_ID = (
    rf"declared:(?:{_NON_SCOPE_ROOT}|"
    rf"{_SCOPE_DECLARATION}(?:/{_SCOPE_DECLARATION})*(?:/{_ANY_DECLARATION})?)"
)

OBJECT_ID_RE = re.compile(
    rf"(?=.{{1,{MAX_OBJECT_ID_LENGTH}}}\Z)(?:{_LEGACY_OBJECT_ID}|{_DECLARED_OBJECT_ID})\Z"
)
_SHA256_RE = re.compile(r"[0-9A-Fa-f]{64}\Z")
_UNSAFE_FILENAME_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


def valid_object_id(value: object) -> bool:
    """Return whether *value* is a bounded stable ID, not a path.

    The slash form is accepted only for the typed declaration namespace
    produced by ``v04_sklt.parse_declarations``. Absolute POSIX/drive/UNC paths,
    traversal, backslashes, controls, and empty namespace segments are outside
    the grammar. A colon in a canonical ID remains ordinary identity text;
    output-path safety belongs to the writer, not this logical-ID validator.
    """

    return isinstance(value, str) and OBJECT_ID_RE.fullmatch(value) is not None


def metadata_key(object_id: str, object_hash: str | None = None) -> str:
    """Return one filesystem-safe metadata filename component for an ID.

    The readable prefix is only a hint; a full SHA-256 of the ID guarantees
    that different IDs with the same sanitized prefix retain distinct keys.
    An optional SHA-256 *object_hash* is appended as a second discriminator.
    The returned value is a single component and never contains ID separators.
    """

    if not valid_object_id(object_id):
        raise ValueError("object_id is not a valid logical object ID")
    if object_hash is not None and (
        not isinstance(object_hash, str) or _SHA256_RE.fullmatch(object_hash) is None
    ):
        raise ValueError("object_hash must be a 64-character SHA-256 hex digest")

    readable = _UNSAFE_FILENAME_CHARS_RE.sub("-", object_id).strip(".-_")
    readable = (readable or "object")[:48].rstrip(".-_") or "object"
    identity_hash = hashlib.sha256(object_id.encode("utf-8")).hexdigest()
    key = f"{readable}-{identity_hash}"
    if object_hash is not None:
        key += f"-{object_hash.lower()}"
    return key
