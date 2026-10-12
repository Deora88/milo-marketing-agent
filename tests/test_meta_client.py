import httpx
import pytest
import respx

from app.config import Settings
from app.meta.client import MetaAPIError, MetaClient, max_usage_pct

S = Settings(meta_dry_run=False, meta_access_token="t", meta_ad_account_id="123")
URL = "https://graph.facebook.com/v26.0/act_123/campaigns"


def test_usage_header_parsing():
    h = httpx.Headers({"x-business-use-case-usage": '{"123":[{"call_count":82,"total_time":10}]}',
                       "x-ad-account-usage": '{"acc_id_util_pct":40}'})
    assert max_usage_pct(h) == 82


@respx.mock
async def test_retries_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr("app.meta.client.asyncio.sleep", _no_sleep)
    route = respx.post(URL).mock(side_effect=[
        httpx.Response(400, json={"error": {"code": 17, "error_subcode": 2446079, "message": "limit"}}),
        httpx.Response(200, json={"id": "999"}),
    ])
    c = MetaClient(S)
    assert (await c.post("/act_123/campaigns", name="x"))["id"] == "999"
    assert route.call_count == 2
    await c.aclose()


@respx.mock
async def test_non_rate_limit_error_raises():
    respx.post(URL).mock(return_value=httpx.Response(400, json={"error": {"code": 100, "message": "bad param"}}))
    c = MetaClient(S)
    with pytest.raises(MetaAPIError):
        await c.post("/act_123/campaigns", name="x")
    await c.aclose()


async def _no_sleep(_):
    return None


@pytest.mark.parametrize("raw", ["123456", "act_123456", " act_123456 "])
async def test_account_id_with_or_without_prefix(raw):
    c = MetaClient(Settings(_env_file=None, meta_ad_account_id=raw))
    assert c.account == "act_123456"
    await c.aclose()


@respx.mock
async def test_error_includes_meta_user_message():
    respx.post(URL).mock(return_value=httpx.Response(400, json={"error": {
        "code": 100, "error_subcode": 1885183, "message": "Invalid parameter",
        "error_user_title": "Ads creative post was created by an app that is in development mode",
        "error_user_msg": "It must be in public to create this ad."}}))
    c = MetaClient(S)
    with pytest.raises(MetaAPIError) as e:
        await c.post("/act_123/campaigns", name="x")
    assert "development mode" in str(e.value) and e.value.subcode == 1885183
    await c.aclose()
