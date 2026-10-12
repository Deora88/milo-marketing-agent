"""Publishing builds campaign -> ad set -> creative -> ad, and resumes after a half-way failure."""
import pytest

from app.config import Settings
from app.db.models import Campaign
from app.meta import campaigns as meta
from app.meta.client import MetaAPIError


class FakeClient:
    def __init__(self, fail_on=None, post_id=""):
        self.s = Settings(_env_file=None, meta_ad_account_id="123", meta_page_id="999", meta_page_post_id=post_id)
        self.account = "act_123"
        self.calls, self.fail_on, self.n, self.fail_subcode = [], fail_on, 0, 1870227

    async def post(self, path, **data):
        self.calls.append((path, data))
        if self.fail_on and path.endswith(self.fail_on):
            self.fail_on = None
            raise MetaAPIError(100, self.fail_subcode, "Invalid parameter")
        self.n += 1
        return {"id": f"id{self.n}"}


def _campaign():
    return Campaign(name="Bakery", daily_budget=50000, countries=["IN"], age_min=22, age_max=45,
                    headline="H", primary_text="T", link_url="https://example.com")


async def test_targeting_sets_advantage_audience_explicitly():
    c, client = _campaign(), FakeClient()
    await meta.create_on_meta(client, c)
    adset = next(d for p, d in client.calls if p.endswith("/adsets"))
    assert adset["targeting"]["targeting_automation"] == {"advantage_audience": 0}
    assert (adset["targeting"]["age_min"], adset["targeting"]["age_max"]) == (22, 45)
    assert all([c.meta_campaign_id, c.meta_adset_id, c.meta_creative_id, c.meta_ad_id])


async def test_failed_publish_resumes_without_duplicate_campaign():
    c, client = _campaign(), FakeClient(fail_on="/adsets")
    with pytest.raises(MetaAPIError):
        await meta.create_on_meta(client, c)
    assert c.meta_campaign_id and not c.meta_adset_id
    first_campaign = c.meta_campaign_id

    await meta.create_on_meta(client, c)  # second Approve
    paths = [p for p, _ in client.calls]
    assert paths.count("/act_123/campaigns") == 1
    assert c.meta_campaign_id == first_campaign
    assert c.meta_adset_id and c.meta_creative_id and c.meta_ad_id


@pytest.mark.parametrize("raw, expected", [
    ("", ""),
    ("555", "999_555"),
    ("999_555", "999_555"),
    ("https://www.facebook.com/permalink.php?story_fbid=555&id=999", "999_555"),
    ("https://www.facebook.com/MiloTestPage/posts/555", "999_555"),
])
def test_existing_post_id_formats(raw, expected):
    assert meta.existing_post_id("999", raw) == expected


async def test_existing_page_post_is_used_as_creative():
    c, client = _campaign(), FakeClient(post_id="555")
    await meta.create_on_meta(client, c)
    creative = next(d for p, d in client.calls if p.endswith("/adcreatives"))
    assert creative["object_story_id"] == "999_555" and "object_story_spec" not in creative
    assert c.meta_ad_id


async def test_dev_mode_error_explains_the_fix():
    c, client = _campaign(), FakeClient(fail_on="/adcreatives")
    client.fail_subcode = 1885183
    with pytest.raises(MetaAPIError) as e:
        await meta.create_on_meta(client, c)
    assert "META_PAGE_POST_ID" in str(e.value)
    assert c.meta_adset_id and not c.meta_creative_id  # resumes from the creative next time


def test_local_radius_targeting_replaces_countries():
    c = _campaign()
    c.location_name, c.latitude, c.longitude, c.radius_km = "Vaishali, Ghaziabad", 28.6457, 77.339, 4
    geo = meta.build_targeting(c)["geo_locations"]
    assert "countries" not in geo
    assert geo["custom_locations"] == [{"latitude": 28.6457, "longitude": 77.339, "radius": 4,
                                        "distance_unit": "kilometer"}]
    assert "countries" in meta.build_targeting(_campaign())["geo_locations"]


@pytest.mark.parametrize("raw", [
    "https://www.facebook.com/permalink.php?story_fbid=pfbid02FAtj44MRQWxTrUR5wc&id=61595172217017",
    "pfbid02FAtj44MRQ",
])
def test_pfbid_links_are_rejected_with_help(raw):
    with pytest.raises(ValueError, match="numeric post id"):
        meta.existing_post_id("999", raw)
