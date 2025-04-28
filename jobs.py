# jobs.py

import datetime

# define your milestone thresholds
MILESTONE_THRESHOLDS = [100, 1000, 3000]  # etc.

def check_ad_performance_alerts():
    # Import here to avoid circular dependency at module load time
    from app import get_db_connection
    from notifications import send_push

    conn   = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    now = datetime.datetime.utcnow()

    # ——— Milestone Achieved Alerts ———
    for threshold in MILESTONE_THRESHOLDS:
        cursor.execute("""
            SELECT
              m.listing_id,
              l.user_id,
              l.title,
              m.impressions
            FROM listing_metrics AS m
            JOIN listings           AS l ON m.listing_id = l.listing_id
            WHERE m.impressions >= %s
              AND NOT EXISTS (
                SELECT 1 FROM notification_log nl
                WHERE nl.listing_id = m.listing_id
                  AND nl.alert_type = %s
              )
        """, (threshold, f"milestone_{threshold}"))
        for row in cursor.fetchall():
            listing_id  = row['listing_id']
            user_id     = row['user_id']
            title       = row['title']
            impressions = row['impressions']

            send_push(
                user_id,
                "🎉 Milestone Reached!",
                f"🎉 Your ad {title} just hit {impressions:,} impressions!",
                url=f"/listing/{listing_id}"
            )
            cursor.execute("""
                INSERT INTO notification_log
                  (listing_id, user_id, alert_type)
                VALUES (%s, %s, %s)
            """, (listing_id, user_id, f"milestone_{threshold}"))
            conn.commit()

    # ——— No Activity Alerts ———
    seven_days_ago = now - datetime.timedelta(days=7)
    cursor.execute("""
        SELECT
          m.listing_id,
          l.user_id,
          l.title
        FROM listing_metrics AS m
        JOIN listings           AS l ON m.listing_id = l.listing_id
        WHERE m.updated_at < %s
          AND NOT EXISTS (
            SELECT 1 FROM notification_log nl
            WHERE nl.listing_id = m.listing_id
              AND nl.alert_type = 'no_activity'
          )
    """, (seven_days_ago,))
    for row in cursor.fetchall():
        listing_id = row['listing_id']
        user_id    = row['user_id']
        title      = row['title']

        send_push(
            user_id,
            "No activity on your ad",
            f"Your ad {title} hasn’t had any new views in 7 days. Consider promoting it.",
            url=f"/listing/{listing_id}"
        )
        cursor.execute("""
            INSERT INTO notification_log
              (listing_id, user_id, alert_type)
            VALUES (%s, %s, 'no_activity')
        """, (listing_id, user_id))
        conn.commit()

    cursor.close()
    conn.close()
