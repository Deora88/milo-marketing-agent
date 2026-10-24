"""Ad preview: read-only, returns only a facebook.com iframe URL, and explains why when there is none."""
import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from app.config import Settings
from app.db import store
from app.db.models import Campaign
from app.main import app
from app.meta import campaigns as meta
from app.meta.client import MetaClient

LIVE = Settings(_env_file=None, meta_dry_run=False, meta_access_token="t", meta_ad_account_id="123")
PREVIEW_URL = "https://graph.facebook.com/v26.0/777/previews"
BODY = '<iframe src="https://www.facebook.com/ads/api/preview_iframe.php?d=AQ&amp;t=AQ" width="540"></iframe>'


@respx.mock
async def test_preview_src_is_extracted_and_unescaped():
    route = respx.get(PREVIEW_URL).mock(return_value=httpx.Response(200, json={"data": [{"body": BODY}]}))
    c = MetaClient(LIVE)
    src = await meta.get_preview_src(c, "777", "MOBILE_FEED_STANDARD")
    assert src == "https://www.facebook.com/ads/api/preview_iframe.php?d=AQ&t=AQ"
    assert route.calls[0].request.url.params["ad_format"] == "MOBILE_FEED_STANDARD"
    await c.aclose()


@respx.mock
async def test_non_facebook_iframe_is_rejected():
    respx.get(PREVIEW_URL).mock(return_value=httpx.Response(200, json={"data": [{"body": '<iframe src="https://evil.example/x">'}]}))
    c = MetaClient(LIVE)
    assert await meta.get_preview_src(c, "777", "MOBILE_FEED_STANDARD") == ""
    await c.aclose()


def _campaign(**kw):
    return store.insert(Campaign(name="Bakery", daily_budget=50000, countries=["IN"], headline="H", primary_text="T",
                                 link_url="https://example.com", **kw)).id


def test_preview_endpoint_explains_dry_run():
    cid = _campaign(meta_ad_id="777")
    r = TestClient(app).get(f"/api/campaigns/{cid}/preview").json()
    assert r["available"] is False and "Dry run" in r["reason"]
    assert "INSTAGRAM_STANDARD" in r["formats"]


@pytest.mark.parametrize("fmt, code", [("MOBILE_FEED_STANDARD", 200), ("NOT_A_FORMAT", 400)])
def test_preview_endpoint_validates_format(fmt, code):
    cid = _campaign()
    assert TestClient(app).get(f"/api/campaigns/{cid}/preview?format={fmt}").status_code == code


@respx.mock
async def test_generate_preview_from_draft_sends_story_spec():
    route = respx.get("https://graph.facebook.com/v26.0/act_123/generatepreviews").mock(
        return_value=httpx.Response(200, json={"data": [{"body": BODY}]}))
    c = MetaClient(Settings(_env_file=None, meta_dry_run=False, meta_access_token="t", meta_ad_account_id="123",
                            meta_page_id="999", meta_instagram_account_id="555"))
    camp = Campaign(name="Cafe", daily_budget=30000, headline="Burgers", primary_text="Shakes",
                    link_url="https://example.com", meta_image_hash="abc")
    src = await meta.generate_preview_src(c, camp, "INSTAGRAM_STANDARD")
    assert src.startswith("https://www.facebook.com/")
    import json
    creative = json.loads(route.calls[0].request.url.params["creative"])
    assert creative["object_story_spec"]["page_id"] == "999"
    assert creative["object_story_spec"]["link_data"]["name"] == "Burgers"
    assert creative["object_story_spec"]["link_data"]["image_hash"] == "abc"
    assert creative["object_story_spec"]["instagram_user_id"] == "555"  # v22+ field, not instagram_actor_id
    await c.aclose()
