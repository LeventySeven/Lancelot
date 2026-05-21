//! cgroup-v2/v1 aware CPU count. Fixes Robyn's os.cpu_count() meltdown
//! (containers report host CPUs, not the cgroup quota → thread explosion).
use std::path::Path;

/// Effective worker count: an explicit `LANCELOT_WORKERS` override wins (the
/// operator-facing `--workers` equivalent — needed where cgroup detection can't
/// see the real allocation, e.g. macOS / non-cgroup hosts, or to pin width);
/// else cgroup quota if present, else logical CPUs. Clamped [1, 64].
pub fn worker_count() -> usize {
    let detected = workers_from(Path::new("/sys/fs/cgroup"), num_cpus_fallback());
    apply_override(std::env::var("LANCELOT_WORKERS").ok(), detected)
}

/// Apply the `LANCELOT_WORKERS` override: a positive integer pins the count
/// (clamped to 64); anything missing/invalid/zero falls back to `detected`.
fn apply_override(env: Option<String>, detected: usize) -> usize {
    match env
        .as_deref()
        .map(str::trim)
        .and_then(|s| s.parse::<usize>().ok())
    {
        Some(n) if n >= 1 => n.min(64),
        _ => detected,
    }
}

fn num_cpus_fallback() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1)
}

fn workers_from(cgroup_root: &Path, logical: usize) -> usize {
    // cgroup v2: cpu.max = "<quota> <period>" or "max <period>"
    let v2 = cgroup_root.join("cpu.max");
    let quota_cpus = std::fs::read_to_string(&v2).ok().and_then(|s| {
        let mut it = s.split_whitespace();
        let quota = it.next()?;
        let period: f64 = it.next()?.parse().ok()?;
        if quota == "max" {
            return None;
        }
        let q: f64 = quota.parse().ok()?;
        if period <= 0.0 {
            return None;
        }
        Some((q / period).ceil() as usize)
    });
    let n = quota_cpus.unwrap_or(logical).min(logical).max(1);
    n.min(64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn cgroup_v2_quota_caps_workers() {
        let dir = std::env::temp_dir().join(format!("lc_cg_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut f = std::fs::File::create(dir.join("cpu.max")).unwrap();
        write!(f, "150000 100000").unwrap(); // 1.5 cores
        assert_eq!(workers_from(&dir, 32), 2); // ceil(1.5) = 2, capped by quota
    }

    #[test]
    fn no_cgroup_uses_logical_clamped() {
        let dir = std::env::temp_dir().join("lc_cg_none");
        let _ = std::fs::remove_dir_all(&dir);
        assert_eq!(workers_from(&dir, 8), 8);
        assert_eq!(workers_from(&dir, 999), 64);
    }

    #[test]
    fn workers_override_wins_when_valid() {
        // A valid positive override pins the count (clamped to 64), regardless
        // of detected width — the operator-facing --workers knob.
        assert_eq!(apply_override(Some("1".into()), 10), 1);
        assert_eq!(apply_override(Some(" 4 ".into()), 10), 4);
        assert_eq!(apply_override(Some("999".into()), 10), 64);
        // Missing / invalid / zero → fall back to detected.
        assert_eq!(apply_override(None, 10), 10);
        assert_eq!(apply_override(Some("0".into()), 10), 10);
        assert_eq!(apply_override(Some("nope".into()), 10), 10);
        assert_eq!(apply_override(Some("".into()), 10), 10);
    }
}
