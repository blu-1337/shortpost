"""One-time token setup for Facebook (Page token) and Instagram, plus Instagram token refresh (CLAUDE.md 13 step 5)."""

from __future__ import annotations

from datetime import datetime, timezone

from .. import tokens
from ..http import ApiError, Http


def auth_facebook(cfg: dict, secrets: dict, http: Http, user_token: str, choose=None) -> dict:
    """Exchange a short-lived user token for a long-lived one, then store the chosen Page's token."""
    ver = cfg["meta"]["graph_version"]
    for k in ("meta_app_id", "meta_app_secret"):
        if not secrets.get(k):
            raise ValueError(f"missing secret {k}: run `shortpost set-secret {k}`")
    ll = http.get(f"https://graph.facebook.com/{ver}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": secrets["meta_app_id"],
        "client_secret": secrets["meta_app_secret"], "fb_exchange_token": user_token})["access_token"]
    pages = http.get(f"https://graph.facebook.com/{ver}/me/accounts", params={"access_token": ll}).get("data", [])
    if not pages:
        raise ValueError("this user manages no Facebook Pages (check the pages_show_list permission)")
    page = pages[0] if len(pages) == 1 or choose is None else choose(pages)
    rec = {"page_id": page["id"], "page_name": page.get("name", ""), "access_token": page["access_token"]}
    try:  # the Instagram professional account linked to this Page (publishes with the same Page token)
        ig = http.get(f"https://graph.facebook.com/{ver}/{page['id']}", params={
            "fields": "instagram_business_account{id,username}", "access_token": page["access_token"]}).get("instagram_business_account")
    except ApiError:
        ig = None
    if ig:
        rec.update(ig_user_id=str(ig["id"]), ig_username=ig.get("username", ""))
    tokens.save("facebook", rec)
    return {**page, "instagram": ig}


def facebook_token_days_left(http: Http, secrets: dict) -> int | None:
    """debug_token: days until the Page token expires, or None when it never expires."""
    t = tokens.load("facebook")
    if not t:
        return None
    d = http.get("https://graph.facebook.com/debug_token", params={
        "input_token": t["access_token"], "access_token": f"{secrets['meta_app_id']}|{secrets['meta_app_secret']}"})["data"]
    exp = d.get("expires_at", 0)
    return None if not exp else int((exp - datetime.now(timezone.utc).timestamp()) // 86400)
