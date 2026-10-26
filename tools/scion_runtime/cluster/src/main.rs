//! Owned, opt-in real TestCluster for DUNGEON-SCION-RUNTIME-002.
//! Context is private; <stop-file>.ready.json is the nonsecret cross-OS signal.
//! Only the external CLI creates the working copy and its .scion state.
#![forbid(unsafe_code)]

use anyhow::{Context, Result, bail, ensure};
use clap::Parser;
use scion_conformance::{ClusterOpts, TestCluster, Via};
use scion_proto::{Endpoint, v1};
use scion_refmodel::{MFile, MTree};
use scion_testkit::{GenCommit, GenRepo};
use serde::Serialize;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;
use tokio::process::Command;

const PIN: &str = "7d445a0744e30f4dcb36a794b0ce42648157e1b9";
// Server bootstrap owns main; this helper creates only its disposable fixture ref.
const FIXTURE_REF: &str = "runtime-fixture";
const HARD_LIMIT: Duration = Duration::from_secs(15 * 60);
const TRUSTED_PATH: &str = "/usr/bin:/bin";
const README: &[u8] = b"# Dungeon Scion runtime fixture\n\nThree real linear changesets; renderer files are materialized by scion checkout.\n";
const SETTINGS_1: &[u8] = b"pub const QUALITY: u32 = 1;\n";
const SETTINGS_2: &[u8] = b"pub const QUALITY: u32 = 2;\n";
const FRAME_1: &[u8] = b"pub fn render_frame() -> u32 { 1 }\n";
const FRAME_2: &[u8] = b"pub fn render_frame() -> u32 { 2 }\n";

#[derive(Parser)]
#[command(about = "Disposable real TLS Scion cluster; no Dungeon metadata writes")]
struct Args {
    #[arg(long)]
    fixture_root: PathBuf,
    /// New context JSON under an existing owned 0700 ext4 directory in /var/tmp.
    #[arg(long)]
    context: PathBuf,
    /// Absent sentinel; readiness is published as <stop-file>.ready.json.
    #[arg(long)]
    stop_file: PathBuf,
    /// Actual previously built scion ELF executable.
    #[arg(long)]
    binary: PathBuf,
}

#[derive(Serialize)]
struct PrivateContext {
    schema_version: u32,
    upstream_commit: &'static str,
    fixture_root_linux: PathBuf,
    scion_binary: PathBuf,
    endpoint: String,
    token: String,
    ca_file: PathBuf,
    revisions: Vec<String>,
}

#[derive(Serialize)]
struct Ready<'a> {
    schema_version: u32,
    status: &'static str,
    context_path: &'a Path,
    fixture_root_linux: &'a Path,
    revisions: &'a [String],
}

fn ready_path(stop: &Path) -> PathBuf {
    let mut name = stop.as_os_str().to_owned();
    name.push(".ready.json");
    PathBuf::from(name)
}

/// Reject symlinks in every existing component, not just the final filename.
fn physical_path(path: &Path, missing_leaf: bool) -> Result<()> {
    ensure!(path.is_absolute(), "absolute physical path required");
    let mut current = PathBuf::new();
    let count = path.components().count();
    for (index, component) in path.components().enumerate() {
        ensure!(matches!(component, Component::RootDir | Component::Normal(_)), "nonphysical path component");
        current.push(component.as_os_str());
        match fs::symlink_metadata(&current) {
            Ok(meta) => ensure!(!meta.file_type().is_symlink(), "symlink path component refused"),
            Err(error) if missing_leaf && index + 1 == count && error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error).context("physical path validation failed"),
        }
    }
    Ok(())
}

fn must_be_absent(path: &Path) -> Result<()> {
    match fs::symlink_metadata(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        _ => bail!("output/sentinel already exists or is inaccessible"),
    }
}

/// The native checkout is exactly the project child of the validated private ext4
/// context directory. The original explicit disposable Windows layout is also allowed.
fn fixture_layout_allowed(fixture: &Path, private: &Path) -> bool {
    if fixture == private.join("project") {
        return true;
    }
    let Some(run) = fixture.parent() else { return false };
    fixture.starts_with("/mnt/c")
        && fixture.file_name().is_some_and(|name| name == "project")
        && run.parent().and_then(Path::file_name).is_some_and(|name| name == "scion-runtime-002")
        && run.parent().and_then(Path::parent).and_then(Path::file_name).is_some_and(|name| name == "artifacts")
}

