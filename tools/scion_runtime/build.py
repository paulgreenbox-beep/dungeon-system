"""Build the pinned external CLI and its narrow real-CLI TestCluster smoke.

Explicit operator-only gate; never called from ordinary Dungeon inspection.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-checkout", type=Path, required=True, help="original clean pinned checkout to verify copied build inputs")
    parser.add_argument("--source-crlf", action="store_true", help="explicit Git text policy for a Windows-created checkout")
    args = parser.parse_args()
    if os.name == "nt":
        parser.error("Linux gate only; this does not enable the production WSL bridge")
    config = json.loads(args.bootstrap.read_text(encoding="utf-8"))
    if config.get("status") != "PREREQUISITES_READY" or config.get("upstream_commit") != "7d445a0744e30f4dcb36a794b0ce42648157e1b9":
        parser.error("successful pinned bootstrap receipt required")
    work = Path(config["work_root"])
    if work.parent != Path("/var/tmp") or not work.name.startswith("dungeon-scion-runtime-002-") or work.is_symlink() or work.stat().st_uid != os.getuid():
        parser.error("not the owned private runtime workspace")
    args.output.mkdir(parents=True, exist_ok=False)
    receipt = {"schema_version": 1, "status": "BUILDING", "upstream_commit": config["upstream_commit"],
               "versions": config["versions"], "platform": config["platform"], "steps": []}
    home = work / ("build-home-" + args.output.name)
    home.mkdir(mode=0o700, exist_ok=False)
    env = dict(config["environment"])
    env.update(PATH=str(work / "cargo/bin") + ":" + str(work / "protoc/bin") + ":/usr/bin:/bin",
               HOME=str(home), LANG="C.UTF-8", RUSTUP_TOOLCHAIN="1.98.0",
               CARGO_BUILD_JOBS="4", CARGO_PROFILE_DEV_DEBUG="0", CARGO_PROFILE_TEST_DEBUG="0")
    destination = args.output / "result.json"

    def save():
        destination.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    save()
    original = args.source_checkout.resolve(strict=True)
    git = ["git", "--no-optional-locks", "-c", f"safe.directory={original}", "-C", str(original)]
    if args.source_crlf:
        git += ["-c", "core.autocrlf=true"]
    receipt["source_text_policy"] = "EXPLICIT_WINDOWS_CRLF" if args.source_crlf else "DEFAULT"
    if (subprocess.check_output([*git, "rev-parse", "HEAD"], env=env, text=True).strip() != config["upstream_commit"]
            or subprocess.check_output([*git, "status", "--porcelain=v1", "--untracked-files=all"], env=env)):
        raise RuntimeError("original source checkout is not clean at the pinned commit")
    tracked = subprocess.check_output([*git, "ls-files", "-z"], env=env).decode().split("\0")[:-1]
    hashes = {}
    for name in tracked:
        source, copied = original / name, work / "upstream" / name
        if source.is_symlink() or copied.is_symlink() or source.read_bytes() != copied.read_bytes():
            raise RuntimeError("copied upstream source differs from the clean pinned checkout")
        hashes[name] = hashlib.sha256(copied.read_bytes()).hexdigest()
    actual = {str(path.relative_to(work / "upstream")) for path in (work / "upstream").rglob("*") if path.is_file()}
    if actual != set(tracked):
        raise RuntimeError("untracked files are present in the copied upstream build tree")
    receipt["verified_tracked_files"] = len(tracked)
    receipt["source_digest"] = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    receipt["environment_policy"] = "EXPLICIT_PRIVATE_ALLOWLIST_NO_AMBIENT_CREDENTIALS"
    save()
    steps = [("build-cli", ["cargo", "build", "--locked", "-p", "scion-cli", "--bin", "scion"]),
             ("binary-help", [str(work / "target/debug/scion"), "--help"]),
             ("upstream-cli-smoke", ["cargo", "test", "--locked", "-p", "scion-cli", "--test", "cli_smoke",
                                     "binary_help_usage_and_whoami", "--", "--exact", "--nocapture"])]
    for name, command in steps:
        started = time.monotonic()
        try:
            with (args.output / (name + ".log")).open("xb") as log:
                with subprocess.Popen(command, cwd=work / "upstream", env=env, stdin=subprocess.DEVNULL,
                                      stdout=log, stderr=subprocess.STDOUT, start_new_session=True) as process:
                    try:
                        code = process.wait(timeout=1800)
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
        except subprocess.TimeoutExpired:
            code = "TIMEOUT"
        receipt["steps"].append({"name": name, "argv": command, "exit_code": code,
                                 "elapsed_seconds": round(time.monotonic() - started, 3)})
        if code != 0:
            receipt["status"] = "BUILD_FAILED" if name == "build-cli" else "RUNTIME_GATE_FAILED"
            save()
            print(json.dumps(receipt), flush=True)
            return 1
        if name == "build-cli":
            binary = work / "target/debug/scion"
            receipt["binary"] = {"path": str(binary), "size": binary.stat().st_size,
                                  "sha256": hashlib.file_digest(binary.open("rb"), "sha256").hexdigest()}
        save()
        print(json.dumps({"step": name, "exit_code": code}), flush=True)
    # Cargo's test profile may rebuild the same target/debug/scion path.
    # Preserve the initial observation, but identify the executable after ALL steps.
    receipt["build_profile_binary"] = receipt["binary"]
    binary = work / "target/debug/scion"
    with binary.open("rb") as stream:
        receipt["binary"] = {"path": str(binary), "size": binary.stat().st_size,
                              "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
    receipt["status"] = "UPSTREAM_REAL_CLI_SMOKE_PASSED"
    save()
    print(json.dumps({"status": receipt["status"], "binary": receipt["binary"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
