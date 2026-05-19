"""Doc-UI HTML shells (deep/03 §5/§6A.4-6A.5). CDN-pinned bootstraps
pointing at openapi_url."""


def swagger_ui_html(openapi_url: str, title: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head><body><div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({{url:"{openapi_url}",dom_id:"#swagger-ui"}})</script>
</body></html>"""


def redoc_html(openapi_url: str, title: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title></head>
<body><div id="redoc"></div>
<redoc spec-url="{openapi_url}"></redoc>
<script src="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"></script>
</body></html>"""


def scalar_html(openapi_url: str, title: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title></head>
<body><script id="api-reference" data-url="{openapi_url}"></script>
<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
</body></html>"""
