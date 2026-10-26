"""Build only the standalone runtime helper using a successful private bootstrap.

Does not launch the helper or change upstream. Call after the parent's CLI smoke.
CHEAPEST_VERIFICATION_PATH: one helper binary, existing CARGO_TARGET_DIR, offline.
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
import stat
import subprocess
import time

PIN = "7d445a0744e30f4dcb36a794b0ce42648157e1b9"
NAME = "dungeon-scion-runtime-cluster"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def physical(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("absolute physical path required")
    for ancestor in (*reversed(path.parents), path):
        if ancestor.is_symlink():
            raise ValueError("symlink path component refused")
    return path.resolve(strict=True)


def run_owned(command: list[str], cwd: Path, env: dict[str, str], log: Path) -> tuple[int | str, float]:
    started = time.monotonic()
    with log.open("xb") as stream:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code: int | str = process.wait(timeout=1800)
        except subprocess.TimeoutExpired:
            code = "TIMEOUT"
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            # Also stop descendants if Cargo exited before a compiler/linker did.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
    return code, round(time.monotonic() - started, 3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if platform.system() != "Linux":
        parser.error("native Linux build required; runtime is a separate parent-owned step")
    config = json.loads(args.bootstrap.read_text(encoding="utf-8"))
    if config.get("status") != "PREREQUISITES_READY" or config.get("upstream_commit") != PIN:
        parser.error("successful pinned bootstrap receipt required")
    work = physical(Path(config["work_root"]))
    mode = work.stat()
    if (work.parent != Path("/var/tmp") or not work.name.startswith("dungeon-scion-runtime-002-")
            or mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) != 0o700):
        parser.error("owned mode-0700 bootstrap workspace required")
    upstream = physical(work / "upstream")
    target = physical(work / "target")
    expected = {"CARGO_HOME": work / "cargo", "RUSTUP_HOME": work / "rustup",
                "PROTOC": work / "protoc/bin/protoc", "CARGO_TARGET_DIR": target}
    for key, value in expected.items():
        if Path(config["environment"][key]) != value:
            parser.error("bootstrap environment differs from owned workspace")
    physical(args.output.parent)
    args.output.mkdir(mode=0o700, exist_ok=False)
    receipt = {"schema_version": 1, "epic": "DUNGEON-SCION-RUNTIME-002", "status": "BUILDING",
               "upstream_commit": PIN, "work_root": str(work), "steps": [],
               "cheapest_verification_path": "standalone helper binary only; shared target; offline",
               "runtime": "NOT_RUN"}
    result_file = args.output / "result.json"

    def save() -> None:
        temporary = result_file.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, result_file)

    save()
    try:
        source = Path(__file__).resolve().parent / "cluster"
        # Every output attempt gets a distinct private source workspace. Preserve prior
        # sources and receipts while reusing only the explicitly shared Cargo target.
        helper = work / ("helper-" + args.output.name)
        helper.mkdir(mode=0o700, exist_ok=False)
        (helper / "src").mkdir()
        manifest = (source / "Cargo.toml.in").read_text(encoding="utf-8")
        # JSON string escaping is also valid inside this TOML basic string.
        replacement = json.dumps(str(upstream))[1:-1]
        (helper / "Cargo.toml").write_text(manifest.replace("__SCION_UPSTREAM__", replacement), encoding="utf-8")
        shutil.copyfile(source / "src/main.rs", helper / "src/main.rs")
        # Preserve upstream resolutions; Cargo adds only the standalone root package.
        # The first offline build updates this private copy, never upstream's lockfile.
        shutil.copyfile(upstream / "Cargo.lock", helper / "Cargo.lock")
        (helper / "home").mkdir(mode=0o700)
        env = {key: str(value) for key, value in expected.items()}
        env.update(PATH=f"{work / 'cargo/bin'}:{work / 'protoc/bin'}:/usr/bin:/bin",
                   HOME=str(helper / "home"), CARGO_BUILD_JOBS="4", CARGO_PROFILE_DEV_DEBUG="0",
                   CARGO_PROFILE_TEST_DEBUG="0", RUSTUP_TOOLCHAIN="1.98.0", LANG="C.UTF-8")
        receipt.update(helper_root=str(helper), manifest=str(helper / "Cargo.toml"),
                       sources={"Cargo.toml.in": digest(source / "Cargo.toml.in"),
                                "src/main.rs": digest(source / "src/main.rs"),
                                "prepare_cluster.py": digest(Path(__file__))},
                       upstream_lock_sha256=digest(upstream / "Cargo.lock"))
        cli = target / "debug/scion"
        receipt["scion_binary_before"] = {"path": str(cli), "sha256": digest(cli), "size": cli.stat().st_size}
        save()
        command = [str(work / "cargo/bin/cargo"), "build", "--offline", "--manifest-path",
                   str(helper / "Cargo.toml"), "--bin", NAME]
        code, seconds = run_owned(command, helper, env, args.output / "build-helper.log")
        receipt["steps"].append({"name": "build-helper", "argv": command, "exit_code": code,
                                 "elapsed_seconds": seconds})
        if code == 0:
            # A pure path-layout guard test only: no main(), TestCluster or external CLI.
            # Use dev codegen to reuse the helper build's dependencies.
            guard_command = [str(work / "cargo/bin/cargo"), "test", "--offline", "--profile", "dev",
                             "--manifest-path", str(helper / "Cargo.toml"), "--bin", NAME,
                             "tests::fixture_root_layout_guard", "--", "--exact"]
            code, seconds = run_owned(guard_command, helper, env, args.output / "guard-test.log")
            receipt["steps"].append({"name": "fixture-root-layout-guard", "argv": guard_command,
                                     "exit_code": code, "elapsed_seconds": seconds})
        receipt["scion_binary_after"] = {"path": str(cli), "sha256": digest(cli), "size": cli.stat().st_size}
        receipt["helper_lock_sha256"] = digest(helper / "Cargo.lock")
        if digest(upstream / "Cargo.lock") != receipt["upstream_lock_sha256"]:
            raise RuntimeError("upstream lockfile changed during build")
        if receipt["scion_binary_after"] != receipt["scion_binary_before"]:
            raise RuntimeError("external CLI changed during helper-only build")
        if code != 0:
            receipt["status"] = "BUILD_FAILED"
            save()
            print(json.dumps({"status": "BUILD_FAILED", "exit_code": code, "receipt": str(result_file)}), flush=True)
            return 1
        binary = target / "debug" / NAME
        receipt["binary"] = {"path": str(binary), "sha256": digest(binary), "size": binary.stat().st_size}
        receipt["status"] = "HELPER_BUILT_RUNTIME_NOT_RUN"
        receipt["launch_contract"] = {"binary": str(binary), "context_parent_mode": "0700",
                                      "context_example": str(work / "run-01/context.json"),
                                      "ready_signal": "<stop-file>.ready.json",
                                      "context_contains_credentials": True}
        save()
        print(json.dumps({"status": receipt["status"], "binary": receipt["binary"],
                          "scion_binary": receipt["scion_binary_after"], "receipt": str(result_file)}), flush=True)
        return 0
    except BaseException as error:
        receipt.update(status="BUILD_FAILED", error=f"{type(error).__name__}: {error}")
        save()
        raise


if __name__ == "__main__":
    raise SystemExit(main())
