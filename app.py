import os
import re
import json
import uuid
import math
import time
import threading
import logging
import smtplib
import hashlib
import requests
import random
import string
from datetime import datetime, timedelta, date
from decimal import Decimal, InvalidOperation
from functools import wraps
from email.message import EmailMessage
from xml.sax.saxutils import escape as xml_escape
from dotenv import load_dotenv
from flask import Flask, render_template, request, session, redirect, url_for, flash, jsonify, abort, Response, current_app, get_flashed_messages
from flask_bcrypt import Bcrypt
from flask_caching import Cache
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer
from slugify import slugify
from authlib.integrations.flask_client import OAuth
from twilio.rest import Client
import mysql.connector
from mysql.connector import pooling
import cloudinary
import cloudinary.uploader
import atexit
from notifications import send_push_notification_to_store_followers
from notifications import VAPID_PUBLIC_KEY
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv()

# ------------------------------
# App & Config
# ------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-key-change-in-production')
app.config['FROM_EMAIL'] = os.getenv('FROM_EMAIL', 'noreply@kwicshop.com')
bcrypt = Bcrypt(app)
cache = Cache(app, config={'CACHE_TYPE': 'SimpleCache'})

# Cloudinary
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)

# Paystack
PAYSTACK_SECRET_KEY = os.getenv('PAYSTACK_SECRET_KEY')
if not PAYSTACK_SECRET_KEY:
    raise ValueError("PAYSTACK_SECRET_KEY missing")

# Twilio (for phone login)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_VERIFY_SERVICE_SID = os.getenv("TWILIO_VERIFY_SERVICE_SID")
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None

# Google OAuth
oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.getenv('GOOGLE_CLIENT_ID'),
    client_secret=os.getenv('GOOGLE_CLIENT_SECRET'),
    authorize_url='https://accounts.google.com/o/oauth2/v2/auth',
    access_token_url='https://oauth2.googleapis.com/token',
    api_base_url='https://www.googleapis.com/oauth2/v2/',
    client_kwargs={'scope': 'email profile'}
)

# ------------------------------
# Database Connection Pool
# ------------------------------
dbconfig = {
    "host": os.getenv('DB_HOST', 'localhost'),
    "user": os.getenv('DB_USER', 'root'),
    "password": os.getenv('DB_PASSWORD', ''),
    "database": os.getenv('DB_DATABASE', ''),
    "port": int(os.getenv('DB_PORT', 3306)),
    "charset": 'utf8mb4',
    "use_unicode": True
}
pool = pooling.MySQLConnectionPool(pool_name="shop_pool", pool_size=10, **dbconfig)

def get_db_connection():
    return pool.get_connection()



# ──────────────── BACKGROUND SCHEDULER (cleanup old user sessions) ────────────────
def cleanup_old_sessions():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_sessions_new WHERE last_activity < DATE_SUB(NOW(), INTERVAL 1 HOUR)")
    deleted = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()
    if deleted:
        print(f"[Cleanup] Removed {deleted} expired user sessions from user_sessions_new")

scheduler = BackgroundScheduler()
scheduler.add_job(
    func=cleanup_old_sessions,
    trigger="interval",
    hours=1,
    id="session_cleanup",
    max_instances=1,      # prevent overlapping runs in the same process
    coalesce=True,        # merge missed runs into one
    misfire_grace_time=300 # wait up to 5 minutes if scheduler was down
)
scheduler.start()

# Optional: shutdown scheduler on app exit gracefully
import atexit
atexit.register(lambda: scheduler.shutdown())





# ------------------------------
# Helpers
# ------------------------------
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in {'png','jpg','jpeg','gif','webp','avif'}


# ---------- CLOUDINARY HELPERS ----------
def upload_to_cloudinary(file, folder="store_promos", resource_type="auto"):
    try:
        timestamp = int(time.time())
        original_filename = secure_filename(file.filename)
        name_without_ext = os.path.splitext(original_filename)[0]
        # Build public_id that already includes the folder
        public_id = f"{folder}/{name_without_ext}_{timestamp}"

        upload_result = cloudinary.uploader.upload(
            file,
            public_id=public_id,
            resource_type=resource_type,
            overwrite=True
        )
        return {
            'success': True,
            'url': upload_result['secure_url'],
            'public_id': upload_result['public_id'],
            'resource_type': upload_result['resource_type']
        }
    except Exception as e:
        current_app.logger.error(f"Cloudinary upload error: {str(e)}")
        return {'success': False, 'error': str(e)}

def delete_from_cloudinary(public_id, resource_type="image"):
    try:
        result = cloudinary.uploader.destroy(public_id, resource_type=resource_type)
        return result.get('result') == 'ok'
    except Exception as e:
        current_app.logger.error(f"Cloudinary delete error: {str(e)}")
        return False




def send_email_notification(recipient, subject, body):
    """Simple SMTP mail (MailerSend or any SMTP)"""
    smtp_server = os.getenv("SMTP_SERVER", "smtp.mailersend.net")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    if not smtp_user or not smtp_password:
        return False
    msg = EmailMessage()
    msg["From"] = f"kwicshop <{app.config['FROM_EMAIL']}>"
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        return True
    except Exception as e:
        app.logger.error(f"Email send failed: {e}")
        return False

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please log in first", "danger")
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated



def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please login first', 'warning')
            return redirect(url_for('login'))
        # Check if user has admin role
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT role FROM users WHERE id = %s", (session['user_id'],))
        user = cursor.fetchone()
        cursor.close()
        conn.close()
        if not user or user['role'] != 'admin':
            flash('Admin access required', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function





def _inc_store_metric(store_id, field, amount=1):
    if field not in {"views","clicks","chats","swaps","sales"}:
        return False
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        today = date.today()
        cur.execute(f"""
            INSERT INTO store_metrics (store_id, dt, {field})
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE {field} = COALESCE({field},0) + VALUES({field})
        """, (store_id, today, amount))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        app.logger.error(f"Metric error: {e}")
        return False
    finally:
        cur.close()
        conn.close()



@app.template_filter('format_number')
def format_number(value):
    if value is None:
        return '0'
    try:
        num = int(value)
        if num >= 1_000_000:
            return f'{num/1_000_000:.1f}M'
        elif num >= 1_000:
            return f'{num/1_000:.1f}K'
        return str(num)
    except (ValueError, TypeError):
        return str(value)



# ------------------------------
# Admin Dashboard
# ------------------------------
@app.route('/admin')
@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Overview counts
    cursor.execute("SELECT COUNT(*) as total FROM stores")
    total_stores = cursor.fetchone()['total']
    
    cursor.execute("SELECT COUNT(*) as total FROM listings")
    total_products = cursor.fetchone()['total']
    
    cursor.execute("SELECT COUNT(*) as total FROM users")
    total_users = cursor.fetchone()['total']
    
    # Aggregate listing metrics (impressions & clicks)
    cursor.execute("""
        SELECT COALESCE(SUM(impressions), 0) as total_impressions,
               COALESCE(SUM(clicks), 0) as total_clicks
        FROM listing_metrics
    """)
    metrics = cursor.fetchone()
    total_impressions = metrics['total_impressions']
    total_clicks = metrics['total_clicks']
    
    # Top 10 highest rated stores
    cursor.execute("""
        SELECT store_id, name, location, rating_avg, rating_count
        FROM stores
        WHERE rating_avg IS NOT NULL
        ORDER BY rating_avg DESC
        LIMIT 10
    """)
    top_stores = cursor.fetchall()
    
    # Last 7 days store metrics (views & clicks)
    cursor.execute("""
        SELECT dt, SUM(views) as total_views, SUM(clicks) as total_clicks
        FROM store_metrics
        WHERE dt >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
        GROUP BY dt
        ORDER BY dt
    """)
    recent_metrics = cursor.fetchall()
    
    # Top 10 products by clicks
    cursor.execute("""
        SELECT l.listing_id, l.title, l.price, 
               COALESCE(lm.impressions, 0) as impressions,
               COALESCE(lm.clicks, 0) as clicks
        FROM listings l
        LEFT JOIN listing_metrics lm ON l.listing_id = lm.listing_id
        ORDER BY COALESCE(lm.clicks, 0) DESC
        LIMIT 10
    """)
    top_products = cursor.fetchall()

    # Top 5 reported stores
    cursor.execute("""
        SELECT s.store_id, s.name, s.location, COUNT(sr.report_id) AS report_count
        FROM store_reports sr
        JOIN stores s ON sr.store_id = s.store_id
        GROUP BY s.store_id, s.name, s.location
        ORDER BY report_count DESC, MAX(sr.created_at) DESC
        LIMIT 5
    """)
    top_reported_stores = cursor.fetchall()

    # Stores with less than 15 visits in the last 30 days
    cursor.execute("""
        SELECT s.store_id, s.name, s.location, COALESCE(SUM(sm.views), 0) AS monthly_views
        FROM stores s
        LEFT JOIN store_metrics sm
            ON sm.store_id = s.store_id
           AND sm.dt >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
        GROUP BY s.store_id, s.name, s.location
        HAVING monthly_views < 15
        ORDER BY monthly_views ASC, s.name ASC
        LIMIT 10
    """)
    inactive_stores = cursor.fetchall()


    # Get active users (last 5 minutes)
    cursor.execute("""
        SELECT COUNT(*) as active_count
        FROM user_sessions_new
        WHERE last_activity > DATE_SUB(NOW(), INTERVAL 5 MINUTE)
    """)
    active_result = cursor.fetchone()
    active_users = active_result['active_count'] if active_result else 0



    
    cursor.close()
    conn.close()
    
    return render_template('admin/dashboard.html',
                         total_stores=total_stores,
                         total_products=total_products,
                         total_users=total_users,
                         total_impressions=total_impressions,
                         total_clicks=total_clicks,
                         top_stores=top_stores,
                         active_users=active_users,
                         recent_metrics=recent_metrics,
                         top_products=top_products,
                         top_reported_stores=top_reported_stores,
                         inactive_stores=inactive_stores)


# ------------------------------
# Admin Stores List with Monitoring
# ------------------------------



@app.before_request
def update_user_activity():
    if not request.endpoint or request.endpoint == 'static':
        return
    if 'user_id' not in session:
        return
    
    user_id = session['user_id']
    session_id = request.cookies.get('session', '') or session.get('_id', '')
    ip = request.remote_addr
    user_agent = request.headers.get('User-Agent', '')[:500]
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_sessions_new (user_id, session_id, last_activity, ip_address, user_agent)
        VALUES (%s, %s, NOW(), %s, %s)
        ON DUPLICATE KEY UPDATE
            session_id = VALUES(session_id),
            last_activity = NOW(),
            ip_address = VALUES(ip_address),
            user_agent = VALUES(user_agent)
    """, (user_id, session_id, ip, user_agent))
    conn.commit()
    cursor.close()
    conn.close()




@app.route('/admin/api/active-users')
@admin_required
def api_active_users():
    minutes = request.args.get('minutes', 5, type=int)
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT COUNT(*) as active_count
        FROM user_sessions_new
        WHERE last_activity > DATE_SUB(NOW(), INTERVAL %s MINUTE)
    """, (minutes,))
    result = cursor.fetchone()
    cursor.close()
    conn.close()
    return {'active_users': result['active_count'] if result else 0}





@app.route('/admin/stores')
@admin_required
def admin_stores():
    sort_by = request.args.get('sort', 'rating')
    search = request.args.get('search', '')
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Base query for stores (with optional search)
    sql = "SELECT * FROM stores WHERE 1=1"
    params = []
    if search:
        sql += " AND (name LIKE %s OR location LIKE %s)"
        search_param = f"%{search}%"
        params.extend([search_param, search_param])
    
    cursor.execute(sql, params)
    stores = cursor.fetchall()
    
    stores_with_metrics = []
    for store in stores:
        # Sum product impressions & clicks via listing_metrics
        cursor.execute("""
            SELECT COALESCE(SUM(lm.impressions), 0) as product_impressions,
                   COALESCE(SUM(lm.clicks), 0) as product_clicks
            FROM listings l
            LEFT JOIN listing_metrics lm ON l.listing_id = lm.listing_id
            WHERE l.store_id = %s
        """, (store['store_id'],))
        prod_metrics = cursor.fetchone()
        
        # Sum store metrics (views, clicks) from store_metrics
        cursor.execute("""
            SELECT COALESCE(SUM(views), 0) as store_views,
                   COALESCE(SUM(clicks), 0) as store_clicks
            FROM store_metrics
            WHERE store_id = %s
        """, (store['store_id'],))
        store_metrics = cursor.fetchone()
        
        # Count products in store
        cursor.execute("SELECT COUNT(*) as total FROM listings WHERE store_id = %s", (store['store_id'],))
        product_count = cursor.fetchone()['total']
        
        stores_with_metrics.append({
            'store': store,
            'product_impressions': prod_metrics['product_impressions'],
            'product_clicks': prod_metrics['product_clicks'],
            'store_views': store_metrics['store_views'],
            'store_clicks': store_metrics['store_clicks'],
            'total_products': product_count
        })
    
    cursor.close()
    conn.close()
    
    # Sorting
    if sort_by == 'rating':
        stores_with_metrics.sort(key=lambda x: x['store']['rating_avg'] or 0, reverse=True)
    elif sort_by == 'clicks':
        stores_with_metrics.sort(key=lambda x: x['product_clicks'], reverse=True)
    elif sort_by == 'views':
        stores_with_metrics.sort(key=lambda x: x['store_views'], reverse=True)
    elif sort_by == 'products':
        stores_with_metrics.sort(key=lambda x: x['total_products'], reverse=True)
    
    return render_template('admin/stores.html',
                         stores=stores_with_metrics,
                         sort_by=sort_by,
                         search=search)


