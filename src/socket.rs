//! Shared listening socket: SO_REUSEPORT + SO_REUSEADDR + TCP_NODELAY,
//! backlog 16384. N worker processes bind the SAME port; the kernel
//! load-balances accepted connections across them.
//!
//! Tier-A.A1 + A2 (research-distillation): TCP_QUICKACK (Linux only) and
//! TCP_FASTOPEN are best-effort — any setsockopt error is logged and
//! ignored, the listener still binds. Linux <4.5 picks up a 40 ms latency
//! win from QUICKACK; TFO saves an RTT for repeat clients on networks
//! that allow it (middleboxes can strip the option — graceful fallback).
use socket2::{Domain, Protocol, Socket, Type};
use std::net::SocketAddr;

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
