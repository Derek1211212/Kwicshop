import json
from pywebpush import webpush, WebPushException
from flask import current_app

def send_push(user_id, title, body, url="/"):
    """
    Sends a web-push notification to every subscription for a given user_id.
    Expects VAPID config from Flask's current_app.config.
    """
    from app import get_db_connection  # Your own DB helper

    # Fetch user's push subscriptions
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT endpoint, p256dh, auth
        FROM push_subscriptions
        WHERE user_id = %s
    """, (user_id,))
    subscriptions = cursor.fetchall()
    cursor.close()
    conn.close()

    if not subscriptions:
        current_app.logger.info(f"[send_push] No subscriptions found for user {user_id}")
        return

    # Build the payload that the service worker will parse
    payload = json.dumps({
        "notification": {
            "title": title,
            "body": body,
            "icon": "https://swap-chief.onrender.com/static/icons/ss.png",
            "badge": "https://swap-chief.onrender.com/static/icons/ss.png",
            "data": {
                "url": url
            }
        }
    })

    # Get VAPID credentials from config
    vapid_private_key = current_app.config["VAPID_PRIVATE_KEY"]
    vapid_claims = current_app.config["VAPID_CLAIMS"]

    for sub in subscriptions:
        subscription_info = {
            "endpoint": sub["endpoint"],
            "keys": {
                "p256dh": sub["p256dh"],
                "auth": sub["auth"]
            }
        }

        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=vapid_private_key,
                vapid_claims=vapid_claims
            )
            current_app.logger.info(f"[send_push] Notification sent to user {user_id}")

        except WebPushException as ex:
            status = getattr(ex.response, 'status_code', None)
            current_app.logger.warning(
                f"[send_push] WebPushException (status {status}) for user {user_id}: {ex}"
            )

            # Remove expired/invalid subscriptions
            if status in (404, 410):
                current_app.logger.info(f"[send_push] Removing expired subscription for user {user_id}")
                conn2 = get_db_connection()
                cur2 = conn2.cursor()
                cur2.execute(
                    "DELETE FROM push_subscriptions WHERE user_id = %s AND endpoint = %s",
                    (user_id, sub["endpoint"])
                )
                conn2.commit()
                cur2.close()
                conn2.close()
