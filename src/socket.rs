//! Shared listening socket: SO_REUSEPORT + SO_REUSEADDR + TCP_NODELAY,
//! backlog 16384. N worker processes bind the SAME port; the kernel
//! load-balances accepted connections across them.
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