fn validate(args: &Args) -> Result<PathBuf> {
    ensure!(cfg!(target_os = "linux"), "native Linux helper required");
    physical_path(&args.context, true)?;
    let private = args.context.parent().context("context parent missing")?;
    ensure!(private.starts_with("/var/tmp") && private != Path::new("/var/tmp"), "context must be private Linux storage under /var/tmp");
    let meta = fs::metadata(private)?;
    ensure!(meta.is_dir() && meta.uid() == rustix::process::geteuid().as_raw()
        && meta.mode() & 0o777 == 0o700, "context parent must be owned and mode 0700");
    // /mnt/c (DrvFS) is explicitly unsuitable for secret context bytes.
    let mounts = fs::read_to_string("/proc/self/mountinfo")?;
    let mut best: Option<(usize, &str)> = None;
    for line in mounts.lines() {
        if let Some((left, right)) = line.split_once(" - ") {
            if let (Some(mount), Some(kind)) = (left.split_whitespace().nth(4), right.split_whitespace().next()) {
                if private.starts_with(Path::new(mount)) && best.is_none_or(|(length, _)| mount.len() > length) {
                    best = Some((mount.len(), kind));
                }
            }
        }
    }
    ensure!(matches!(best, Some((_, "ext4"))), "context must reside on ext4");
    must_be_absent(&args.context)?;
    physical_path(&args.stop_file, true)?;
    must_be_absent(&args.stop_file)?;
    let ready = ready_path(&args.stop_file);
    physical_path(&ready, true)?;
    must_be_absent(&ready)?;
    ensure!(!args.stop_file.starts_with(&args.fixture_root), "stop sentinel must be outside the checkout");
    physical_path(&args.fixture_root, true)?;
    ensure!(fixture_layout_allowed(&args.fixture_root, private),
        "fixture must be exactly the private ext4 context parent's project child or the explicit /mnt/c/.../artifacts/scion-runtime-002/<run>/project");
    if args.fixture_root.try_exists()? {
        ensure!(args.fixture_root.is_dir() && fs::read_dir(&args.fixture_root)?.next().is_none(), "fixture root must be absent or empty");
    }
    physical_path(&args.binary, false)?;
    let binary_meta = fs::metadata(&args.binary)?;
    ensure!(binary_meta.is_file() && binary_meta.mode() & 0o111 != 0, "executable binary required");
    let mut magic = [0; 4];
    File::open(&args.binary)?.read_exact(&mut magic)?;
    ensure!(magic == *b"\x7fELF", "actual ELF Scion binary required");
    Ok(ready)
}

fn snapshot(settings: &[u8], frame: &[u8]) -> MTree {
    [("README.md", README), ("renderer/settings.rs", settings), ("renderer/frame.rs", frame)]
        .into_iter()
        .map(|(path, content)| (path.as_bytes().to_vec(), MFile { mode: 1, content: content.to_vec() }))
        .collect()
}

fn fixture_repo() -> GenRepo {
    let trees = [snapshot(SETTINGS_1, FRAME_1), snapshot(SETTINGS_2, FRAME_1), snapshot(SETTINGS_2, FRAME_2)];
    let messages = ["runtime fixture: initial renderer", "runtime fixture: quality 2", "runtime fixture: frame 2"];
    GenRepo {
        commits: trees.into_iter().enumerate().map(|(i, tree)| {
            let mut change_id = [0x53; 16];
            change_id[15] = i as u8;
            GenCommit {
                parents: if i == 0 { vec![] } else { vec![i - 1] },
                tree,
                author: b"Dungeon runtime fixture <fixture@example.invalid>".to_vec(),
                message: messages[i].as_bytes().to_vec(),
                change_id,
            }
        }).collect(),
    }
}

/// Atomic no-clobber publication; secret bytes are mode 0600 from creation.
fn publish(path: &Path, value: &impl Serialize) -> Result<()> {
    let parent = path.parent().context("output parent missing")?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    temporary.as_file().set_permissions(fs::Permissions::from_mode(0o600))?;
    serde_json::to_writer(&mut temporary, value)?;
    temporary.write_all(b"\n")?;
    temporary.as_file().sync_all()?;
    temporary.persist_noclobber(path).map_err(|_| anyhow::anyhow!("atomic output publication failed"))?;
    File::open(parent)?.sync_all()?;
    Ok(())
}

