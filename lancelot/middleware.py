"""Two-tier middleware (U20/U21). `app.add_middleware(cls, **opts)` is the
pure-ASGI escape hatch — an unmodified ASGI middleware wrapping the app
callable, NEVER BaseHTTPMiddleware/call_next. CORSMiddleware (U21) lands
in M1-P4 T8."""