# ------------------------------
# Admin Store Detail (Complete Monitoring)
# ------------------------------
@app.route('/admin/store/<int:store_id>')
@admin_required
def admin_store_detail(store_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Get store info
    cursor.execute("SELECT * FROM stores WHERE store_id = %s", (store_id,))
    store = cursor.fetchone()
    if not store:
        abort(404)


    cursor.execute("""
        SELECT id, username, email, contact, name, avatar, account_status
        FROM users
        WHERE id = %s
    """, (store['user_id'],))
    store_owner = cursor.fetchone()



    
    # Store metrics aggregation
    cursor.execute("""
        SELECT COALESCE(SUM(views), 0) as total_views,
               COALESCE(SUM(clicks), 0) as total_clicks,
               COALESCE(SUM(chats), 0) as total_chats,
               COALESCE(SUM(swaps), 0) as total_swaps,
               COALESCE(SUM(sales), 0) as total_sales
        FROM store_metrics
        WHERE store_id = %s
    """, (store_id,))
    store_metrics_agg = cursor.fetchone()
    
    # Product level metrics for this store
    cursor.execute("""
        SELECT COALESCE(SUM(lm.impressions), 0) as total_impressions,
               COALESCE(SUM(lm.clicks), 0) as total_clicks,
               COALESCE(SUM(lm.carousel_impressions), 0) as total_carousel
        FROM listings l
        LEFT JOIN listing_metrics lm ON l.listing_id = lm.listing_id
        WHERE l.store_id = %s
    """, (store_id,))
    product_metrics = cursor.fetchone()
    
    # Store ratings & comments
    cursor.execute("""
        SELECT sr.*, u.username
        FROM store_ratings sr
        LEFT JOIN users u ON sr.user_id = u.id
        WHERE sr.store_id = %s
        ORDER BY sr.created_at DESC
    """, (store_id,))
    store_ratings = cursor.fetchall()
    
    # Calculate average rating if not stored
    avg_rating = store['rating_avg']
    if not avg_rating and store_ratings:
        avg_rating = sum(r['rating'] for r in store_ratings) / len(store_ratings)
    
    # Get all products in store with their metrics & ratings
    cursor.execute("""
        SELECT l.*
        FROM listings l
        WHERE l.store_id = %s
    """, (store_id,))
    listings = cursor.fetchall()
    
    products = []
    for listing in listings:
        # Listing metrics
        cursor.execute("""
            SELECT impressions, clicks, carousel_impressions
            FROM listing_metrics
            WHERE listing_id = %s
        """, (listing['listing_id'],))
        metric = cursor.fetchone()
        
        # Listing ratings
        cursor.execute("""
            SELECT AVG(rating_value) as avg_rating, COUNT(*) as rating_count
            FROM ratings
            WHERE listing_id = %s
        """, (listing['listing_id'],))
        rating_info = cursor.fetchone()
        
        # Listing reviews count
        cursor.execute("SELECT COUNT(*) as review_count FROM reviews WHERE listing_id = %s", (listing['listing_id'],))
        review_count = cursor.fetchone()['review_count']
        
        products.append({
            'listing': listing,
            'impressions': metric['impressions'] if metric else 0,
            'clicks': metric['clicks'] if metric else 0,
            'carousel_impressions': metric['carousel_impressions'] if metric else 0,
            'rating_avg': float(rating_info['avg_rating']) if rating_info['avg_rating'] else None,
            'ratings_count': rating_info['rating_count'] if rating_info else 0,
            'reviews_count': review_count
        })
    
    # Sort products by clicks (most popular first)
    products.sort(key=lambda x: x['clicks'], reverse=True)
    
    # Daily metrics for last 30 days
    cursor.execute("""
        SELECT dt, views, clicks, chats, sales
        FROM store_metrics
        WHERE store_id = %s AND dt >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
        ORDER BY dt
    """, (store_id,))
    daily_metrics = cursor.fetchall()
    
    # Follower count
    cursor.execute("SELECT COUNT(*) as follower_count FROM follows WHERE store_id = %s", (store_id,))
    follower_count = cursor.fetchone()['follower_count']


    cursor.execute("""
        SELECT s.*, u.username as admin_name
        FROM store_sanctions s
        LEFT JOIN users u ON s.admin_id = u.id
        WHERE s.store_id = %s
        ORDER BY s.created_at DESC
    """, (store_id,))
    sanctions = cursor.fetchall()    

    cursor.execute("""
        SELECT COUNT(*) AS report_count
        FROM store_reports
        WHERE store_id = %s
    """, (store_id,))
    report_summary = cursor.fetchone() or {'report_count': 0}

    cursor.execute("""
        SELECT sr.*, u.username
        FROM store_reports sr
        LEFT JOIN users u ON sr.reporter_user_id = u.id
        WHERE sr.store_id = %s
        ORDER BY sr.created_at DESC
    """, (store_id,))
    store_reports = cursor.fetchall()
    
    cursor.close()
    conn.close()
    
    return render_template('admin/store_detail.html',
                         store=store,
                         store_owner=store_owner,          # NEW
                         store_metrics=store_metrics_agg,
                         product_metrics=product_metrics,
                         store_ratings=store_ratings,
                         avg_rating=avg_rating,
                         products=products,
                         daily_metrics=daily_metrics,
                         sanctions=sanctions,
                         report_summary=report_summary,
                         store_reports=store_reports,
                         follower_count=follower_count)




# ------------------------------
# Admin Product Detail
# ------------------------------
@app.route('/admin/product/<int:listing_id>')
@admin_required
def admin_product_detail(listing_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Get listing info
    cursor.execute("SELECT * FROM listings WHERE listing_id = %s", (listing_id,))
    listing = cursor.fetchone()
    if not listing:
        abort(404)
    
    # Get listing metrics
    cursor.execute("SELECT * FROM listing_metrics WHERE listing_id = %s", (listing_id,))
    metric = cursor.fetchone()
    
    # Get ratings
    cursor.execute("""
        SELECT r.*, u.username
        FROM ratings r
        LEFT JOIN users u ON r.user_id = u.id
        WHERE r.listing_id = %s
        ORDER BY r.created_at DESC
    """, (listing_id,))
    ratings = cursor.fetchall()
    
    # Calculate avg rating
    avg_rating = None
    if ratings:
        avg_rating = sum(float(r['rating_value']) for r in ratings) / len(ratings)
    
    # Get reviews
    cursor.execute("""
        SELECT rev.*, u.username
        FROM reviews rev
        LEFT JOIN users u ON rev.user_id = u.id
        WHERE rev.listing_id = %s
        ORDER BY rev.created_at DESC
    """, (listing_id,))
    reviews = cursor.fetchall()
    
    cursor.close()
    conn.close()
    
    return render_template('admin/product_detail.html',
                         listing=listing,
                         metric=metric,
                         ratings=ratings,
                         reviews=reviews,
                         avg_rating=avg_rating)


# ------------------------------
# Optional: API endpoint for store metrics (JSON)
# ------------------------------
@app.route('/admin/api/store/<int:store_id>/metrics')
@admin_required
def api_store_metrics(store_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT dt, views, clicks, chats, sales
        FROM store_metrics
        WHERE store_id = %s AND dt >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
        ORDER BY dt
    """, (store_id,))
    metrics = cursor.fetchall()
    cursor.close()
    conn.close()
    
    return {
        'dates': [m['dt'].strftime('%Y-%m-%d') for m in metrics],
        'views': [m['views'] for m in metrics],
        'clicks': [m['clicks'] for m in metrics],
        'chats': [m['chats'] for m in metrics],
        'sales': [m['sales'] for m in metrics]
    }







# ------------------------------
# Authentication Routes (minimal)
# ------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    # Get the 'next' URL from query string or form, default to home
    next_url = request.args.get('next') or request.form.get('next') or url_for('home')

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, password, account_status FROM users WHERE email = %s", (email,))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if not user or not check_password_hash(user['password'], password):
            flash("Invalid email or password", "danger")
            return redirect(url_for('login', next=next_url))

        if user['account_status'] == 'Suspended':
            flash("Account suspended. Contact support.", "danger")
            return redirect(url_for('login', next=next_url))

        session['user_id'] = user['id']
        session.permanent = True
        flash("Logged in successfully", "success")
        
        # ✅ Redirect to the original destination (e.g., /my-store)
        return redirect(next_url)

    return render_template('login.html', next_url=next_url)



@app.route("/login/phone", methods=["GET", "POST"])
def login_phone():
    next_url = request.args.get("next") or request.form.get("next") or url_for("home")

    if request.method == "POST":
        country_code = _clean(request.form.get("country_code"))
        phone_number = _clean(request.form.get("phone_number"))

        if not country_code or not phone_number:
            flash("Please enter both country code and phone number.", "danger")
            return redirect(url_for("login_phone", next=next_url))

        contact = normalize_contact(country_code, phone_number)
        user = get_user_by_contact(contact)

        if not user:
            flash(
                "We couldn't find an account with that phone number. Please sign up.",
                "danger",
            )
            return redirect(url_for("signup"))

        if user.get("account_status") == "Suspended":
            flash(
                "Your account is suspended. Please email swapsphere@gmail.com to request reactivation.",
                "danger",
            )
            return redirect(url_for("login_phone", next=next_url))

        if not twilio_client or not TWILIO_VERIFY_SERVICE_SID:
            flash(
                "Phone login is currently unavailable. Please use email/password.",
                "danger",
            )
            return redirect(url_for("login", next=next_url))

        try:
            # Twilio Verify expects E.164, so prepend + if not present
            to_number = contact
            if not to_number.startswith("+"):
                to_number = "+" + to_number

            verification = twilio_client.verify.v2.services(
                TWILIO_VERIFY_SERVICE_SID
            ).verifications.create(to=to_number, channel="sms")

            logging.info(f"Sent verification to {to_number}: status={verification.status}")

            session["phone_login_contact"] = contact
            session["phone_login_next"] = next_url

            flash("We sent a verification code to your phone.", "success")
            return redirect(url_for("login_phone_verify"))

        except Exception as e:
            logging.error(f"Error sending OTP via Twilio to {contact}: {e}")
            flash(
                "We couldn't send a verification code right now. Please try again later.",
                "danger",
            )
            return redirect(url_for("login_phone", next=next_url))

    return render_template("login_phone.html", next_url=next_url)



@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute("""
                SELECT id, security_question
                FROM users
                WHERE email = %s
            """, (email,))
            user = cursor.fetchone()

            if not user or not user['security_question']:
                flash('No account found with that email or no security question set.', 'danger')
                return redirect(url_for('forgot_password'))

            # Store in session for next step
            session['reset_user_id'] = user['id']
            session['reset_question'] = user['security_question']

            return redirect(url_for('verify_security_answer'))

        except Exception as e:
            logging.exception("Error in forgot_password")
            flash('An error occurred. Please try again.', 'danger')
        finally:
            cursor.close()
            conn.close()

    return render_template('forgot_password.html')




@app.route('/verify-security-answer', methods=['GET', 'POST'])
def verify_security_answer():
    if 'reset_user_id' not in session or 'reset_question' not in session:
        flash('Session expired or invalid request. Please start over.', 'danger')
        return redirect(url_for('forgot_password'))

    question = session['reset_question']

    if request.method == 'POST':
        answer = request.form.get('security_answer', '').strip().lower()

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute("""
                SELECT security_answer_hash
                FROM users
                WHERE id = %s
            """, (session['reset_user_id'],))
            user = cursor.fetchone()

            if not user:
                _cleanup_reset_session()
                flash('Account not found.', 'danger')
                return redirect(url_for('forgot_password'))

            if check_password_hash(user['security_answer_hash'], answer):
                session['reset_verified'] = True
                return redirect(url_for('reset_password_form'))
            else:
                flash('Incorrect answer. Please try again.', 'danger')

        except Exception as e:
            logging.exception("Error verifying security answer")
            flash('An error occurred. Please try again.', 'danger')
        finally:
            cursor.close()
            conn.close()

    return render_template('verify_security_answer.html', question=question)












def _cleanup_reset_session():
    """
    Clear temporary reset-related keys from the session.
    Safe to call even if keys don't exist.
    """
    session.pop('reset_user_id', None)
    session.pop('reset_question', None)   # if you use this in verify route
    session.pop('reset_verified', None)





@app.route('/reset-password-form', methods=['GET', 'POST'])
def reset_password_form():
    if 'reset_verified' not in session or 'reset_user_id' not in session:
        flash('Unauthorized access. Please start the reset process again.', 'danger')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        password = request.form.get('password')
        confirm = request.form.get('confirm_password')

        if not password or password != confirm:
            flash('Passwords do not match or are empty.', 'danger')
            return redirect(url_for('reset_password_form'))

        hashed = generate_password_hash(password)

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE users SET password = %s WHERE id = %s",
                           (hashed, session['reset_user_id']))
            conn.commit()

            _cleanup_reset_session()

            flash('Password reset successful! Please log in.', 'success')
            return redirect(url_for('login'))

        except Exception as e:
            conn.rollback()
            logging.exception("Error resetting password")
            flash('Error resetting password. Try again.', 'danger')
        finally:
            cursor.close()
            conn.close()

    return render_template('reset_password_form.html')







@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT * FROM password_reset_tokens
            WHERE token = %s AND expires_at > %s
        """, (token, datetime.now(UTC)))
        token_record = cursor.fetchone()

        if not token_record:
            flash('Invalid or expired token.', 'danger')
            return redirect(url_for('forgot_password'))

        if request.method == 'POST':
            new_password = request.form.get('password', '')
            confirm_password = request.form.get('confirm_password', '')

            if not new_password:
                flash('Password cannot be empty.', 'danger')
                return redirect(request.url)

            if new_password != confirm_password:
                flash('Passwords do not match.', 'danger')
                return redirect(request.url)

            hashed_password = generate_password_hash(new_password)

            cursor.execute("UPDATE users SET password = %s WHERE id = %s",
                           (hashed_password, token_record['user_id']))
            cursor.execute("DELETE FROM password_reset_tokens WHERE token = %s", (token,))
            conn.commit()

            flash('Password updated successfully! You can now log in.', 'success')
            return redirect(url_for('login'))

        return render_template('reset_password.html', token=token)

    except Exception:
        if conn:
            conn.rollback()
        logging.exception("Error in reset_password")
        flash('Error resetting password. Please try again.', 'danger')
        return redirect(url_for('forgot_password'))
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()








@app.route('/logout')
def logout():
    session.clear()
    flash("Logged out", "info")
    return redirect(url_for('home'))

@app.route('/signup', methods=['GET','POST'])
def signup():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if not username or not email or not password:
            flash("All fields required", "danger")
            return redirect(url_for('signup'))
        if password != confirm:
            flash("Passwords do not match", "danger")
            return redirect(url_for('signup'))
        if len(password) < 8:
            flash("Password must be at least 8 characters", "danger")
            return redirect(url_for('signup'))
        
        # ✅ Use werkzeug's generate_password_hash
        hashed = generate_password_hash(password)
        
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO users (username, email, password) VALUES (%s, %s, %s)",
                        (username, email, hashed))
            conn.commit()
            flash("Account created! Please log in.", "success")
            return redirect(url_for('login'))
        except mysql.connector.IntegrityError:
            flash("Email or username already taken", "danger")
            return redirect(url_for('signup'))
        finally:
            cur.close()
            conn.close()
    return render_template('signup.html')




# Google OAuth
@app.route('/login/google')
def google_login():
    redirect_uri = url_for('google_callback', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route('/login/google/callback')
def google_callback():
    token = google.authorize_access_token()
    resp = google.get('userinfo')
    userinfo = resp.json()
    email = userinfo.get('email')
    name = userinfo.get('name')
    if not email:
        flash("Google login failed", "danger")
        return redirect(url_for('login'))
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id FROM users WHERE email = %s", (email,))
    user = cur.fetchone()
    if not user:
        # create new user
        cur.execute("INSERT INTO users (username, email, password) VALUES (%s, %s, '')",
                    (name or email.split('@')[0], email))
        conn.commit()
        user_id = cur.lastrowid
    else:
        user_id = user['id']
    cur.close()
    conn.close()
    session['user_id'] = user_id
    flash("Logged in with Google", "success")
    return redirect(url_for('all_shops'))

# ------------------------------
# Shop Core Routes
# ------------------------------
STORE_PHONE_RULES = {
    '+1': (10, 10), '+44': (9, 10), '+233': (9, 9), '+234': (10, 10),
    '+27': (9, 9), '+254': (9, 9), '+91': (10, 10), '+971': (9, 9),
    '+61': (9, 9), '+81': (9, 10), '+49': (7, 11), '+33': (9, 9),
    '+31': (9, 9), '+39': (6, 10), '+34': (9, 9),
}


def normalize_store_contact(country_code, value):
    """Validate and save supported store contacts in international E.164 form."""
    country_code = (country_code or '').strip()
    raw_contact = (value or '').strip()
    if country_code not in STORE_PHONE_RULES or not raw_contact:
        return None

    digits = re.sub(r'\D', '', raw_contact)
    country_digits = country_code[1:]
    if raw_contact.startswith('+'):
        # A manually supplied international number must match the selected code.
        if not digits.startswith(country_digits):
            return None
        national_number = digits[len(country_digits):]
    elif digits.startswith(country_digits):
        national_number = digits[len(country_digits):]
    else:
        # Most national formats include one trunk zero; Italy retains it in E.164.
        national_number = digits if country_code == '+39' or not digits.startswith('0') else digits[1:]

    min_length, max_length = STORE_PHONE_RULES[country_code]
    if not national_number.isdigit() or not min_length <= len(national_number) <= max_length:
        return None

    return f'{country_code}{national_number}'


@app.route('/create-store', methods=['GET','POST'])
@login_required
def create_store():
    if request.method == 'POST':
        wants_json = (
            request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or request.accept_mimetypes.best == 'application/json'
        )
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        location = request.form.get('location', '').strip()
        country_code = request.form.get('country_code', '').strip()
        contact_input = request.form.get('contact', '').strip()
        contact = normalize_store_contact(country_code, contact_input)
        email = request.form.get('email', '').strip()
        store_type = request.form.get('store_type', '').strip()
        
        logo_file = request.files.get('logo')
        if not name or not store_type or not description or not location or not contact_input or not email or not (logo_file and logo_file.filename):
            msg = "All fields except store banner are required"
            if wants_json:
                return jsonify({"success": False, "message": msg}), 400
            flash(msg, "danger")
            return redirect(url_for('create_store'))

        if not contact:
            msg = "Enter a valid phone number for the selected country code."
            if wants_json:
                return jsonify({"success": False, "message": msg}), 400
            flash(msg, "danger")
            return redirect(url_for('create_store'))
        
        slug = slugify(name)
        
        # Helper to extract URL from Cloudinary result
        def get_url(result):
            if not result:
                return None
            if isinstance(result, dict):
                return result.get('secure_url') or result.get('url')
            return result
        
        logo = None
        banner = None
        
        if logo_file and logo_file.filename:
            logo_upload = upload_to_cloudinary(logo_file, 'stores/logos')
            logo = get_url(logo_upload)
            if not logo:
                print("Logo upload failed:", logo_upload)
        
        banner_file = request.files.get('banner')
        if banner_file and banner_file.filename:
            banner_upload = upload_to_cloudinary(banner_file, 'stores/banners')
            banner = get_url(banner_upload)
            if not banner:
                print("Banner upload failed:", banner_upload)
        
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO stores (user_id, name, slug, logo, banner, description, location, contact, store_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (session['user_id'], name, slug, logo, banner, description, location, contact, store_type))
            conn.commit()
            store_id = cur.lastrowid
            session['show_store_welcome'] = store_id
            
            if wants_json:
                return jsonify({
                    "success": True,
                    "redirect": url_for('store_home', store_id=store_id),
                    "message": "Store created successfully!"
                })
            flash("Store created successfully!", "success")
            return redirect(url_for('store_home', store_id=store_id))
        except mysql.connector.IntegrityError:
            msg = "A store with this name already exists"
            if wants_json:
                return jsonify({"success": False, "message": msg}), 409
            flash(msg, "danger")
            return redirect(url_for('create_store'))
        except Exception as e:
            print("ERROR CREATING STORE:", e)
            if wants_json:
                return jsonify({"success": False, "message": "Server error while creating store"}), 500
            flash("Server error while creating store", "danger")
            return redirect(url_for('create_store'))
        finally:
            cur.close()
            conn.close()
    
    return render_template('create_store.html')



def _get_store_performance_metrics(cur, store_id, period='30'):
    period = str(period)
    if period == 'all':
        date_filter = "1 = 1"
    elif period == 'today':
        date_filter = "dt = CURDATE()"
    elif period == '7':
        date_filter = "dt >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)"
    else:
        period = '30'
        date_filter = "dt >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)"

    cur.execute(f"""
        SELECT
            COALESCE(SUM(views), 0) as views,
            COALESCE(SUM(clicks), 0) as clicks,
            COALESCE(SUM(chats), 0) as whatsapp_redirects,
            COALESCE(SUM(chats), 0) as chats,
            COALESCE(SUM(swaps), 0) as swaps,
            COALESCE(SUM(sales), 0) as sales
        FROM store_metrics
        WHERE store_id = %s AND {date_filter}
    """, (store_id,))
    row = cur.fetchone() or {}

    return {
        'period': period,
        'views': {'current': row.get('views') or 0, 'change': 0},
        'clicks': {'current': row.get('clicks') or 0, 'change': 0},
        'whatsapp_redirects': {'current': row.get('whatsapp_redirects') or 0, 'change': 0},
        'chats': {'current': row.get('chats') or 0, 'change': 0},
        'swaps': {'current': row.get('swaps') or 0, 'change': 0},
        'sales': {'current': row.get('sales') or 0, 'change': 0},
    }


@app.route('/store/<int:store_id>/performance-metrics')
@login_required
def store_performance_metrics(store_id):
    period = request.args.get('period', '30')
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT store_id FROM stores WHERE store_id = %s AND user_id = %s", (store_id, session['user_id']))
        if not cur.fetchone():
            return jsonify({'success': False, 'message': 'Store not found'}), 404

        return jsonify({'success': True, 'metrics': _get_store_performance_metrics(cur, store_id, period)})
    finally:
        cur.close()
        conn.close()


@app.route('/store/<int:store_id>')
@login_required
def store_home(store_id):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    # 1. Fetch the store
    cur.execute("SELECT * FROM stores WHERE store_id = %s AND user_id = %s", (store_id, session['user_id']))
    store = cur.fetchone()
    if not store:
        flash("Store not found or you don't have permission.", "danger")
        return redirect(url_for('home'))

    # Build absolute store URL (e.g., https://kwicshop.com/store/haven-apple-store)
    store_absolute_url = url_for('store_detail', slug=store['slug'], _external=True)

    # After: store = cur.fetchone()
    if not store:
        flash("Store not found or you don't have permission.", "danger")
        return redirect(url_for('home'))

    # --- CHECK STORE STATUS & GET DISABLE REASON ---
    disable_reason = None
    if store.get('is_active') == 0:
        cur.execute("""
            SELECT reason FROM store_sanctions
            WHERE store_id = %s AND action = 'disable'
            ORDER BY created_at DESC LIMIT 1
        """, (store_id,))
        row = cur.fetchone()
        if row:
            disable_reason = row['reason']


    # 2. Promo
    cur.execute("SELECT * FROM store_promos WHERE store_id = %s AND active = 1", (store_id,))
    promo = cur.fetchone() or {}

    # 3. Metrics
    metrics = _get_store_performance_metrics(cur, store_id, '30')

    # 4. Top products
    cur.execute("""
        SELECT 
            l.listing_id, 
            l.title, 
            l.image1,
            COALESCE(m.impressions, 0) AS impressions,
            COALESCE(m.clicks, 0) AS clicks,
            ROUND(COALESCE(m.clicks, 0) * 100.0 / NULLIF(COALESCE(m.impressions, 0), 0), 1) AS ctr
        FROM listings l
        LEFT JOIN listing_metrics m ON l.listing_id = m.listing_id
        WHERE l.store_id = %s
        ORDER BY m.impressions DESC
        LIMIT 5
    """, (store_id,))
    top_by_impressions = cur.fetchall()

    cur.execute("""
        SELECT 
            l.listing_id, 
            l.title, 
            l.image1,
            COALESCE(m.impressions, 0) AS impressions,
            COALESCE(m.clicks, 0) AS clicks,
            ROUND(COALESCE(m.clicks, 0) * 100.0 / NULLIF(COALESCE(m.impressions, 0), 0), 1) AS ctr
        FROM listings l
        LEFT JOIN listing_metrics m ON l.listing_id = m.listing_id
        WHERE l.store_id = %s
        ORDER BY m.clicks DESC
        LIMIT 5
    """, (store_id,))
    top_by_clicks = cur.fetchall()

    cur.close()
    conn.close()
    show_store_welcome = session.pop('show_store_welcome', None) == store_id

    return render_template('store_home.html',
                          store=store,
                          promo=promo,
                          metrics=metrics,
                          top_by_impressions=top_by_impressions,
                          top_by_clicks=top_by_clicks,
                          now=datetime.utcnow(),
                          disable_reason=disable_reason,
                          show_store_welcome=show_store_welcome,
                          store_absolute_url=store_absolute_url)   # <-- ADD THIS






@app.route('/store/<int:store_id>/categories')
@login_required
def get_store_categories(store_id):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT DISTINCT category
        FROM listings
        WHERE store_id = %s AND category IS NOT NULL AND category != ''
        ORDER BY category
    """, (store_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    categories = [row['category'] for row in rows]
    return jsonify(categories)






@app.route('/store/<int:store_id>/update-socials', methods=['POST'])
@login_required
def update_store_socials(store_id):
    # Verify store ownership
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT user_id FROM stores WHERE store_id = %s", (store_id,))
    store = cur.fetchone()
    if not store or store['user_id'] != session['user_id']:
        cur.close()
        conn.close()
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    data = request.get_json()
    facebook = data.get('facebook', '').strip()
    x = data.get('x', '').strip()
    instagram = data.get('instagram', '').strip()
    tiktok = data.get('tiktok', '').strip()

    # Update using the exact column names from your table definition
    cur.execute("""
        UPDATE stores 
        SET Facebook = %s,
            `X ( formerly Twitter)` = %s,
            Instagram = %s,
            TikTok = %s
        WHERE store_id = %s
    """, (facebook, x, instagram, tiktok, store_id))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({'success': True, 'message': 'Social links updated!'})


    






@app.route('/store/<slug>/edit', methods=['GET', 'POST'])
@login_required
def edit_store(slug):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM stores WHERE slug = %s AND user_id = %s", (slug, session['user_id']))
    store = cur.fetchone()
    if not store:
        flash("Store not found or you don't have permission.", "danger")
        cur.close()
        conn.close()
        return redirect(url_for('all_shops'))

    if request.method == 'POST':
        # Basic fields
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        location = request.form.get('location', '').strip()
        contact = request.form.get('contact', '').strip()
        store_type = request.form.get('store_type', '').strip()

        # Delivery options (checkbox list)
        delivery_options = request.form.getlist('delivery_options')
        delivery_json = json.dumps(delivery_options)

        # Start with existing media
        logo_url = store.get('logo')
        banner_url = store.get('banner')
        tour_video_url = store.get('tour_video')

        # Handle logo removal
        if request.form.get('remove_logo'):
            logo_url = None
        # Handle logo upload (replaces existing)
        elif 'logo' in request.files and request.files['logo'].filename:
            logo_result = upload_to_cloudinary(request.files['logo'], 'stores/logos')
            if logo_result and logo_result.get('success'):
                logo_url = logo_result.get('url')
            else:
                flash("Logo upload failed", "warning")

        # Handle banner removal
        if request.form.get('remove_banner'):
            banner_url = None
        elif 'banner' in request.files and request.files['banner'].filename:
            banner_result = upload_to_cloudinary(request.files['banner'], 'stores/banners')
            if banner_result and banner_result.get('success'):
                banner_url = banner_result.get('url')
            else:
                flash("Banner upload failed", "warning")

        # Handle tour video removal
        if request.form.get('remove_tour_video'):
            tour_video_url = None
        elif 'tour_video' in request.files and request.files['tour_video'].filename:
            video_result = upload_to_cloudinary(request.files['tour_video'], 'stores/videos')
            if video_result and video_result.get('success'):
                tour_video_url = video_result.get('url')
            else:
                flash("Tour video upload failed", "warning")

        # Update the store
        cur.execute("""
            UPDATE stores
            SET name = %s,
                description = %s,
                location = %s,
                contact = %s,
                store_type = %s,
                delivery_options = %s,
                logo = %s,
                banner = %s,
                tour_video = %s
            WHERE store_id = %s
        """, (name, description, location, contact, store_type, delivery_json,
              logo_url, banner_url, tour_video_url, store['store_id']))
        conn.commit()
        flash("Store updated successfully!", "success")
        cur.close()
        conn.close()
        return redirect(url_for('store_home', store_id=store['store_id']))

    # For GET request: prepare current delivery options as list
    current_delivery = []
    if store.get('delivery_options'):
        try:
            current_delivery = json.loads(store['delivery_options'])
        except (json.JSONDecodeError, TypeError):
            current_delivery = []

    cur.close()
    conn.close()
    return render_template('edit_store.html', store=store, current_delivery=current_delivery)







@app.route('/store/<slug>/inventory')
@login_required
def store_inventory(slug):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    # IMPORTANT: Select slug here
    cur.execute("""
        SELECT store_id, name, slug 
        FROM stores 
        WHERE slug = %s AND user_id = %s AND is_active = 1
    """, (slug, session['user_id']))
    store = cur.fetchone()

    if not store:
        abort(403)

    # Fetch listings...
    cur.execute("""
        SELECT * FROM listings 
        WHERE store_id = %s
        ORDER BY created_at DESC
    """, (store['store_id'],))
    listings = cur.fetchall()

    cur.close()
    conn.close()

    return render_template('store_inventory.html', store=store, listings=listings)







@app.route('/store/<slug>/inventory/<int:listing_id>/edit', 
           methods=['GET', 'POST'],
           endpoint='edit_inventory')
@login_required
def edit_inventory(slug, listing_id):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    # Verify store ownership
    cur.execute("""
        SELECT store_id, name, slug 
        FROM stores 
        WHERE slug = %s AND user_id = %s AND is_active = 1
    """, (slug, session['user_id']))
    store = cur.fetchone()

    if not store:
        cur.close()
        conn.close()
        abort(403)

    # Verify listing - use backticks around condition since it's a reserved word
    cur.execute("""
        SELECT listing_id, user_id, store_id, title, description, category, 
               `condition` as item_condition, location, contact, price, status, deal_type,
               contact_for_price, image_url, image1, image2, image3, image4, created_at
        FROM listings
        WHERE listing_id = %s AND store_id = %s
    """, (listing_id, store['store_id']))
    listing = cur.fetchone()

    if not listing:
        cur.close()
        conn.close()
        abort(404)
    
    # Rename the key to 'condition' for template compatibility
    if listing and 'item_condition' in listing:
        listing['condition'] = listing['item_condition']

    # Load additional offers for Swap Deals
    offers = []
    if listing.get('deal_type') == 'Swap Deal':
        cur.execute("""
            SELECT * FROM offered_items 
            WHERE listing_id = %s 
            ORDER BY item_id ASC 
            LIMIT 100 OFFSET 1
        """, (listing_id,))
        offers = cur.fetchall()

    # Helper to upload a file and return URL string (or None)
    def upload_file_and_get_url(file, folder):
        if file and file.filename:
            result = upload_to_cloudinary(file, folder)
            if result and isinstance(result, dict):
                return result.get('url')  # extract URL
            return result  # fallback in case it's already a string
        return None

    # POST handling
    if request.method == 'POST':
        # Whitelisted safe update fields
        allowed = ['title', 'description', 'category', 'condition', 'location',
                   'contact', 'status', 'price', 'desired_swap', 'required_cash',
                   'additional_cash', 'swap_notes']
        update_fields = {}
        for k in allowed:
            val = request.form.get(k)
            if val is not None:
                update_fields[k] = val
        update_fields['contact_for_price'] = 1 if request.form.get('contact_for_price') == '1' else 0
        if update_fields['contact_for_price']:
            update_fields['price'] = None

        # Handle main listing images
        for key in ['image_url', 'image1', 'image2', 'image3', 'image4']:
            file = request.files.get(f"{key}_file")
            url = upload_file_and_get_url(file, 'listings')
            if url:
                update_fields[key] = url

        # Update main listing
        if update_fields:
            set_sql = ", ".join(f"`{k}`=%s" for k in update_fields)
            values = list(update_fields.values()) + [listing_id]
            cur.execute(f"UPDATE listings SET {set_sql} WHERE listing_id=%s", values)

        # Swap Deal offered items logic
        if listing.get('deal_type') == 'Swap Deal':
            # First offered item (always present)
            cur.execute("SELECT item_id FROM offered_items WHERE listing_id=%s ORDER BY item_id ASC LIMIT 1", (listing_id,))
            first = cur.fetchone()
            if first:
                # Build update fields for first offered item
                offer_fields = {}
                # Title, description, condition come from main listing or override?
                # Usually they are separate – we'll use form data for offered items
                offer_fields['title'] = request.form.get('offer_title_0') or listing.get('title')
                offer_fields['description'] = request.form.get('offer_description_0') or listing.get('description')
                offer_fields['condition'] = request.form.get('offer_condition_0') or listing.get('condition')
                # Images
                for img_key in ['image_url', 'image1', 'image2', 'image3', 'image4']:
                    file = request.files.get(f"offer_{img_key}_0")
                    url = upload_file_and_get_url(file, 'offers')
                    if url:
                        offer_fields[img_key] = url
                    elif not url and first.get(img_key):
                        # Keep existing if no new upload
                        offer_fields[img_key] = first.get(img_key)

                # Update first offered item
                set_sql = ", ".join(f"`{k}`=%s" for k in offer_fields)
                values = list(offer_fields.values()) + [first['item_id']]
                cur.execute(f"UPDATE offered_items SET {set_sql} WHERE item_id=%s", values)

            # Additional offers (index 1+)
            titles = request.form.getlist('offer_title[]')
            descs = request.form.getlist('offer_description[]')
            conds = request.form.getlist('offer_condition[]')
            # Image file lists for each additional offer
            # We'll iterate over the existing offers (they are already in the DB)
            for idx, offer in enumerate(offers):
                if idx < len(titles):
                    offer_fields = {
                        'title': titles[idx],
                        'description': descs[idx],
                        'condition': conds[idx]
                    }
                    # Handle images
                    for img_key in ['image_url', 'image1', 'image2', 'image3', 'image4']:
                        file = request.files.get(f"offer_{img_key}_{idx+1}")
                        url = upload_file_and_get_url(file, 'offers')
                        if url:
                            offer_fields[img_key] = url
                        elif not url and offer.get(img_key):
                            offer_fields[img_key] = offer.get(img_key)
                    set_sql = ", ".join(f"`{k}`=%s" for k in offer_fields)
                    values = list(offer_fields.values()) + [offer['item_id']]
                    cur.execute(f"UPDATE offered_items SET {set_sql} WHERE item_id=%s", values)

        conn.commit()
        cur.close()
        conn.close()
        flash("Item updated successfully!", "success")
        return redirect(url_for('store_inventory', slug=slug))

    cur.close()
    conn.close()
    return render_template('edit_inventory.html', store=store, listing=listing, offers=offers)








def send_push_notification_to_store_followers(store_id, title, body, url):
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
    
    vapid_private_key = os.environ.get('VAPID_PRIVATE_KEY')
    vapid_public_key = os.environ.get('VAPID_PUBLIC_KEY')
    vapid_claims = {"sub": "mailto:notifications@kwicshop.com"}
    
    for sub in subscriptions:
        subscription_info = {
            "endpoint": sub['endpoint'],
            "keys": {"p256dh": sub['p256dh'], "auth": sub['auth']}
        }
        payload = {
            "title": title,
            "body": body,
            "url": url,
            "icon": "/static/store-icon.png"
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=json.dumps(payload),
                vapid_private_key=vapid_private_key,
                vapid_claims=vapid_claims
            )
        except WebPushException as e:
            print(f"Push failed for {sub['endpoint']}: {e}")





def slugify(text):
    """Simple slugify for URL generation (if needed elsewhere)"""
    return re.sub(r'[\W_]+', '-', text.lower()).strip('-')

@app.route('/store/add-item', methods=['POST'])
@login_required
def store_add_item():
    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    
    # Get store details
    cur.execute("SELECT store_id, name, slug FROM stores WHERE user_id = %s LIMIT 1", (user_id,))
    store = cur.fetchone()
    if not store:
        cur.close()
        conn.close()
        return jsonify({"success": False, "message": "No store found. Create one first."}), 400
    
    store_id = store['store_id']
    store_name = store['name']
    store_slug = store.get('slug')

    def parse_price(deal_type, price_str, contact_for_price):
        if deal_type != 'Outright Sales':
            return None, None
        if contact_for_price:
            return None, None
        if not price_str:
            return None, "Price is required"
        try:
            normalized_price = price_str.replace(',', '').strip()
            if not re.fullmatch(r'\d+(\.\d{1,2})?', normalized_price):
                return None, "Invalid price. Use numbers only, with up to 2 decimal places."
            price = Decimal(normalized_price)
            if price <= 0:
                raise ValueError
            price = price.quantize(Decimal('0.01'))
            return price, None
        except Exception:
            return None, "Invalid price. Must be a positive number."

    def upload_listing_images(files):
        images = []
        for f in files:
            if f and allowed_file(f.filename):
                result = upload_to_cloudinary(f, 'listings')
                if result:
                    if isinstance(result, dict):
                        url = result.get('url') or result.get('secure_url')
                    else:
                        url = result
                    if url:
                        images.append(url)
        return images[:5]

    products_payload = request.form.get('products_json')
    if products_payload:
        try:
            products = json.loads(products_payload)
        except json.JSONDecodeError:
            cur.close()
            conn.close()
            return jsonify({"success": False, "message": "Invalid product batch data"}), 400
    else:
        products = [{
            "deal_type": request.form.get('deal_type'),
            "title": request.form.get('title', ''),
            "description": request.form.get('description', ''),
            "category": request.form.get('category', ''),
            "condition": request.form.get('condition', ''),
            "location": request.form.get('location', ''),
            "contact": request.form.get('contact', ''),
            "price": request.form.get('price', ''),
            "contact_for_price": request.form.get('contact_for_price') == '1',
            "plan": request.form.get('plan', 'Free'),
            "image_field": "images[]"
        }]

    if not isinstance(products, list) or not products:
        cur.close()
        conn.close()
        return jsonify({"success": False, "message": "Add at least one product before submitting."}), 400

    inserted_listing_ids = []
    inserted_titles = []
    try:
        for idx, product in enumerate(products):
            deal_type = (product.get('deal_type') or 'Outright Sales').strip()
            title = (product.get('title') or '').strip()
            description = (product.get('description') or '').strip()
            category = (product.get('category') or '').strip()
            condition = (product.get('condition') or '').strip()
            location = (product.get('location') or '').strip()
            contact = (product.get('contact') or '').strip()
            price_str = str(product.get('price') or '').strip()
            raw_contact_for_price = product.get('contact_for_price')
            contact_for_price = raw_contact_for_price is True or str(raw_contact_for_price).lower() in ('1', 'true', 'on', 'yes')
            plan = (product.get('plan') or 'Free').strip()

            if not title or not description or not category or not location or not contact:
                raise ValueError(f"Product {idx + 1}: missing required fields")
            if deal_type == 'Outright Sales' and not condition:
                raise ValueError(f"Product {idx + 1}: please select a condition")

            price, price_error = parse_price(deal_type, price_str, contact_for_price)
            if price_error:
                raise ValueError(f"Product {idx + 1}: {price_error}")

            image_field = product.get('image_field') or f'product_images_{idx}[]'
            images = upload_listing_images(request.files.getlist(image_field))
            if deal_type == 'Outright Sales' and len(images) < 2:
                raise ValueError(f"Product {idx + 1}: at least 2 images required")

            padded = (images + [None] * 5)[:5]
            cur.execute("""
                INSERT INTO listings (user_id, store_id, title, description, category, `condition`, location, contact,
                                      price, contact_for_price, deal_type, Plan, image_url, image1, image2, image3, image4)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (user_id, store_id, title, description, category, condition, location, contact,
                  price, 1 if contact_for_price else 0, deal_type, plan,
                  padded[0], padded[1], padded[2], padded[3], padded[4]))
            inserted_listing_ids.append(cur.lastrowid)
            inserted_titles.append(title)

        conn.commit()
    except ValueError as e:
        conn.rollback()
        cur.close()
        conn.close()
        return jsonify({"success": False, "message": str(e)}), 400
    except Exception as e:
        conn.rollback()
        app.logger.error("Add item batch error: %s", e, exc_info=True)
        cur.close()
        conn.close()
        return jsonify({"success": False, "message": "Could not add products right now. Please try again."}), 500

    cur.close()
    conn.close()

    # Send push notification
    try:
        from notifications import send_push_notification_to_store_followers
        listing_count = len(inserted_listing_ids)
        notification_title = f"{listing_count} new items from {store_name}!" if listing_count > 1 else f"New item from {store_name}!"
        notification_body = f"{inserted_titles[0]} and {listing_count - 1} more" if listing_count > 1 else inserted_titles[0][:120]
        notification_url = url_for('store_detail', slug=store_slug, _external=True) if listing_count > 1 and store_slug else url_for('listing_details', listing_id=inserted_listing_ids[0], _external=True)
        send_push_notification_to_store_followers(
            store_id=store_id,
            title=notification_title,
            body=notification_body,
            url=notification_url
        )
    except Exception as e:
        print(f"Push notification error (non-critical): {e}")

    item_word = "item" if len(inserted_listing_ids) == 1 else "items"
    
    return jsonify({
        "success": True,
        "redirect": url_for('store_home', store_id=store_id),
        "count": len(inserted_listing_ids),
        "message": f"{len(inserted_listing_ids)} {item_word} added successfully!"
    })



@app.route('/store/<slug>/listing/<int:listing_id>/delete', methods=['POST'])
@login_required
def delete_store_listing(slug, listing_id):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT store_id FROM stores WHERE slug = %s AND user_id = %s", (slug, session['user_id']))
    store = cur.fetchone()
    if not store:
        flash("Store not found", "danger")
        return redirect(url_for('all_shops'))
    cur.execute("DELETE FROM listings WHERE listing_id = %s AND store_id = %s", (listing_id, store['store_id']))
    conn.commit()
    cur.close()
    conn.close()
    flash("Listing deleted", "success")
    return redirect(url_for('store_inventory', slug=slug))


@app.route('/store/<int:store_id>/delete', methods=['POST'])
@login_required
def delete_store(store_id):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    try:
        cur.execute(
            "SELECT store_id, name FROM stores WHERE store_id = %s AND user_id = %s",
            (store_id, session['user_id'])
        )
        store = cur.fetchone()
        if not store:
            flash("Store not found or you do not have permission to delete it.", "danger")
            return redirect(url_for('home'))

        cur.execute("SELECT listing_id FROM listings WHERE store_id = %s", (store_id,))
        listing_ids = [row['listing_id'] for row in cur.fetchall()]

        def has_column(table, column):
            cur.execute("""
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = DATABASE()
                  AND table_name = %s
                  AND column_name = %s
                LIMIT 1
            """, (table, column))
            return cur.fetchone() is not None

        def delete_by_column(table, column, values):
            if not values or not has_column(table, column):
                return
            placeholders = ", ".join(["%s"] * len(values))
            cur.execute(f"DELETE FROM `{table}` WHERE `{column}` IN ({placeholders})", tuple(values))

        listing_linked_tables = [
            'offered_items',
            'listing_metrics',
            'ratings',
            'reviews',
            'proposals',
            'wishlist',
            'wishlists',
            'wishlist_items',
            'notifications',
            'notification_log',
        ]
        for table in listing_linked_tables:
            delete_by_column(table, 'listing_id', listing_ids)

        store_linked_tables = [
            'store_metrics',
            'store_ratings',
            'store_reports',
            'store_sanctions',
            'store_promos',
            'follows',
            'push_subscriptions',
            'notifications',
            'notification_log',
        ]
        for table in store_linked_tables:
            delete_by_column(table, 'store_id', [store_id])

        cur.execute("DELETE FROM listings WHERE store_id = %s", (store_id,))
        cur.execute("DELETE FROM stores WHERE store_id = %s AND user_id = %s", (store_id, session['user_id']))
        conn.commit()

        session['store_delete_message'] = f"Store '{store['name']}' and all related products/data have been deleted."
        return redirect(url_for('home'))
    except Exception as e:
        conn.rollback()
        app.logger.error("Store deletion failed for store_id=%s: %s", store_id, e, exc_info=True)
        flash("Could not delete the store right now. Please try again.", "danger")
        return redirect(url_for('store_home', store_id=store_id))
    finally:
        cur.close()
        conn.close()

# ------------------------------
# Follow / Unfollow
# ------------------------------
@app.route('/store/<int:store_id>/follow', methods=['POST'])
@login_required
def follow_store(store_id):
    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO follows (user_id, store_id) VALUES (%s, %s)", (user_id, store_id))
        conn.commit()
        cur.execute("SELECT COUNT(*) AS follower_count FROM follows WHERE store_id = %s", (store_id,))
        follower_count = cur.fetchone()[0]
        return jsonify({"success": True, "follower_count": follower_count})
    except Exception:
        conn.rollback()
        return jsonify({"success": False, "message": "Already following or error"}), 400
    finally:
        cur.close()
        conn.close()

@app.route('/store/<int:store_id>/unfollow', methods=['POST'])
@login_required
def unfollow_store(store_id):
    user_id = session['user_id']
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM follows WHERE user_id = %s AND store_id = %s", (user_id, store_id))
        conn.commit()
        cur.execute("SELECT COUNT(*) AS follower_count FROM follows WHERE store_id = %s", (store_id,))
        follower_count = cur.fetchone()[0]
        return jsonify({"success": True, "follower_count": follower_count})
    finally:
        cur.close()
        conn.close()



@app.route('/store/<int:store_id>/follow-status', methods=['GET'])
def follow_status(store_id):
    if 'user_id' not in session:
        return jsonify({'followed': False})

    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute(
        "SELECT id FROM follows WHERE user_id = %s AND store_id = %s",
        (user_id, store_id)
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return jsonify({'followed': row is not None})


        

# ------------------------------
# Store Ratings
# ------------------------------
@app.route('/ratings/store', methods=['POST'])
@login_required
def rate_store():
    data = request.get_json()
    store_id = data.get('store_id')
    rating = int(data.get('rating'))
    comment = data.get('comment', '').strip()
    if not (1 <= rating <= 5):
        return jsonify({"success": False, "message": "Rating 1-5"}), 400
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO store_ratings (store_id, user_id, rating, comment)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE rating = VALUES(rating), comment = VALUES(comment)
        """, (store_id, session['user_id'], rating, comment))
        conn.commit()
        # recompute avg
        cur.execute("SELECT AVG(rating) as avg, COUNT(*) as cnt FROM store_ratings WHERE store_id=%s", (store_id,))
        row = cur.fetchone()
        avg = row[0] or 0
        cnt = row[1]
        cur.execute("UPDATE stores SET rating_avg=%s, rating_count=%s WHERE store_id=%s", (avg, cnt, store_id))
        conn.commit()
        return jsonify({"success": True, "rating_avg": float(avg), "rating_count": cnt})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        cur.close()
        conn.close()










# ------------------------------
# Promotions (popup/video)
# ------------------------------
@app.route('/store/<slug>/upload-promo-media', methods=['POST'])
def upload_promo_media(slug):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    try:
        cur.execute("SELECT store_id, user_id FROM stores WHERE slug = %s", (slug,))
        store = cur.fetchone()
        if not store:
            return jsonify({'success': False, 'message': 'Store not found'}), 404
        if store['user_id'] != session['user_id']:
            return jsonify({'success': False, 'message': 'Permission denied'}), 403

        if 'media' not in request.files:
            return jsonify({'success': False, 'message': 'No file uploaded'}), 400
        file = request.files['media']
        if file.filename == '':
            return jsonify({'success': False, 'message': 'Empty filename'}), 400

        result = upload_to_cloudinary(file, folder=f"store_promos_temp/{store['store_id']}")
        if not result['success']:
            return jsonify({'success': False, 'message': result['error']}), 500

        return jsonify({
            'success': True,
            'url': result['url'],
            'public_id': result['public_id'],
            'resource_type': result['resource_type'],
            'file_type': 'video' if result['resource_type'] == 'video' else 'image'
        })
    except Exception as e:
        current_app.logger.error(f"Upload promo media error: {str(e)}")
        return jsonify({'success': False, 'message': 'Upload failed'}), 500
    finally:
        cur.close()
        conn.close()

# ---------- ROUTE: UPDATE STORE PROMO ----------
from datetime import datetime

@app.route('/store/<slug>/update-promo', methods=['POST'])
def update_store_promo(slug):
    if 'user_id' not in session:
        flash('Please log in first.', 'error')
        return redirect(url_for('login'))

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    try:
        cur.execute("SELECT store_id, user_id FROM stores WHERE slug = %s", (slug,))
        store = cur.fetchone()
        if not store:
            flash('Store not found.', 'error')
            return redirect(url_for('home'))
        if store['user_id'] != session['user_id']:
            flash('Permission denied.', 'error')
            return redirect(url_for('store_home', store_id=store_id))   # ← use store_id

        store_id = store['store_id']

        # Get current promo to delete old media
        cur.execute("SELECT public_id, media_url FROM store_promos WHERE store_id = %s", (store_id,))
        existing_promo = cur.fetchone()

        media_url = request.form.get('media_url', '')
        public_id = request.form.get('public_id', '')
        temp_public_id = request.form.get('temp_public_id', '')

        # Handle new file upload
        if 'media_file' in request.files and request.files['media_file'].filename:
            file = request.files['media_file']
            result = upload_to_cloudinary(file, folder=f"store_promos/{store_id}")
            if result['success']:
                media_url = result['url']
                public_id = result['public_id']
                # Delete old permanent media
                if existing_promo and existing_promo.get('public_id'):
                    delete_from_cloudinary(
                        existing_promo['public_id'],
                        'video' if existing_promo.get('media_url', '').endswith(('.mp4','.mov','.webm')) else 'image'
                    )
                # Delete temp file
                if temp_public_id:
                    delete_from_cloudinary(temp_public_id, 'video' if file.content_type.startswith('video/') else 'image')
        # Clone temp file if it exists and no new file
        elif temp_public_id and not public_id:
            try:
                clone_result = cloudinary.uploader.upload(
                    cloudinary.utils.cloudinary_url(temp_public_id)[0],
                    public_id=f"store_promos/{store_id}/{temp_public_id.split('/')[-1]}",
                    resource_type=request.form.get('resource_type', 'image')
                )
                media_url = clone_result['secure_url']
                public_id = clone_result['public_id']
                delete_from_cloudinary(temp_public_id, request.form.get('resource_type', 'image'))
            except Exception as e:
                current_app.logger.error(f"Error cloning temp file: {str(e)}")

        # Get form data
        media_type = request.form.get('media_type', 'image')
        description = request.form.get('description', '')
        button_text = request.form.get('button_text', 'Shop Now')
        button_link = request.form.get('button_link', '')
        frequency = request.form.get('frequency', 'once_per_session')
        active = request.form.get('active', '1') == '1'

        # --- NEW: Parse start_date and end_date ---
        start_date_str = request.form.get('start_date')
        end_date_str = request.form.get('end_date')
        start_date = None
        end_date = None
        if start_date_str:
            try:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            except ValueError:
                flash('Invalid start date format.', 'error')
                return redirect(url_for('store_home', store_id=store_id))   # ← use store_id
        if end_date_str:
            try:
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            except ValueError:
                flash('Invalid start date format.', 'error')
                return redirect(url_for('store_home', store_id=store_id))   # ← use store_id

        # Check if promo exists
        cur.execute("SELECT promo_id FROM store_promos WHERE store_id = %s", (store_id,))
        existing = cur.fetchone()

        if existing:
            # Update existing promo
            cur.execute("""
                UPDATE store_promos 
                SET media_type = %s, media_url = %s, public_id = %s,
                    description = %s, button_text = %s, button_link = %s,
                    frequency = %s, active = %s,
                    start_date = %s, end_date = %s,
                    updated_at = NOW()
                WHERE store_id = %s
            """, (media_type, media_url, public_id, description, button_text,
                  button_link, frequency, active,
                  start_date, end_date, store_id))
        else:
            # Insert new promo
            cur.execute("""
                INSERT INTO store_promos 
                (store_id, media_type, media_url, public_id, description, button_text, 
                 button_link, frequency, active, start_date, end_date, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            """, (store_id, media_type, media_url, public_id, description, button_text,
                  button_link, frequency, active, start_date, end_date))
        conn.commit()
        flash('Promotion settings saved successfully!', 'success')
    except Exception as e:
        current_app.logger.error(f"Error updating promo: {str(e)}")
        conn.rollback()
        flash('Error saving promotion settings.', 'error')
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('store_home', store_id=store_id))


@app.route('/store/<slug>/delete-promo', methods=['POST'])
@login_required
def delete_store_promo(slug):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        DELETE p FROM store_promos p
        JOIN stores s ON p.store_id = s.store_id
        WHERE s.slug = %s AND s.user_id = %s
    """, (slug, session['user_id']))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"success": True})