/// Only bounded ASCII identifiers may cross the child-output diagnostic boundary.
fn safe_error_label(value: &serde_json::Value) -> String {
    match value.as_str() {
        Some(label) if !label.is_empty() && label.len() <= 64
            && label.bytes().all(|byte| byte.is_ascii_alphabetic() || byte == b'_') => label.to_ascii_uppercase(),
        _ => "WITHHELD".to_owned(),
    }
}

fn contains_bytes(bytes: &[u8], needle: &[u8]) -> bool {
    !needle.is_empty() && bytes.windows(needle.len()).any(|window| window == needle)
}

/// Preserve exact stdout, never the message in a public diagnostic. This file outlives
/// the temporary HOME, stays on ext4, is 0600 from creation and cannot replace an attempt.
fn checkout_failure(stdout: &[u8], token: &str, private: &Path) -> String {
    if token.is_empty() || contains_bytes(stdout, token.as_bytes())
        || contains_bytes(stdout, b"v4.public.") || contains_bytes(stdout, b"v4.local.") {
        return "schema=WITHHELD code=WITHHELD subcode=WITHHELD private_stdout_saved=false".to_owned();
    }
    let saved = (|| -> Result<()> {
        let mut temporary = tempfile::NamedTempFile::new_in(private)?;
        temporary.as_file().set_permissions(fs::Permissions::from_mode(0o600))?;
        temporary.write_all(stdout)?;
        temporary.as_file().sync_all()?;
        temporary.persist_noclobber(private.join("checkout-failure.stdout"))
            .map_err(|_| anyhow::anyhow!("private stdout publication failed"))?;
        File::open(private)?.sync_all()?;
        Ok(())
    })().is_ok();
    let parsed = serde_json::from_slice::<serde_json::Value>(stdout).ok();
    let (schema, code, subcode) = match parsed.as_ref() {
        Some(value) if value["schema"] == "scion.cli.error.v1" => {
            let data = &value["data"];
            ("scion.cli.error.v1", safe_error_label(&data["code"]),
                if data["subcode"].is_null() { "NONE".to_owned() } else { safe_error_label(&data["subcode"]) })
        }
        _ => ("UNRECOGNIZED", "WITHHELD".to_owned(), "WITHHELD".to_owned()),
    };
    format!("schema={schema} code={code} subcode={subcode} private_stdout_saved={saved}")
}

