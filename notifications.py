# notifications.py

import json
from pywebpush import webpush, WebPushException
from config import VAPID_PRIVATE_KEY, VAPID_CLAIMS

def send_push(user_id, title, body, url="/"):
    from app import get_db_connection

    conn   = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT endpoint, p256dh, auth
        FROM push_subscriptions
        WHERE user_id = %s
    """, (user_id,))
    subs = cursor.fetchall()
    cursor.close()
    conn.close()

    if not subs:
        print(f"[send_push] No subscriptions for user {user_id}")
        return

    payload = json.dumps({
        "title": title,
        "body":  body,
        "url":   url
    })

    for s in subs:
        subscription_info = {
            "endpoint": s["endpoint"],
            "keys": {
                "p256dh": s["p256dh"],
                "auth":   s["auth"]
            }
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=VAPID_CLAIMS
            )
            print(f"[send_push] Notification sent to user {user_id}")
        except WebPushException as ex:
            print(f"[send_push] Push failed for user {user_id}: {repr(ex)}")