# ------------------------------
# Store Boost (Paystack)
# ------------------------------
STORE_PLANS = {
    "Silver": {"price": 25, "days": 14},
    "Gold":   {"price": 50, "days": 21},
    "Diamond":{"price": 100, "days": 30}
}




def save_file(file, subfolder):
    if not file or not file.filename:
        return None

    filename = secure_filename(file.filename)
    # Generate unique filename to prevent collisions
    import uuid
    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    filename = f"{uuid.uuid4().hex}.{ext}"

    # Base upload folder
    upload_folder = os.path.join(current_app.static_folder, subfolder)  # e.g., static/videos

    if not os.path.exists(upload_folder):
        os.makedirs(upload_folder)

    filepath = os.path.join(upload_folder, filename)
    file.save(filepath)

    # Return URL path that Flask can serve via /static/
    return f"/static/{subfolder}/{filename}"





@app.route('/store/<slug>/upload-tour-video', methods=['POST'])
@login_required
def upload_tour_video(slug):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT store_id, user_id, tour_video FROM stores WHERE slug=%s", (slug,))
    store = cur.fetchone()

    if not store or store['user_id'] != session['user_id']:
        cur.close()
        conn.close()
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    video = request.files.get('tour_video')
    allowed_extensions = {'mp4', 'mov', 'webm', 'avi'}

    if not video or not video.filename:
        return jsonify({'success': False, 'message': 'No video provided'}), 400

    ext = video.filename.rsplit('.', 1)[1].lower() if '.' in video.filename else ''
    if ext not in allowed_extensions:
        return jsonify({'success': False, 'message': 'Invalid video format'}), 400

    # 1. Delete previous video from Cloudinary if it exists
    old_video_url = store.get('tour_video')
    old_public_id = None
    if old_video_url:
        # Extract public_id from the stored Cloudinary URL
        # Example URL: https://res.cloudinary.com/.../upload/v1234567/store_promos/name_timestamp.mp4
        # You can parse it, but simpler: store public_id in DB alongside URL.
        # For now, we assume you only store the URL. Better approach: add a column tour_video_public_id.
        # We'll show a robust way below.

        # If you have a separate column tour_video_public_id:
        # old_public_id = store.get('tour_video_public_id')
        # if old_public_id:
        #     delete_from_cloudinary(old_public_id, resource_type="video")
        pass

    # 2. Upload new video to Cloudinary
    upload_result = upload_to_cloudinary(video, folder="store_videos", resource_type="video")

    if not upload_result['success']:
        cur.close()
        conn.close()
        return jsonify({'success': False, 'message': f'Upload failed: {upload_result["error"]}'}), 500

    # 3. Update database with new Cloudinary URL and public_id
    cur.execute("""
        UPDATE stores 
        SET tour_video = %s, tour_video_public_id = %s
        WHERE store_id = %s
    """, (upload_result['url'], upload_result['public_id'], store['store_id']))
    conn.commit()

    # 4. If there was an old video and we have its public_id, delete it now
    if old_public_id:
        delete_from_cloudinary(old_public_id, resource_type="video")

    cur.close()
    conn.close()
    return jsonify({'success': True, 'video_url': upload_result['url']})






