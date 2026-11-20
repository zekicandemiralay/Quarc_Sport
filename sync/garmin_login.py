"""
Admin fallback: a Garmin Connect login from the server's terminal.

Not needed normally — people connect Garmin themselves in the app (Settings →
Garmin Connect), two-factor code included. This is for when the app can't, e.g.
to help someone over the phone. It saves Garmin's OAuth tokens for the account,
so syncing carries on without asking again (until the tokens eventually expire).

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

    # Clear any "needs a code" state and start their import right away.
    sync.set_status(user_id, "connected")
    sync._execute("UPDATE users SET sync_requested_at = now() WHERE id = %s", (user_id,))

    print(f"Logged in. Tokens saved for {username}; their sync starts within seconds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
