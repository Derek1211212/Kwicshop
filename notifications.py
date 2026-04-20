import os
import json
import threading
from pywebpush import webpush, WebPushException
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.backends import default_backend

VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY')
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY')
VAPID_CLAIMS = {"sub": "mailto:support@kwicshop.com"}

def send_push_notification_to_store_followers(store_id, title, body, url):
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        print("VAPID keys not configured. Push notifications disabled.")
        return
    
    # Convert private key string to proper format if needed
    private_key = None
    try:
        # Try to load as PEM
        private_key = serialization.load_pem_private_key(
            VAPID_PRIVATE_KEY.encode('utf-8'),
            password=None,
            backend=default_backend()
        )
    except:
        # If it's already a key object, use as is
        private_key = VAPID_PRIVATE_KEY
    
    from app import get_db_connection
    
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    
    cur.execute("""
        SELECT ps.endpoint, ps.p256dh, ps.auth
        FROM push_subscriptions ps
        JOIN follows f ON ps.user_id = f.user_id AND ps.store_id = f.store_id
        WHERE f.store_id = %s
    """, (store_id,))
    subscriptions = cur.fetchall()
    cur.close()
    conn.close()
    
    if not subscriptions:
        print(f"No subscriptions for store {store_id}")
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
                vapid_private_key=private_key,
                vapid_claims=VAPID_CLAIMS
            )
            print(f"Push sent to {sub['endpoint'][:50]}...")
        except WebPushException as e:
            print(f"Push failed: {e}")
    
    for sub in subscriptions:
        t = threading.Thread(target=send, args=(sub,))
        t.daemon = True
        t.start()