@app.route('/store/<slug>/update-theme', methods=['POST'])
def update_store_theme(slug):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 401

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    
    cur.execute("SELECT store_id, user_id FROM stores WHERE slug = %s", (slug,))
    store = cur.fetchone()
    
    if not store or store['user_id'] != session['user_id']:
        cur.close()
        conn.close()
        return jsonify({'success': False, 'message': 'Not authorized'}), 403

    data = request.get_json()
    theme = data.get('color_theme')

    # Expanded list – must match the IDs used in templates
    valid_themes = [
        'default', 'warm-food', 'cool-ocean', 'gold-premium',
        'purple-luxury', 'forest-green', 'coral-vibrant', 'midnight-dark',
        'emerald-teal', 'sunset-orange', 'lavender-plum', 'sky-blue',
        'ruby-red', 'mustard-yellow', 'slate-charcoal', 'rose-gold',
        'deep-navy', 'burgundy-wine'
    ]
    
    if theme not in valid_themes:
        cur.close()
        conn.close()
        return jsonify({'success': False, 'message': 'Invalid theme'}), 400

    cur.execute("UPDATE stores SET color_theme = %s WHERE store_id = %s", 
                (theme, store['store_id']))
    conn.commit()
    
    cur.close()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Theme updated'})







@app.route('/store/<slug>/boost', methods=['GET','POST'])
@login_required
def store_boost(slug):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT store_id, Plan, Plan_expiry_date FROM stores WHERE slug=%s AND user_id=%s", (slug, session['user_id']))
    store = cur.fetchone()
    if not store:
        flash("Store not found", "danger")
        return redirect(url_for('all_shops'))
    if request.method == 'POST':
        plan = request.form.get('plan')
        if plan not in STORE_PLANS:
            flash("Invalid plan", "danger")
            return redirect(url_for('store_boost', slug=slug))
        amount = STORE_PLANS[plan]['price']
        # Store pending in session
        session['pending_boost'] = {'store_id': store['store_id'], 'plan': plan, 'days': STORE_PLANS[plan]['days']}
        # Redirect to Paystack
        return redirect(url_for('paystack_payment', plan=plan, amount=amount))
    cur.close()
    conn.close()
    return render_template('boost.html', store=store, plans=STORE_PLANS)

