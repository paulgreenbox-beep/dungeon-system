"""Opt-in actual Scion CLI/backend checks against an owned live TestCluster.

The private context contains a disposable test token and is never a receipt.
This script does not install tools, prepare a checkout, or alter Dungeon state.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def tree_hashes(root: Path) -> dict:
    result = {}
    for path in root.rglob("*"):
        info = path.lstat()
        row = {"mode": stat.S_IMODE(info.st_mode)}
        if path.is_symlink():
            row.update(kind="symlink", target=os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            row.update(kind="file", size=info.st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        elif stat.S_ISDIR(info.st_mode):
            row.update(kind="directory")
        else:
            row.update(kind="special")
        result[path.relative_to(root).as_posix()] = row
    return result


def execute(context: dict, output: Path, expected_binary_sha256: str) -> dict:
    from tools.project_structure import vcs_backends, vcs_service
    root = Path(context["fixture_root_linux"])
    binary = Path(context["scion_binary"])
    if context.get("upstream_commit") != "7d445a0744e30f4dcb36a794b0ce42648157e1b9":
        raise ValueError("context does not match the reviewed upstream pin")
    if (not root.is_absolute() or not root.is_dir() or root.is_symlink()
            or not binary.is_absolute() or binary.is_symlink() or binary.read_bytes()[:4] != b"\x7fELF"):
        raise ValueError("explicit physical Linux fixture and real ELF executable required")
    binary_digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_binary_sha256) or binary_digest != expected_binary_sha256:
        raise ValueError("executable does not match the final successful build observation")
    revisions = context["revisions"]
    if (not isinstance(revisions, list) or len(revisions) < 3
            or any(not isinstance(value, str) or not re.fullmatch(r"c1[0-9a-f]{64}", value) for value in revisions)):
        raise ValueError("three actual changeset IDs required, ordered oldest to newest")
    token = context["token"]
    original_env = dict(os.environ)
    output.mkdir(parents=True, exist_ok=False)
    receipt = {"schema_version": 1, "status": "RUNNING", "upstream_commit": context["upstream_commit"],
               "binary_sha256": binary_digest,
               "scion_revisions": revisions, "cases": [], "dirty_state_claim": "UNKNOWN"}
    before = tree_hashes(root)

    def write(name, value):
        rendered = json.dumps(value, indent=2, ensure_ascii=True) + "\n"
        if token and token in rendered:
            raise RuntimeError("a test credential reached the receipt boundary")
        (output / name).write_text(rendered, encoding="utf-8")

    def case(name, predicate, kind):
        if not predicate:
            raise AssertionError(name)
        receipt["cases"].append({"case": name, "status": "PASS", "kind": kind})

    try:
        os.environ.update(PATH=str(binary.parent) + ":" + original_env["PATH"],
                          DUNGEON_SCION_RUNTIME_ENDPOINT=context["endpoint"],
                          DUNGEON_SCION_RUNTIME_TOKEN=token,
                          DUNGEON_SCION_RUNTIME_CA_FILE=context["ca_file"],
                          DUNGEON_SCION_RUNTIME_TRANSPORT="native")
        scope = vcs_backends.VersionScope("main", "SCION", ".", endpoint_profile="RUNTIME")
        backend = vcs_backends.backend_for(root, scope)
        executable, child_env, _ = backend._configuration()
        case("exact_built_executable", Path(executable) == binary, "REAL_BINARY")
        # Preserve raw real CLI JSON, independently of Python normalization.
        for name, arguments in (("show", ["show", revisions[-1]]),
                                ("log", ["log", "--limit", "3", "--rev", revisions[-1]])):
            started = time.monotonic()
            process = vcs_backends._run([executable, "-C", str(root), "--json", *arguments], root, child_env)
            case(name + "_exit_zero", process.returncode == 0, "REAL_CLI_REAL_SERVER")
            case(name + "_output_budget", len(process.stdout) + len(process.stderr) <= 1024 * 1024, "REAL_CLI_REAL_SERVER")
            raw = json.loads(process.stdout)
            vcs_backends._scion_payload(process.stdout, name, 3)
            if token.encode() in process.stdout or token.encode() in process.stderr:
                raise RuntimeError("test credential reached raw process output")
            (output / (name + "-raw.json")).write_bytes(process.stdout)
            write(name + ".json", raw)
            receipt["cases"].append({"case": name + "_raw", "status": "PASS", "kind": "REAL_CLI_REAL_SERVER",
                                      "exit_code": process.returncode, "elapsed_seconds": round(time.monotonic() - started, 3),
                                      "stdout_sha256": hashlib.sha256(process.stdout).hexdigest()})
            if name == "show":
                case("show_actual_id", raw["data"]["id"] == revisions[-1], "REAL_CLI_REAL_SERVER")
            else:
                entries = raw["data"]["entries"]
                case("log_order", [item["id"] for item in entries] == list(reversed(revisions)), "REAL_CLI_REAL_SERVER")
                case("log_parents", all(entries[i + 1]["id"] in entries[i]["parents"] for i in range(2)), "REAL_CLI_REAL_SERVER")
        # `cat` emits exact file bytes in human mode (pinned Reply::new contract).
        # Bind real server contents to the checkout bytes later captured by CAS.
        for relative in ("renderer/settings.rs", "renderer/frame.rs"):
            process = vcs_backends._run([executable, "-C", str(root), "cat", revisions[-1] + ":" + relative], root, child_env)
            case("server_source_bytes_" + relative, process.returncode == 0
                 and process.stdout == (root / relative).read_bytes(), "REAL_CLI_REAL_SERVER")
        for name, result in (("backend_inspect", backend.inspect(revision=revisions[-1])),
                             ("backend_history", backend.history(limit=2, revision=revisions[-1]))):
            write(name + ".json", result)
            record = result["source_revision"]
            case(name, result["availability"] == "AVAILABLE" and record["backend"] == "SCION"
                 and record["revision"] == revisions[-1] and record["dirty_state"] == "UNKNOWN", "DUNGEON_REAL_BACKEND")
        query = vcs_service.vcs_query(root, "history", "main", revision=revisions[-1], limit=1)
        write("service-history.json", query)
        case("service_declared_scope", len(query["scopes"]["main"]["data"]["entries"]) == 1,
             "DUNGEON_REAL_BACKEND")
        unknown_id = "c1" + "0" * 64
        failed = vcs_backends._run([executable, "-C", str(root), "--json", "show", unknown_id], root, child_env)
        error_reply = json.loads(failed.stdout)
        case("actual_not_found_error", failed.returncode != 0 and error_reply.get("schema") == "scion.cli.error.v1"
             and error_reply.get("data", {}).get("code") == "NOT_FOUND", "REAL_CLI_REAL_SERVER")
        if token.encode() in failed.stdout:
            raise RuntimeError("credential reached negative raw response")
        (output / "unknown-revision-raw.json").write_bytes(failed.stdout)
        unknown = backend.inspect(revision=unknown_id)
        write("unknown-revision.json", unknown)
        case("unknown_revision", unknown["availability"] == "UNKNOWN"
             and any(item["code"] == "QUERY_FAILED" for item in unknown["issues"]), "REAL_CLI_REAL_SERVER")
        os.environ["DUNGEON_SCION_RUNTIME_TOKEN"] = "disposable-invalid-token"
        wrong_auth = backend.inspect(revision=revisions[-1])
        write("wrong-auth.json", wrong_auth)
        case("wrong_auth", wrong_auth["availability"] == "AUTH_REQUIRED", "REAL_CLI_REAL_SERVER")
        os.environ["DUNGEON_SCION_RUNTIME_TOKEN"] = token
        # An actually closed loopback port, not a fake Scion JSON response.
        with socket.socket() as temporary:
            temporary.bind(("127.0.0.1", 0))
            port = temporary.getsockname()[1]
        os.environ["DUNGEON_SCION_RUNTIME_ENDPOINT"] = f"https://localhost:{port}"
        # A selector is backend-specific; do not send a Scion ID to Git.
        outage = vcs_service.vcs_query(root, "history", revision=None, limit=2)
        write("server-unavailable-mixed.json", outage)
        case("scion_outage_git_survives", outage["status"] == "PARTIAL"
             and outage["scopes"]["storage"]["availability"] == "AVAILABLE"
             and outage["scopes"]["main"]["source_revision"]["backend"] == "SCION"
             and outage["scopes"]["main"]["availability"] == "SERVER_UNREACHABLE", "REAL_BINARY_TRANSPORT_FAILURE")
        os.environ["DUNGEON_SCION_RUNTIME_ENDPOINT"] = "http://localhost:1"
        bad_endpoint = backend.inspect(revision=revisions[-1])
        case("bad_endpoint_fail_closed", bad_endpoint["availability"] == "NOT_CONFIGURED", "ADAPTER_REJECTION")
        os.environ["DUNGEON_SCION_RUNTIME_ENDPOINT"] = context["endpoint"]
        invalid = backend.inspect(revision="--invalid-revision")
        case("invalid_selector_fail_closed", invalid["availability"] != "AVAILABLE", "ADAPTER_REJECTION")
        # Fault injection into real-response bytes is explicitly NOT a server receipt.
        show_raw = (output / "show-raw.json").read_bytes()
        unsupported = json.loads(show_raw)
        unsupported["schema"] = "scion.cli.show.v999"
        for name, raw in (("malformed_json", show_raw[:-1] + b"not-json"),
                          ("unsupported_schema", json.dumps(unsupported).encode()),
                          ("unexpected_output", b"unexpected-output\n" + show_raw)):
            rejected = False
            try:
                vcs_backends._scion_payload(raw, "show")
            except vcs_backends._Failure:
                rejected = True
            case(name, rejected, "ADAPTER_FAULT_INJECTION_NOT_RUNTIME_PROOF")
        case("wrong_backend_namespace", not vcs_backends.validate_revision(
            {**record, "backend": "GIT"}), "ADAPTER_FAULT_INJECTION_NOT_RUNTIME_PROOF")
        # Exercise the REAL CLI against a deliberately stalled owned TCP peer.
        # This proves the adapter's process deadline, not a Scion-server response.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.2)
        stopped = threading.Event()
        connections = []
        def stall():
            while not stopped.is_set():
                try:
                    connection, _ = listener.accept()
                    connections.append(connection)
                except socket.timeout:
                    continue
                except OSError:
                    break
        thread = threading.Thread(target=stall, daemon=True)
        thread.start()
        try:
            os.environ["DUNGEON_SCION_RUNTIME_ENDPOINT"] = f"https://127.0.0.1:{listener.getsockname()[1]}"
            started = time.monotonic()
            timed_out = backend.inspect(revision=revisions[-1])
            elapsed = time.monotonic() - started
            write("cli-timeout.json", timed_out)
            case("actual_cli_timeout", any(issue["code"] == "TIMEOUT" for issue in timed_out["issues"])
                 and connections and elapsed < 15, "REAL_BINARY_TRANSPORT_FAULT")
        finally:
            stopped.set()
            listener.close()
            thread.join(timeout=2)
            for connection in connections:
                connection.close()
            os.environ["DUNGEON_SCION_RUNTIME_ENDPOINT"] = context["endpoint"]
        mixed = vcs_service.vcs_query(root, "history", revision=None, limit=2)
        write("mixed-history.json", mixed)
        case("mixed_actual_ids", mixed["scopes"]["storage"]["availability"] == "AVAILABLE"
             and mixed["scopes"]["main"]["availability"] == "AVAILABLE"
             and mixed["scopes"]["main"]["source_revision"]["revision"] == revisions[-1], "DUNGEON_REAL_BACKEND")
        case("working_copy_diff_stays_unsupported", any(item["code"] == "UNSUPPORTED_OPERATION" for item in backend.diff()["issues"]), "ADAPTER_REJECTION")
        case("read_queries_persistent_inventory_unchanged", before == tree_hashes(root), "REAL_CLI_AND_DUNGEON_HASH_CHECK")
        receipt["status"] = "TESTED_WITH_REAL_SCION"
        write("result.json", receipt)
        return receipt
    except Exception as exc:
        receipt.update(status="FAILED", error=type(exc).__name__ + ": " + str(exc).replace(token, "[REDACTED]"))
        write("result.json", receipt)
        raise
    finally:
        os.environ.clear()
        os.environ.update(original_env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary-sha256", required=True, help="final CLI hash from the successful helper/build receipt")
    args = parser.parse_args()
    if os.name == "nt":
        parser.error("native Linux execution required")
    context = json.loads(args.context.read_text(encoding="utf-8"))
    receipt = execute(context, args.output, args.binary_sha256)
    print(json.dumps({"status": receipt["status"], "cases": len(receipt["cases"]),
                      "scion_revisions": receipt["scion_revisions"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
