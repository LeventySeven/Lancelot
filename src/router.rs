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

    pub fn add(&mut self, method: &str, path: &str, slot: usize) {
        self.by_method
            .entry(method.to_string())
            .or_default()
            .insert(path.to_string(), slot)
            .expect("route insert");
    }

    /// Returns the handler slot for an exact/param match, else None (404).
    pub fn route(&self, method: &str, path: &str) -> Option<usize> {
        let m = self.by_method.get(method)?;
        m.at(path).ok().map(|hit| *hit.value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn matches_method_and_path() {
        let mut r = Router::new();
        r.add("POST", "/echo", 0);
        r.add("GET", "/users/{id}", 7);
        assert_eq!(r.route("POST", "/echo"), Some(0));
        assert_eq!(r.route("GET", "/users/42"), Some(7));
        assert_eq!(r.route("POST", "/nope"), None);
        assert_eq!(r.route("GET", "/echo"), None); // method-scoped
    }
}