@app.route('/paystack_payment')
@login_required
def paystack_payment():
    plan = request.args.get('plan')
    amount = int(float(request.args.get('amount', 0)) * 100)  # pesewas
    if not plan or amount <= 0:
        flash("Invalid payment request", "danger")
        return redirect(url_for('all_shops'))
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT email FROM users WHERE id = %s", (session['user_id'],))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if not user:
        flash("User not found", "danger")
        return redirect(url_for('all_shops'))
    payload = {
        "email": user['email'],
        "amount": amount,
        "callback_url": url_for('paystack_verify', _external=True),
        "metadata": {"plan": plan, "user_id": session['user_id']}
    }
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    resp = requests.post("https://api.paystack.co/transaction/initialize", json=payload, headers=headers).json()
    if resp.get('status'):
        return redirect(resp['data']['authorization_url'])
    flash("Payment initialization failed", "danger")
    return redirect(url_for('all_shops'))

@app.route('/paystack_verify')
@login_required
def paystack_verify():
    ref = request.args.get('reference')
    if not ref:
        flash("Missing reference", "danger")
        return redirect(url_for('all_shops'))
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    resp = requests.get(f"https://api.paystack.co/transaction/verify/{ref}", headers=headers).json()
    if not resp.get('status') or resp['data']['status'] != 'success':
        flash("Payment failed", "danger")
        return redirect(url_for('all_shops'))
    metadata = resp['data']['metadata']
    plan = metadata.get('plan')
    # Apply boost or create listing from pending
    if 'pending_boost' in session:
        pending = session.pop('pending_boost')
        store_id = pending['store_id']
        days = pending['days']
        expires = datetime.utcnow() + timedelta(days=days)
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE stores SET Plan=%s, Plan_expiry_date=%s WHERE store_id=%s", (plan, expires, store_id))
        conn.commit()
        cur.close()
        conn.close()
        flash(f"Store boosted to {plan} plan!", "success")
        return redirect(url_for('store_home', store_id=store_id))
    elif 'pending_listing' in session:
        pending = session.pop('pending_listing')
        # insert listing as paid
        conn = get_db_connection()
        cur = conn.cursor()
        images = pending.get('images', [])
        padded = (images + [None]*5)[:5]
        cur.execute("""
            INSERT INTO listings (user_id, store_id, title, description, category, location, contact,
                                  price, contact_for_price, deal_type, Plan, image_url, image1, image2, image3, image4)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (pending['user_id'], pending['store_id'], pending['title'], pending['description'],
              pending['category'], pending['location'], pending['contact'], pending['price'],
              1 if pending.get('contact_for_price') else 0, pending['deal_type'], plan,
              padded[0], padded[1], padded[2], padded[3], padded[4]))
        conn.commit()
        cur.close()
        conn.close()
        flash("Listing created with promotion!", "success")
        return redirect(url_for('store_inventory', slug=slugify(pending['title'])))
    flash("No pending action", "warning")
    return redirect(url_for('all_shops'))

# ------------------------------
# All Shops (public listing)
# ------------------------------


@app.route('/')
def home():
    get_flashed_messages()
    store_delete_message = session.pop('store_delete_message', None)
    search = request.args.get('search', '').strip()
    location = request.args.get('location', '').strip()
    store_type = request.args.get('store_type', '').strip()
    
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    
    query = "SELECT * FROM stores WHERE is_active = 1"
    params = []

    if search:
        query += " AND name LIKE %s"
        params.append(f"%{search}%")

    if location:
        query += " AND location LIKE %s"
        params.append(f"%{location}%")

    if store_type:
        query += " AND store_type = %s"
        params.append(store_type)

    # ⬇️ LIMIT 4 – only 4 stores on main page
    query += " ORDER BY Plan_priority DESC, trust_score DESC, created_at DESC LIMIT 6"

    cur.execute(query, params)
    stores = cur.fetchall()
    
    cur.execute("""
        SELECT DISTINCT store_type 
        FROM stores 
        WHERE store_type IS NOT NULL 
        AND store_type != ''
    """)
    store_types = cur.fetchall()

    cur.close()
    conn.close()
    
    # Mark followed status if logged in
    if 'user_id' in session:
        conn = get_db_connection()
        cur = conn.cursor()

        for s in stores:
            cur.execute(
                "SELECT 1 FROM follows WHERE user_id=%s AND store_id=%s",
                (session['user_id'], s['store_id'])
            )
            s['is_followed'] = cur.fetchone() is not None

        cur.close()
        conn.close()
    
    return render_template(
        'shops.html',
        stores=stores,
        store_types=store_types,
        search=search,
        location=location,
        store_type=store_type,
        is_logged_in='user_id' in session,
        store_delete_message=store_delete_message
    )


# NEW ENDPOINT: returns ALL active stores matching filters (no limit)
@app.route('/api/stores/all')
def api_stores_all():
    search = request.args.get('search', '').strip()
    location = request.args.get('location', '').strip()
    store_type = request.args.get('store_type', '').strip()
    
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    
    # Added store_type to SELECT
    query = """
        SELECT store_id, name, location, slug, store_type
        FROM stores
        WHERE is_active = 1
    """
    params = []
    
    if search:
        query += " AND name LIKE %s"
        params.append(f"%{search}%")
    
    if location:
        query += " AND location LIKE %s"
        params.append(f"%{location}%")
    
    if store_type:
        query += " AND store_type = %s"
        params.append(store_type)
    
    query += " ORDER BY Plan_priority DESC, trust_score DESC, created_at DESC"
    
    cur.execute(query, params)
    stores = cur.fetchall()
    cur.close()
    conn.close()
    
    return {"stores": stores}




# ------------------------------
# Store Detail (public view)
# ------------------------------
@app.route('/store/<slug>')
def store_detail(slug):
    user_id = session.get('user_id')
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    
    cur.execute("""
        SELECT 
            store_id, user_id, name, slug, logo, banner, tour_video, description,
            location, contact, delivery_options, verified, trust_score,
            rating_avg, rating_count, is_active, created_at, updated_at,
            store_link, store_type, Plan, Plan_expiry_date, color_theme,
            promo_media_type, promo_media_url, promo_description,
            promo_button_text, promo_button_link, promo_frequency,
            promo_active, promo_start_date, promo_end_date, Plan_priority,
            COALESCE(`Facebook`, '') AS facebook_url,
            COALESCE(`X ( formerly Twitter)`, '') AS twitter_url,
            COALESCE(`Instagram`, '') AS instagram_url,
            COALESCE(`TikTok`, '') AS tiktok_url
        FROM stores 
        WHERE slug = %s AND is_active = 1
    """, (slug,))
    store = cur.fetchone()
    if not store:
        abort(404)
    
    # get listings
    cur.execute("SELECT * FROM listings WHERE store_id = %s AND status != 'deleted' ORDER BY created_at DESC", (store['store_id'],))
    listings = cur.fetchall()
    cur.execute("""
        SELECT DISTINCT category
        FROM listings
        WHERE store_id = %s AND category IS NOT NULL AND category != ''
        ORDER BY category
    """, (store['store_id'],))
    category_rows = cur.fetchall()
    categories = [row['category'] for row in category_rows]

    
    # Process each listing to add computed fields
    for listing in listings:
        # Convert price to float if it exists
        try:
            listing['price_float'] = float(listing['price']) if listing['price'] else 0.0
        except (TypeError, ValueError):
            listing['price_float'] = 0.0
        
        # Determine main image and slideshow image set
        image_candidates = [
            listing.get('image_url'),
            listing.get('image1'),
            listing.get('image2'),
            listing.get('image3'),
            listing.get('image4')
        ]
        listing_images = []
        for image in image_candidates:
            if image and image not in listing_images:
                listing_images.append(image)

        main_image = listing_images[0] if listing_images else None
        if not main_image:
            main_image = url_for('static', filename='images/placeholder.jpg')
            listing_images = [main_image]
        listing['main_image'] = main_image
        listing['images'] = listing_images
        
        # Convert numeric fields for swap deals
        try:
            listing['required_cash_float'] = float(listing['required_cash']) if listing.get('required_cash') else 0.0
        except (TypeError, ValueError):
            listing['required_cash_float'] = 0.0
        
        try:
            listing['additional_cash_float'] = float(listing['additional_cash']) if listing.get('additional_cash') else 0.0
        except (TypeError, ValueError):
            listing['additional_cash_float'] = 0.0
    
    # get ratings – fix ambiguous created_at
    cur.execute("""
        SELECT r.rating, r.comment, r.created_at AS created_at, u.username 
        FROM store_ratings r 
        JOIN users u ON r.user_id = u.id 
        WHERE r.store_id = %s 
        ORDER BY r.created_at DESC 
        LIMIT 20
    """, (store['store_id'],))
    ratings = cur.fetchall()
    
    # get promo
    cur.execute("""
        SELECT * FROM store_promos 
        WHERE store_id = %s AND active = 1 
          AND (start_date IS NULL OR start_date <= CURDATE()) 
          AND (end_date IS NULL OR end_date >= CURDATE())
    """, (store['store_id'],))
    promo = cur.fetchone()

    cur.execute("SELECT COUNT(*) AS follower_count FROM follows WHERE store_id = %s", (store['store_id'],))
    follower_count = cur.fetchone()['follower_count']

    is_following = False
    if user_id is not None:
        cur.execute(
            "SELECT 1 FROM follows WHERE user_id = %s AND store_id = %s",
            (user_id, store['store_id'])
        )
        is_following = cur.fetchone() is not None
    
    cur.close()
    conn.close()

    is_logged_in = user_id is not None
    is_owner = user_id == store.get('user_id')  # adjust to your column

    return render_template(
        'store_detail.html',
        store=store,
        listings=listings,
        ratings=ratings,
        promo=promo,
        categories=categories, 
        is_logged_in=is_logged_in,
        is_owner=is_owner,
        is_following=is_following,
        follower_count=follower_count,
        vapid_public_key=VAPID_PUBLIC_KEY
    )


@app.route('/store/<slug>/report', methods=['POST'])
def report_store(slug):
    reason = (request.form.get('reason') or '').strip()
    if len(reason) < 10:
        flash('Please add a clear reason for reporting this store.', 'warning')
        return redirect(url_for('store_detail', slug=slug))

    reporter_user_id = session.get('user_id')
    raw_fingerprint = f"{request.remote_addr or ''}|{request.headers.get('User-Agent', '')[:250]}"
    reporter_fingerprint = hashlib.sha256(raw_fingerprint.encode('utf-8')).hexdigest()

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT store_id FROM stores WHERE slug = %s", (slug,))
        store = cur.fetchone()
        if not store:
            abort(404)

        store_id = store['store_id']
        cur.execute("""
            SELECT report_id
            FROM store_reports
            WHERE store_id = %s
              AND (
                    (reporter_user_id IS NOT NULL AND reporter_user_id = %s)
                    OR (reporter_user_id IS NULL AND reporter_fingerprint = %s)
                  )
            LIMIT 1
        """, (store_id, reporter_user_id, reporter_fingerprint))
        if cur.fetchone():
            flash('You have already reported this store. Thank you for helping us review it.', 'info')
            return redirect(url_for('store_detail', slug=slug))

        cur.execute("""
            INSERT INTO store_reports
                (store_id, reporter_user_id, reporter_fingerprint, reason, ip_address, user_agent)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            store_id,
            reporter_user_id,
            reporter_fingerprint,
            reason,
            request.remote_addr,
            request.headers.get('User-Agent', '')[:500]
        ))

        cur.execute("SELECT COUNT(*) AS report_count FROM store_reports WHERE store_id = %s", (store_id,))
        report_count = cur.fetchone()['report_count']
        if report_count > 5:
            cur.execute("UPDATE stores SET is_flagged = 1 WHERE store_id = %s", (store_id,))

        conn.commit()
        flash('Thank you. This store report has been submitted for review.', 'success')
    except mysql.connector.Error as e:
        conn.rollback()
        app.logger.error("Store report error: %s", e)
        flash('Could not submit the report right now. Please try again.', 'danger')
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('store_detail', slug=slug))






