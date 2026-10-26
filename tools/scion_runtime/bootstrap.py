"""Explicit Linux-only prerequisites for the disposable Scion runtime gate.

Not imported by Dungeon/MCP. No sudo, shell profile edits or system installs.
Downloads are from official Rust / Protocol Buffers release infrastructure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import tempfile
import urllib.request
import zipfile

PIN = "7d445a0744e30f4dcb36a794b0ce42648157e1b9"
RUST = "1.98.0"
PROTOC = "32.1"
MAX_DOWNLOAD = 512 * 1024 * 1024


def fetch(url: str, destination: Path) -> str:
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "Dungeon-Scion-runtime-002"})
    total = 0
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("xb") as stream:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise RuntimeError("download exceeds the explicit size budget")
            stream.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def run(argv: list[str], env: dict, log: Path, timeout: int = 900) -> None:
    with log.open("xb") as stream:
        with subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=stream,
                              stderr=subprocess.STDOUT, start_new_session=True) as process:
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise
    if code:
        raise RuntimeError(f"prerequisite process failed: exit={code}; log={log}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--source-crlf", action="store_true", help="validate a Windows CRLF checkout using its explicit Git text policy")
    args = parser.parse_args()
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        parser.error("requires native x86_64 Linux (WSL execution is only for this disposable gate)")
    upstream = args.upstream.resolve(strict=True)
    # No working-tree modifications, and no moving tag/branch selection.
    git = ["git", "--no-optional-locks", "-c", f"safe.directory={upstream}", "-C", str(upstream)]
    if args.source_crlf:
        git += ["-c", "core.autocrlf=true"]
    head = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    if head != PIN:
        parser.error("upstream is not the approved pinned commit")
    if subprocess.check_output([*git, "status", "--porcelain=v1", "--untracked-files=all"]):
        parser.error("pinned upstream checkout must be clean")
    tracked = subprocess.check_output([*git, "ls-files", "-z"]).decode().split("\0")[:-1]
    work = Path(tempfile.mkdtemp(prefix="dungeon-scion-runtime-002-", dir="/var/tmp"))
    work.chmod(0o700)
    downloads = work / "downloads"
    downloads.mkdir()
    receipt = {"schema_version": 1, "epic": "DUNGEON-SCION-RUNTIME-002", "upstream_commit": PIN,
               "work_root": str(work), "platform": platform.platform(), "rust": RUST,
               "protoc": PROTOC, "status": "PREPARING", "downloads": {}}
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    if args.receipt.exists():
        parser.error("receipt exists; use a new output name rather than overwriting an attempt")

    def save():
        args.receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    save()
    try:
        url = "https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init"
        installer = downloads / "rustup-init"
        actual = fetch(url, installer)
        checksum = downloads / "rustup-init.sha256"
        fetch(url + ".sha256", checksum)
        expected = checksum.read_text(encoding="utf-8").split()[0]
        if len(expected) != 64 or actual != expected:
            raise RuntimeError("official rustup installer checksum mismatch")
        receipt["downloads"]["rustup-init"] = {"url": url, "sha256": actual, "upstream_checksum_matched": True}
        installer.chmod(0o700)
        # Do not expose ambient credentials or RUSTUP_* mirror overrides to installers.
        home = work / "installer-home"
        home.mkdir(mode=0o700)
        env = dict(PATH="/usr/bin:/bin", LANG="C.UTF-8", HOME=str(home),
                   RUSTUP_HOME=str(work / "rustup"), CARGO_HOME=str(work / "cargo"),
                   RUSTUP_INIT_SKIP_PATH_CHECK="yes")
        run([str(installer), "-y", "--no-modify-path", "--profile", "minimal", "--default-toolchain", RUST],
            env, work / "rustup-install.log")
        release_url = f"https://api.github.com/repos/protocolbuffers/protobuf/releases/tags/v{PROTOC}"
        metadata = downloads / "protoc-release.json"
        fetch(release_url, metadata)
        release = json.loads(metadata.read_text(encoding="utf-8"))
        name = f"protoc-{PROTOC}-linux-x86_64.zip"
        asset = next(item for item in release["assets"] if item["name"] == name)
        expected = asset.get("digest")
        if not isinstance(expected, str) or not expected.startswith("sha256:") or len(expected) != 71:
            raise RuntimeError("official protoc release does not expose a SHA-256 digest")
        url = f"https://github.com/protocolbuffers/protobuf/releases/download/v{PROTOC}/{name}"
        if asset["browser_download_url"] != url:
            raise RuntimeError("unexpected protoc download URL")
        archive = downloads / name
        actual = fetch(url, archive)
        if "sha256:" + actual != expected:
            raise RuntimeError("official protoc asset digest mismatch")
        receipt["downloads"]["protoc"] = {"url": url, "sha256": actual, "upstream_checksum_matched": True}
        protoc_root = work / "protoc"
        protoc_root.mkdir()
        with zipfile.ZipFile(archive) as zipped:
            for item in zipped.infolist():
                destination = protoc_root / item.filename
                if (not destination.resolve().is_relative_to(protoc_root)
                        or item.file_size > MAX_DOWNLOAD or (item.external_attr >> 16) & 0o170000 == 0o120000):
                    raise RuntimeError("unsafe protoc archive entry")
            zipped.extractall(protoc_root)
        (protoc_root / "bin/protoc").chmod(0o700)
        env["PATH"] = str(work / "cargo/bin") + ":" + str(protoc_root / "bin") + ":" + env["PATH"]
        env["PROTOC"] = str(protoc_root / "bin/protoc")
        env["CARGO_TARGET_DIR"] = str(work / "target")
        receipt["environment"] = {key: env[key] for key in ("RUSTUP_HOME", "CARGO_HOME", "PROTOC", "CARGO_TARGET_DIR")}
        receipt["versions"] = {name: subprocess.check_output([str(exe), "--version"], env=env, text=True).strip()
                               for name, exe in (("rustc", work / "cargo/bin/rustc"),
                                                 ("cargo", work / "cargo/bin/cargo"),
                                                 ("protoc", protoc_root / "bin/protoc"))}
        # A copy, not an edit to the reviewed upstream checkout. Build I/O stays on ext4.
        copied = work / "upstream"
        copied.mkdir()
        source_hashes = {}
        for name in tracked:
            source = upstream / name
            if source.is_symlink() or not source.is_file():
                raise RuntimeError("tracked upstream input is not a regular file")
            target = copied / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            source_hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
        receipt["tracked_source_digest"] = hashlib.sha256(json.dumps(source_hashes, sort_keys=True).encode()).hexdigest()
        receipt["status"] = "PREREQUISITES_READY"
        save()
        print(json.dumps({"status": receipt["status"], "work_root": str(work), "versions": receipt["versions"]}), flush=True)
        return 0
    except Exception as exc:
        receipt.update(status="PREREQUISITES_FAILED", error=f"{type(exc).__name__}: {exc}")
        save()
        raise


if __name__ == "__main__":
    raise SystemExit(main())
