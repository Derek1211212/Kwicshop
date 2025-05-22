# jobs.py

import datetime

# Define your milestone thresholds
MILESTONE_THRESHOLDS = [100, 1000, 3000]

def check_ad_performance_alerts():
    """
    This function is intended to be run by APScheduler at regular intervals.
    It pushes an application context so that `current_app` and config values
    (e.g. VAPID keys) are available, then checks for milestone and no-activity
    alerts and sends push notifications accordingly.
    """
    # Local imports to avoid circular dependencies at module load time
    from app import app, get_db_connection
    from notifications import send_push
    from flask import current_app

    # Push the Flask application context
    with app.app_context():
        current_app.logger.debug("Running ad performance alerts job")

        conn   = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        now    = datetime.datetime.utcnow()

        # ——— Milestone Achieved Alerts ———
        for threshold in MILESTONE_THRESHOLDS:
            current_app.logger.debug(f"Checking for listings with impressions ≥ {threshold}")
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

                current_app.logger.info(
                    f"Milestone reached: {threshold} impressions for listing {listing_id}"
                )
                send_push(
                    user_id,
                    "🎉 Milestone Reached!",
                    f"Your ad “{title}” just hit {impressions:,} impressions!",
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
        current_app.logger.debug("Checking for listings with no activity in past 7 days")
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

            current_app.logger.info(
                f"No-activity alert for listing {listing_id}"
            )
            send_push(
                user_id,
                "No Activity on Your Ad",
                f"Your ad “{title}” hasn’t had any views in 7 days. Consider promoting it.",
                url=f"/listing/{listing_id}"
            )

            cursor.execute("""
                INSERT INTO notification_log
                  (listing_id, user_id, alert_type)
                VALUES (%s, %s, 'no_activity')
            """, (listing_id, user_id))
            conn.commit()

        # Clean up
        cursor.close()
        conn.close()
        current_app.logger.debug("Ad performance alerts job completed")
