"""Bounded Windows capture for DUNGEON-SCION-RUNTIME-002.

This records local source bytes and per-scope observations. It does not run the
Scion CLI and is not Scion runtime acceptance.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import subprocess
import sys
import tomllib


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.project_structure.libraries import discover_libraries
from tools.project_structure.projections import write_projections
from tools.project_structure.v03_cas import cas_verify
from tools.project_structure.v03_evidence import (
    create_evidence,
    evidence_applicability,
    manifest_path,
    seal_evidence,
    seal_path,
    verify_evidence,
)
from tools.project_structure.v03_manifest import DEFAULT_RESERVED
from tools.project_structure.v03_scan import scan_project_v03
from tools.project_structure.v03_snapshots import create_snapshot_v03
from tools.project_structure.v041_paths import atomic_bytes, publish_files
from tools.project_structure.validator import validate_snapshot


EPIC = "DUNGEON-SCION-RUNTIME-002"
PROJECT_ID = "dungeon.scion-runtime-002"
SNAPSHOT_ID = "windows-scion-runtime-002"
EVIDENCE_ID = "EV-DUNGEON-SCION-RUNTIME-002"
SCION_REVISION_RE = re.compile(r"c1[0-9a-f]{64}\Z")
MAX_STATE_BYTES = 1024 * 1024
FIXTURE_GIT_NAME = "Dungeon Scion Runtime Fixture"
FIXTURE_GIT_EMAIL = "fixture@invalid"


class CaptureError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _absolute_windows_path(value: str, label: str) -> Path:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CaptureError(f"INVALID_{label.upper()}_PATH")
    pure = PureWindowsPath(value)
    if (not pure.is_absolute() or not pure.drive or pure.drive.startswith("\\\\")
            or value.startswith(("\\\\", "//")) or ".." in pure.parts):
        raise CaptureError(f"INVALID_{label.upper()}_PATH")
    if any(part.endswith((".", " ")) or ":" in part for part in pure.parts[1:]):
        raise CaptureError(f"INVALID_{label.upper()}_PATH")
    path = Path(value)
    if not path.is_absolute():
        raise CaptureError(f"INVALID_{label.upper()}_PATH")
    return path


def _reject_reparse_chain(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if not os.path.lexists(current):
            break
        try:
            info = current.lstat()
        except OSError as exc:
            raise CaptureError("PATH_INSPECTION_FAILED") from exc
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise CaptureError("REPARSE_PATH_REJECTED")


def _physical_directory(path: Path) -> Path:
    _reject_reparse_chain(path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CaptureError("DIRECTORY_UNAVAILABLE") from exc
    if not resolved.is_dir():
        raise CaptureError("DIRECTORY_UNAVAILABLE")
    return resolved


def _within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(str(path)), os.path.normcase(str(root)))) == os.path.normcase(str(root))
    except ValueError:
        return False


def _existing_regular_file(root: Path, relative: str) -> Path:
    path = root / relative
    _reject_reparse_chain(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise CaptureError("SCION_CHECKOUT_INPUT_MISSING") from exc
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise CaptureError("SCION_CHECKOUT_INPUT_UNSAFE")
    return path


def _load_scion_state(root: Path, expected_revision: str) -> dict:
    state_path = _existing_regular_file(root, ".scion/state")
    if state_path.stat().st_size > MAX_STATE_BYTES:
        raise CaptureError("SCION_STATE_TOO_LARGE")
    try:
        state = tomllib.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise CaptureError("SCION_STATE_INVALID") from exc
    if not isinstance(state, dict) or not SCION_REVISION_RE.fullmatch(str(state.get("base", ""))):
        raise CaptureError("SCION_BASE_INVALID")
    if state["base"] != expected_revision:
        raise CaptureError("SCION_BASE_MISMATCH")
    if state.get("pending") is not None:
        raise CaptureError("SCION_PENDING_OPERATION")
    return state


@contextlib.contextmanager
def _windows_platform_observation_environment():
    """Reach ScionBackend's native-Windows gate without caller credentials.

    The reserved .invalid endpoint is process-local and is never contacted:
    ScionBackend returns UNSUPPORTED_PLATFORM before token lookup or execution.
    Remove all inherited Scion settings for the bounded operation and restore
    them exactly afterward.
    """
    prefixes = ("SCION_", "DUNGEON_SCION_")
    saved = {key: value for key, value in os.environ.items() if key.startswith(prefixes)}
    for key in saved:
        os.environ.pop(key, None)
    os.environ["DUNGEON_SCION_RUNTIME_ENDPOINT"] = "https://windows-capture.invalid"
    os.environ["DUNGEON_SCION_RUNTIME_TRANSPORT"] = "native"
    try:
        yield
    finally:
        for key in tuple(os.environ):
            if key.startswith(prefixes):
                os.environ.pop(key, None)
        os.environ.update(saved)


def _git(root: Path, *args: str, author: bool = False) -> str:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GIT_", "SCION_", "DUNGEON_SCION_"))}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    command = ["git", "--no-pager", "-C", str(root)]
    if author:
        command.extend(("-c", f"user.name={FIXTURE_GIT_NAME}",
                        "-c", f"user.email={FIXTURE_GIT_EMAIL}",
                        "-c", "user.useConfigOnly=true", "-c", "commit.gpgsign=false"))
    command.extend(args)
    try:
        result = subprocess.run(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=60, check=False, text=True, encoding="utf-8")
    except (OSError, subprocess.SubprocessError) as exc:
        raise CaptureError("FIXTURE_GIT_UNAVAILABLE") from exc
    if result.returncode:
        raise CaptureError("FIXTURE_GIT_FAILED")
    return result.stdout.strip()


def _manifest_bytes() -> bytes:
    manifest = {
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "modules": [{"id": "renderer", "path": "renderer"},
                    {"id": "storage", "path": "storage"}],
        "reserved_dirs": sorted(DEFAULT_RESERVED),
        "ignore": [],
        "docs": {},
        "evidence_store": {"type": "local-cas", "root": "../store"},
        # Declaring VersionScope "main" prevents the implicit main Git scope.
        "version_scopes": [
            {"id": "main", "backend": "SCION", "root": ".", "repository": ".",
             "mode": "MONOREPO", "endpoint_profile": "RUNTIME"},
            {"id": "storage", "backend": "GIT", "root": "storage", "repository": "storage",
             "mode": "REPOSITORY"},
        ],
    }
    return (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _settings_sklt() -> bytes:
    return ("schema_version:\n    1\n"
            "id:\n    renderer.settings\n"
            "object_id:\n    renderer.settings\n"
            "kind:\n    component\n"
            "responsibility:\n    bind the Scion renderer settings source to its declared history scope\n"
            "level:\n    L0\n"
            "git_scope:\n    main\n"
            "source:\n    path: renderer/settings.rs\n"
            "inputs:\n    renderer configuration\n"
            "outputs:\n    renderer settings\n"
            "depends_on:\n"
            "effects:\n    source bytes are captured independently in Dungeon CAS\n"
            "isolation:\n    BOUNDED\n"
            "storage_state:\n    LIVE\n"
            "retention:\n    PERMANENT\n"
            "state:\n    SKLT\n").encode("utf-8")


def _database_sklt() -> bytes:
    return ("schema_version:\n    1\n"
            "id:\n    storage.database\n"
            "object_id:\n    storage.database\n"
            "kind:\n    component\n"
            "responsibility:\n    provide a disposable nested Git source for cross-scope snapshot capture\n"
            "level:\n    L0\n"
            "git_scope:\n    storage\n"
            "source:\n    path: storage/database.rs\n"
            "inputs:\n    disposable fixture data\n"
            "outputs:\n    database fixture source\n"
            "depends_on:\n"
            "effects:\n    nested Git revision is captured independently from Scion\n"
            "isolation:\n    BOUNDED\n"
            "storage_state:\n    LIVE\n"
            "retention:\n    PERMANENT\n"
            "state:\n    SKLT\n").encode("utf-8")


def _preflight_outputs(root: Path, store: Path, output: Path, draft: Path) -> None:
    reserved = (
        root / ".project", root / "evidence", root / "snapshots", root / "storage",
        root / "renderer" / "settings.sklt", root / "renderer" / ".exp",
        root / "docs" / ".grade", store, output, draft,
    )
    for path in reserved:
        _reject_reparse_chain(path)
    if any(os.path.lexists(path) for path in reserved):
        raise CaptureError("EXISTING_MANAGED_OR_RECEIPT_OUTPUT")


def _safe_issue_codes(issues) -> list[str]:
    return sorted({str(getattr(issue, "code", "CAPTURE_ISSUE")) for issue in issues})


def _publish_receipt(artifacts_root: Path, output: Path, receipt: dict) -> None:
    encoded = (json.dumps(receipt, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    atomic_bytes(artifacts_root, output, encoded, exclusive=True)


def capture_fixture(fixture_root: str, scion_revision: str, output_path: str) -> tuple[dict, int]:
    if os.name != "nt":
        raise CaptureError("WINDOWS_ONLY")
    if not isinstance(scion_revision, str) or not SCION_REVISION_RE.fullmatch(scion_revision):
        raise CaptureError("INVALID_SCION_REVISION")

    artifact_boundary = _physical_directory(REPOSITORY_ROOT / "artifacts" / "scion-runtime-002")
    raw_root = _absolute_windows_path(fixture_root, "fixture")
    _reject_reparse_chain(raw_root)
    root = _physical_directory(raw_root)
    if not _within(root, artifact_boundary):
        raise CaptureError("FIXTURE_OUTSIDE_SCION_RUNTIME_ARTIFACTS")
    relative_root = root.relative_to(artifact_boundary)
    if len(relative_root.parts) < 2 or relative_root.parts[-1].casefold() != "project":
        raise CaptureError("FIXTURE_ROOT_MUST_BE_ATTEMPT_PROJECT")

    raw_output = _absolute_windows_path(output_path, "output")
    _reject_reparse_chain(raw_output)
    output_parent = _physical_directory(raw_output.parent)
    output = output_parent / raw_output.name
    if not _within(output, artifact_boundary) or _within(output, root):
        raise CaptureError("OUTPUT_OUTSIDE_RECEIPT_BOUNDARY")
    if output.suffix.lower() != ".json":
        raise CaptureError("OUTPUT_MUST_BE_JSON")
    draft = output.with_name(output.stem + ".evidence-draft.json")
    if os.path.lexists(output) or os.path.lexists(draft):
        raise CaptureError("RECEIPT_PATH_EXISTS")

    store = root.parent / "store"
    label = "NOTREAL" if any(part.startswith("windows-capture-NOTREAL-")
                              for part in relative_root.parts[:-1]) else "CALLER_PROVIDED"
    base_receipt = {
        "schema_version": 1,
        "epic": EPIC,
        "status": "BLOCKED",
        "fixture_label": label,
        "project_id": PROJECT_ID,
        "snapshot_id": SNAPSHOT_ID,
        "evidence_id": EVIDENCE_ID,
        "output": output.name,
        "created_files": [],
        "errors": [],
    }

    try:
        if (root.name.casefold() != "project"
                or not _within(_physical_directory(root.parent), artifact_boundary)):
            raise CaptureError("FIXTURE_PARENT_INVALID")
        state = _load_scion_state(root, scion_revision)
        _existing_regular_file(root, "renderer/settings.rs")
        _existing_regular_file(root, "renderer/frame.rs")
        _preflight_outputs(root, store, output, draft)

        output_files = {
            root / ".project" / "project.json": _manifest_bytes(),
            root / "renderer" / "settings.sklt": _settings_sklt(),
            root / "storage" / "database.sklt": _database_sklt(),
            root / "storage" / "database.rs": (
                b"// Disposable DUNGEON-SCION-RUNTIME-002 nested-Git fixture.\n"
                b"pub struct DatabaseFixture;\n"
            ),
        }
        publish_files(root, output_files)
        base_receipt["created_files"].extend((
            ".project/project.json", "renderer/settings.sklt",
            "storage/database.sklt", "storage/database.rs",
        ))

        snapshot, manifest, scan_issues = scan_project_v03(root)
        structural_issues = [issue for issue in scan_issues + validate_snapshot(snapshot, include_projections=False)
                             if getattr(issue, "severity", "ERROR") == "ERROR"]
        if structural_issues:
            raise CaptureError("MANAGED_FIXTURE_INVALID")
        scopes = {scope.scope_id: scope for scope in manifest.effective_version_scopes()}
        if (manifest.git_scopes or set(scopes) != {"main", "storage"}
                or scopes["main"].backend != "SCION" or scopes["main"].root != "."
                or scopes["main"].endpoint_profile != "RUNTIME"
                or scopes["storage"].backend != "GIT" or scopes["storage"].root != "storage"):
            raise CaptureError("VERSION_SCOPE_CONFIGURATION_MISMATCH")
        projection_issues = write_projections(snapshot)
        if projection_issues:
            raise CaptureError("PROJECTION_WRITE_FAILED")
        base_receipt["created_files"].extend(("renderer/.exp", "storage/.exp", "docs/.grade"))

        storage_root = root / "storage"
        _git(storage_root, "init", "--quiet")
        git_top = Path(_git(storage_root, "rev-parse", "--show-toplevel")).resolve(strict=True)
        if git_top != storage_root.resolve(strict=True):
            raise CaptureError("NESTED_GIT_ROOT_MISMATCH")
        _git(storage_root, "add", "--all")
        _git(storage_root, "commit", "--quiet", "--no-gpg-sign", "-m",
             f"{EPIC} disposable fixture", author=True)
        git_revision = _git(storage_root, "rev-parse", "--verify", "HEAD")
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", git_revision):
            raise CaptureError("NESTED_GIT_REVISION_INVALID")
        if _git(storage_root, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
            raise CaptureError("NESTED_GIT_NOT_CLEAN")

        snapshot, manifest, scan_issues = scan_project_v03(root)
        if any(getattr(issue, "severity", "ERROR") == "ERROR" for issue in scan_issues):
            raise CaptureError("MANAGED_FIXTURE_INVALID")
        with _windows_platform_observation_environment():
            snapshot_issues = create_snapshot_v03(
                snapshot, SNAPSHOT_ID, manifest, discover_libraries(root)
            )
        if snapshot_issues:
            raise CaptureError("SNAPSHOT_WRITE_FAILED")
        snapshot_manifest_path = root / "snapshots" / SNAPSHOT_ID / "manifest.json"
        snapshot_manifest = json.loads(snapshot_manifest_path.read_text(encoding="utf-8"))
        revisions = snapshot_manifest.get("revisions", {})
        scion_row, git_row = revisions.get("main", {}), revisions.get("storage", {})
        if (snapshot_manifest.get("revision_consistency") != "PER_SCOPE_OBSERVATIONS_NOT_ATOMIC"
                or set(revisions) != {"main", "storage"}
                or scion_row.get("backend") != "SCION" or scion_row.get("revision") != state["base"]
                or scion_row.get("scope_id") != "main" or scion_row.get("dirty_state") != "UNKNOWN"
                or scion_row.get("availability") != "UNSUPPORTED_PLATFORM"
                or git_row.get("backend") != "GIT" or git_row.get("scope_id") != "storage"
                or git_row.get("revision") != git_revision or git_row.get("availability") != "AVAILABLE"):
            raise CaptureError("SNAPSHOT_REVISION_ASSERTION_FAILED")
        base_receipt["created_files"].append(f"snapshots/{SNAPSHOT_ID}/manifest.json")
        base_receipt["snapshot"] = {
            "revision_consistency": snapshot_manifest["revision_consistency"],
            "scopes": revisions,
        }

        draft_data = {
            "id": EVIDENCE_ID,
            "task_id": EPIC,
            "scope": "PROTOTYPE",
            "objects": ["renderer.settings"],
            "structure": {"snapshot": SNAPSHOT_ID},
            "execution": {"provider": "dungeon_system", "type": "tool",
                          "id": "windows-scion-source-capture"},
            "source": {"revision": None, "files": ["renderer/settings.rs"]},
            "inputs": {},
            "artifacts": {},
            "result": {"status": "CAPTURED"},
        }
        atomic_bytes(artifact_boundary, draft,
                     (json.dumps(draft_data, indent=2) + "\n").encode("utf-8"), exclusive=True)
        base_receipt["created_files"].append(draft.name)
        with _windows_platform_observation_environment():
            evidence, create_issues = create_evidence(root, draft)
        if evidence is None:
            raise CaptureError("EVIDENCE_CREATE_FAILED")
        evidence_source = evidence.get("source", {}).get("files", [])
        if (len(evidence_source) != 1 or evidence_source[0].get("path") != "renderer/settings.rs"
                or evidence_source[0].get("sha256") != hashlib.sha256((root / "renderer/settings.rs").read_bytes()).hexdigest()
                or evidence_source[0].get("cas") != "sha256:" + evidence_source[0].get("sha256", "")):
            raise CaptureError("SOURCE_CAPTURE_ASSERTION_FAILED")
        valid, _, _ = cas_verify(root, evidence_source[0]["cas"])
        if not valid:
            raise CaptureError("SOURCE_CAS_VERIFY_FAILED")
        evidence_scopes = evidence.get("source_revision", {}).get("scopes", {})
        evidence_scion = evidence_scopes.get("main", {})
        if (set(evidence_scopes) != {"main"} or evidence_scion.get("revision") != state["base"]
                or evidence_scion.get("dirty_state") != "UNKNOWN"
                or evidence_scion.get("availability") != "UNSUPPORTED_PLATFORM"):
            raise CaptureError("EVIDENCE_REVISION_ASSERTION_FAILED")
        base_receipt["created_files"].append(f"evidence/manifests/{EVIDENCE_ID}.json")

        with _windows_platform_observation_environment():
            seal, seal_issues = seal_evidence(root, EVIDENCE_ID)
            verified = verify_evidence(root, EVIDENCE_ID)
            proof = evidence_applicability(
                root, EVIDENCE_ID, object_id="renderer.settings", scope="PROTOTYPE",
                task_id=EPIC, source_paths=["renderer/settings.rs"],
            )
        proof_codes = sorted({item.get("code", "") for item in proof.get("issues", [])})
        if proof.get("proof_state") == "VERIFIED" or "VCS_PROOF_ADAPTER_UNSUPPORTED" not in proof_codes:
            raise CaptureError("PROOF_FAIL_CLOSED_ASSERTION_FAILED")
        if verified.get("integrity_state") not in ("VERIFIED", "SEALED"):
            raise CaptureError("EVIDENCE_INTEGRITY_VERIFY_FAILED")
        if verified.get("proof_state") == "VERIFIED":
            raise CaptureError("PROOF_FAIL_CLOSED_ASSERTION_FAILED")
        if seal is not None:
            base_receipt["created_files"].append(f"evidence/seals/{EVIDENCE_ID}.json")

        manifest_bytes = manifest_path(root, EVIDENCE_ID).read_bytes()
        seal_bytes = seal_path(root, EVIDENCE_ID).read_bytes() if seal is not None else None
        base_receipt.update({
            "status": "CAPTURED" if seal is not None else "CAPTURED_UNSEALED",
            "nested_git_commit": git_revision,
            "source": {
                "path": evidence_source[0]["path"],
                "sha256": evidence_source[0]["sha256"],
                "cas": evidence_source[0]["cas"],
            },
            "evidence": {
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "seal_sha256": hashlib.sha256(seal_bytes).hexdigest() if seal_bytes else None,
                "integrity_state": verified["integrity_state"],
            },
            "proof": {
                "state": proof["proof_state"],
                "verified": False,
                "issue_codes": proof_codes,
            },
            "errors": ([] if seal is not None else [{"code": "EVIDENCE_SEAL_BLOCKED"},
                                                        *[{"code": code} for code in _safe_issue_codes(seal_issues)]]),
        })
        _publish_receipt(artifact_boundary, output, base_receipt)
        return base_receipt, 0 if seal is not None else 2
    except CaptureError as exc:
        base_receipt["errors"].append({"code": exc.code})
    except Exception as exc:  # Keep unexpected diagnostics and environment values out of the receipt.
        base_receipt["errors"].append({"code": "CAPTURE_FAILED", "error_type": type(exc).__name__})

    try:
        _publish_receipt(artifact_boundary, output, base_receipt)
    except Exception:
        pass
    return base_receipt, 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-root", required=True, help="absolute Windows Scion checkout under artifacts/scion-runtime-002")
    parser.add_argument("--scion-revision", required=True, help="actual c1 plus 64 lowercase hexadecimal digits")
    parser.add_argument("--output", required=True, help="new absolute JSON receipt path under artifacts/scion-runtime-002")
    args = parser.parse_args(argv)
    try:
        receipt, code = capture_fixture(args.fixture_root, args.scion_revision, args.output)
    except CaptureError as exc:
        receipt, code = {"schema_version": 1, "epic": EPIC, "status": "BLOCKED",
                         "errors": [{"code": exc.code}]}, 2
    print(json.dumps(receipt, indent=2, ensure_ascii=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
