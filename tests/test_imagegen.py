"""Ad image generation (FLUX.1 [schnell] on Cloudflare Workers AI), with Cloudflare mocked."""
import base64
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.creative import imagegen
from app.db import store
from app.db.models import AuditLog, Campaign
from app.main import app
from app.mcp_server.server import TOOL_NAMES, generate_ad_image
from tests.conftest import WEB

CF = Settings(_env_file=None, cloudflare_account_id="acc123", cloudflare_api_token="tok", image_daily_limit=2)
URL = "https://api.cloudflare.com/client/v4/accounts/acc123/ai/run/@cf/black-forest-labs/flux-1-schnell"
JPEG = b"\xff\xd8\xff fake jpeg"
OK = {"success": True, "result": {"image": base64.b64encode(JPEG).decode()}}


def _draft(**kw):
    return store.insert(Campaign(name="Cafe", daily_budget=20000, headline="Burgers in Vaishali",
                                 primary_text="Fresh shakes", link_url="https://example.com", **kw)).id


@pytest.fixture(autouse=True)
def _tmp_images(tmp_path, monkeypatch):
    monkeypatch.setattr(imagegen, "IMAGE_DIR", tmp_path)


def test_default_model_is_flux_schnell():
    assert Settings(_env_file=None).image_model == "@cf/black-forest-labs/flux-1-schnell"
    assert "generate_ad_image" in TOOL_NAMES


@respx.mock
async def test_generates_and_attaches_image_without_text():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=OK))
    cid = _draft()
    c = await imagegen.generate_for_campaign(cid, "burger and milkshake on a cafe table", settings=CF)
    sent = json.loads(route.calls[0].request.content)
    assert route.calls[0].request.headers["authorization"] == "Bearer tok"
    assert "no text" in sent["prompt"] and sent["steps"] == 4
    assert open(c.image_path, "rb").read() == JPEG and c.meta_image_hash is None
    assert store.find(AuditLog, {"event": "image_generated"})


@respx.mock
async def test_daily_limit_and_errors():
    respx.post(URL).mock(return_value=httpx.Response(200, json=OK))
    cid = _draft()
    for _ in range(2):
        await imagegen.generate_for_campaign(cid, "x", settings=CF)
    with pytest.raises(imagegen.ImageGenUnavailable, match="Daily image limit"):
        await imagegen.generate_for_campaign(cid, "x", settings=CF)


@respx.mock
@pytest.mark.parametrize("status, body, match", [
    (401, {"success": False, "errors": [{"message": "Authentication error"}]}, "rejected the API token"),
    (429, {"success": False}, "allowance"),
    (400, {"success": False, "errors": [{"message": "bad prompt"}]}, "bad prompt"),
])
async def test_cloudflare_errors_are_clear(status, body, match):
    respx.post(URL).mock(return_value=httpx.Response(status, json=body))
    with pytest.raises(imagegen.ImageGenUnavailable, match=match):
        await imagegen.generate_for_campaign(_draft(), "x", settings=CF)


async def test_not_configured_and_already_on_meta():
    with pytest.raises(imagegen.ImageGenUnavailable, match="CLOUDFLARE_ACCOUNT_ID"):
        await imagegen.generate_for_campaign(_draft(), "x", settings=Settings(_env_file=None))
    with pytest.raises(ValueError, match="already created on Meta"):
        await imagegen.generate_for_campaign(_draft(meta_creative_id="c1"), "x", settings=CF)


async def test_mcp_tool_degrades_gracefully_without_keys():
    r = await generate_ad_image(_draft(), "a cafe")
    assert r["status"] == "unavailable" and "upload" in r["message"]


def test_api_endpoint_and_status():
    c = TestClient(app, headers=WEB)
    cid = _draft()
    r = c.post(f"/api/campaigns/{cid}/generate-image", json={"prompt": "a cafe"})
    assert r.status_code == 503 and "CLOUDFLARE" in r.json()["detail"]
    st = c.get("/api/status").json()["image_generation"]
    assert st["configured"] is False and st["model"] == "@cf/black-forest-labs/flux-1-schnell"
    assert TestClient(app).post(f"/api/campaigns/{cid}/generate-image", json={}).status_code == 403  # CSRF header
