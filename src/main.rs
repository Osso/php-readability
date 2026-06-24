use std::fs;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::Command;

const AUDIT_SCRIPT: &str = include_str!("../scripts/audit-repo.py");
const PYTHON: &str = "/usr/bin/python3";

fn main() {
    let script_path = match write_embedded_script() {
        Ok(path) => path,
        Err(error) => {
            eprintln!("php-readability: failed to prepare embedded audit script: {error}");
            std::process::exit(1);
        }
    };

    let error = Command::new(PYTHON)
        .arg(script_path)
        .args(std::env::args_os().skip(1))
        .exec();

    eprintln!("php-readability: failed to exec {PYTHON}: {error}");
    std::process::exit(127);
}

fn write_embedded_script() -> std::io::Result<PathBuf> {
    let script_path = embedded_script_path();
    let temp_path = script_path.with_extension(format!("{}.tmp", std::process::id()));

    fs::write(&temp_path, AUDIT_SCRIPT)?;
    fs::rename(&temp_path, &script_path)?;
    Ok(script_path)
}

fn embedded_script_path() -> PathBuf {
    std::env::temp_dir().join("php-readability-audit-repo.py")
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
}
