"""Doc-UI HTML shells (deep/03 §5/§6A.4-6A.5). CDN-pinned bootstraps
pointing at openapi_url. A2: title is HTML-escaped; openapi_url is a JSON
string literal in JS contexts and HTML-attr-escaped in attribute contexts
(defense-in-depth — these are public functions; in-tree callers pass only
constants)."""
import html
import json


def swagger_ui_html(openapi_url: str, title: str) -> str:
    t = html.escape(title)
    u = json.dumps(openapi_url)  # safe JS string literal
    return f"""<!DOCTYPE html>
<html><head><title>{t}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head><body><div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({{url:{u},dom_id:"#swagger-ui"}})</script>
</body></html>"""


def redoc_html(openapi_url: str, title: str) -> str:
    t = html.escape(title)
    u = html.escape(openapi_url, quote=True)
    return f"""<!DOCTYPE html>
<html><head><title>{t}</title></head>
<body><div id="redoc"></div>
<redoc spec-url="{u}"></redoc>
<script src="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"></script>
</body></html>"""


def scalar_html(openapi_url: str, title: str) -> str:
    t = html.escape(title)
    u = html.escape(openapi_url, quote=True)
    return f"""<!DOCTYPE html>
<html><head><title>{t}</title></head>
<body><script id="api-reference" data-url="{u}"></script>
<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
</body></html>"""
