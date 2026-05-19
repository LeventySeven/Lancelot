from lancelot import App
from lancelot.params import Depends, Security
from lancelot.security import OAuth2PasswordBearer, APIKeyHeader

oauth2 = OAuth2PasswordBearer(tokenUrl="token", scopes={"read": "Read"})
apikey = APIKeyHeader(name="X-Key")


def test_openapi_security_schemes_and_operation_security():
    app = App()

    @app.get("/a")
    def a(t=Depends(oauth2)):
        return {}

    @app.get("/b")
    def b(k=Security(apikey, scopes=["read"])):
        return {}

    doc = app.openapi()
    cs = doc["components"]["securitySchemes"]
    assert cs["OAuth2PasswordBearer"] == {
        "type": "oauth2",
        "flows": {"password": {"scopes": {"read": "Read"}, "tokenUrl": "token"}}}
    assert cs["APIKeyHeader"] == {"type": "apiKey", "in": "header", "name": "X-Key"}
    assert doc["paths"]["/a"]["get"]["security"] == [{"OAuth2PasswordBearer": []}]
    assert doc["paths"]["/b"]["get"]["security"] == [{"APIKeyHeader": ["read"]}]
