use std::ffi::{OsStr, OsString};
use std::fs;
#[cfg(not(coverage))]
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};

const AUDIT_SCRIPT: &str = include_str!("../scripts/audit-repo.py");
const DEFAULT_PYTHON: &str = "/usr/bin/python3";
#[cfg(test)]
const MISSING_PYTHON: &str = "/definitely/missing/php-readability-python";
static TEMP_FILE_COUNTER: AtomicU64 = AtomicU64::new(0);

fn main() {
    std::process::exit(run(std::env::args_os().skip(1), python_path()));
}

fn run(args: impl IntoIterator<Item = OsString>, python: impl AsRef<OsStr>) -> i32 {
    let script_path = match write_embedded_script() {
        Ok(path) => path,
        Err(error) => {
            eprintln!("php-readability: failed to prepare embedded audit script: {error}");
            return 1;
        }
    };

    execute_script(python.as_ref(), script_path, args)
}

fn python_path() -> OsString {
    std::env::var_os("PHP_READABILITY_PYTHON").unwrap_or_else(|| OsString::from(DEFAULT_PYTHON))
}

#[cfg(not(coverage))]
fn execute_script(
    python: &OsStr,
    script_path: PathBuf,
    args: impl IntoIterator<Item = OsString>,
) -> i32 {
    let error = Command::new(python).arg(script_path).args(args).exec();

    eprintln!(
        "php-readability: failed to exec {}: {error}",
        python.to_string_lossy()
    );
    127
}

#[cfg(coverage)]
fn execute_script(
    python: &OsStr,
    script_path: PathBuf,
    args: impl IntoIterator<Item = OsString>,
) -> i32 {
    match Command::new(python).arg(script_path).args(args).status() {
        Ok(status) => status.code().unwrap_or(1),
        Err(error) => {
            eprintln!(
                "php-readability: failed to exec {}: {error}",
                python.to_string_lossy()
            );
            127
        }
    }
}

fn write_embedded_script() -> std::io::Result<PathBuf> {
    let script_path = embedded_script_path();
    let temp_path = temporary_script_path(&script_path);
    if let Some(parent) = script_path.parent() {
        fs::create_dir_all(parent)?;
    }

    fs::write(&temp_path, AUDIT_SCRIPT)?;
    fs::rename(&temp_path, &script_path)?;
    Ok(script_path)
}

fn embedded_script_path() -> PathBuf {
    std::env::temp_dir().join("php-readability-audit-repo.py")
}

fn temporary_script_path(script_path: &std::path::Path) -> PathBuf {
    let sequence = TEMP_FILE_COUNTER.fetch_add(1, Ordering::Relaxed);

    script_path.with_extension(format!("{}.{}.tmp", std::process::id(), sequence))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedded_script_contains_audit_entrypoint() {
        assert!(AUDIT_SCRIPT.starts_with("#!/usr/bin/env python3"));
        assert!(AUDIT_SCRIPT.contains("Readability audit for PHP repos"));
    }

    #[test]
    fn writes_embedded_script_to_temp_dir() {
        let path = write_embedded_script().expect("write embedded script");
        let contents = fs::read_to_string(&path).expect("read embedded script");

        assert_eq!(path, embedded_script_path());
        assert!(contents.contains("Usage: audit-repo.py"));
    }

    #[test]
    fn temporary_script_path_is_unique_per_call() {
        let script_path = embedded_script_path();

        assert_ne!(
            temporary_script_path(&script_path),
            temporary_script_path(&script_path)
        );
    }

    #[test]
    fn run_reports_missing_python() {
        let code = run([OsString::from("--json")], MISSING_PYTHON);

        assert_eq!(code, 127);
    }
}
