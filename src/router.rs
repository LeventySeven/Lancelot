//! Per-method matchit router. Const-tier is a stub in M0 (returns None);
//! the real pre-baked CachedResponse tier lands post-M0.
use matchit::Router as Matchit;
use std::collections::HashMap;

#[derive(Default)]
pub struct Router {
    by_method: HashMap<String, Matchit<usize>>, // value = handler slot index
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
            .map_err(|e| format!("route insert failed for {method} {path}: {e}"))
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
}
