//! Shared listening socket: SO_REUSEPORT + SO_REUSEADDR + TCP_NODELAY,
//! backlog 16384. N worker processes bind the SAME port; the kernel
//! load-balances accepted connections across them.
//!
//! Tier-A.A1 + A2 (research-distillation): TCP_QUICKACK (Linux only) and
//! TCP_FASTOPEN are best-effort — any setsockopt error is logged and
//! ignored, the listener still binds. Linux <4.5 picks up a 40 ms latency
//! win from QUICKACK; TFO saves an RTT for repeat clients on networks
//! that allow it (middleboxes can strip the option — graceful fallback).
//!
//! T1.6 (byte-path mine FINDINGS): SO_KEEPALIVE + idle/interval/count.
//! Half-open connections (k8s pod evict, Lambda kill, spot termination,
//! a mobile client dropping off the network) leave a peer that never sends
//! FIN/RST — the connection sits ESTABLISHED on our side forever, burning a
//! `LANCELOT_MAX_CONNECTIONS` slot indefinitely (the exact resource that
//! gates concurrent AI workloads). With keepalive the kernel probes the dead
//! peer and reaps the socket, releasing the slot. Off by default (the OS
//! keepalive default is ~2 h — Blazeio sets idle=30/intvl=10/cnt=3 →
//! dead peer reaped in ~60 s); the operator opts in via
//! `LANCELOT_TCP_KEEPALIVE_SECS`. Like the QUICKACK/FASTOPEN knobs above it
//! is best-effort — a `set_tcp_keepalive` error is ignored and the listener
//! still binds.
use socket2::{Domain, Protocol, Socket, TcpKeepalive, Type};
use std::net::SocketAddr;
use std::time::Duration;

/// T1.6: TCP keepalive idle time, in seconds. `0` (default) = OFF — leave the
/// OS default (~2 h first probe), i.e. byte-identical to the pre-keepalive
/// behavior. When `>0`, SO_KEEPALIVE is enabled with this idle time; the probe
/// interval + count are derived (sane Blazeio-class values) so a half-open peer
/// is reaped in roughly `idle + interval*count` seconds. Clamped [0, 86400].
fn keepalive_secs() -> u64 {
    static S: std::sync::OnceLock<u64> = std::sync::OnceLock::new();
    *S.get_or_init(|| {
        std::env::var("LANCELOT_TCP_KEEPALIVE_SECS")
            .ok()
            .and_then(|s| s.trim().parse::<u64>().ok())
            .map(|n| n.min(86_400))
            .unwrap_or(0)
    })
}

pub fn bind_reuseport(addr: SocketAddr) -> std::io::Result<std::net::TcpListener> {
    let domain = if addr.is_ipv4() {
        Domain::IPV4
    } else {
        Domain::IPV6
    };
    let sock = Socket::new(domain, Type::STREAM, Some(Protocol::TCP))?;
    sock.set_reuse_address(true)?;
    #[cfg(unix)]
    sock.set_reuse_port(true)?;
    sock.set_nodelay(true)?;
    // T1.6: SO_KEEPALIVE + idle/interval/count (best-effort). Only when the
    // operator opts in (`LANCELOT_TCP_KEEPALIVE_SECS` > 0); the default leaves
    // the socket untouched. socket2's typed `set_tcp_keepalive` wraps the raw
    // setsockopt(SO_KEEPALIVE / TCP_KEEPIDLE / TCP_KEEPINTVL / TCP_KEEPCNT) —
    // `with_interval`/`with_retries` are available on Linux + macOS under the
    // `all` feature (already enabled in Cargo.toml). Interval = 10 s, count = 3
    // (Blazeio values): after `idle` seconds of silence the kernel probes every
    // 10 s and gives up after 3 misses, reaping the dead peer's slot. Error is
    // ignored — keepalive is a best-effort reclaim, never a bind blocker.
    let idle = keepalive_secs();
    if idle > 0 {
        let ka = TcpKeepalive::new()
            .with_time(Duration::from_secs(idle))
            .with_interval(Duration::from_secs(10))
            .with_retries(3);
        let _ = sock.set_tcp_keepalive(&ka);
    }
    // TCP_QUICKACK: Linux disables delayed-ACK for this socket (40 ms
    // win on kernels <4.5; harmless on newer). socket2 does not expose
    // this; raw setsockopt via libc. Other OSes: no-op.
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::io::AsRawFd;
        let fd = sock.as_raw_fd();
        let on: libc::c_int = 1;
        // SAFETY: fd is a valid socket FD owned by `sock`; we pass a
        // pointer to a stack int of the documented length. Error return
        // is checked & ignored — TCP_QUICKACK is best-effort (some
        // systems disallow it for non-root, or pre-3.15 lack it).
        let _ = unsafe {
            libc::setsockopt(
                fd,
                libc::IPPROTO_TCP,
                libc::TCP_QUICKACK,
                &on as *const _ as *const libc::c_void,
                std::mem::size_of_val(&on) as libc::socklen_t,
            )
        };
        // TCP_FASTOPEN: queue depth for SYN-cookie-shaped fast-open
        // (5 is the value Blazeio + Robyn use). Errors ignored:
        // unsupported kernels / sysctl-disabled return EOPNOTSUPP.
        let qlen: libc::c_int = 5;
        let _ = unsafe {
            libc::setsockopt(
                fd,
                libc::IPPROTO_TCP,
                23, /* TCP_FASTOPEN — libc doesn't expose on all targets */
                &qlen as *const _ as *const libc::c_void,
                std::mem::size_of_val(&qlen) as libc::socklen_t,
            )
        };
    }
    sock.bind(&addr.into())?;
    sock.listen(16384)?;
    Ok(sock.into())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn two_listeners_same_port() {
        let addr: SocketAddr = "127.0.0.1:0".parse().unwrap();
        let l1 = bind_reuseport(addr).unwrap();
        let port = l1.local_addr().unwrap().port();
        // Second bind on the SAME concrete port must succeed with REUSEPORT.
        let again: SocketAddr = format!("127.0.0.1:{port}").parse().unwrap();
        let l2 = bind_reuseport(again).expect("REUSEPORT second bind must succeed");
        assert_eq!(
            l1.local_addr().unwrap().port(),
            l2.local_addr().unwrap().port()
        );
    }
}
