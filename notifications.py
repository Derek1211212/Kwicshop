# notifications.py

import json
from pywebpush import webpush, WebPushException

# ← Import current_app from Flask
from flask import current_app

def send_push(user_id, title, body, url="/"):
    """
    Send a web-push notification to every subscription for `user_id`,
    using VAPID keys stored in current_app.config.
    """
    from app import get_db_connection  # your own DB helper

    # Fetch subscriptions
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
        current_app.logger.info(f"[send_push] No subscriptions for user {user_id}")
        return

    payload = json.dumps({
        "title": title,
        "body":  body,
        "url":   url
        "icon":  "https://swap-chief.onrender.com/static/icons/ss.png"
    })

    # Pull VAPID credentials from Flask’s config
    vapid_private = current_app.config['VAPID_PRIVATE_KEY']
    vapid_claims  = current_app.config['VAPID_CLAIMS']

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
                vapid_private_key=vapid_private,
                vapid_claims=vapid_claims
            )
            current_app.logger.info(f"[send_push] Notification sent to user {user_id}")
        except WebPushException as ex:
            status = getattr(ex, 'response', None) and ex.response.status_code
            current_app.logger.warning(
                f"[send_push] Push failed ({status}) for user {user_id}: {ex}"
            )
            # Optional: remove expired subscriptions
            if status in (404, 410):
                current_app.logger.info(f"[send_push] Removing expired subscription for user {user_id}")
                conn2 = get_db_connection()
                cur2 = conn2.cursor()
                cur2.execute(
                    "DELETE FROM push_subscriptions WHERE user_id=%s AND endpoint=%s",
                    (user_id, s["endpoint"])
                )
                conn2.commit()
                cur2.close()
                conn2.close()
