import json
import os
import threading
from pywebpush import webpush, WebPushException

VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY')
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY')
VAPID_CLAIMS = {"sub": "mailto:support@kwicshop.com"}

def send_push_notification_to_store_followers(store_id, title, body, url):
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        print("VAPID keys not configured. Push notifications disabled.")
        return
    
    # Import db function here to avoid circular import
    from app import get_db_connection   # adjust 'app' to your main filename if different
    
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    
    cur.execute("""
        SELECT ps.endpoint, ps.p256dh, ps.auth
        FROM push_subscriptions ps
        JOIN follows sf ON ps.user_id = sf.user_id AND ps.store_id = sf.store_id
        WHERE sf.store_id = %s
    """, (store_id,))
    subscriptions = cur.fetchall()
    cur.close()
    conn.close()
    
    if not subscriptions:
        return
    
    payload = {"title": title, "body": body, "url": url}
    
    def send(sub):
        sub_info = {
            "endpoint": sub['endpoint'],
            "keys": {"p256dh": sub['p256dh'], "auth": sub['auth']}
        }
        try:
            webpush(
                subscription_info=sub_info,
                data=json.dumps(payload),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=VAPID_CLAIMS
            )
        except WebPushException as e:
            print(f"Push failed: {e}")
    
    for sub in subscriptions:
        t = threading.Thread(target=send, args=(sub,))
        t.daemon = True
        t.start()