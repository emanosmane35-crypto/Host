"""
Backend API tests for the canary Discord-bot website.
Covers: GET /api/, GET/POST /api/status, GET /api/bot/status (live bot data).
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://mark-stripper-4.preview.emergentagent.com").rstrip("/")


@pytest.fixture(scope="session")
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---- Module: hello world ---------------------------------------------------
class TestRoot:
    def test_root_hello_world(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/")
        assert r.status_code == 200
        data = r.json()
        assert data.get("message") == "Hello World"


# ---- Module: StatusCheck CRUD ---------------------------------------------
class TestStatusCheck:
    def test_post_status_check_creates_doc(self, api_client):
        payload = {"client_name": "TEST_pytest_client"}
        r = api_client.post(f"{BASE_URL}/api/status", json=payload)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["client_name"] == "TEST_pytest_client"
        assert "id" in data and isinstance(data["id"], str) and len(data["id"]) > 0
        assert "timestamp" in data

    def test_get_status_checks_includes_created(self, api_client):
        # ensure at least one TEST_ doc exists
        api_client.post(f"{BASE_URL}/api/status", json={"client_name": "TEST_listcheck"})
        r = api_client.get(f"{BASE_URL}/api/status")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        # Should not leak Mongo _id
        for doc in data:
            assert "_id" not in doc
        names = [d.get("client_name") for d in data]
        assert "TEST_listcheck" in names


# ---- Module: /api/bot/status (live Discord bot) ---------------------------
REQUIRED_FIELDS = {
    "online": bool,
    "uptime": str,
    "servers": int,
    "members": int,
    "latency": int,
    "commands_run": int,
    "last_ping": str,
    "bot_name": str,
}


class TestBotStatus:
    def test_bot_status_returns_required_schema(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/bot/status")
        assert r.status_code == 200, r.text
        data = r.json()
        for field, ftype in REQUIRED_FIELDS.items():
            assert field in data, f"missing field {field}"
            assert isinstance(data[field], ftype), (
                f"{field} expected {ftype.__name__} got {type(data[field]).__name__}: {data[field]!r}"
            )
        # bot_avatar can be a string or None
        assert "bot_avatar" in data
        assert data["bot_avatar"] is None or isinstance(data["bot_avatar"], str)

    def test_bot_status_is_online_and_has_guilds(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/bot/status")
        data = r.json()
        assert data["online"] is True, f"bot reported offline: {data}"
        assert data["servers"] >= 1, f"expected >=1 server, got {data['servers']}"
        # bot_name should be Canary per problem statement
        assert data["bot_name"].lower().startswith("canary")
        # latency should be a sane positive number when online
        assert data["latency"] >= 0

    def test_bot_status_heartbeat_is_fresh(self, api_client):
        r = api_client.get(f"{BASE_URL}/api/bot/status")
        data = r.json()
        # last_ping format like "Xs ago"
        assert data["last_ping"].endswith("ago") or data["last_ping"] in ("never", "unknown")
        if data["last_ping"].endswith("s ago"):
            secs = int(data["last_ping"].split("s")[0])
            assert secs < 45, f"heartbeat stale ({secs}s)"

    def test_bot_status_counters_update_over_time(self, api_client):
        r1 = api_client.get(f"{BASE_URL}/api/bot/status").json()
        time.sleep(12)  # heartbeat is every 10s
        r2 = api_client.get(f"{BASE_URL}/api/bot/status").json()
        # uptime_seconds should be present and monotonic
        assert "uptime_seconds" in r1 and "uptime_seconds" in r2
        assert r2["uptime_seconds"] >= r1["uptime_seconds"], (
            f"uptime_seconds went backwards: {r1['uptime_seconds']} -> {r2['uptime_seconds']}"
        )
