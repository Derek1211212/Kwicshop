# notifications.py
from pywebpush import webpush, WebPushException
import json

# Paste in the VAPID keys you generated
VAPID_PUBLIC = "BMtSvERTURwR1YKBRM7fPZZEDDTkx8Gxyunu1s0X6xdGy5b2i2d9fn042-E4VIx_0gIiv7QFq2l8I0uXO_bagR8"
VAPID_PRIVATE = "EK1XTPn3DVXuk_8cZMOblzwWR7GOiuusbgN6xF0cOsU"
VAPID_CLAIMS = {"sub": "mailto:Derickbill3@gmail.com"}

def send_push(user_id, title, body, url="/"):
    # import here to avoid circular imports
    from app import get_db_connection
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)   # ← use a cursor
    try:
        # Fetch the subscriptions
        cursor.execute(
            "SELECT endpoint, p256dh, auth "
            "FROM push_subscriptions WHERE user_id = %s",
            (user_id,)
        )
        subs = cursor.fetchall()            # ← fetch from the cursor

    finally:
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
                vapid_private_key=VAPID_PRIVATE,
                vapid_public_key=VAPID_PUBLIC,
                vapid_claims=VAPID_CLAIMS
            )
        except WebPushException as ex:
            # Log and continue
            print("Push failed:", repr(ex))