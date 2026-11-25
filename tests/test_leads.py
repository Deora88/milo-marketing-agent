"""Leads in the Inbox: list with sender names, and the CSV download for Excel."""
import csv
import io

from fastapi.testclient import TestClient

from app.db import store
from app.db.models import CustomerProfile, Lead, now
from app.main import app
from tests.conftest import WEB


def _lead(**kw):
    base = dict(platform="messenger", page_id="999", customer_id="2919676", phone="9871701615",
                note="How much does a car wash cost? 9871701615")
    return store.insert(Lead(**{**base, **kw}))


def test_leads_list_has_sender_name_newest_first():
    store.insert(CustomerProfile(platform="messenger", customer_id="2919676", name="Himanshu Singh", fetched_at=now()))
    _lead()
    _lead(customer_id="igsid_1", platform="instagram", phone="9811122233", note="call me")
    rows = TestClient(app, headers=WEB).get("/api/leads").json()
    assert [r["phone"] for r in rows] == ["9811122233", "9871701615"]
    assert rows[1]["sender"]["name"] == "Himanshu Singh" and rows[1]["sender"]["id"] == "2919676"
    assert rows[0]["sender"]["name"] == ""  # no lookup yet: the Inbox shows "Unknown sender" + id


def test_csv_download_opens_in_excel():
    store.insert(CustomerProfile(platform="messenger", customer_id="2919676", name="हिमांशु", fetched_at=now()))
    _lead(phone="+919871701615")
    _lead(customer_id="x", name="=HYPERLINK(\"http://evil\")", phone="", note="@SUM(1+1)")
    r = TestClient(app).get("/api/leads.csv")  # a plain GET, like a download link
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="milo-leads-' in r.headers["content-disposition"]
    assert r.content.startswith("﻿".encode())  # BOM: Excel reads UTF-8 (Hindi names)
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0] == ["Captured at (UTC)", "Name", "Phone", "Platform", "Sender ID", "Message"]
    evil, good = rows[1], rows[2]
    assert good[1:5] == ["हिमांशु", "+919871701615", "messenger", "2919676"]  # phone not mangled
    assert evil[1].startswith("'=") and evil[5].startswith("'@")  # formulas shown as text, never run


def test_csv_with_no_leads_has_just_the_header():
    r = TestClient(app).get("/api/leads.csv")
    assert r.content.decode("utf-8-sig").strip() == "Captured at (UTC),Name,Phone,Platform,Sender ID,Message"
