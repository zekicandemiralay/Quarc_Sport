"""
One-time interactive Garmin Connect login, for accounts with two-factor authentication.

The sync service cannot answer an MFA prompt by itself. Run this once and it saves
Garmin's OAuth tokens for the account, so syncing carries on without asking again
(until the tokens eventually expire).

  bash garmin-login.sh <your Quarc username>

First save your Garmin email + password in Quarc Sport (Settings → Garmin Connect) —
this script reuses those, so the password is never typed here or written to disk.
"""

import sys

import db
import sync
from garminconnect import Garmin


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: bash garmin-login.sh <Quarc username>")
        return 2
    username = sys.argv[1]

    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, garmin_email_enc, garmin_pass_enc FROM users WHERE lower(username) = lower(%s)",
            (username,),
        )
        row = cur.fetchone()
    conn.close()

    if not row:
        print(f'No Quarc Sport user "{username}" yet. Sign in to the app once, then try again.')
        return 1
    if row[1] is None or row[2] is None:
        print("No Garmin credentials saved for this user. Add them in the app first: Settings → Garmin Connect.")
        return 1

    user_id = row[0]
    token_dir = sync.get_token_dir(user_id)
    # prompt_mfa makes the library ask for the code itself when Garmin wants one, and
    # login() then writes the OAuth tokens into token_dir.
    client = Garmin(
        email=sync.decrypt_credential(row[1]),
        password=sync.decrypt_credential(row[2]),
        prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
    )
    client.login(tokenstore=token_dir)

    print(f"Logged in. Tokens saved for {username}; the sync service will use them automatically.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
