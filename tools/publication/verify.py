"""Verify tests, the source plugin, and actual Git-index bytes before publishing.

No remote writes. Receipts are local artifacts, never release attachments.
Secret findings report rule/path/line only, never matching values.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "tools/codex-plugin/dungeon-system"
sys.path.insert(0, str(ROOT))
MAX_FILE_BYTES = 1_048_576
SKILLS = {"dungeon-adoption", "dungeon-evidence", "dungeon-navigate",
          "dungeon-review", "dungeon-structure", "dungeon-task-trace"}
FORBIDDEN_PREFIXES = ("artifacts/", "snapshots/", "evidence/", "DungeonStore/",
                      "tools/codex-plugin/backups/", "tools/hardening_v041/",
                      ".git/", ".codex/", ".aws/", ".ssh/", ".venv/", "target/", "build/", "dist/")
PUBLIC_AUDITS = {"docs/audits/README.md", "docs/audits/public-hardening-v041.md"}
PRIVATE_CHRONICLES = {"docs/cronica/0007-plugin-adoption-v04.md", "docs/cronica/0008-plugin-hardening-v041.md"}
DERIVED = {".project/objects.json", ".project/state.json", "docs/.grade"}
BINARY_SUFFIXES = {".exe", ".dll", ".pyd", ".pdb", ".obj", ".o", ".so", ".a", ".lib",
                   ".pyc", ".dpack", ".zip", ".7z", ".tar", ".gz", ".bin"}
RULES = {
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "openai-key": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "user-home-path": re.compile(r"(?i)(?:[a-z]:[\\/]+Users[\\/]+|/(?:home|Users)/[a-z0-9_.-]+/)"),
    "credential-url": re.compile(r"(?i)https?://[^\s/@:]+:[^\s/@]+@"),
}
ASSIGNMENT = re.compile(r'''(?im)\b(?:password|api[_-]?key|client[_-]?secret|access[_-]?token|secret|token)["']?\s*[=:]\s*["']([^\s"']{12,})["']''')


def path_issues(relative: str) -> list[str]:
    path = PurePosixPath(relative)
    issues = []
    lower = relative.casefold()
    components = tuple(part.casefold() for part in path.parts)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative or any(ord(c) < 32 for c in relative):
        issues.append("unsafe-relative-path")
    devices = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if any(part.endswith((".", " ")) or part.split(".")[0].upper() in devices for part in path.parts):
        issues.append("unsafe-windows-path-component")
    private_dirs = {"artifacts", "snapshots", "evidence", "dungeonstore", "__pycache__", ".git", ".codex", ".aws",
                    ".ssh", ".venv", "venv", "node_modules", "target", "build", "dist", "backups", "hardening_v041"}
    if lower.startswith(tuple(prefix.casefold() for prefix in FORBIDDEN_PREFIXES)) or any(part in private_dirs for part in components[:-1]):
        issues.append("local-environment-or-private-artifact")
    if lower.startswith("docs/audits/") and lower not in {path.casefold() for path in PUBLIC_AUDITS}:
        issues.append("private-audit")
    if lower in {path.casefold() for path in PRIVATE_CHRONICLES | DERIVED} or path.name.casefold() == ".exp":
        issues.append("private-or-derived")
    if path.suffix.lower() in BINARY_SUFFIXES:
        issues.append("binary-or-pack")
    if path.name == ".env" or path.name.startswith(".env.") and path.name != ".env.example":
        issues.append("local-environment")
    if path.suffix.lower() in {".key", ".pem"} or path.name.lower() in {"credentials.json", "secrets.json", ".pypirc"}:
        issues.append("credential-file")
    if lower.startswith(".agents/") and lower != ".agents/plugins/marketplace.json":
        issues.append("private-agent-configuration")
    return issues


def scan_content(relative: str, content: bytes) -> list[dict]:
    findings = [{"path": relative, "rule": rule} for rule in path_issues(relative)]
    if len(content) > MAX_FILE_BYTES:
        findings.append({"path": relative, "rule": "file-over-1-MiB"})
    try:
        text = content.decode("utf-8-sig")
        if "\x00" in text:
            raise ValueError("binary NUL")
    except (UnicodeError, ValueError):
        return findings + [{"path": relative, "rule": "non-text-content"}]
    for rule, pattern in RULES.items():
        findings += [{"path": relative, "rule": rule, "line": text.count("\n", 0, match.start()) + 1}
                     for match in pattern.finditer(text)]
    for match in ASSIGNMENT.finditer(text):
        value = match.group(1).lower()
        if not any(marker in value for marker in ("example", "placeholder", "dummy", "your_", "test_", "${", "<", "*")):
            findings.append({"path": relative, "rule": "credential-assignment", "line": text.count("\n", 0, match.start()) + 1})
    return findings


def git(*args, input=None):
    result = subprocess.run(["git", *args], cwd=ROOT, input=input, capture_output=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed (exit {result.returncode}); inspect locally without copying credentials")
    return result.stdout


def index_blobs() -> list[tuple[str, str, bytes]]:
    entries = []
    for record in git("ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        mode, oid, stage = metadata.decode("ascii").split()
        if stage != "0" or mode not in {"100644", "100755"} or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
            raise ValueError("index contains an unmerged entry, link, gitlink or unsupported mode")
        entries.append((path.decode("utf-8"), oid))
    if not entries:
        raise ValueError("index is empty")
    if len({path.casefold() for path, _ in entries}) != len(entries):
        raise ValueError("index contains case-colliding paths")
    batch_input = b"".join(oid.encode() + b"\n" for _, oid in entries)
    sizes = git("cat-file", "--batch-check", input=batch_input).splitlines()
    if len(sizes) != len(entries):
        raise ValueError("unexpected Git blob size inventory")
    for (path, oid), raw in zip(entries, sizes):
        fields = raw.decode("ascii").split()
        if len(fields) != 3 or fields[:2] != [oid, "blob"] or int(fields[2]) > MAX_FILE_BYTES:
            raise ValueError(f"non-blob or oversized index entry: {path}")
    stream = io.BytesIO(git("cat-file", "--batch", input=batch_input))
    result = []
    for path, oid in entries:
        header = stream.readline().decode("ascii").strip().split()
        if len(header) != 3 or header[:2] != [oid, "blob"]:
            raise ValueError("unexpected Git blob response")
        size = int(header[2])
        content = stream.read(size)
        if len(content) != size or stream.read(1) != b"\n":
            raise ValueError("truncated Git blob")
        result.append((path, oid, content))
    return result


def new_output(raw: str) -> Path:
    path = Path(raw).absolute()
    if not path.resolve().is_relative_to((ROOT / "artifacts").resolve()):
        raise ValueError("output must be a new directory under this checkout's artifacts")
    if path.exists():
        raise ValueError("output exists; preserve prior receipts and use a new path")
    path.mkdir(parents=True)
    return path


def save(output: Path, name: str, data) -> None:
    with (output / name).open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def mit_check(content: bytes) -> bool:
    text = content.decode("utf-8-sig").replace("\r\n", "\n")
    lines = text.splitlines()
    if len(lines) < 10 or lines[0] != "MIT License" or not re.fullmatch(r"Copyright \(c\) 2026 .+", lines[2]):
        return False
    # The canonical standard license body, excluding the variable copyright line.
    required = ("Permission is hereby granted, free of charge, to any person obtaining a copy",
                "subject to the following conditions:",
                "The above copyright notice and this permission notice shall be included in all",
                'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR',
                "OTHER DEALINGS IN THE SOFTWARE.")
    normalized = " ".join(text.split())
    return all(" ".join(part.split()) in normalized for part in required)


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rows = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.rows.append({"test": test.id(), "status": "PASS"})

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.rows.append({"test": test.id(), "status": "SKIP", "reason": reason})


def _run_test_suites(output: Path) -> dict:
    import importlib.metadata
    for dependency in ("jsonschema", "PyYAML"):
        importlib.metadata.version(dependency)  # Missing deps are not a silent skip.
    inputs = sorted({*ROOT.joinpath("tools/project_structure").glob("*.py"), *ROOT.joinpath("tests").glob("*.py"),
                     *ROOT.joinpath("tools/publication").glob("*.py"), *PLUGIN.joinpath("tests").glob("*.py"),
                     PLUGIN / "mcp/server.py", PLUGIN / "scripts/launch_mcp.cmd", PLUGIN / ".mcp.json"})
    before = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    summaries = []
    for name, suite in (("core", unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))),
                        ("plugin", unittest.defaultTestLoader.discover(str(PLUGIN / "tests")))):
        with (output / f"{name}.log").open("x", encoding="utf-8") as stream:
            result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=Result).run(suite)
        item = {"suite": name, "run": result.testsRun, "PASS": sum(row["status"] == "PASS" for row in result.rows),
                "FAIL": len(result.failures), "ERROR": len(result.errors), "SKIP": len(result.skipped),
                "successful": result.wasSuccessful(), "skips": [row for row in result.rows if row["status"] == "SKIP"]}
        save(output, name + ".json", item)
        summaries.append(item)
    after = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    save(output, "input-hashes.json", {"before": before, "after": after, "unchanged": before == after})
    return {"successful": before == after and all(item["successful"] for item in summaries), "suites": summaries,
            "inputs_unchanged": before == after}


def run_tests(output: Path) -> dict:
    # Fixtures are not projects to discover/index. Keep them outside a watched
    # source checkout, while retaining receipts under ignored artifacts/.
    from tests.fixture_cleanup import cleanup_temporary_directory
    original_temp = tempfile.gettempdir()
    original_env = {name: os.environ.get(name) for name in ("TEMP", "TMP")}
    owned = tempfile.TemporaryDirectory(prefix="dungeon-verification-")
    try:
        tempfile.tempdir = owned.name
        os.environ.update(TEMP=owned.name, TMP=owned.name)
        return _run_test_suites(output)
    finally:
        tempfile.tempdir = original_temp
        for name, value in original_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        cleanup_temporary_directory(owned)


def stop_owned(process):
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            process.kill()
    process.wait(timeout=15)


def source_hashes(root: Path):
    result = {}
    skipped_dirs = {".git", "artifacts", "snapshots", "__pycache__", ".venv", "venv", "node_modules", "backups", "hardening_v041"}
    for current, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in skipped_dirs and not (Path(current) / name).is_symlink()]
        for name in files:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if name.endswith(".pyc") or relative.startswith("docs/audits/") and relative not in PUBLIC_AUDITS:
                continue
            if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
                continue
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def smoke(output: Path, plugin: Path | None = None) -> dict:
    import yaml
    package = plugin or PLUGIN
    manifest = json.loads((package / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    names = {p.parent.name for p in (package / "skills").glob("*/SKILL.md")}
    if names != SKILLS:
        raise ValueError("six exact Skill names are required")
    for name in names:
        text = (package / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        front = yaml.safe_load(text.split("---", 2)[1])
        if front.get("name") != name or not isinstance(front.get("description"), str):
            raise ValueError("invalid Skill frontmatter")
    for reference in ("evidence.md", "proof-adapters-v041.md"):
        if (package / "references" / reference).read_bytes() != (ROOT / "docs" / reference).read_bytes():
            raise ValueError("bundled reference differs from canonical public guide")
    if not mit_check((ROOT / "LICENSE").read_bytes()) or (package / "LICENSE").read_bytes() != (ROOT / "LICENSE").read_bytes():
        raise ValueError("root/bundled MIT license mismatch")
    env = {**os.environ, "DUNGEON_PROJECT_ROOT": str(ROOT), "DUNGEON_WRITABLE_ROOTS": "[]", "DUNGEON_PYTHON": sys.executable}
    requests = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
        "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "public-source-smoke", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": {"progressToken": 1}}}]
    calls = [("dungeon_project_probe", {}), ("dungeon_status", {}),
             ("dungeon_inspect_object", {"object_id": "storage.cas"}),
             ("dungeon_read_sklt", {"sklt_path": "storage/cas.sklt"}), ("dungeon_refresh", {})]
    for number, (name, arguments) in enumerate(calls, 3):
        requests.append({"jsonrpc": "2.0", "id": number, "method": "tools/call", "params": {
            "name": name, "arguments": {"project_root": str(ROOT), **arguments}, "_meta": {"progressToken": number}}})
    command = ([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "scripts\\launch_mcp.cmd"] if os.name == "nt"
               else [sys.executable, "-B", str(package / "mcp/server.py")])
    before = source_hashes(ROOT)
    package_before = source_hashes(package)
    process = subprocess.Popen(command, cwd=package, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="strict")
    try:
        stdout, stderr = process.communicate("".join(json.dumps(item) + "\n" for item in requests), timeout=60)
    except subprocess.TimeoutExpired:
        stop_owned(process)
        raise RuntimeError("owned MCP smoke timed out")
    replies = {row["id"]: row for row in (json.loads(line) for line in stdout.splitlines())}
    if process.returncode or set(replies) != set(range(8)) or any("error" in row for row in replies.values()):
        raise ValueError("MCP process/protocol failure; inspect local smoke streams")
    info = replies[0]["result"]["serverInfo"]
    tool_names = [item["name"] for item in replies[2]["result"]["tools"]]
    if info.get("version") != "0.4.1" or len(tool_names) != 30 or len(set(tool_names)) != 30:
        raise ValueError("MCP version/catalog mismatch")
    read_status = {name: replies[number]["result"]["structuredContent"].get("status") for number, (name, _) in enumerate(calls[:-1], 3)}
    if any(value != "PASS" for value in read_status.values()):
        raise ValueError("one of four MCP read calls failed")
    probe = replies[3]["result"]["structuredContent"]
    deny = replies[7]["result"]
    denied = deny.get("isError") is True and any(item.get("code") == "PROJECT_READ_ONLY" for item in deny["structuredContent"].get("issues", []))
    unchanged = before == source_hashes(ROOT) and package_before == source_hashes(package)
    passed = denied and unchanged and probe.get("write_enabled") is False and probe.get("mode") == "MANAGED"
    save(output, "smoke-responses.json", replies)
    (output / "smoke-stderr.log").write_text(stderr, encoding="utf-8")
    return {"successful": passed, "server_version": info["version"], "manifest_version": manifest["version"],
            "tool_count": len(tool_names), "tool_names": tool_names, "skill_count": len(names), "skills": sorted(names),
            "read_calls": read_status, "empty_allowlist_write_rejected": denied, "source_and_package_unchanged": unchanged,
            "mode": probe.get("mode"), "write_enabled": probe.get("write_enabled"), "windows_launcher": os.name == "nt"}


def staged_scan(output: Path, export: bool) -> dict:
    entries = index_blobs()
    findings = [issue for path, _, content in entries for issue in scan_content(path, content)]
    by_name = {path: content for path, _, content in entries}
    for required in ("README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", ".gitignore"):
        if required not in by_name:
            findings.append({"path": required, "rule": "required-public-file-missing"})
    if "LICENSE" in by_name and not mit_check(by_name["LICENSE"]):
        findings.append({"path": "LICENSE", "rule": "standard-MIT-license-not-detected"})
    inventory = [{"path": path, "git_blob": oid, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                 for path, oid, content in entries]
    save(output, "staged-inventory.json", inventory)
    result = {"successful": not findings, "files": len(entries), "total_bytes": sum(len(content) for _, _, content in entries),
              "max_file_bytes": max(len(content) for _, _, content in entries), "findings": findings,
              "scope": "actual Git index blob bytes, not working-tree guesses or .gitignore alone",
              "limitations": "bounded pattern/privacy/size scan plus review, not a guarantee that every possible secret is detectable"}
    if export and not findings:
        destination = output / "source"
        destination.mkdir()
        for path, _, content in entries:
            target = destination.joinpath(*PurePosixPath(path).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(content)
        result["export"] = "source/ (exact staged bytes; excluded local environment absent)"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("test", "smoke", "scan"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--export", action="store_true")
    parser.add_argument("--plugin", type=Path, help="optional installed/staged plugin root for smoke")
    args = parser.parse_args()
    output = new_output(args.output)
    try:
        if args.command == "test":
            result = run_tests(output)
        elif args.command == "smoke":
            result = smoke(output, args.plugin)
        else:
            if not args.staged:
                parser.error("scan requires --staged; examine actual proposed Git content")
            result = staged_scan(output, args.export)
        save(output, "result.json", result)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["successful"] else 1
    except Exception as exc:
        result = {"successful": False, "error": f"{type(exc).__name__}: {exc}"}
        save(output, "failure.json", result)
        print(json.dumps(result, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