@app.route('/listing/<int:listing_id>', methods=['GET', 'POST'])
def listing_details(listing_id):
    conn = None
    cursor = None
    listing = None

    app.logger.debug(f"Accessing listing_details for listing_id: {listing_id}, method: {request.method}")

    # Handle unexpected POST requests gracefully
    if request.method == 'POST':
        app.logger.debug(f"Unexpected POST to listing_details: {request.form.to_dict()}")
        flash('Error: Invalid form submission. Please use the proposal form.', 'danger')
        return redirect(url_for('listing_details', listing_id=listing_id))

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Step 1: Fetch main listing + owner + avg rating & count in one query
        app.logger.debug("Fetching main listing + avg rating + owner info")
        cursor.execute("""
            SELECT l.*, u.username, u.email,
                   COALESCE(avg_r.avg_rating, 0) AS avg_rating,
                   COALESCE(avg_r.rating_count, 0) AS rating_count
            FROM listings AS l
            JOIN users AS u ON l.user_id = u.id
            LEFT JOIN (
                SELECT listing_id, AVG(rating_value) AS avg_rating, COUNT(*) AS rating_count
                FROM ratings
                WHERE listing_id = %s
                GROUP BY listing_id
            ) AS avg_r ON l.listing_id = avg_r.listing_id
            WHERE l.listing_id = %s
        """, (listing_id, listing_id))
        listing = cursor.fetchone()
        if not listing:
            app.logger.warning(f"No listing found for ID: {listing_id}")
            abort(404)

        # Step 2: Fetch reviews (limit to latest 20)
        app.logger.debug("Fetching reviews (limit 20)")
        cursor.execute("""
            SELECT r.review_id, r.review_text, r.created_at, u.username AS reviewer
            FROM reviews AS r
            JOIN users AS u ON r.user_id = u.id
            WHERE r.listing_id = %s
            ORDER BY r.created_at DESC
            LIMIT 20
        """, (listing_id,))
        listing['reviews'] = cursor.fetchall() or []

        # Step 3: Fetch offered items
        app.logger.debug("Fetching offered items")
        cursor.execute("""
            SELECT title, description, `condition`, image1, image2, image3, image4
            FROM offered_items
            WHERE listing_id = %s
        """, (listing_id,))
        listing['offered_items'] = cursor.fetchall() or []

        # Step 4: Fetch similar listings by category (exclude current listing)
        app.logger.debug("Fetching similar listings by category")
        cursor.execute("""
            SELECT l.listing_id, l.title, l.description, l.category,
                   l.image_url, l.image1, l.image2, l.image3, l.image4,
                   l.price, l.contact_for_price, l.deal_type, l.condition, l.location,
                   COALESCE(avg_r.avg_rating, 0) AS avg_rating,
                   COALESCE(avg_r.rating_count, 0) AS rating_count
            FROM listings AS l
            LEFT JOIN (
                SELECT listing_id, AVG(rating_value) AS avg_rating, COUNT(*) AS rating_count
                FROM ratings
                GROUP BY listing_id
            ) AS avg_r ON l.listing_id = avg_r.listing_id
            WHERE l.category = %s
              AND l.listing_id != %s
              AND (l.status IS NULL OR l.status != 'sold')
            ORDER BY l.created_at DESC
            LIMIT 8
        """, (listing['category'], listing_id))
        similar_listings = cursor.fetchall() or []

        # Process similar listings to set main_image and price_float
        for item in similar_listings:
            # Collect all possible image fields
            images = [img for img in [
                item.get('image_url'),
                item.get('image1'),
                item.get('image2'),
                item.get('image3'),
                item.get('image4')
            ] if img]
            item['main_image'] = images[0] if images else '/static/images/placeholder.jpg'
            # Convert price to float for display
            try:
                item['price_float'] = float(item['price']) if item['price'] else 0.0
            except:
                item['price_float'] = 0.0

        app.logger.debug(f"Listing data loaded successfully: {listing}")

    except mysql.connector.Error as e:
        app.logger.error(f"Database error in listing_details: {str(e)}", exc_info=True)
        if conn:
            conn.rollback()
        abort(500, description=f"Database error: {str(e)}")
    except Exception as e:
        app.logger.error(f"Unexpected error in listing_details: {str(e)}", exc_info=True)
        if conn:
            conn.rollback()
        abort(500, description=f"Unexpected error: {str(e)}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

    return render_template(
        'listing_details.html',
        listing=listing,
        listing_id=listing_id,
        similar_listings=similar_listings
    )





@app.route('/metrics/listing/impression', methods=['POST'])
def track_listing_impression():
    """
    Increment impressions count for a listing.
    Expects JSON: { "listing_id": 123 }
    """
    data = request.get_json()
    if not data or 'listing_id' not in data:
        return jsonify({'error': 'Missing listing_id'}), 400

    listing_id = data['listing_id']
    user_id = session.get('user_id')  # optional: track per user, but we'll just increment total

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Use INSERT ... ON DUPLICATE KEY UPDATE to handle first view
        cur.execute("""
            INSERT INTO listing_metrics (listing_id, impressions, clicks, updated_at)
            VALUES (%s, 1, 0, NOW())
            ON DUPLICATE KEY UPDATE
                impressions = impressions + 1,
                updated_at = NOW()
        """, (listing_id,))
        conn.commit()
        return jsonify({'success': True}), 200
    except mysql.connector.Error as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route('/metrics/listing/click', methods=['POST'])
def track_listing_click():
    """
    Increment clicks count for a listing.
    Expects JSON: { "listing_id": 123 }
    """
    data = request.get_json()
    if not data or 'listing_id' not in data:
        return jsonify({'error': 'Missing listing_id'}), 400

    listing_id = data['listing_id']

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO listing_metrics (listing_id, impressions, clicks, updated_at)
            VALUES (%s, 0, 1, NOW())
            ON DUPLICATE KEY UPDATE
                clicks = clicks + 1,
                updated_at = NOW()
        """, (listing_id,))
        conn.commit()
        return jsonify({'success': True}), 200
    except mysql.connector.Error as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()







@app.route('/metrics/listing/whatsapp-click', methods=['POST'])
def track_listing_whatsapp_click():
    """
    Increment WhatsApp button clicks for a listing.
    Expects JSON: { "listing_id": 123 }
    """
    data = request.get_json()
    if not data or 'listing_id' not in data:
        return jsonify({'error': 'Missing listing_id'}), 400

    listing_id = data['listing_id']

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT store_id FROM listings WHERE listing_id = %s", (listing_id,))
        listing_row = cur.fetchone()
        store_id = listing_row[0] if listing_row else None

        cur.execute("""
            INSERT INTO listing_metrics (listing_id, impressions, clicks, whatsapp_clicks, updated_at)
            VALUES (%s, 0, 0, 1, NOW())
            ON DUPLICATE KEY UPDATE
                whatsapp_clicks = COALESCE(whatsapp_clicks, 0) + 1,
                updated_at = NOW()
        """, (listing_id,))

        if store_id:
            cur.execute("""
                INSERT INTO store_metrics (store_id, dt, chats)
                VALUES (%s, CURDATE(), 1)
                ON DUPLICATE KEY UPDATE chats = COALESCE(chats, 0) + 1
            """, (store_id,))

        conn.commit()
        return jsonify({'success': True}), 200
    except mysql.connector.Error as e:
        return jsonify({'error': str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route('/create_proposal/<int:listing_id>', methods=['GET', 'POST'])
@login_required
def create_proposal(listing_id):
    if request.method != 'POST':
        return redirect(url_for('listing_details', listing_id=listing_id))

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Ensure proposals table exists
        cursor.execute("SHOW TABLES LIKE 'proposals'")
        if not cursor.fetchone():
            app.logger.error("Proposals table does not exist")
            flash('Server error: Proposals table missing.', 'danger')
            return redirect(url_for('listing_details', listing_id=listing_id))

        # Validate listing exists and fetch its contact field
        cursor.execute("SELECT user_id, title, contact FROM listings WHERE listing_id = %s", (listing_id,))
        listing = cursor.fetchone()
        if not listing:
            app.logger.error("Listing ID %s does not exist", listing_id)
            flash('Invalid listing ID.', 'danger')
            return redirect(url_for('listing_details', listing_id=listing_id))

        owner_id = listing['user_id']
        listing_title = listing['title']
        listing_contact = listing.get('contact')  # ← store listing's own contact

        # Gather form data
        proposer_id = session.get('user_id')
        proposed_item = request.form.get('proposed_item', '').strip()
        additional_cash_raw = request.form.get('additional_cash', '').strip()
        message = request.form.get('message', '').strip()
        detailed_description = request.form.get('detailed_description', '').strip()
        condition = request.form.get('condition', '').strip()
        phone_number = request.form.get('phone_number', '').strip()
        email_address = request.form.get('email_address', '').strip()

        # NEW: optional vendor WhatsApp + listing title from hidden fields
        vendor_whatsapp_form = request.form.get('vendor_whatsapp', '').strip()
        listing_title_from_form = request.form.get('listing_title', '').strip()
        if listing_title_from_form:
            listing_title = listing_title_from_form  # prefer explicit form title if present

        # Validate required fields
        if not all([proposed_item, detailed_description, condition, phone_number, email_address]):
            flash('All required fields must be filled.', 'danger')
            return redirect(url_for('listing_details', listing_id=listing_id))

        # Safe parse for additional_cash
        additional_cash = None
        if additional_cash_raw:
            try:
                additional_cash = float(additional_cash_raw.replace(',', '').strip())
            except ValueError:
                flash('Invalid value for additional cash.', 'danger')
                return redirect(url_for('listing_details', listing_id=listing_id))

        # Handle image uploads (Cloudinary)
        image_urls = []
        for i in range(1, 5):
            file = request.files.get(f'image{i}')
            if file and file.filename and allowed_file(file.filename):
                try:
                    upload_result = cloudinary.uploader.upload(
                        file,
                        folder="swaphub/proposals",
                        resource_type="image"
                    )
                    image_url = upload_result.get("secure_url")
                    image_urls.append(image_url)
                except Exception as e:
                    app.logger.exception(f"Cloudinary upload failed for proposal image {i}: {e}")
                    flash('Error uploading images. Please try again.', 'danger')
                    return redirect(url_for('listing_details', listing_id=listing_id))
            else:
                image_urls.append(None)

        # Insert into proposals
        insert_query = '''
            INSERT INTO proposals (
                listing_id, user_id, proposed_item,
                additional_cash, message, status,
                detailed_description, `condition`,
                `Phone_number`, `Email_address`,
                image1, image2, image3, image4
            ) VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s)
        '''
        params = (
            listing_id, proposer_id, proposed_item,
            additional_cash, message,
            detailed_description, condition,
            phone_number, email_address,
            *image_urls
        )
        cursor.execute(insert_query, params)
        conn.commit()

        # Lookup owner using users.id
        cursor.execute("SELECT email, contact, name, username FROM users WHERE id = %s", (owner_id,))
        owner = cursor.fetchone()

        # Lookup proposer name for friendly text (best effort)
        proposer_name = "A user"
        proposer_email = None
        if proposer_id:
            try:
                cursor.execute("SELECT name, email FROM users WHERE id = %s", (proposer_id,))
                proposer = cursor.fetchone()
                if proposer:
                    proposer_name = proposer.get('name') or proposer.get('email') or proposer_name
                    proposer_email = proposer.get('email')
            except Exception:
                app.logger.debug("Could not fetch proposer name for user id=%s", proposer_id, exc_info=True)

        # === WhatsApp deep-link construction ===
        # Determine vendor WhatsApp number:
        #  1) from hidden form field (preferred),
        #  2) fallback to listing.contact,
        #  3) fallback to owner.contact from DB
        vendor_number_raw = vendor_whatsapp_form or listing_contact or (owner.get('contact') if owner else '')

        def normalize_msisdn(raw: str) -> str:
            """Keep only digits for wa.me link (e.g. '+233544...' -> '233544...')."""
            if not raw:
                return ''
            digits = ''.join(ch for ch in raw if ch.isdigit())
            return digits

        wa_phone = normalize_msisdn(vendor_number_raw)

        if not wa_phone:
            app.logger.warning("No valid WhatsApp number for owner_id=%s, listing_id=%s", owner_id, listing_id)
            flash('Your proposal was saved, but the owner has no valid WhatsApp number configured.', 'warning')
            return redirect(url_for('listing_details', listing_id=listing_id))

        # Build WhatsApp message text (from proposer to owner)
        owner_display_name = None
        if owner:
            owner_display_name = owner.get('name') or owner.get('username') or None
        if not owner_display_name:
            owner_display_name = "there"

        # Format additional cash nicely
        if additional_cash is not None:
            additional_cash_str = f"{additional_cash:,.2f}"
        else:
            additional_cash_str = "0.00"

        wa_message = (
            f"Hi {owner_display_name}, I'm interested in your listing \"{listing_title}\" on SwapHub.\n\n"
            f"Here is my swap proposal:\n"
            f"- Item I'm offering: {proposed_item}\n"
            f"- Condition: {condition}\n"
            f"- Additional cash (GHS): {additional_cash_str}\n\n"
            f"My contact details:\n"
            f"- Phone: {phone_number}\n"
            f"- Email: {email_address}\n\n"
        )

        if detailed_description:
            wa_message += f"Details about my item:\n{detailed_description}\n\n"

        if message:
            wa_message += f"Additional message:\n{message}\n\n"

        wa_message += "This proposal has also been saved on SwapHub."

        # URL-encode message
        from urllib.parse import quote_plus
        wa_text = quote_plus(wa_message)
        wa_url = f"https://wa.me/{wa_phone}?text={wa_text}"

        # Optional: Web-push notification (you can keep this or remove)
        try:
            push_title = "New proposal received"
            push_body = f"{proposer_name} sent a proposal for \"{listing_title}\"."
            send_push(
                owner_id,
                push_title,
                push_body,
                url_for('dashboard')
            )
            app.logger.info("Push notification sent to user %s for listing %s", owner_id, listing_id)
        except Exception as push_err:
            app.logger.error("Push notification error: %s", push_err, exc_info=True)

        # IMPORTANT: we NO LONGER send email here; instead we redirect user to WhatsApp
        app.logger.info(
            "Proposal created successfully for listing_id=%s; redirecting to WhatsApp %s",
            listing_id, wa_url
        )

        # No flash here because we are leaving the site to WhatsApp;
        # if you want, you can store something in session and show it when they come back.
        return redirect(wa_url)

    except Exception as e:
        if conn:
            conn.rollback()
        app.logger.exception("Error creating proposal: %s", e)
        flash('Error submitting proposal. Please try again.', 'danger')
        return redirect(url_for('listing_details', listing_id=listing_id))

    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

            



# ------------------------------
# Metrics Endpoints
# ------------------------------
@app.route("/metrics/store/view", methods=["POST"])
def metric_store_view():
    data = request.get_json() or {}
    store_id = data.get("store_id")
    if store_id:
        _inc_store_metric(int(store_id), "views", 1)
    return jsonify({"success": True})

@app.route("/metrics/store/click", methods=["POST"])
def metric_store_click():
    data = request.get_json() or {}
    store_id = data.get("store_id")
    if store_id:
        _inc_store_metric(int(store_id), "clicks", 1)
    return jsonify({"success": True})

# ------------------------------
# Redirect /my-store
# ------------------------------
@app.route('/my-store')
@login_required
def my_store_redirect():
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    # Fetch any store (active or inactive) for this user
    cur.execute("SELECT store_id FROM stores WHERE user_id = %s LIMIT 1", (session['user_id'],))
    store = cur.fetchone()
    cur.close()
    conn.close()
    if store:
        return redirect(url_for('store_home', store_id=store['store_id']))
    else:
        flash("You don't have a store yet. Create one now!", "info")
        return redirect(url_for('create_store'))




@app.route('/store/subscribe', methods=['POST'])
@login_required
def subscribe_to_store():
    data = request.json
    store_id = data.get('store_id')
    subscription = data.get('subscription')
    user_id = session['user_id']
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO push_subscriptions (user_id, store_id, endpoint, p256dh, auth)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            endpoint = VALUES(endpoint),
            p256dh = VALUES(p256dh),
            auth = VALUES(auth)
    """, (user_id, store_id, subscription['endpoint'], 
          subscription['keys']['p256dh'], subscription['keys']['auth']))
    conn.commit()
    cur.close()
    conn.close()
    
    return jsonify({"success": True})




