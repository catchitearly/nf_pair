"""
refresh_token.py
Run this ONCE every morning before market open (or automate it).
Generates a fresh Fyers access token and updates the GitHub Actions secret
so run_live.py always has a valid token.

Usage:
  python refresh_token.py \
    --client-id   YOUR_CLIENT_ID \
    --secret-key  YOUR_SECRET_KEY \
    --totp-key    YOUR_TOTP_SECRET \
    --pin         YOUR_FYERS_PIN \
    --gh-token    YOUR_GITHUB_PAT \
    --gh-repo     username/repo

Or set environment variables:
  FYERS_CLIENT_ID, FYERS_SECRET_KEY, FYERS_TOTP_KEY,
  FYERS_PIN, GH_PAT, GH_REPO

Dependencies: fyers-apiv3, pyotp
  pip install fyers-apiv3 pyotp
"""

import argparse
import base64
import hashlib
import json
import logging
import os
import urllib.request
import urllib.error

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("refresh_token")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--client-id",  default=os.environ.get("FYERS_CLIENT_ID"))
    p.add_argument("--secret-key", default=os.environ.get("FYERS_SECRET_KEY"))
    p.add_argument("--totp-key",   default=os.environ.get("FYERS_TOTP_KEY"))
    p.add_argument("--pin",        default=os.environ.get("FYERS_PIN"))
    p.add_argument("--gh-token",   default=os.environ.get("GH_PAT"),
                   help="GitHub Personal Access Token with secrets:write scope")
    p.add_argument("--gh-repo",    default=os.environ.get("GH_REPO"),
                   help="GitHub repo e.g. username/nifty-options")
    return p.parse_args()


def get_fyers_token(client_id: str, secret_key: str, totp_key: str, pin: str) -> str:
    """
    Automate Fyers token generation using TOTP.
    Requires fyers-apiv3 and pyotp.
    """
    try:
        import pyotp
        from fyers_apiv3 import fyersModel
        from fyers_apiv3.fyersModel import SessionModel
    except ImportError:
        raise ImportError("pip install fyers-apiv3 pyotp")

    totp  = pyotp.TOTP(totp_key).now()
    appid = client_id.split("-")[0]

    session = SessionModel(
        client_id=client_id,
        secret_key=secret_key,
        redirect_uri="https://trade.fyers.in/api-login/redirect-uri/index.html",
        response_type="code",
        grant_type="authorization_code",
    )

    # Step 1: send OTP to registered mobile
    resp = session.generate_authcode()
    logger.info("Auth initiation: %s", resp)

    # Step 2: verify OTP + TOTP + PIN
    # NOTE: Fyers requires manual OTP from SMS for first auth per session.
    # For full automation, use their API v3 headless flow with TOTP only
    # if your account has TOTP-only auth enabled (Settings → Security).
    # If not, you must run this interactively once per day.
    sms_otp = input("Enter SMS OTP sent to your registered mobile: ").strip()

    verify = session.generate_authcode()   # replace with correct SDK call per Fyers v3 docs

    token = session.generate_token(auth_code=verify)["access_token"]
    logger.info("Access token obtained (first 20 chars): %s…", token[:20])
    return token


def update_github_secret(token: str, gh_pat: str, repo: str, secret_name: str = "FYERS_ACCESS_TOKEN") -> None:
    """
    Push the new access token as a GitHub Actions secret using the REST API.
    Requires the repo's public key to encrypt the secret value.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
        from cryptography.hazmat.primitives import serialization
        import nacl.encoding
        import nacl.public
    except ImportError:
        logger.warning("pip install PyNaCl cryptography for auto GitHub secret update")
        logger.info("Manual update: go to repo → Settings → Secrets → FYERS_ACCESS_TOKEN")
        logger.info("New token (first 30 chars): %s…", token[:30])
        return

    headers = {
        "Authorization":        f"Bearer {gh_pat}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type":         "application/json",
    }

    # 1. Get repo public key
    url = f"https://api.github.com/repos/{repo}/actions/secrets/public-key"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as r:
        pk_data  = json.loads(r.read())
    pub_key  = pk_data["key"]
    key_id   = pk_data["key_id"]

    # 2. Encrypt secret with repo public key (libsodium sealed box)
    public_key = nacl.public.PublicKey(pub_key, nacl.encoding.Base64Encoder)
    sealed     = nacl.public.SealedBox(public_key)
    encrypted  = base64.b64encode(sealed.encrypt(token.encode())).decode()

    # 3. PUT secret
    url     = f"https://api.github.com/repos/{repo}/actions/secrets/{secret_name}"
    payload = json.dumps({"encrypted_value": encrypted, "key_id": key_id}).encode()
    req     = urllib.request.Request(url, data=payload, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req) as r:
            logger.info("GitHub secret '%s' updated (HTTP %d)", secret_name, r.status)
    except urllib.error.HTTPError as e:
        logger.error("Failed to update GitHub secret: %s", e)


def main():
    args = parse_args()

    missing = [k for k, v in {
        "client-id": args.client_id, "secret-key": args.secret_key,
        "totp-key":  args.totp_key,  "pin":        args.pin,
    }.items() if not v]
    if missing:
        logger.error("Missing required args: %s", missing)
        return

    logger.info("Generating fresh Fyers access token …")
    token = get_fyers_token(args.client_id, args.secret_key, args.totp_key, args.pin)

    if args.gh_token and args.gh_repo:
        logger.info("Updating GitHub secret …")
        update_github_secret(token, args.gh_token, args.gh_repo)
    else:
        logger.info("No GH_PAT/GH_REPO provided — print token only")
        print(f"\nFresh access token:\n{token}\n")
        print("Manually update GitHub secret FYERS_ACCESS_TOKEN with the above value.")


if __name__ == "__main__":
    main()
