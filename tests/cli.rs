use std::fs;
use std::process::Command;

const PYTHON_ENV: &str = "PHP_READABILITY_PYTHON";
const MISSING_PYTHON: &str = "/definitely/missing/php-readability-python";

fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_php-readability")
}

fn unique_temp_dir(name: &str) -> std::path::PathBuf {
    std::env::temp_dir().join(format!("php-readability-{name}-{}", std::process::id()))
}

#[test]
fn prints_json_for_empty_project() {
    let project = unique_temp_dir("empty-project");
    fs::create_dir_all(&project).expect("create empty project");

    let output = Command::new(binary())
        .arg(&project)
        .arg("--json")
        .output()
        .expect("run php-readability");

    assert!(output.status.success());
    assert_eq!(String::from_utf8_lossy(&output.stdout).trim(), "[]");
    assert!(output.stderr.is_empty());

    fs::remove_dir_all(project).expect("remove empty project");
}

#[test]
fn exits_127_when_python_is_missing() {
    let output = Command::new(binary())
        .arg("--json")
        .env(PYTHON_ENV, MISSING_PYTHON)
        .output()
        .expect("run php-readability");

    assert_eq!(output.status.code(), Some(127));
    assert!(String::from_utf8_lossy(&output.stderr).contains("failed to exec"));
}