@app.route('/marketplace')
def marketplace():
    return render_template('marketplace.html')    






@app.route('/api/marketplace/products')
def api_marketplace_products():
    page = request.args.get('page', 1, type=int)
    per_page = 20
    search = request.args.get('search', '')
    category = request.args.get('category', '')
    condition = request.args.get('condition', '')
    deal_type = request.args.get('deal_type', '')
    sort = request.args.get('sort', 'newest')
    
    offset = (page - 1) * per_page
    
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    
    # Build WHERE clause dynamically
    where_parts = ["l.status = 'Active'"]
    params = []
    
    if search:
        where_parts.append("l.title LIKE %s")
        params.append(f"%{search}%")
    if category:
        where_parts.append("l.category = %s")
        params.append(category)
    if condition:
        where_parts.append("l.condition = %s")
        params.append(condition)
    if deal_type:
        where_parts.append("l.deal_type = %s")
        params.append(deal_type)
    
    where_clause = "WHERE " + " AND ".join(where_parts)
    
    # COUNT query (to get total number of products)
    count_query = f"""
        SELECT COUNT(*) as total
        FROM listings l
        {where_clause}
    """
    cur.execute(count_query, params)
    total = cur.fetchone()['total']   # This will now work
    
    # Main data query
    query = f"""
        SELECT l.listing_id, l.title, l.image_url, l.image1, l.category, 
               l.price, l.contact_for_price, l.deal_type, l.condition, l.is_featured, l.store_id,
               s.name as store_name, s.trust_score,
               COALESCE(lm.impressions, 0) as impressions,
               COALESCE(lm.clicks, 0) as clicks
        FROM listings l
        LEFT JOIN stores s ON l.store_id = s.store_id
        LEFT JOIN listing_metrics lm ON l.listing_id = lm.listing_id
        {where_clause}
    """
    
    # Sorting
    if sort == 'price_asc':
        query += " ORDER BY l.price ASC"
    elif sort == 'price_desc':
        query += " ORDER BY l.price DESC"
    elif sort == 'popular':
        query += " ORDER BY COALESCE(lm.impressions, 0) DESC"
    else:  # newest
        query += " ORDER BY l.created_at DESC"
    
    # Pagination
    query += " LIMIT %s OFFSET %s"
    params.extend([per_page, offset])
    
    cur.execute(query, params)
    products = cur.fetchall()
    
    cur.close()
    conn.close()
    
    return jsonify({
        'success': True,
        'products': products,
        'pagination': {
            'current_page': page,
            'per_page': per_page,
            'total': total,
            'total_pages': (total + per_page - 1) // per_page
        }
    })





