//! `contract-check --root <parity-contracts> --fds <descriptor set> [--baseline <fixtures dir>]`
//! `contract-check --root <parity-contracts> --tenant-maps <file.json>`: checks one tenant maps
//! file (JSON) against the schema and CONVENTIONS.md's "Tenant maps" rules.
//! `contract-check --root <parity-contracts> --logs <file.ndjson|-> [--logs ...] --catalogue
//! <events.<service>.v1.json> [--catalogue ...] [--skip-non-json]`: checks captured NDJSON log
//! lines against log-record.v1, the catalogues and the masking rules (0.13.0); exit 1 on any
//! violation. With `--flow <xsid>` instead prints that call's records in order (no checks); since
//! 0.16.0 `--flow` also takes an `op_id` or a `saga_id` and prints that operation's records.

use contract_check::logs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

const USAGE: &str = "usage: contract-check --root <dir> --fds <file> [--baseline <dir>]
       contract-check --root <dir> --tenant-maps <file.json>
       contract-check --root <dir> --logs <file.ndjson|-> [--logs ...] --catalogue <file.json> [--catalogue ...] [--skip-non-json]
       contract-check --root <dir> --logs <file.ndjson|-> [--logs ...] --flow <xsid|op_id|saga_id>";

fn main() -> ExitCode {
    let mut args = std::env::args().skip(1);
    let (mut root, mut fds, mut baseline, mut maps) = (None, None, None, None);
    let (mut logs, mut catalogues, mut flow, mut skip_non_json) = (vec![], vec![], None, false);
    while let Some(flag) = args.next() {
        if flag == "--skip-non-json" {
            skip_non_json = true;
            continue;
        }
        let Some(value) = args.next() else {
            eprintln!("{flag} needs a value\n{USAGE}");
            return ExitCode::from(2);
        };
        match flag.as_str() {
            "--root" => root = Some(PathBuf::from(value)),
            "--fds" => fds = Some(PathBuf::from(value)),
            "--baseline" => baseline = Some(PathBuf::from(value)),
            "--tenant-maps" => maps = Some(PathBuf::from(value)),
            "--logs" => logs.push(value),
            "--catalogue" => catalogues.push(PathBuf::from(value)),
            "--flow" => flow = Some(value),
            _ => {
                eprintln!("unknown argument {flag}\n{USAGE}");
                return ExitCode::from(2);
            }
        }
    }
    if let (Some(root), Some(maps)) = (&root, &maps) {
        return tenant_maps(root, maps);
    }
    if let (Some(root), false) = (&root, logs.is_empty()) {
        return check_logs(root, &logs, &catalogues, flow.as_deref(), skip_non_json);
    }
    let (Some(root), Some(fds)) = (root, fds) else {
        eprintln!("{USAGE}");
        return ExitCode::from(2);
    };
    match contract_check::run(&root, &fds, baseline.as_deref()) {
        Ok(report) => {
            for (dir, n) in &report.checked {
                println!("{dir}: {n} fixtures");
            }
            if report.errors.is_empty() {
                println!(
                    "coverage complete; {} fixtures checked",
                    report.checked.values().sum::<usize>()
                );
                ExitCode::SUCCESS
            } else {
                report.errors.iter().for_each(|e| eprintln!("error: {e}"));
                eprintln!("{} errors", report.errors.len());
                ExitCode::FAILURE
            }
        }
        Err(e) => {
            eprintln!("error: {e:#}");
            ExitCode::FAILURE
        }
    }
}

fn tenant_maps(root: &Path, file: &Path) -> ExitCode {
    let checked = contract_check::Schemas::load(root).and_then(|schemas| {
        let text = std::fs::read_to_string(file)?;
        let value: serde_json::Value = serde_json::from_str(&text)?;
        Ok(contract_check::tenants::violations(&schemas, &value))
    });
    match checked {
        Ok(found) if found.is_empty() => {
            println!("{}: valid", file.display());
            ExitCode::SUCCESS
        }
        Ok(found) => {
            found.iter().for_each(|v| eprintln!("error: {v}"));
            ExitCode::FAILURE
        }
        Err(e) => {
            eprintln!("error: {e:#}");
            ExitCode::FAILURE
        }
    }
}

fn read_log(name: &str) -> std::io::Result<String> {
    if name == "-" {
        let mut text = String::new();
        std::io::stdin().read_to_string(&mut text)?;
        Ok(text)
    } else {
        std::fs::read_to_string(name)
    }
}

/// At most this many violations are printed; the count is always complete.
const PRINTED: usize = 200;

fn check_logs(
    root: &Path,
    files: &[String],
    catalogues: &[PathBuf],
    flow: Option<&str>,
    skip_non_json: bool,
) -> ExitCode {
    let mut texts = Vec::new();
    for name in files {
        match read_log(name) {
            Ok(t) => texts.push(t),
            Err(e) => {
                eprintln!("error: reading {name}: {e}");
                return ExitCode::FAILURE;
            }
        }
    }
    if let Some(xsid) = flow {
        logs::flow(&texts, xsid)
            .iter()
            .for_each(|line| println!("{line}"));
        return ExitCode::SUCCESS;
    }
    if catalogues.is_empty() {
        eprintln!("--logs needs at least one --catalogue (or --flow)\n{USAGE}");
        return ExitCode::from(2);
    }
    let loaded = contract_check::Schemas::load(root).and_then(|schemas| {
        let masking = logs::Masking::load(root)?;
        Ok((schemas, masking))
    });
    let (schemas, masking) = match loaded {
        Ok(v) => v,
        Err(e) => {
            eprintln!("error: {e:#}");
            return ExitCode::FAILURE;
        }
    };
    let cats = match logs::Catalogues::load(root, &schemas, catalogues) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("error: {e:#}");
            return ExitCode::FAILURE;
        }
    };
    // Each file is one capture: seqs are compared within a file (a seq 1 starts a new process).
    let mut checker = logs::Checker::new(&schemas, &cats, &masking);
    let (mut lines, mut found) = (0, 0);
    for (name, text) in files.iter().zip(&texts) {
        checker.new_stream();
        let (n, violations) = logs::check_ndjson(&mut checker, text, skip_non_json);
        lines += n;
        for (line, v) in violations {
            if found < PRINTED {
                eprintln!("{name}:{line}: {v}");
            }
            found += 1;
        }
    }
    println!("{lines} log lines checked, {found} violations");
    if found == 0 {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}
