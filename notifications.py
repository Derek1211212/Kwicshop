# notifications.py

import json
from pywebpush import webpush, WebPushException
from config import VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_CLAIMS  # wherever you keep these

def send_push(user_id, title, body, url="/"):
    # import here so you don’t create a circular import at module load time
    from app import get_db_connection

    conn   = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = %s",
        (user_id,)
    )
    subs = cursor.fetchall()
    cursor.close()
    conn.close()

    payload = json.dumps({"title": title, "body": body, "url": url})
    for s in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": s['endpoint'],
                    "keys": {"p256dh": s['p256dh'], "auth": s['auth']}
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_public_key=VAPID_PUBLIC_KEY,
                vapid_claims=VAPID_CLAIMS
            )
        except WebPushException as ex:
            print("Push failed:", repr(ex))
