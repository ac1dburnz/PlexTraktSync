"""Check the local Docker UI without authorizing or changing a Trakt account."""
import json
import os
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

base = os.environ.get("PKCE_SMOKE_BASE_URL", "http://localhost:8090")
key = os.environ["LIST_BRIDGE_SECRET"]
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    page = browser.new_page()
    page.goto(base)
    page.locator("#key").fill(key)
    page.locator("#connect").click()
    page.locator("#builder").wait_for(state="visible")
    assert page.locator("#authMethod").input_value() == "browser"
    assert "Browser / session token" in page.locator("#authStatus").inner_text()
    assert page.locator("#connectPkce").is_enabled()
    page.locator("#authMethod").select_option("pkce")
    page.locator("#useAuth").click()
    expect(page.locator("#status")).to_contain_text("cannot be read")
    page.locator("#authMethod").select_option("browser")
    page.locator("#useAuth").click()
    expect(page.locator("#status")).to_contain_text("Authentication method saved")
    authorization = []
    def capture(route):
        authorization.append(route.request.url)
        route.fulfill(status=200, content_type="text/html", body="<p>Authorization link captured for local test.</p>")
    page.route("https://auth.trakt.tv/**", capture)
    page.locator("#connectPkce").click()
    page.wait_for_url("https://auth.trakt.tv/**")
    assert len(authorization) == 1
    params = parse_qs(urlsplit(authorization[0]).query)
    assert params["client_id"] == [os.environ["TRAKT_PKCE_CLIENT_ID"]]
    assert params["redirect_uri"] == ["http://localhost:18090/oauth/callback"]
    assert params["code_challenge_method"] == ["S256"]
    assert "client_secret" not in params and "code_verifier" not in params
    assert params["state"] and params["code_challenge"]
    page.goto(base)
    page.locator("#key").fill(key)
    page.locator("#connect").click()
    page.locator("#builder").wait_for(state="visible")
    assert page.locator("#authMethod").input_value() == "browser"
    response = page.request.get(base + "/oauth/callback?code=fake&state=incorrect")
    assert response.status == 400
    response = page.request.post(base + "/api/auth/pkce/start", data={})
    assert response.status == 403
    browser.close()
print(json.dumps({"ui_connected": True, "method_switch_errors_handled": True,
                  "browser_method_preserved": True, "pkce_link_valid": True,
                  "invalid_callback_rejected": True, "start_requires_bridge_key": True}))