async fn serve(args: Args) -> Result<()> {
    let ready_file = validate(&args)?;
    let private = args.context.parent().context("context parent missing")?;
    let home = tempfile::Builder::new().prefix("cluster-home-").tempdir_in(private)?;
    fs::set_permissions(home.path(), fs::Permissions::from_mode(0o700))?;
    // Upstream installs an in-memory LogCapture, not a stdout subscriber.
    let cluster = TestCluster::start(ClusterOpts::default().with_daemon(false)).await;
    let repo = fixture_repo();
    let ids = cluster.seed(&repo).await;
    ensure!(ids.len() == 3 && ids[0] != ids[1] && ids[1] != ids[2], "three distinct seed changesets required");
    let client = cluster.client(Via::Server, &TestCluster::seeder()).await;
    client.refs().create_ref(v1::CreateRefRequest {
        name: FIXTURE_REF.to_owned(),
        target: ids[2].as_bytes().to_vec(),
        reason: "DUNGEON-SCION-RUNTIME-002 disposable fixture".to_owned(),
    }).await.map_err(|status| anyhow::anyhow!(
        "CreateRef runtime-fixture RPC failed: code={:?}", status.code()
    ))?;
    let endpoint = match &cluster.servers[0].endpoint {
        Endpoint::Https(url) => url.clone(),
        _ => bail!("real TLS server endpoint required"),
    };
    let ca = cluster.servers[0].tls.as_ref().and_then(|tls| tls.ca_pem.as_ref()).context("test TLS CA missing")?;
    let ca_file = home.path().join("ca.pem");
    let mut ca_stream = fs::OpenOptions::new().write(true).create_new(true)
        .open(&ca_file)?;
    ca_stream.set_permissions(fs::Permissions::from_mode(0o600))?;
    ca_stream.write_all(ca.as_slice())?;
    ca_stream.sync_all()?;
    let context = PrivateContext {
        schema_version: 1,
        upstream_commit: PIN,
        fixture_root_linux: args.fixture_root.clone(),
        scion_binary: args.binary.clone(),
        endpoint,
        token: cluster.token_for(&TestCluster::gh("alice")),
        ca_file,
        revisions: ids.iter().map(|id| id.to_text()).collect(),
    };
    // Keep Tokio's TLS server workers alive while waiting for the external executable.
    let mut command = Command::new(&args.binary);
    command.env_clear()
        .env("PATH", TRUSTED_PATH).env("HOME", home.path())
        .env("TMPDIR", home.path()).env("SCION_LOG", "off")
        .env("SCION_ENDPOINT", &context.endpoint)
        .env("SCION_TOKEN", &context.token).env("SCION_CA_FILE", &context.ca_file)
        .current_dir(home.path())
        .args(["--json", "checkout", FIXTURE_REF]).arg(&args.fixture_root)
        .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null())
        .kill_on_drop(true);
    let output = tokio::time::timeout(Duration::from_secs(90), command.output()).await
        .context("external checkout timed out")?.context("external checkout could not start")?;
    if !output.status.success() {
        bail!("external checkout failed (exit {:?}); {}", output.status.code(),
            checkout_failure(&output.stdout, &context.token, private));
    }
    let reply: serde_json::Value = serde_json::from_slice(&output.stdout).context("checkout did not return JSON")?;
    ensure!(reply["schema"] == "scion.cli.checkout.v1" && reply["data"]["base"] == context.revisions[2]
        && reply["data"]["tracking_ref"] == FIXTURE_REF, "checkout response did not identify the seeded fixture head");
    for (path, expected) in [("README.md", README), ("renderer/settings.rs", SETTINGS_2), ("renderer/frame.rs", FRAME_2)] {
        let file = args.fixture_root.join(path);
        physical_path(&file, false)?;
        ensure!(fs::read(file)? == expected, "CLI checkout materialized unexpected fixture content");
    }
    ensure!(args.fixture_root.join(".scion").is_dir(), "CLI working-copy metadata missing");
    publish(&args.context, &context)?;
    let ready = Ready {
        schema_version: 1, status: "READY", context_path: &args.context,
        fixture_root_linux: &args.fixture_root, revisions: &context.revisions,
    };
    publish(&ready_file, &ready)?;
    println!("{}", serde_json::to_string(&ready)?);
    std::io::stdout().flush()?;
    loop {
        if fs::symlink_metadata(&args.stop_file).is_ok() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    // InProcessServer/FakeGitHub Drop abort their owned listener tasks.
    drop(client);
    drop(cluster);
    Ok(())
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> Result<()> {
    // Upstream setup APIs panic on errors. Never echo their debug/credential payloads.
    std::panic::set_hook(Box::new(|_| eprintln!("Scion fixture setup panicked; details withheld")));
    let args = Args::parse();
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    tokio::select! {
        result = tokio::time::timeout(HARD_LIMIT, serve(args)) => {
            result.context("15-minute fixture lifetime expired; owned cluster dropped")?
        }
        result = tokio::signal::ctrl_c() => { result?; Ok(()) }
        _ = terminate.recv() => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::fixture_layout_allowed;
    use std::path::Path;

    #[test]
    fn fixture_root_layout_guard() {
        let private = Path::new("/var/tmp/dungeon-scion-runtime-002-test/run-04");
        assert!(fixture_layout_allowed(&private.join("project"), private));
        assert!(fixture_layout_allowed(Path::new("/mnt/c/disposable/artifacts/scion-runtime-002/run-04/project"), private));
        for refused in [
            "/var/tmp/dungeon-scion-runtime-002-test/run-04",
            "/var/tmp/dungeon-scion-runtime-002-test/run-04/other",
            "/var/tmp/dungeon-scion-runtime-002-test/run-05/project",
            "/var/tmp/dungeon-scion-runtime-002-test/run-04/project/nested",
            "/mnt/c/disposable/project",
            "/mnt/c/disposable/artifacts/other/run-04/project",
            "/mnt/d/disposable/artifacts/scion-runtime-002/run-04/project",
            "/var/tmp/dungeon-scion-runtime-002-test/run-04/other/../project",
        ] {
            assert!(!fixture_layout_allowed(Path::new(refused), private), "unexpected fixture grant: {refused}");
        }
    }
}
