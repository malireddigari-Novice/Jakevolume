"""
Two-step Schwab OAuth re-auth that avoids schwab-py's blocking input() prompt.

The normal `SchwabClient().login()` path calls `client_from_manual_flow`, which
prints a URL and then BLOCKS on `input('Redirect URL> ')` — unusable from a
non-interactive shell (it hits EOF). This splits that single interactive step
into two ordinary commands:

  1. python schwab_reauth.py url
        Prints the Schwab authorize URL and stashes the auth context.

  2. <open URL in a browser, log in, click Allow. The browser is redirected to
     https://127.0.0.1:8182/?code=...&state=... and shows a "can't reach this
     page" error — that is expected; copy the ENTIRE address-bar URL.>

  3. python schwab_reauth.py finish "<paste the full redirect URL>"
        Exchanges the auth code and writes a fresh schwab_token.json
        (identical format to the standard manual flow).

Then verify with:  python check_connections.py
"""
import json
import os
import sys

import schwab.auth as _auth

import config

# Auth context (callback_url, authorization_url, state) is stashed here between
# step 1 and step 2 so the second process reconstructs the exact same context.
_CTX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".schwab_authctx.json")


def step_url() -> None:
    ctx = _auth.get_auth_context(config.SCHWAB_API_KEY, config.SCHWAB_CALLBACK_URL)
    with open(_CTX_PATH, "w") as f:
        json.dump(
            {
                "callback_url": ctx.callback_url,
                "authorization_url": ctx.authorization_url,
                "state": ctx.state,
            },
            f,
        )
    print(ctx.authorization_url)


def step_finish(received_url: str) -> None:
    if not os.path.exists(_CTX_PATH):
        sys.exit("No auth context found — run `python schwab_reauth.py url` first.")
    with open(_CTX_PATH) as f:
        d = json.load(f)
    ctx = _auth.AuthContext(d["callback_url"], d["authorization_url"], d["state"])

    # Reuse schwab-py's own token writer so the on-disk format matches exactly
    # what the standard manual flow produces.
    make_writer = getattr(_auth, "_" + "_make_update_token_func")
    token_write_func = make_writer(config.SCHWAB_TOKEN_FILE)

    _auth.client_from_received_url(
        config.SCHWAB_API_KEY,
        config.SCHWAB_APP_SECRET,
        ctx,
        received_url.strip(),
        token_write_func,
    )
    os.remove(_CTX_PATH)
    print(f"OK: fresh token written to {config.SCHWAB_TOKEN_FILE}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "url":
        step_url()
    elif len(sys.argv) >= 3 and sys.argv[1] == "finish":
        step_finish(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)
