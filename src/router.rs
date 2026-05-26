//! Per-method matchit router. Const-tier is a stub in M0 (returns None);
//! the real pre-baked CachedResponse tier lands post-M0.
use matchit::Router as Matchit;
use std::collections::HashMap;

#[derive(Default)]
pub struct Router {
    by_method: HashMap<String, Matchit<usize>>, // value = handler slot index
    // M7 T4.10: registered path templates per method, captured at add() time.
    // Matchit doesn't expose iteration of its inserted patterns; we track them
    // separately so the 404 path can suggest a closest-match via Levenshtein
    // ("did you mean /users/{id}?") — LanceDB pattern, pays nothing on the
    // happy path, fires only when no route matches.
    patterns: HashMap<String, Vec<String>>,
}

impl Router {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register a route. Returns `Err(message)` on a matchit Conflict
    /// (duplicate/overlapping route) or malformed path — audit HIGH #3:
    /// the M0 `.expect()` panicked across the FFI seam at registration on
    /// any user route mistake. Surface a clean error instead.
    pub fn add(&mut self, method: &str, path: &str, slot: usize) -> Result<(), String> {
        self.by_method
            .entry(method.to_string())
            .or_default()
            .insert(path.to_string(), slot)
            .map_err(|e| format!("route insert failed for {method} {path}: {e}"))?;
        self.patterns
            .entry(method.to_string())
            .or_default()
            .push(path.to_string());
        Ok(())
    }

    /// Returns the handler slot for an exact/param match, else None (404).
    pub fn route(&self, method: &str, path: &str) -> Option<usize> {
        let m = self.by_method.get(method)?;
        m.at(path).ok().map(|hit| *hit.value)
    }

    /// True if `path` matches a route under ANY method — lets the seam emit
    /// 405 Method Not Allowed (path exists, wrong method) vs 404 (no such
    /// path), matching HTTP semantics / FastAPI (domain-web).
    pub fn matches_any_method(&self, path: &str) -> bool {
        self.by_method.values().any(|m| m.at(path).is_ok())
    }

    /// M7 T4.10: find the closest registered path under any method for a
    /// 404'd request — Levenshtein distance, threshold = max(2, q.len()/3).
    /// Returns None if no path is "close enough" (avoids misleading
    /// suggestions). Pure-Rust, no extra dep.
    pub fn closest_path(&self, query: &str) -> Option<String> {
        // DoS guard: this runs on every 404 with the attacker-controlled URI.
        // hyper accepts URIs up to ~hundreds of KB; without a cap, levenshtein
        // allocates O(uri_len) per registered route per request. Real typo'd
        // paths are tiny — anything over 256 bytes gets the plain 404, no
        // suggestion. Caps per-call work to a constant regardless of input.
        if query.len() > 256 {
            return None;
        }
        let q_lower = query.to_ascii_lowercase();
        let threshold = (query.len() / 3).max(2);
        let mut best: Option<(usize, &String)> = None;
        for paths in self.patterns.values() {
            for p in paths {
                let d = levenshtein(&q_lower, &p.to_ascii_lowercase());
                if d <= threshold && best.map_or(true, |(bd, _)| d < bd) {
                    best = Some((d, p));
                }
            }
        }
        best.map(|(_, p)| p.clone())
    }
}

/// Minimal Levenshtein distance (stdlib-only, O(n*m) memory using two rows).
/// Used only on the 404 path; not hot. ~30 LOC.
fn levenshtein(a: &str, b: &str) -> usize {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    let m = a.len();
    let n = b.len();
    if m == 0 {
        return n;
    }
    if n == 0 {
        return m;
    }
    let mut prev: Vec<usize> = (0..=n).collect();
    let mut curr = vec![0usize; n + 1];
    for (i, ca) in a.iter().enumerate() {
        curr[0] = i + 1;
        for (j, cb) in b.iter().enumerate() {
            let cost = if ca == cb { 0 } else { 1 };
            curr[j + 1] = (curr[j] + 1)
                .min(prev[j + 1] + 1)
                .min(prev[j] + cost);
        }
        std::mem::swap(&mut prev, &mut curr);
    }
    prev[n]
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn matches_method_and_path() {
        let mut r = Router::new();
        r.add("POST", "/echo", 0).unwrap();
        r.add("GET", "/users/{id}", 7).unwrap();
        assert_eq!(r.route("POST", "/echo"), Some(0));
        assert_eq!(r.route("GET", "/users/42"), Some(7));
        assert_eq!(r.route("POST", "/nope"), None);
        assert_eq!(r.route("GET", "/echo"), None); // method-scoped
    }

    #[test]
    fn duplicate_route_is_err_not_panic() {
        let mut r = Router::new();
        r.add("GET", "/x", 0).unwrap();
        // Same path twice → matchit Conflict → clean Err (no panic).
        assert!(r.add("GET", "/x", 1).is_err());
    }

    #[test]
    fn wrong_method_is_405_not_404() {
        let mut r = Router::new();
        r.add("GET", "/echo", 0).unwrap();
        // DELETE /echo: no GET-match, but the path exists → 405 candidate.
        assert_eq!(r.route("DELETE", "/echo"), None);
        assert!(r.matches_any_method("/echo")); // → seam emits 405
        assert!(!r.matches_any_method("/nope")); // → seam emits 404
    }

    // M7 T4.10 — Levenshtein "did you mean?" tests.

    #[test]
    fn closest_path_suggests_near_match() {
        let mut r = Router::new();
        r.add("GET", "/users", 0).unwrap();
        r.add("GET", "/healthz", 1).unwrap();
        r.add("POST", "/items", 2).unwrap();
        // typo: /user → /users (distance 1)
        assert_eq!(r.closest_path("/user"), Some("/users".to_string()));
        // typo: /healtz → /healthz (distance 1)
        assert_eq!(r.closest_path("/healtz"), Some("/healthz".to_string()));
        // exact match — also "closest", since distance 0
        assert_eq!(r.closest_path("/users"), Some("/users".to_string()));
    }

    #[test]
    fn closest_path_oversized_query_is_rejected_cheaply() {
        let mut r = Router::new();
        r.add("GET", "/users", 0).unwrap();
        // DoS guard: a huge attacker URI returns None without running the
        // O(len) levenshtein DP per route. Anything > 256 bytes is dropped.
        let huge = "/".to_string() + &"a".repeat(500_000);
        assert_eq!(r.closest_path(&huge), None);
        // boundary: 256 chars still considered; 257 rejected.
        assert!(r.closest_path(&"a".repeat(257)).is_none());
    }

    #[test]
    fn closest_path_returns_none_for_far_paths() {
        let mut r = Router::new();
        r.add("GET", "/users", 0).unwrap();
        // /completely_different — too far, distance > threshold.
        assert_eq!(r.closest_path("/completely_different"), None);
    }

    #[test]
    fn closest_path_is_case_insensitive() {
        let mut r = Router::new();
        r.add("GET", "/Users", 0).unwrap();
        assert_eq!(r.closest_path("/users"), Some("/Users".to_string()));
    }

    #[test]
    fn closest_path_empty_router_returns_none() {
        let r = Router::new();
        assert_eq!(r.closest_path("/anything"), None);
    }
}
