"""Get a LinkedIn access token for Milo's LinkedIn posts (3-legged OAuth, run once every ~60 days).

    Post as yourself (your own profile; self-serve, no LinkedIn review):
        uv run python scripts/linkedin_token.py --as me --client-id <id> --client-secret <secret>
      Your app needs the products "Share on LinkedIn" and "Sign In with LinkedIn using OpenID Connect".

    Post as the company Page (needs the Community Management API approved, and a Page admin signing in):
        uv run python scripts/linkedin_token.py --as page --client-id <id> --client-secret <secret>

Before running it, in the LinkedIn Developer Portal: Auth tab -> Authorized redirect URLs -> add
  http://localhost:8765/callback
The script opens the sign-in page, catches the redirect on this computer, swaps the code for a token, and finds
your member id (--as me) or the Pages you administer (--as page). Then put the printed lines into .env.
The token is shown once and never saved by this script.

(The portal's own "OAuth Token Tools" page can make a token too; this script also finds the Page id for you.)
"""
import argparse
import http.server
import secrets
import sys
import threading
import webbrowser
from datetime import date, timedelta
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

REDIRECT = "http://localhost:8765/callback"
SCOPES = {"me": "openid profile w_member_social",
          "page": "w_organization_social r_organization_social rw_organization_admin"}
VERSION = "202610"


def _catch_code(expected_state: str) -> str:
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            if urlparse(self.path).path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            if q.get("state", [""])[0] != expected_state:
                result["error"] = "state mismatch (ignored a request that wasn't from this sign-in)"
            elif "error" in q:
                result["error"] = f"{q['error'][0]}: {q.get('error_description', [''])[0]}"
            else:
                result["code"] = q.get("code", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and go back to the terminal.")
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("localhost", 8765), Handler)
    server.serve_forever()
    if "error" in result:
        sys.exit(f"LinkedIn sign-in failed: {result['error']}")
    return result["code"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--client-secret", required=True)
    ap.add_argument("--as", dest="author", choices=["me", "page"], default="me",
                    help="me = post as your own profile (default); page = post as the company Page")
    args = ap.parse_args()

    state = secrets.token_urlsafe(16)
    url = "https://www.linkedin.com/oauth/v2/authorization?" + urlencode(
        {"response_type": "code", "client_id": args.client_id, "redirect_uri": REDIRECT, "state": state,
         "scope": SCOPES[args.author]})
    print("Opening LinkedIn sign-in. If no browser opens, visit:\n" + url)
    webbrowser.open(url)
    code = _catch_code(state)

    tok = httpx.post("https://www.linkedin.com/oauth/v2/accessToken", timeout=30, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": args.client_id, "client_secret": args.client_secret})
    if tok.status_code >= 400:
        sys.exit(f"Token exchange failed ({tok.status_code}): {tok.text[:300]}")
    body = tok.json()
    token = body["access_token"]
    expires = date.today() + timedelta(seconds=int(body.get("expires_in", 60 * 86400)))

    if args.author == "me":
        me = httpx.get("https://api.linkedin.com/v2/userinfo", timeout=30,
                       headers={"Authorization": f"Bearer {token}"})
        if me.status_code >= 400:
            sys.exit(f"Couldn't read your profile ({me.status_code}): {me.text[:200]}. Is 'Sign In with LinkedIn "
                     "using OpenID Connect' added to the app?")
        info = me.json()
        print(f"\nSigned in as {info.get('name', '?')}. Add these lines to .env "
              "(and set LINKEDIN_DRY_RUN=false when you are ready to post for real):\n")
        print("LINKEDIN_AUTHOR=me")
        print(f"LINKEDIN_PERSON_ID={info['sub']}")
        print(f"LINKEDIN_ACCESS_TOKEN={token}")
        print(f"LINKEDIN_TOKEN_EXPIRES_AT={expires.isoformat()}")
        return

    pages = httpx.get("https://api.linkedin.com/rest/organizationAcls", timeout=30,
                      params={"q": "roleAssignee", "role": "ADMINISTRATOR", "state": "APPROVED"},
                      headers={"Authorization": f"Bearer {token}", "LinkedIn-Version": VERSION,
                               "X-Restli-Protocol-Version": "2.0.0"})
    orgs = []
    if pages.status_code < 400:
        orgs = [e.get("organization", "") for e in pages.json().get("elements", [])]
    print("\nAdd these lines to .env (and set LINKEDIN_DRY_RUN=false when you are ready to post for real):\n")
    print("LINKEDIN_AUTHOR=page")
    print(f"LINKEDIN_ACCESS_TOKEN={token}")
    print(f"LINKEDIN_TOKEN_EXPIRES_AT={expires.isoformat()}")
    if len(orgs) == 1:
        print(f"LINKEDIN_ORGANIZATION_ID={orgs[0].rsplit(':', 1)[-1]}")
    elif orgs:
        print("# This member administers several Pages; pick one:")
        for o in orgs:
            print(f"# LINKEDIN_ORGANIZATION_ID={o.rsplit(':', 1)[-1]}")
    else:
        print("# LINKEDIN_ORGANIZATION_ID=<the number in your Page's admin URL, linkedin.com/company/<id>/admin>")
        if pages.status_code >= 400:
            print(f"# (couldn't list the Pages: HTTP {pages.status_code})")


if __name__ == "__main__":
    main()