# ------------------------------
# Admin: Toggle Store Status (Enable/Disable with reason)
# ------------------------------
@app.route('/admin/store/<int:store_id>/toggle-status', methods=['POST'])
@admin_required
def admin_toggle_store_status(store_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Check if store exists
    cursor.execute("SELECT is_active FROM stores WHERE store_id = %s", (store_id,))
    store = cursor.fetchone()
    if not store:
        cursor.close()
        conn.close()
        flash('Store not found', 'danger')
        return redirect(url_for('admin_stores'))
    
    new_status = not store['is_active']
    reason = request.form.get('reason', '').strip()
    
    if new_status == 0 and not reason:
        flash('Please provide a reason for disabling the store', 'warning')
        cursor.close()
        conn.close()
        return redirect(url_for('admin_store_detail', store_id=store_id))
    
    # Update store active status
    cursor.execute("UPDATE stores SET is_active = %s WHERE store_id = %s", (new_status, store_id))
    
    # Log the sanction
    action = 'disable' if new_status == 0 else 'enable'
    cursor.execute("""
        INSERT INTO store_sanctions (store_id, admin_id, action, reason)
        VALUES (%s, %s, %s, %s)
    """, (store_id, session['user_id'], action, reason if action == 'disable' else None))
    
    conn.commit()
    cursor.close()
    conn.close()
    
    flash(f'Store has been {"disabled" if new_status == 0 else "enabled"} successfully.', 'success')
    return redirect(url_for('admin_store_detail', store_id=store_id))


# ------------------------------
# Admin: Get Sanction History (for AJAX or direct)
# ------------------------------
@app.route('/admin/store/<int:store_id>/sanctions')
@admin_required
def admin_store_sanctions(store_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT s.*, u.username as admin_name
        FROM store_sanctions s
        LEFT JOIN users u ON s.admin_id = u.id
        WHERE s.store_id = %s
        ORDER BY s.created_at DESC
    """, (store_id,))
    sanctions = cursor.fetchall()
    cursor.close()
    conn.close()
    return {'sanctions': sanctions}









from flask import send_from_directory

@app.route('/googlefbaf22f94e24fef4.html')
def google_verification():
    return render_template('googlefbaf22f94e24fef4.html')    


@app.route('/robots.txt')
def robots_txt():
    robots = """User-agent: *
Allow: /

Sitemap: https://sellkwic.com/sitemap.xml
"""
    return Response(robots, mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap():
    today = date.today().isoformat()
    urls = [
        ("https://sellkwic.com/", today, "1.00"),
        ("https://sellkwic.com/marketplace", today, "0.85"),
        ("https://sellkwic.com/create-store", today, "0.80"),
        ("https://sellkwic.com/info", today, "0.70"),
    ]

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)

        cur.execute("""
            SELECT slug, COALESCE(DATE(created_at), CURDATE()) AS lastmod
            FROM stores
            WHERE is_active = 1 AND slug IS NOT NULL AND slug != ''
            ORDER BY Plan_priority DESC, trust_score DESC, created_at DESC
        """)
        for store in cur.fetchall():
            urls.append((f"https://sellkwic.com/store/{store['slug']}", str(store['lastmod']), "0.80"))

        cur.execute("""
            SELECT listing_id, COALESCE(DATE(created_at), CURDATE()) AS lastmod
            FROM listings
            WHERE status IS NULL OR status NOT IN ('deleted', 'sold')
            ORDER BY created_at DESC
        """)
        for listing in cur.fetchall():
            urls.append((f"https://sellkwic.com/listing/{listing['listing_id']}", str(listing['lastmod']), "0.64"))
    except Exception as e:
        app.logger.error("Sitemap generation error: %s", e, exc_info=True)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    ]
    for loc, lastmod, priority in urls:
        xml_parts.append(
            f"<url><loc>{xml_escape(loc)}</loc><lastmod>{lastmod}</lastmod><priority>{priority}</priority></url>"
        )
    xml_parts.append('</urlset>')
    return Response("\n".join(xml_parts), mimetype='application/xml')



@app.route('/info')
def info_page():
    return render_template('info.html')





# ------------------------------
# Run
# ------------------------------
if __name__ == '__main__':
    app.run(debug=True, port=5002)