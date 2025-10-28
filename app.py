import markupsafe
import flask
flask.Markup = markupsafe.Markup

# Now import the rest of your modules
import os
import threading
import logging
from mailersend import Email  # Correct import for v2.0.0 SDK
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv
from flask import Flask, render_template, url_for, abort, request, session, redirect, flash, jsonify, current_app
import mysql.connector
from functools import wraps
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField
from werkzeug.utils import secure_filename
from flask_bcrypt import Bcrypt
import uuid
import traceback
import requests
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer
import secrets
from datetime import datetime, timedelta, UTC
from werkzeug.security import generate_password_hash, check_password_hash
import json
from notifications import send_push
from config import VAPID_PUBLIC_KEY
from authlib.integrations.flask_client import OAuth
from apscheduler.schedulers.background import BackgroundScheduler
import random
import itertools
from mysql.connector import pooling
from flask_caching import Cache
import multiprocessing
import threading
from datetime import datetime, date
from mysql.connector import connect, Error
import string
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import socket
from email.message import EmailMessage
import re
import redis







import logging

# Set up logging
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# Load environment variables from .env file
load_dotenv()

PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY')
if not PAYSTACK_SECRET_KEY:
    raise ValueError("PAYSTACK_SECRET_KEY is not set in the environment")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'))

app.config['SECRET_KEY'] = 'fa470fe714e44404511cbad16224f52777068d05bb5c29ed'
app.config['SMTP_SERVER'] = os.getenv('SMTP_SERVER', 'in-v3.mailjet.com')
app.config['SMTP_PORT'] = int(os.getenv('SMTP_PORT', 587))
app.config['SMTP_USERNAME'] = os.getenv('SMTP_USERNAME', '')
app.config['SMTP_PASSWORD'] = os.getenv('SMTP_PASSWORD', '')
app.config['FROM_EMAIL'] = os.getenv('FROM_EMAIL', '')


serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])


app.config.from_pyfile('config.py')

impressions_cache = {}
cache_lock = threading.Lock()  # to avoid race conditions

clicks_cache = {}
clicks_cache_lock = threading.Lock()


# Initialize the scheduler
scheduler = BackgroundScheduler()
scheduler.start()


from logging.handlers import RotatingFileHandler
if not app.debug:
    handler = RotatingFileHandler('app.log', maxBytes=10240, backupCount=10)
    handler.setLevel(logging.ERROR)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    app.logger.addHandler(handler)



# Set up upload folder and allowed extensions
UPLOAD_FOLDER = os.path.join(os.getcwd(), 'static', 'images')
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'avif'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS




cache = Cache(config={
    'CACHE_TYPE': 'SimpleCache',
    'CACHE_DEFAULT_TIMEOUT': 300
})
cache.init_app(app)



# Database connection function
dbconfig = {
    "host":       os.getenv('DB_HOST', 'localhost'),
    "user":       os.getenv('DB_USER', 'root'),
    "password":   os.getenv('DB_PASSWORD', ''),
    "database":   os.getenv('DB_DATABASE', ''),
    "port":       int(os.getenv('DB_PORT', 3306)),
    "charset":    'utf8mb4',
    "collation":  'utf8mb4_unicode_ci',
    "use_unicode": True
}

# ─── 2) Determine how many app‑server processes you’re running ──────────────
#    (e.g. Gunicorn --workers or similar). Default to 1 in dev.
try:
    WEB_CONCURRENCY = int(os.getenv('WEB_CONCURRENCY', '1'))
    if WEB_CONCURRENCY < 1:
        raise ValueError
except ValueError:
    WEB_CONCURRENCY = 1

# ─── 3) Define your total‑app ceiling and per‑pool cap ───────────────────────
TOTAL_APP_CONN = 200   # across all workers, aim to use no more than this
MAX_PER_POOL   = 15    # mysql.connector.pooling hard upper bound
MIN_PER_POOL   = 5     # always at least this many connections

# ─── 4) Compute per‑process pool size ────────────────────────────────────────
raw_size = TOTAL_APP_CONN // WEB_CONCURRENCY
pool_size = max(MIN_PER_POOL, min(raw_size, MAX_PER_POOL))

logger.info(
    f"DB Pool Configuration → WEB_CONCURRENCY={WEB_CONCURRENCY}, "
    f"TOTAL_APP_CONN={TOTAL_APP_CONN}, raw_per_pool={raw_size}, "
    f"using pool_size={pool_size}"
)

# ─── 5) Instantiate the MySQLConnectionPool ────────────────────────────────
cnxpool = pooling.MySQLConnectionPool(
    pool_name="mypool",
    pool_size=pool_size,
    pool_reset_session=True,
    **dbconfig
)

# ─── 6) Helper to get a connection from the pool ────────────────────────────
def get_db_connection():
    """
    Returns a mysql.connector connection from the configured pool.
    """
    return cnxpool.get_connection()



oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.environ['GOOGLE_CLIENT_ID'],
    client_secret=os.environ['GOOGLE_CLIENT_SECRET'],
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={
        # Remove 'openid' so no id_token is returned
        'scope': 'email profile'
    }
)



OFFSET_PATH = os.path.join(os.path.dirname(__file__), "carousel_offset.txt")

def read_offset():
    """Read the integer offset from OFFSET_PATH, or return 0 if not present / invalid."""
    try:
        with open(OFFSET_PATH, "r") as f:
            val = int(f.read().strip())
            return val
    except Exception:
        return 0

def write_offset(val):
    """Write the integer val into OFFSET_PATH (overwriting)."""
    try:
        with open(OFFSET_PATH, "w") as f:
            f.write(str(val))
    except Exception as e:
        # If writing fails (permissions, etc.), just log and skip.
        logging.error(f"Failed to write carousel_offset.txt: {e}")





def check_and_update_expired_plans():
    try:
        # Connect to the database using your existing function
        conn = get_db_connection()
        cursor = conn.cursor()

        # Define plan durations in days
        PLAN_DURATIONS = {
            'Diamond': 30,   # 3 months
            'Gold': 21,      # 2 months
            'Silver': 14,    # 1 month
            
        }

        # Fetch listings with non-Free plans
        cursor.execute("SELECT user_id, Plan, created_at FROM listings WHERE Plan != 'Free'")
        listings = cursor.fetchall()

        # Use current time (assuming database created_at is in UTC)
        now = datetime.now()
        expired_ids = []

        # Check each listing for expiration
        for listing_id, plan, created_at in listings:
            duration_days = PLAN_DURATIONS.get(plan, 0)
            if duration_days == 0:  # Skip invalid plans
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Warning: Invalid plan '{plan}' for listing ID {listing_id}")
                continue
            if now > created_at + timedelta(days=duration_days):
                expired_ids.append(listing_id)

        # Update expired listings to 'Free'
        if expired_ids:
            format_strings = ','.join(['%s'] * len(expired_ids))
            cursor.execute(f"UPDATE listings SET Plan = 'Free' WHERE user_id IN ({format_strings})", tuple(expired_ids))
            conn.commit()
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Updated {cursor.rowcount} expired listings to 'Free'.")
        else:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] No expired listings to update.")

        cursor.close()
        conn.close()
    except mysql.connector.Error as db_err:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Database error: {db_err}")
    except Exception as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error running expiration job: {e}")

# APScheduler setup for testing
scheduler = BackgroundScheduler()
scheduler.add_job(func=check_and_update_expired_plans, trigger="interval", hours=12)  # 10 seconds for testing
scheduler.start()









# 3) The home route
@app.route('/')
def home():
    search            = request.args.get('search', '').strip()
    selected_category = request.args.get('category', 'All')
    deal_type_filter  = request.args.get('deal_type', 'All')
    location_q        = request.args.get('location', '').strip()
    page              = max(1, request.args.get('page', 1, type=int))

    DEFAULT_PER_PAGE = 40
    MAX_PER_PAGE     = 200
    per_page = min(MAX_PER_PAGE, request.args.get('per_page', DEFAULT_PER_PAGE, type=int))
    offset   = (page - 1) * per_page

    user_logged_in = 'user_id' in session
    user_subscribed = False
    carousel_listings = []
    listings = []
    suggestion_listings = []
    show_suggestions = False
    total_pages = 0

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 1. CAROUSEL
        carousel_listings = _get_carousel(cursor)

        # 2. MAIN LISTINGS
        listings, total_pages = _get_main_listings(
            cursor,
            search=search,
            category=selected_category,
            deal_type=deal_type_filter,
            location=location_q,
            per_page=per_page,
            offset=offset,
        )

        # 3. SUGGESTIONS (only if no exact matches)
        if search and not listings:
            show_suggestions = True
            suggestion_listings = _get_suggestions(cursor, search)

        # 4. ATTACH OFFERS + IMAGES
        all_items = listings + suggestion_listings
        offers = _attach_offered_items(cursor, all_items)

        base_img = url_for('static', filename='images/', _external=True)
        for item in all_items:
            item['image_url'] = base_img + (item.get('image1') or 'placeholder.jpg')
            item['offers'] = offers.get(item['listing_id'], [])
            item.setdefault('required_cash', 0)
            item.setdefault('additional_cash', 0)
            item.setdefault('desired_swap', '')
            item.setdefault('price', item.get('price', 0))
            item.setdefault('location', item.get('location', ''))
            item.setdefault('contact', item.get('contact', ''))

        # 5. WISHLIST
        if user_logged_in and all_items:
            uid = session['user_id']
            cursor.execute("SELECT 1 FROM push_subscriptions WHERE user_id=%s LIMIT 1", (uid,))
            user_subscribed = cursor.fetchone() is not None

            visible_ids = [x['listing_id'] for x in all_items]
            if visible_ids:
                ph = ','.join(['%s'] * len(visible_ids))
                cursor.execute(f"SELECT listing_id FROM wishlists WHERE user_id=%s AND listing_id IN ({ph})", (uid, *visible_ids))
                wish_ids = {r['listing_id'] for r in cursor.fetchall()}
            else:
                wish_ids = set()

            for item in all_items:
                item['is_wishlisted'] = item['listing_id'] in wish_ids
        else:
            for item in all_items:
                item['is_wishlisted'] = False

        # 6. FEATURED
        featured = listings[0] if listings else (suggestion_listings[0] if suggestion_listings else None)

        return render_template(
            'home.html',
            carousel_listings=carousel_listings,
            listings=listings,
            suggestion_listings=suggestion_listings,
            show_suggestions=show_suggestions,
            featured_listing=featured,
            search=search,
            selected_category=selected_category,
            deal_type_filter=deal_type_filter,
            location=location_q,
            user_logged_in=user_logged_in,
            user_subscribed=user_subscribed,
            vapid_public_key=app.config.get('VAPID_PUBLIC_KEY', ''),
            page=page,
            total_pages=total_pages
        )

    except Exception as e:
        logging.error("Error in home(): %s", e, exc_info=True)
        if conn: conn.rollback()
        raise
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


# ———————————————————————— HELPERS ————————————————————————

def _get_carousel(cursor):
    cursor.execute("SELECT listing_id, image1, title, `Plan` FROM listings ORDER BY created_at DESC LIMIT 20")
    raw = cursor.fetchall()
    PLAN_WEIGHTS = {'Diamond':5,'Gold':4,'Silver':3,'Bronze':2,'Free':1}
    weighted = [i for i, r in enumerate(raw) for _ in range(PLAN_WEIGHTS.get(r['Plan'],1))]
    if not weighted: return []
    jitter = random.randrange(len(weighted))
    offset = (read_offset() + jitter) % len(weighted)
    write_offset((offset + 1) % len(weighted))
    seen, ordered = set(), []
    for idx in weighted[offset:] + weighted[:offset]:
        if idx not in seen:
            seen.add(idx)
            ordered.append(raw[idx])
        if len(ordered) == 5: break
    base_img = url_for('static', filename='images/', _external=True)
    for c in ordered:
        c['banner_image'] = base_img + (c.get('image1') or 'placeholder.jpg')
    return ordered


def _get_main_listings(cursor, *, search, category, deal_type, location, per_page, offset):
    base_q = """
        SELECT l.listing_id, l.title, l.description, l.category, l.deal_type, l.`Plan`,
               l.image1, l.price, l.required_cash, l.additional_cash, l.desired_swap,
               l.location, l.contact, u.username, IFNULL(m.impressions,0) AS impressions
        FROM listings l
        JOIN users u ON l.user_id = u.id
        LEFT JOIN listing_metrics m ON l.listing_id = m.listing_id
        WHERE 1=1
    """
    params = []
    if search:
        like = f"%{search}%"
        base_q += " AND (l.title LIKE %s OR l.description LIKE %s OR l.category LIKE %s)"
        params += [like, like, like]
    if category != 'All':
        base_q += " AND l.category=%s"
        params.append(category)
    if deal_type != 'All':
        base_q += " AND l.deal_type=%s"
        params.append(deal_type)
    if location:
        base_q += " AND l.location IS NOT NULL"
        base_q += " AND (l.location=%s OR SOUNDEX(l.location)=SOUNDEX(%s) OR l.location LIKE %s)"
        params += [location, location, f"%{location}%"]

    plan_case = "CASE l.`Plan` WHEN 'Diamond' THEN 5 WHEN 'Gold' THEN 4 WHEN 'Silver' THEN 3 ELSE 1 END"
    order = f"ORDER BY {plan_case} DESC, l.created_at DESC"

    cache_key = f"grid:{search}:{category}:{deal_type}:{location}:{per_page}:{offset}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    # Count
    count_q = "SELECT COUNT(*) AS total FROM listings l WHERE 1=1"
    count_p = []
    if search: count_q += " AND (l.title LIKE %s OR l.description LIKE %s OR l.category LIKE %s)"; count_p += [like, like, like]
    if category != 'All': count_q += " AND l.category=%s"; count_p.append(category)
    if deal_type != 'All': count_q += " AND l.deal_type=%s"; count_p.append(deal_type)
    if location:
        count_q += " AND l.location IS NOT NULL"
        count_q += " AND (l.location=%s OR SOUNDEX(l.location)=SOUNDEX(%s) OR l.location LIKE %s)"
        count_p += [location, location, f"%{location}%"]
    cursor.execute(count_q, tuple(count_p))
    total = cursor.fetchone()['total']

    # Listings
    cursor.execute(base_q + " " + order + " LIMIT %s OFFSET %s", tuple(params + [per_page, offset]))
    listings = cursor.fetchall()
    listings = _rotate_weighted(listings)
    total_pages = (total + per_page - 1) // per_page
    cache.set(cache_key, (listings, total_pages), timeout=60)
    return listings, total_pages


# --------------------------------------------------------------
# 2. SUGGESTIONS – FULLY WRITTEN SQL (no "..." placeholders)
# --------------------------------------------------------------
def _get_suggestions(cursor, search):
    import re
    listings = []

    # ---------- 1. Token-based partial match ----------
    tokens = [t for t in re.split(r'\s+', search) if len(t) > 2]
    if tokens:
        conds = []
        params = []
        for t in tokens:
            conds.append("(l.title LIKE %s OR l.description LIKE %s OR l.category LIKE %s)")
            like = f"%{t}%"
            params += [like, like, like]

        q = f"""
            SELECT l.listing_id, l.title, l.description, l.category, l.deal_type, l.`Plan`,
                   l.image1, l.price, l.required_cash, l.additional_cash, l.desired_swap,
                   l.location, l.contact, u.username, IFNULL(m.impressions,0) AS impressions
            FROM listings l
            JOIN users u ON l.user_id = u.id
            LEFT JOIN listing_metrics m ON l.listing_id = m.listing_id
            WHERE {' OR '.join(conds)}
            ORDER BY m.impressions DESC, l.created_at DESC
            LIMIT 24
        """
        cursor.execute(q, tuple(params))
        listings = cursor.fetchall()

    # ---------- 2. Category fallback ----------
    if not listings:
        like = f"%{search}%"
        q = """
            SELECT l.listing_id, l.title, l.description, l.category, l.deal_type, l.`Plan`,
                   l.image1, l.price, l.required_cash, l.additional_cash, l.desired_swap,
                   l.location, l.contact, u.username, IFNULL(m.impressions,0) AS impressions
            FROM listings l
            JOIN users u ON l.user_id = u.id
            LEFT JOIN listing_metrics m ON l.listing_id = m.listing_id
            WHERE l.category LIKE %s
            ORDER BY m.impressions DESC, l.created_at DESC
            LIMIT 24
        """
        cursor.execute(q, (like,))
        listings = cursor.fetchall()

    # ---------- 3. Popular fallback ----------
    if not listings:
        q = """
            SELECT l.listing_id, l.title, l.description, l.category, l.deal_type, l.`Plan`,
                   l.image1, l.price, l.required_cash, l.additional_cash, l.desired_swap,
                   l.location, l.contact, u.username, IFNULL(m.impressions,0) AS impressions
            FROM listings l
            JOIN users u ON l.user_id = u.id
            LEFT JOIN listing_metrics m ON l.listing_id = m.listing_id
            ORDER BY m.impressions DESC, l.created_at DESC
            LIMIT 24
        """
        cursor.execute(q)
        listings = cursor.fetchall()

    return listings


def _rotate_weighted(listings):
    if not listings: return []
    weights = {'Diamond':5,'Gold':4,'Silver':3,'Free':1}
    idxs = [i for i, l in enumerate(listings) for _ in range(weights.get(l['Plan'],1))]
    if not idxs: return listings
    jitter = random.randrange(len(idxs))
    offset = (read_offset() + jitter) % len(idxs)
    write_offset((offset + 1) % len(idxs))
    seen, out = set(), []
    for i in idxs[offset:] + idxs[:offset]:
        if i not in seen:
            seen.add(i)
            out.append(listings[i])
        if len(out) == len(listings): break
    return out


def _attach_offered_items(cursor, items):
    swap_ids = [i['listing_id'] for i in items if i.get('deal_type') == 'Swap Deal']
    offers = {}
    if swap_ids:
        ph = ','.join(['%s'] * len(swap_ids))
        cursor.execute(f"""
            SELECT listing_id, item_id, title, description, image1, `condition`
            FROM offered_items WHERE listing_id IN ({ph})
            ORDER BY listing_id, item_id
        """, tuple(swap_ids))
        base_img = url_for('static', filename='images/', _external=True)
        for o in cursor.fetchall():
            o['image1'] = base_img + (o.get('image1') or 'placeholder.jpg')
            offers.setdefault(o['listing_id'], []).append(o)
    return offers




@app.route('/api/me/home-meta')
def home_meta():
    """
    Returns per-user small data for the home page for the visible listings.
    Query param: ids=1,2,3 (comma-separated listing_ids)
    If user not logged in, returns safe defaults.
    """
    user_id = session.get('user_id')
    ids_param = request.args.get('ids', '')
    # parse ints safely
    try:
        listing_ids = [int(x) for x in ids_param.split(',') if x.strip().isdigit()]
    except Exception:
        listing_ids = []

    # default response for anonymous users (so front-end can still call this safely)
    if not user_id or not listing_ids:
        return jsonify({
            "user_subscribed": False,
            "wishlisted_ids": []
        })

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Check push subscription (fast with LIMIT 1)
        cursor.execute(
            "SELECT 1 FROM push_subscriptions WHERE user_id=%s LIMIT 1",
            (user_id,)
        )
        user_subscribed = cursor.fetchone() is not None

        # Wishlists only for the visible listing ids
        ph = ','.join(['%s'] * len(listing_ids))
        query = f"SELECT listing_id FROM wishlists WHERE user_id=%s AND listing_id IN ({ph})"
        cursor.execute(query, tuple([user_id] + listing_ids))
        wishlisted = [row['listing_id'] for row in cursor.fetchall()]

        return jsonify({
            "user_subscribed": bool(user_subscribed),
            "wishlisted_ids": wishlisted
        })
    except Exception as e:
        app.logger.exception("Error in /api/me/home-meta")
        return jsonify({"user_subscribed": False, "wishlisted_ids": []}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()








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
        listing_id=listing_id
    )





# User lookup now returns account_status
def authenticate_user(email, password):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT id, account_status FROM users WHERE email = %s AND password = %s",
        (email, password)
    )
    user = cursor.fetchone()
    cursor.close()
    conn.close()
    return user

# Login Required Decorator (if needed for protected routes)
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page')
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    # Determine where to go after login: first from query, then form, then home
    next_url = request.args.get('next') or request.form.get('next') or url_for('home')

    # Initialize or retrieve the per-email failure counts
    session.setdefault('failed_logins', {})

    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not email or not password:
            flash('Please enter both email and password', 'danger')
            return redirect(url_for('login', next=next_url))

        # Check account suspension status
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT account_status FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row[0] == 'Suspended':
            flash(
                'Your account is suspended. Please email swapsphere@gmail.com to request reactivation.',
                'danger'
            )
            return redirect(url_for('login', next=next_url))

        # Authenticate credentials
        user = authenticate_user(email, password)
        if user:
            # Successful login
            session['failed_logins'].pop(email, None)
            session['user_id'] = user['id']
            session.permanent = True

            # If there's a post-login message, flash it now
            post_msg = session.pop('post_login_message', None)
            if post_msg:
                flash(post_msg, 'success')

            logging.info(f"User {user['id']} logged in successfully")
            return redirect(next_url)
        else:
            # Failed login: increment count
            failed = session['failed_logins'].get(email, 0) + 1
            session['failed_logins'][email] = failed
            logging.warning(f"Failed login attempt {failed} for email: {email}")

            # Provide warnings or suspend as needed
            if failed == 3:
                flash('Warning: One more failed attempt will lock your account.', 'warning')
            elif failed >= 4:
                try:
                    conn = get_db_connection()
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE users SET account_status = 'Suspended' WHERE email = %s",
                        (email,)
                    )
                    conn.commit()
                    cur.close()
                    conn.close()
                    logging.warning(f"User account suspended due to repeated failures: {email}")
                except Exception as e:
                    logging.error(f"Error suspending account {email}: {e}")
                flash(
                    'Your account has been suspended due to multiple failed login attempts. '
                    'Please email swapsphere@gmail.com to request reactivation.',
                    'danger'
                )
            else:
                flash('Invalid email or password', 'danger')

        # On any failure, stay on login
        return redirect(url_for('login', next=next_url))

    # GET request: render form. post‑login message will NOT appear here.
    return render_template('login.html', next_url=next_url)


# ─── 2) Kick‐off Google OAuth Flow ────────────────────────────────────────────
@app.route('/login/google')
def login_google():
    next_url = request.args.get('next') or url_for('home')
    redirect_uri = url_for('authorize', _external=True)
    return google.authorize_redirect(redirect_uri, state=next_url)


# ─── 3) OAuth2 Callback Handler ──────────────────────────────────────────────
@app.route('/oauth2callback')
def authorize():
    # Exchange code for access token
    token = google.authorize_access_token()

    # Fetch userinfo endpoint from metadata
    userinfo_endpoint = google.server_metadata.get('userinfo_endpoint')
    resp = google.get(userinfo_endpoint)
    resp.raise_for_status()
    user_info = resp.json()

    # Extract the Google ID
    google_id = user_info.get('sub') or user_info.get('id')
    if not google_id:
        raise RuntimeError("No 'sub' or 'id' in userinfo response")

    # Create or find the user
    user = get_or_create_user(
        google_id=google_id,
        email=user_info.get('email'),
        username=user_info.get('name'),  # your non-null field
        avatar=user_info.get('picture')
    )

    # Log them in using 'id'
    session['user_id'] = user['id']
    session.permanent = True
    logging.info(f"User {user['id']} logged in via Google")

    # Redirect back to original page
    next_url = request.args.get('state') or url_for('home')
    return redirect(next_url)








# ─── 5) Helper: find-or-create user by Google ID ────────────────────────────
def get_or_create_user(google_id, email, username, avatar=None):
    """
    Look up a user by google_id. If none exists, insert a new user
    providing username (non-null) and a NULL password for Google SSO.
    Returns a dict with at least 'id' (the PK) and other fields.
    """
    conn = get_db_connection()
    cur  = conn.cursor(dictionary=True)

    # Try to find existing user
    cur.execute("SELECT * FROM users WHERE google_id = %s", (google_id,))
    user = cur.fetchone()

    if not user:
        # Insert new user
        cur.execute("""
            INSERT INTO users
              (google_id, email, username, avatar, role, password, created_at)
            VALUES
              (%s, %s, %s, %s, 'Customer', NULL, NOW())
        """, (google_id, email, username, avatar))
        conn.commit()
        new_id = cur.lastrowid
        # Build a minimal user dict
        user = {
            'id': new_id,
            'google_id': google_id,
            'email': email,
            'username': username,
            'avatar': avatar,
            'role': 'Customer'
        }

    cur.close()
    conn.close()
    return user







def authenticate_user(email, password):
    """Verify user credentials against database"""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Get user by email
        cursor.execute("""
            SELECT id, email, password 
            FROM users 
            WHERE email = %s
        """, (email,))
        user = cursor.fetchone()
        
        # Verify password if user exists
        if user and check_password_hash(user['password'], password):
            return {
                'id': user['id'],
                'email': user['email']
                # Add other user fields you need in session
            }
        return None
        
    except Exception as e:
        logging.error(f"Authentication error for {email}: {str(e)}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def create_user(form):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        sql = """
            INSERT INTO users (username, email, contact, password) 
            VALUES (%s, %s, %s, %s)
        """
        username = form['username']
        email = form['email']
        country_code = form['country_code']
        phone_number = form['phone_number'].strip()

        # Remove all whitespace
        phone_number = "".join(phone_number.split())

        # Remove leading zero if it exists
        if phone_number.startswith("0"):
            phone_number = phone_number[1:]

        # Combine country code and phone number
        contact = country_code + phone_number

        hashed_password = generate_password_hash(form['password'])

        cursor.execute(sql, (username, email, contact, hashed_password))
        conn.commit()

        user_id = cursor.lastrowid
        return {'id': user_id, 'username': username, 'email': email, 'contact': contact}

    except Exception as e:
        logging.error(f"Error creating user: {str(e)}")
        if conn:
            conn.rollback()
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        user = create_user(request.form)
        if user:
            flash('Account created successfully! Please log in.')
            return redirect(url_for('login'))  # Redirect to login page
        flash('Account created successfully! Please log in.')
    
    return render_template('signup.html')







@app.route('/logout', methods=['GET', 'POST'])
def logout():
    session.pop('user_id', None)
    return redirect(url_for('home'))






def send_email_notification(recipient_email, subject, body):
    """
    Send email using SendGrid's SMTP relay (TLS on port 587).
    Returns True on success, False on failure. Does not raise.
    NOTE: Credentials are embedded for testing only — remove/rotate before production.
    """
    if not recipient_email:
        app.logger.warning("send_email_notification called without recipient_email")
        return False

    # ==============================
    # 🔐 SENDGRID SMTP SETTINGS
    # ==============================
    SMTP_SERVER = "smtp.sendgrid.net"
    SMTP_PORT = 587
    SMTP_USER = "apikey"  # This must remain literally 'apikey'
    SMTP_PASSWORD = "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"  # TEST KEY ONLY

    # ==============================
    # 📧 SENDER ADDRESS
    # ==============================
    # Use a domain-based or verified sender email in your SendGrid account.
    # Once domain authentication is verified, use:
    # sender_email = "no-reply@tghenterprise.net"
    sender_email = "no-reply@tghenterprise.net"

    # ==============================
    # ✉️ BUILD THE EMAIL MESSAGE
    # ==============================
    msg = EmailMessage()
    msg["From"] = f"SwapHub Notifications <{sender_email}>"
    msg["To"] = recipient_email
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        # Create SMTP connection with explicit socket timeout
        app.logger.info("Connecting to SendGrid SMTP server...")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
            server.ehlo()
            # Upgrade connection to TLS
            server.starttls()
            server.ehlo()
            # Login with SendGrid SMTP credentials
            server.login(SMTP_USER, SMTP_PASSWORD)
            # Send the email message
            server.send_message(msg)

        app.logger.info("✅ SendGrid SMTP: Email successfully sent to %s", recipient_email)
        return True

    except (smtplib.SMTPException, socket.timeout, ConnectionRefusedError) as e:
        app.logger.error("❌ SendGrid SMTP error sending to %s: %s", recipient_email, e, exc_info=True)
        return False
    except Exception as e:
        app.logger.exception("❌ Unexpected error sending email to %s: %s", recipient_email, e)
        return False







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

        # Validate listing exists
        cursor.execute("SELECT user_id, title FROM listings WHERE listing_id = %s", (listing_id,))
        listing = cursor.fetchone()
        if not listing:
            app.logger.error("Listing ID %s does not exist", listing_id)
            flash('Invalid listing ID.', 'danger')
            return redirect(url_for('listing_details', listing_id=listing_id))

        owner_id = listing['user_id']
        listing_title = listing['title']

        # Gather form data
        proposer_id = session.get('user_id')
        proposed_item = request.form.get('proposed_item', '').strip()
        additional_cash_raw = request.form.get('additional_cash', '').strip()
        message = request.form.get('message', '').strip()
        detailed_description = request.form.get('detailed_description', '').strip()
        condition = request.form.get('condition', '').strip()
        phone_number = request.form.get('phone_number', '').strip()
        email_address = request.form.get('email_address', '').strip()

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

        # Handle image uploads
        upload_folder = app.config.get('UPLOAD_FOLDER', '/tmp/uploads')
        os.makedirs(upload_folder, exist_ok=True)
        image_filenames = []
        for i in range(1, 5):
            file = request.files.get(f'image{i}')
            if file and file.filename and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                path = os.path.join(upload_folder, filename)
                file.save(path)
                image_filenames.append(filename)
            else:
                image_filenames.append(None)

        # Insert into proposals (use exact column names from your schema)
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
            *image_filenames
        )
        cursor.execute(insert_query, params)
        conn.commit()

        # Lookup owner using users.id
        cursor.execute("SELECT email, contact, name FROM users WHERE id = %s", (owner_id,))
        owner = cursor.fetchone()

        # Lookup proposer name for friendly email (best effort)
        proposer_name = "A user"
        if proposer_id:
            try:
                cursor.execute("SELECT name, email FROM users WHERE id = %s", (proposer_id,))
                proposer = cursor.fetchone()
                if proposer:
                    proposer_name = proposer.get('name') or proposer.get('email') or proposer_name
            except Exception:
                app.logger.debug("Could not fetch proposer name for user id=%s", proposer_id, exc_info=True)

        if owner:
            owner_email = owner.get('email')

            # Prepare friendly, inbox-friendly subject and body
            listing_url = url_for('listing_details', listing_id=listing_id, _external=True)
            friendly_subject = f"New proposal for your listing: {listing_title}"
            friendly_body = f"""Hi there,

{proposer_name} has sent you a new swap proposal for your listing "{listing_title}" on SwapHub.

Proposal summary:
- Offered item: {proposed_item}
- Condition: {condition}
- Additional cash offered: {"GH₵{:,.2f}".format(additional_cash) if additional_cash is not None else "N/A"}

Message from {proposer_name}:
{message if message else 'No additional message provided.'}

View the proposal and respond: {listing_url}

If you no longer want to receive these notifications, please update your notification settings in your dashboard.

Thanks for being on SwapHub!
The SwapHub Team
"""

            # Send email notification (non-fatal)
            if owner_email:
                try:
                    ok = send_email_notification(
                        owner_email,
                        friendly_subject,
                        friendly_body
                    )
                    if not ok:
                        app.logger.warning("Email notification failed for user id=%s", owner_id)
                except Exception as e:
                    app.logger.exception("Unexpected error in send_email_notification: %s", e)

            # Web-push notification (still wrapped)
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

        flash('Your swap proposal has been submitted successfully! The owner will be notified.', 'success')
        return redirect(url_for('listing_details', listing_id=listing_id))

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






@app.route('/check_login', methods=['GET'])
def check_login():
    if 'user_id' in session:
        return jsonify({'logged_in': True})
    else:
        return jsonify({'logged_in': False})





@app.route('/dashboard')
@login_required
def dashboard():
    conn    = None
    cursor  = None
    try:
        # 1) Open DB
        conn   = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 2) Current user
        user_id = session.get('user_id')
        cursor.execute("SELECT id, username, email, created_at FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        if not user:
            abort(404, "User not found")

        # 3) Fetch listings + impressions, clicks, proposal_count
        cursor.execute("""
            SELECT 
                l.*,
                IFNULL(m.impressions, 0) AS impressions,
                IFNULL(m.clicks, 0) AS clicks,
                (SELECT COUNT(*) FROM proposals p WHERE p.listing_id = l.listing_id) AS proposal_count
            FROM listings l
            LEFT JOIN listing_metrics m ON l.listing_id = m.listing_id
            WHERE l.user_id = %s
        """, (user_id,))
        listings = cursor.fetchall()

        # Dedupe (just in case)
        listings = list({l['listing_id']: l for l in listings}.values())

        # 4) Fetch offered items for swap deals
        for l in listings:
            if l['deal_type'] == 'Swap Deal':
                cursor.execute("""
                    SELECT listing_id, title, description, `condition`, image1, image2, image3, image4
                    FROM offered_items
                    WHERE listing_id = %s
                """, (l['listing_id'],))
                l['offered_items'] = cursor.fetchall()
            else:
                l['offered_items'] = []

        # 5) Bulk‑fetch wishlist counts
        listing_ids = [l['listing_id'] for l in listings]
        if listing_ids:
            ph = ','.join(['%s'] * len(listing_ids))
            cursor.execute(
                f"SELECT listing_id, COUNT(*) AS cnt FROM wishlists WHERE listing_id IN ({ph}) GROUP BY listing_id",
                tuple(listing_ids)
            )
            wl_counts = {r['listing_id']: r['cnt'] for r in cursor.fetchall()}
        else:
            wl_counts = {}

        # 6) Compute days_active & attach wishlist_count
        today = datetime.utcnow().date()
        for l in listings:
            # wishlist
            l['wishlist_count'] = wl_counts.get(l['listing_id'], 0)
            # days active
            created = l.get('created_at')
            if created:
                created_date = created.date() if hasattr(created, 'date') else created
                l['days_active'] = (today - created_date).days
            else:
                l['days_active'] = 0

        # 7) Fetch proposals for the dashboard
        cursor.execute("""
            SELECT p.*, l.title AS listing_title, u.username AS sender_username, u.contact AS sender_contact
            FROM proposals p
            JOIN listings l ON p.listing_id = l.listing_id
            JOIN users u ON p.user_id = u.id
            WHERE l.user_id = %s
        """, (user_id,))
        proposals = cursor.fetchall()

        unique_titles = list({p['listing_title'] for p in proposals})

        # 8) Promotion plan prices (for sidebar or modal)
        plan_prices = {
            'Diamond': 100,
            'Gold':     70,
            'Silver':   40,
            'Bronze':   20
        }

        # 9) Render
        return render_template(
            'dashboard.html',
            user=user,
            listings=listings,
            proposals=proposals,
            unique_titles=unique_titles,
            plan_prices=plan_prices
        )

    except Exception as e:
        app.logger.error("Error in /dashboard: %s", e, exc_info=True)
        abort(500, "Server error")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()




UPLOAD_FOLDER = "static/images"  

@app.route('/listings/<int:listing_id>', methods=['DELETE'])
@login_required
def delete_listing(listing_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # 1) Confirm listing exists and belongs to the user
        cursor.execute("SELECT * FROM listings WHERE listing_id = %s", (listing_id,))
        listing = cursor.fetchone()
        if not listing:
            return jsonify({'success': False, 'error': 'Listing not found'}), 404
        if listing['user_id'] != session['user_id']:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403

        # Helper to delete a local file if exists
        def delete_image_file(image_path):
            if not image_path:
                return
            # protect against absolute path deletion attacks by only allowing relative within UPLOAD_FOLDER
            full_path = os.path.normpath(os.path.join(UPLOAD_FOLDER, image_path))
            if not full_path.startswith(os.path.normpath(UPLOAD_FOLDER)):
                # don't delete paths that attempt to escape the upload folder
                print(f"Skipping unsafe path: {image_path}")
                return
            if os.path.exists(full_path):
                try:
                    os.remove(full_path)
                except Exception as e:
                    # non-fatal: log and continue (DB delete still in transaction)
                    print(f"Warning deleting file {full_path}: {e}")

        # 2) Fetch offered_items to delete their images
        cursor.execute("SELECT * FROM offered_items WHERE listing_id = %s", (listing_id,))
        offered_items = cursor.fetchall()

        # 3) Fetch proposals to delete their images
        cursor.execute("SELECT * FROM proposals WHERE listing_id = %s", (listing_id,))
        proposals = cursor.fetchall()

        # 4) Delete images from offered_items
        for item in offered_items:
            for field in ['image_url', 'image1', 'image2', 'image3', 'image4']:
                # item may be a dict due to dictionary=True
                delete_image_file(item.get(field))

        # 5) Delete images from proposals
        for p in proposals:
            for field in ['image1', 'image2', 'image3', 'image4']:
                delete_image_file(p.get(field))

        # 6) Delete images from main listing
        for field in ['image_url', 'image1', 'image2', 'image3', 'image4']:
            delete_image_file(listing.get(field))

        # 7) Delete dependent rows (order matters because of FK constraints)
        # Delete offered_items first
        cursor.execute("DELETE FROM offered_items WHERE listing_id = %s", (listing_id,))

        # Delete proposals next
        cursor.execute("DELETE FROM proposals WHERE listing_id = %s", (listing_id,))

        # Delete listing metrics (impressions/clicks)
        cursor.execute("DELETE FROM listing_metrics WHERE listing_id = %s", (listing_id,))

        # 8) Delete the listing row (double-checking user_id to be safe)
        cursor.execute("DELETE FROM listings WHERE listing_id = %s AND user_id = %s",
                       (listing_id, session['user_id']))

        # ensure the delete actually affected a row (defense-in-depth)
        if cursor.rowcount == 0:
            conn.rollback()
            return jsonify({'success': False, 'error': 'Delete failed or unauthorized'}), 400

        conn.commit()
        return jsonify({'success': True, 'message': 'Listing and related data deleted successfully'}), 200

    except mysql.connector.Error as e:
        conn.rollback()
        return jsonify({'success': False, 'error': f"DB error: {str(e)}"}), 500

    except Exception as e:
        conn.rollback()
        # catch unexpected errors (like filesystem errors) and return 500
        return jsonify({'success': False, 'error': f"Server error: {str(e)}"}), 500

    finally:
        cursor.close()
        conn.close()




from threading import Thread
import time
from pywebpush import webpush, WebPushException

@app.route('/proposals/<int:proposal_id>', methods=['PUT'])
@login_required
def update_proposal(proposal_id):
    status = request.json.get('status', '').lower()
    if status not in ('accepted', 'declined', 'negotiated'):
        return jsonify({'error': 'Invalid status'}), 400

    actor_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    fetch_time = update_time = log_time = 0.0

    try:
        # 1) Fetch + auth
        start = time.time()
        cursor.execute("""
            SELECT
              p.user_id   AS proposer_id,
              p.listing_id,
              l.user_id   AS owner_id,
              l.title     AS listing_title
            FROM proposals p
            JOIN listings  l  ON p.listing_id = l.listing_id
            WHERE p.id = %s
        """, (proposal_id,))
        row = cursor.fetchone()
        fetch_time = time.time() - start

        if not row:
            return jsonify({'error': 'Proposal not found'}), 404
        if row['owner_id'] != actor_id:
            return jsonify({'error': 'Not authorized'}), 403

        proposer_id   = row['proposer_id']
        listing_id    = row['listing_id']
        listing_title = row['listing_title']

        # 2) Update status
        start = time.time()
        cursor.execute(
            "UPDATE proposals SET status = %s WHERE id = %s",
            (status, proposal_id)
        )
        conn.commit()
        update_time = time.time() - start

        if cursor.rowcount == 0:
            return jsonify({'error': 'Update failed'}), 500

        # 3) Build notification payload
        if status == 'accepted':
            alert_type = 'proposal_accepted'
            title = "Proposal Accepted"
            body  = f"Your proposal for “{listing_title}” was accepted!"
        elif status == 'declined':
            alert_type = 'proposal_declined'
            title = "Proposal Declined"
            body  = f"Your proposal for “{listing_title}” was declined."
        else:
            alert_type = 'proposal_negotiated'
            title = "Proposal Negotiated"
            body  = f"Your proposal for “{listing_title}” is up for negotiation."

        # 4) Log notification
        start = time.time()
        cursor.execute("""
            INSERT INTO notification_log (listing_id, user_id, alert_type)
            VALUES (%s, %s, %s)
        """, (listing_id, proposer_id, alert_type))
        conn.commit()
        log_time = time.time() - start

        # 5) Load push subscriptions
        cursor.execute("""
            SELECT endpoint, p256dh, auth
            FROM push_subscriptions
            WHERE user_id = %s
        """, (proposer_id,))
        subscriptions = cursor.fetchall()


    except Exception as e:
        conn.rollback()
        app.logger.error("DB error in update_proposal: %s", e)
        return jsonify({'error': 'Server error'}), 500

    finally:
        cursor.close()
        conn.close()

    # 6) Compute link once
    link = url_for('listing_details', listing_id=listing_id, _external=True)

    # 7) Background push worker
    def _push_worker(subs, title, body, link):
        key    = app.config['VAPID_PRIVATE_KEY']
        claims = app.config['VAPID_CLAIMS']
        with app.app_context():
            for sub in subs:
                payload = {
                    "notification": {
                        "title": title,
                        "body":  body,
                        "data":  { "url": "/my-proposals", "type": "proposal" }
                    }
                }
                try:
                    webpush(
                        subscription_info={
                            "endpoint": sub['endpoint'],
                            "keys": {
                                "p256dh": sub['p256dh'],
                                "auth":   sub['auth']
                            }
                        },
                        data=json.dumps(payload),
                        vapid_private_key=key,
                        vapid_claims=claims
                    )
                except WebPushException as wp_err:
                    app.logger.error("WebPushError for %s: %s", sub['endpoint'], wp_err)
                except Exception as e:
                    app.logger.error("Async push error: %s", e)

    # 8) Spawn thread (no DB here)
    start = time.time()
    Thread(
        target=_push_worker,
        args=(subscriptions, title, body, link),
        daemon=True
    ).start()
    push_spawn_time = time.time() - start

    # 9) Log timings
    app.logger.info(
        f"Fetch: {fetch_time:.3f}s, "
        f"Update: {update_time:.3f}s, "
        f"Log: {log_time:.3f}s, "
        f"PushSpawn: {push_spawn_time:.3f}s"
    )

    return jsonify({'success': True, 'reload': True}), 200





@app.route('/profile', methods=['PUT'])
@login_required
def update_profile():
    data = request.get_json()
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            UPDATE users 
            SET username=%s, email=%s 
            WHERE id=%s
        """, (data['username'], data['email'], session['user_id']))
        conn.commit()
        session['username'] = data['username']
        session['email'] = data['email']
        return jsonify({'success': True})
    finally:
        cursor.close()
        conn.close()


@app.route('/change-password', methods=['PUT'])
@login_required
def change_password():
    # Get plaintext passwords from request
    old_password = request.json.get('oldPassword')
    new_password = request.json.get('newPassword')
    
    if not old_password or not new_password:
        return jsonify({'error': 'Both old and new passwords are required'}), 400
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        # 1. Verify old password matches (plaintext comparison)
        cursor.execute("SELECT password FROM users WHERE id=%s", (session['user_id'],))
        user = cursor.fetchone()
        
        if not user or user['password'] != old_password:  # Plaintext comparison
            return jsonify({'error': 'Invalid current password'}), 401
        
        # 2. Update with new plaintext password
        cursor.execute("UPDATE users SET password=%s WHERE id=%s", 
                     (new_password, session['user_id']))  # Storing plaintext
        conn.commit()
        
        return jsonify({'success': True, 'message': 'Password updated'})
        
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        cursor.close()
        conn.close()



# Add these new routes to your existing app.py

@app.route('/listings/<int:listing_id>/edit')
@login_required
def edit_listing(listing_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        # Fetch the listing, including price and images, to determine deal_type and verify ownership
        cursor.execute("""
            SELECT listing_id, user_id, deal_type, title, description, 
                   category, location, contact, desired_swap, 
                   desired_swap_description, required_cash, additional_cash,
                   price, image_url, image1, image2, image3
            FROM listings 
            WHERE listing_id = %s AND user_id = %s
        """, (listing_id, session['user_id']))
        listing = cursor.fetchone()
        
        if not listing:
            flash('Listing not found or you don’t have permission to edit it', 'danger')
            return redirect(url_for('dashboard'))
        
        # Initialize variables
        offered_items = []
        
        # For Swap Deals, fetch items from offered_items table
        if listing['deal_type'] == 'Swap Deal':
            cursor.execute("""
                SELECT item_id, listing_id, title, description, `condition`,
                       image1, image2, image3, image4
                FROM offered_items 
                WHERE listing_id = %s
            """, (listing_id,))
            offered_items = cursor.fetchall()  # Could return multiple items
        
        return render_template('edit_listing.html', listing=listing, offered_items=offered_items)
    
    finally:
        cursor.close()
        conn.close()




@app.route('/listings/<int:listing_id>/update', methods=['POST'])
@login_required
def update_listing(listing_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # 1) Verify ownership + deal_type
        cursor.execute(
            "SELECT user_id, deal_type FROM listings WHERE listing_id = %s",
            (listing_id,)
        )
        row = cursor.fetchone()
        if not row or row['user_id'] != session['user_id']:
            flash('You do not have permission to edit this listing', 'danger')
            return redirect(url_for('dashboard'))
        deal_type = row['deal_type']

        # 2) Fetch entire existing listings row
        cursor.execute("SELECT * FROM listings WHERE listing_id = %s", (listing_id,))
        existing = cursor.fetchone()

        # 3) Field‐fallback helper
        def getf(name):
            v = request.form.get(name)
            if v is None or v.strip() == '':
                return existing[name]
            return v.strip()

        title       = getf('title')
        description = getf('description')
        category    = getf('category')
        location    = getf('location')
        contact     = getf('contact')

        if deal_type == 'Swap Deal':
            desired_swap             = getf('desired_swap')
            desired_swap_description = getf('desired_swap_description')
            required_cash            = request.form.get('required_cash') or existing['required_cash']
            additional_cash          = request.form.get('additional_cash') or existing['additional_cash']
        else:
            condition_sales = getf('condition')
            price           = getf('price')

        # 4) Handle Outright images (always preserve old if no new upload)
        upload_dir = os.path.join(app.root_path, 'static', 'images')
        os.makedirs(upload_dir, exist_ok=True)

        form_to_db = [
            ('image',  'image_url'),
            ('image1', 'image1'),
            ('image2', 'image2'),
            ('image3', 'image3'),
            ('image4', 'image4'),
        ]
        images_to_save = {}
        for form_field, db_col in form_to_db:
            file = request.files.get(form_field)
            if file and allowed_file(file.filename):
                fname = secure_filename(file.filename)
                uniq  = f"{uuid.uuid4().hex}_{fname}"
                file.save(os.path.join(upload_dir, uniq))
                images_to_save[db_col] = uniq
            else:
                images_to_save[db_col] = existing[db_col]

        # 5) Update listings table
        if deal_type == 'Swap Deal':
            sql = """
                UPDATE listings
                SET
                  title=%s,
                  description=%s,
                  category=%s,
                  location=%s,
                  contact=%s,
                  desired_swap=%s,
                  desired_swap_description=%s,
                  required_cash=%s,
                  additional_cash=%s
                WHERE listing_id = %s
            """
            params = [
                title, description, category, location, contact,
                desired_swap, desired_swap_description,
                required_cash, additional_cash,
                listing_id
            ]
        else:
            sql = """
                UPDATE listings
                SET
                  title=%s,
                  description=%s,
                  category=%s,
                  location=%s,
                  contact=%s,
                  `condition`=%s,
                  price=%s,
                  image_url=%s,
                  image1=%s,
                  image2=%s,
                  image3=%s,
                  image4=%s
                WHERE listing_id = %s
            """
            params = [
                title, description, category, location, contact,
                condition_sales, price,
                images_to_save['image_url'],
                images_to_save['image1'],
                images_to_save['image2'],
                images_to_save['image3'],
                images_to_save['image4'],
                listing_id
            ]

        cursor.execute(sql, params)

        # 6) Swap-Deal: preserve offered_items images
        if deal_type == 'Swap Deal':
            # a) Fetch existing offered_items fields only (no 'id')
            cursor.execute("""
                SELECT
                  title   AS existing_title,
                  description AS existing_description,
                  `condition` AS existing_condition,
                  image1  AS existing_image1,
                  image2  AS existing_image2,
                  image3  AS existing_image3,
                  image4  AS existing_image4
                FROM offered_items
                WHERE listing_id = %s
            """, (listing_id,))
            old_items = cursor.fetchall()

            # b) Delete old rows
            cursor.execute("DELETE FROM offered_items WHERE listing_id = %s", (listing_id,))

            # c) Re-insert up to requested count, falling back to old
            requested = min(int(request.form.get('offered_items_count', 0)), 2)
            for idx in range(requested):
                slot = idx + 1
                old = old_items[idx] if idx < len(old_items) else {}

                otitle = request.form.get(f'offered_title_{slot}') or old.get('existing_title')
                if not otitle:
                    continue  # never insert a NULL title

                odesc = request.form.get(f'offered_description_{slot}') or old.get('existing_description', '')
                ocond = request.form.get(f'offered_condition_{slot}')   or old.get('existing_condition', '')

                oimgs = []
                for j in range(1, 5):
                    key = f'offered_image_{slot}_{j}'
                    file = request.files.get(key)
                    if file and allowed_file(file.filename):
                        fname = secure_filename(file.filename)
                        uniq  = f"{uuid.uuid4().hex}_{fname}"
                        file.save(os.path.join(upload_dir, uniq))
                        oimgs.append(uniq)
                    else:
                        oimgs.append(old.get(f'existing_image{j}'))

                cursor.execute("""
                    INSERT INTO offered_items
                      (listing_id, title, description, `condition`, image1, image2, image3, image4)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    listing_id,
                    otitle,
                    odesc,
                    ocond,
                    oimgs[0], oimgs[1], oimgs[2], oimgs[3]
                ))

        # 7) Commit & redirect
        conn.commit()
        flash('Listing updated successfully!', 'success')
        return redirect(url_for('dashboard'))

    except Exception as e:
        conn.rollback()
        app.logger.error(f"[UPDATE-LISTING] Error updating listing #{listing_id}: {e}", exc_info=True)
        flash(f'An error occurred while updating your listing: {e}', 'danger')
        return redirect(url_for('edit_listing', listing_id=listing_id))

    finally:
        cursor.close()
        conn.close()










@app.route('/my-proposals')
def my_proposals():
    # 1) Ensure the user is logged in
    if 'user_id' not in session:
        return redirect(url_for('login', next=request.url))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # 2) Fetch proposals, coalescing additional_cash and created_at
        cursor.execute("""
            SELECT 
                p.*,
                l.title            AS listing_title,
                l.description      AS listing_description,
                l.user_id          AS lister_id,
                l.contact          AS listing_contact,
                u.username         AS lister_username,
                u.name             AS lister_name,
                u.contact          AS lister_contact,
                IFNULL(p.additional_cash, 0)   AS additional_cash,
                IFNULL(p.created_at, '1970-01-01 00:00:00') AS created_at
            FROM proposals p
            JOIN listings l 
              ON p.listing_id = l.listing_id
            JOIN users u 
              ON l.user_id = u.id
            WHERE p.user_id = %s
            ORDER BY 
              created_at DESC
        """, (session['user_id'],))

        proposals = cursor.fetchall()

        # 3) Post-process each row:
        for p in proposals:
            # a) Convert created_at from string to datetime if needed
            ca = p.get('created_at')
            if isinstance(ca, str):
                try:
                    p['created_at'] = datetime.strptime(ca, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    # fallback if format differs:
                    p['created_at'] = None

            # b) Ensure any numeric comparisons in template are safe
            if p.get('additional_cash') is None:
                p['additional_cash'] = 0
            # If you compare other fields, default them here:
            # if p.get('some_number') is None:
            #     p['some_number'] = 0

        return render_template('my_proposals.html', proposals=proposals)

    except Exception as e:
        app.logger.error(f"Error fetching proposals: {e}")
        return render_template('my_proposals.html', proposals=[])
    finally:
        cursor.close()
        conn.close()


@app.route('/proposals/<int:proposal_id>', methods=['DELETE'])
def delete_proposal(proposal_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Verify the proposal belongs to the current user before deleting
        cursor.execute("""
            DELETE FROM proposals 
            WHERE id = %s AND user_id = %s
        """, (proposal_id, session['user_id']))
        
        if cursor.rowcount == 0:
            return jsonify({'error': 'Proposal not found or not authorized'}), 404
        
        conn.commit()
        return jsonify({'success': True}), 200
    except Exception as e:
        conn.rollback()
        print(f"Error deleting proposal: {e}")
        return jsonify({'error': 'Failed to delete proposal'}), 500
    finally:
        cursor.close()
        conn.close()




from flask import (
    request, session, redirect, url_for,
    flash, jsonify
)
import os, uuid
from werkzeug.utils import secure_filename

ALLOWED_EXTENSIONS = {'png','jpg','jpeg','gif', 'webp', 'avif'}

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS



@app.route('/listings', methods=['POST'])
@login_required
def create_listing():
    logger.debug(f"Processing listing for user_id: {session['user_id']}")
    
    # 1) Common data
    dt = request.form.get('deal_type', 'Swap Deal')
    deal_type = dt if dt == 'Swap Deal' else 'Outright Sales'
    title = request.form['title'].strip()
    description = request.form.get('description', '').strip()
    category = request.form['category'].strip()
    location = request.form['location'].strip()
    contact = request.form['contact'].strip()
    plan = request.form.get('plan', 'Free')
    logger.debug(f"Common data: deal_type={deal_type}, title={title}, category={category}, plan={plan}")

    # 2) Gather main images (only for Outright Sales)
    main_images = []
    if deal_type == 'Outright Sales':
        for f in request.files.getlist('images[]'):
            if f and allowed_file(f.filename):
                fn = secure_filename(f.filename)
                u = f"{uuid.uuid4().hex}_{fn}"
                f.save(os.path.join(app.config['UPLOAD_FOLDER'], u))
                main_images.append(u)
                if len(main_images) >= 5:
                    break
        if not main_images:
            flash("At least one image is required for sales.", "error")
            logger.error("No images uploaded for sale")
            return redirect(url_for('dashboard'))
    logger.debug(f"Main images: {main_images}")

    # 3) Swap-offer fields
    off_titles = request.form.getlist('offer_title[]')
    off_conds = request.form.getlist('offer_condition[]')
    off_descs = request.form.getlist('offer_description[]')
    logger.debug(f"Offer data: titles={off_titles}, conditions={off_conds}, descriptions={off_descs}")

    # Process offered items (only for Swap Deal)
    offered_items = []
    if deal_type == 'Swap Deal':
        if not (1 <= len(off_conds) <= 2):
            flash("Offer between 1 and 2 items.", "error")
            logger.error(f"Invalid number of offered items: {len(off_conds)}")
            return redirect(url_for('dashboard'))

        # Group images by item
        images_per_item = []
        offer_image_files_1 = request.files.getlist('offer_images_1[]')
        offer_image_files_2 = request.files.getlist('offer_images_2[]')
        image_lists = [offer_image_files_1, offer_image_files_2][:len(off_conds)]
        logger.debug(f"Image lists: {[[f.filename for f in lst] for lst in image_lists]}")

        def save_files(file_list):
            out = []
            for f in file_list:
                if f and allowed_file(f.filename):
                    fn = secure_filename(f.filename)
                    u = f"{uuid.uuid4().hex}_{fn}"
                    f.save(os.path.join(app.config['UPLOAD_FOLDER'], u))
                    out.append(u)
            return out

        for i in range(len(off_conds)):
            if not off_conds[i].strip() or not off_descs[i].strip():
                flash(f"Item {i+1} must have a condition and description.", "error")
                logger.error(f"Item {i+1} missing condition or description")
                return redirect(url_for('dashboard'))

            item_title = off_titles[i].strip() if i < len(off_titles) and off_titles[i].strip() else title
            item_images = save_files(image_lists[i][:4]) if i < len(image_lists) else []
            if not item_images:
                flash(f"Item {i+1} must have at least one image.", "error")
                logger.error(f"Item {i+1} has no images: {image_lists[i] if i < len(image_lists) else []}")
                return redirect(url_for('dashboard'))

            offered_items.append({
                'title': item_title,
                'condition': off_conds[i],
                'description': off_descs[i],
                'images': item_images + [None] * (4 - len(item_images))
            })
            logger.debug(f"Item {i+1}: title={item_title}, images={item_images}")

    # 4) Deal-type specifics
    desired_swap = None
    desired_swap_description = request.form.get('swap_notes', '').strip()
    additional_cash = None
    required_cash = None
    price = None

    if deal_type == 'Swap Deal':
        desired_swap = request.form.get('desired_swap').strip()
        additional_cash = request.form.get('additional_cash') or None
        required_cash = request.form.get('required_cash') or None
        if not desired_swap:
            flash("Desired item is required for swap.", "error")
            logger.error("Missing desired swap item")
            return redirect(url_for('dashboard'))
        # Extract condition from first offered item
        condition = offered_items[0]['condition']
    else:
        price = request.form.get('price') or None
        # Grab the sale-condition directly from the form
        condition = request.form.get('condition')
        off_conds = [condition]
        off_descs = [description]
        offered_items = []
    logger.debug(f"Deal specifics: desired_swap={desired_swap}, price={price}, condition={condition}")

    # 5) Combine description
    if deal_type == 'Swap Deal':
        joined_offers = "\n\n".join([item['description'] for item in offered_items])
        combined_description = f"{description}\n\n{joined_offers}" if joined_offers else description
    else:
        combined_description = description
    logger.debug(f"Combined description: {combined_description}")

    # 6) PAYSTACK flow
    if plan != 'Free':
        session['pending_listing'] = {
            'user_id': session['user_id'],
            'title': title,
            'description': combined_description,
            'category': category,
            'desired_swap': desired_swap,
            'desired_swap_description': desired_swap_description,
            'additional_cash': additional_cash,
            'required_cash': required_cash,
            'location': location,
            'contact': contact,
            'main_images': main_images if deal_type == 'Outright Sales' else (offered_items[0]['images'][:4] if offered_items else []),
            'plan': plan,
            'deal_type': deal_type,
            'price': price,
            'offered_items': offered_items
        }
        logger.debug(f"Stored pending listing for payment: {session['pending_listing']}")
        plan_fees = {'Bronze': 20, 'Silver': 50, 'Gold': 100, 'Diamond': 200}
        return redirect(url_for('paystack_payment', plan=plan, amount=plan_fees.get(plan, 0)))

    # 7) INSERT INTO listings
    conn = get_db_connection()
    cursor = conn.cursor()
    listing_params = [
        session['user_id'], title, combined_description, category,
        desired_swap, desired_swap_description,
        additional_cash, required_cash,
        condition,
        location, contact
    ]
    if deal_type == 'Outright Sales':
        listing_params += main_images + [None] * (5 - len(main_images))
    else:
        # Use first offered item's images for Swap Deal
        swap_images = offered_items[0]['images'][:4] if offered_items else []
        listing_params += swap_images + [None] * (5 - len(swap_images))
    listing_params += [plan, deal_type, price]
    logger.debug(f"Listing params: {listing_params}")
    cursor.execute("""
        INSERT INTO listings (
            user_id, title, description, category,
            desired_swap, desired_swap_description,
            additional_cash, required_cash,
            `condition`, location, contact,
            image_url, image1, image2, image3, image4,
            plan, deal_type, price
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, listing_params)
    lid = cursor.lastrowid
    logger.debug(f"Inserted listing with ID: {lid}")

    # 8) Insert offered items (only for Swap Deal)
    if deal_type == 'Swap Deal':
        for item in offered_items:
            cursor.execute("""
                INSERT INTO offered_items (
                    listing_id, title, description, `condition`,
                    image1, image2, image3, image4
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                lid, item['title'], item['description'], item['condition'],
                item['images'][0], item['images'][1], item['images'][2], item['images'][3]
            ))
            logger.debug(f"Inserted offered item: {item}")

    conn.commit()
    cursor.close()
    conn.close()

    flash("Listing created successfully!", "success")
    logger.debug("Listing creation completed successfully")
    return redirect(url_for('dashboard'))




@app.route('/paystack_payment')
@login_required
def paystack_payment():
    plan = request.args.get('plan')
    amount = request.args.get('amount', type=float)
    if amount is None or not plan:
        flash("Invalid payment parameters.", "error")
        logger.error(f"Invalid payment parameters: plan={plan}, amount={amount}")
        return redirect(url_for('home'))

    amount_kobo = int(amount * 100)
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT email FROM users WHERE id=%s", (session['user_id'],))
    user = cursor.fetchone()
    cursor.close()
    conn.close()
    if not user:
        flash("User not found.", "error")
        logger.error(f"User not found for id: {session['user_id']}")
        return redirect(url_for('home'))

    payload = {
        "email": user['email'],
        "amount": amount_kobo,
        "metadata": {"pending_listing": session.get('pending_listing')},
        "callback_url": url_for('paystack_verify', _external=True)
    }
    headers = {
        "Authorization": "Bearer sk_test_38d38a400d7c1a34c826930691e8c23fce8dde98",
        "Content-Type": "application/json"
    }
    logger.debug(f"Initiating Paystack payment: plan={plan}, amount_kobo={amount_kobo}, user_email={user['email']}")
    resp = requests.post("https://api.paystack.co/transaction/initialize",
                         json=payload, headers=headers)
    data = resp.json()
    if data.get('status'):
        logger.debug(f"Payment initialized, redirecting to: {data['data']['authorization_url']}")
        return redirect(data['data']['authorization_url'])
    flash("Payment initialization failed.", "error")
    logger.error(f"Payment initialization failed: {data}")
    return redirect(url_for('home'))






@app.route('/paystack_verify')
@login_required
def paystack_verify():
    logger.debug("Verifying Paystack payment")
    ref = request.args.get('reference')
    if not ref:
        flash("Payment reference missing.", "error")
        logger.error("Missing payment reference")
        return redirect(url_for('home'))

    # Verify with Paystack
    headers = {"Authorization": "Bearer sk_test_38d38a400d7c1a34c826930691e8c23fce8dde98"}
    resp = requests.get(f"https://api.paystack.co/transaction/verify/{ref}", headers=headers)
    result = resp.json()
    if not (result.get('status') and result['data']['status'] == 'success'):
        flash("Payment verification failed.", "error")
        logger.error(f"Payment verification failed: {result}")
        return redirect(url_for('home'))

    # Pull pending listing
    p = result['data']['metadata'].get('pending_listing') or session.pop('pending_listing', None)
    if not p:
        flash("No pending listing.", "error")
        logger.error("No pending listing found")
        return redirect(url_for('home'))
    logger.debug(f"Pending listing: {p}")

    # Clean up numeric fields
    raw_additional = str(p.get('additional_cash', '')).strip()
    raw_required = str(p.get('required_cash', '')).strip()
    raw_price = str(p.get('price', '')).strip()
    additional_cash = int(float(raw_additional)) if raw_additional else None
    required_cash = int(float(raw_required)) if raw_required else None
    price = int(float(raw_price)) if raw_price else None

    # Validate offered items for Swap Deal
    offered_items = p.get('offered_items', [])
    if p['deal_type'] == 'Swap Deal':
        if not (1 <= len(offered_items) <= 2):
            flash("Invalid number of offered items.", "error")
            logger.error(f"Invalid number of offered items: {len(offered_items)}")
            return redirect(url_for('home'))
        for i, item in enumerate(offered_items, 1):
            if not (item.get('condition') and item.get('description') and item['images'][0]):
                flash(f"Item {i} is incomplete.", "error")
                logger.error(f"Item {i} incomplete: {item}")
                return redirect(url_for('home'))

    # Insert into listings
    conn = get_db_connection()
    cursor = conn.cursor()
    listing_params = [
        p['user_id'], p['title'], p['description'], p['category'],
        p.get('desired_swap'), p.get('desired_swap_description'),
        additional_cash, required_cash,
        (offered_items[0]['condition'] if offered_items else None),
        p['location'], p['contact']
    ]
    main_images = p.get('main_images', [])[:4]  # Use up to 4 images
    listing_params += main_images + [None] * (5 - len(main_images))
    listing_params += [p['plan'], p['deal_type'], price]
    logger.debug(f"Listing params for verification: {listing_params}")
    cursor.execute("""
        INSERT INTO listings (
            user_id, title, description, category,
            desired_swap, desired_swap_description,
            additional_cash, required_cash,
            `condition`, location, contact,
            image_url, image1, image2, image3, image4,
            plan, deal_type, price
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, listing_params)
    lid = cursor.lastrowid
    logger.debug(f"Inserted listing with ID: {lid}")

    # Insert offered items (only for Swap Deal)
    if p['deal_type'] == 'Swap Deal':
        for item in offered_items:
            cursor.execute("""
                INSERT INTO offered_items (
                    listing_id, title, description, `condition`,
                    image1, image2, image3, image4
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                lid, item['title'], item['description'], item['condition'],
                item['images'][0], item['images'][1], item['images'][2], item['images'][3]
            ))
            logger.debug(f"Inserted offered item: {item}")

    conn.commit()
    cursor.close()
    conn.close()

    session.pop('pending_listing', None)
    flash("Your product has been listed!", "success")
    logger.debug("Payment verified and listing created")
    return redirect(url_for('dashboard'))





@app.route('/debug_form')
@login_required
def debug_form():
    return {
        'form_data': {k: v for k, v in request.form.items()},
        'files': {k: [f.filename for f in request.files.getlist(k)] for k in request.files}
    }




@app.route('/submit_rating/<int:listing_id>', methods=['POST'])
def submit_rating(listing_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Please log in to rate this listing'}), 401
    
    try:
        rating_value = float(request.form.get('rating'))
        if not (1 <= rating_value <= 5):
            raise ValueError("Rating must be between 1 and 5")
            
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get listing owner ID
        cursor.execute("SELECT user_id FROM listings WHERE listing_id = %s", (listing_id,))
        listing_owner_id = cursor.fetchone()[0]
        
        # Insert or update rating
        cursor.execute("""
            INSERT INTO ratings (listing_id, user_id, owner_id, rating_value)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE rating_value = VALUES(rating_value)
        """, (listing_id, session['user_id'], listing_owner_id, rating_value))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        return jsonify({'success': True, 'message': 'Rating submitted successfully'})
        
    except Exception as e:
        logging.error("Error submitting rating: %s", e)
        return jsonify({'success': False, 'message': str(e)}), 400

@app.route('/submit_review/<int:listing_id>', methods=['POST'])
def submit_review(listing_id):
    if 'user_id' not in session:
        app.logger.warning(f"Unauthorized review attempt for listing_id: {listing_id}")
        return jsonify({'success': False, 'message': 'Please log in to review this listing'}), 401
    
    try:
        review_text = request.form.get('review_text', '').strip()
        if not review_text:
            app.logger.warning(f"Empty review text for listing_id: {listing_id}, user_id: {session['user_id']}")
            return jsonify({'success': False, 'message': 'Review text cannot be empty'}), 400
        
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Verify listing exists and get owner ID
        cursor.execute("SELECT user_id FROM listings WHERE listing_id = %s", (listing_id,))
        listing = cursor.fetchone()
        if not listing:
            app.logger.error(f"Listing not found for listing_id: {listing_id}")
            cursor.close()
            conn.close()
            return jsonify({'success': False, 'message': 'Listing not found'}), 404
        listing_owner_id = listing['user_id']
        
        # Insert review
        cursor.execute("""
            INSERT INTO reviews (listing_id, user_id, owner_id, review_text, created_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (listing_id, session['user_id'], listing_owner_id, review_text, datetime.utcnow()))
        conn.commit()
        
        # Get the new review with username
        cursor.execute("""
            SELECT r.review_id, r.review_text, r.created_at, u.username AS reviewer
            FROM reviews r
            JOIN users u ON r.user_id = u.id
            WHERE r.review_id = LAST_INSERT_ID()
        """)
        new_review = cursor.fetchone()
        
        cursor.close()
        conn.close()
        
        app.logger.debug(f"Submitted review for listing_id: {listing_id}, review: {new_review}")
        return jsonify({
            'success': True,
            'message': 'Review submitted successfully',
            'review': {
                'review_id': new_review['review_id'],
                'review_text': new_review['review_text'],
                'created_at': new_review['created_at'].strftime('%B %d, %Y'),
                'reviewer': new_review['reviewer']
            }
        })
        
    except mysql.connector.Error as e:
        app.logger.error(f"Database error submitting review for listing_id: {listing_id}: {str(e)}", exc_info=True)
        if 'conn' in locals() and conn:
            conn.rollback()
            conn.close()
        return jsonify({'success': False, 'message': f'Database error: {str(e)}'}), 500
    except Exception as e:
        app.logger.error(f"Unexpected error submitting review for listing_id: {listing_id}: {str(e)}", exc_info=True)
        if 'conn' in locals() and conn:
            conn.rollback()
            conn.close()
        return jsonify({'success': False, 'message': f'Unexpected error: {str(e)}'}), 500









# ========== Ensure send_email_notification exists (guarded) ==========
# If your codebase already defines `send_email_notification`, this block will NOT override it.
if 'send_email_notification' not in globals():
    def send_email_notification(recipient_email, subject, body):
        """
        Send email using SendGrid's SMTP relay (TLS on port 587).
        Returns True on success, False on failure. Does not raise.
        NOTE: Credentials are embedded for testing only — remove/rotate before production.
        """
        if not recipient_email:
            app.logger.warning("send_email_notification called without recipient_email")
            return False

        # ==============================
        # 🔐 SENDGRID SMTP SETTINGS
        # ==============================
        SMTP_SERVER = "smtp.sendgrid.net"
        SMTP_PORT = 587
        SMTP_USER = "apikey"  # This must remain literally 'apikey'
        SMTP_PASSWORD = "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"  # TEST KEY ONLY

        # ==============================
        # 📧 SENDER ADDRESS
        # ==============================
        # Use a domain-based or verified sender email in your SendGrid account.
        sender_email = app.config.get('FROM_EMAIL', "no-reply@tghenterprise.net")

        # ==============================
        # ✉️ BUILD THE EMAIL MESSAGE
        # ==============================
        msg = EmailMessage()
        msg["From"] = f"SwapHub Notifications <{sender_email}>"
        msg["To"] = recipient_email
        msg["Subject"] = subject
        msg.set_content(body)

        try:
            # Create SMTP connection with explicit socket timeout
            app.logger.info("Connecting to SendGrid SMTP server...")
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
                server.ehlo()
                # Upgrade connection to TLS
                server.starttls()
                server.ehlo()
                # Login with SendGrid SMTP credentials
                server.login(SMTP_USER, SMTP_PASSWORD)
                # Send the email message
                server.send_message(msg)

            app.logger.info("✅ SendGrid SMTP: Email successfully sent to %s", recipient_email)
            return True

        except (smtplib.SMTPException, socket.timeout, ConnectionRefusedError) as e:
            app.logger.error("❌ SendGrid SMTP error sending to %s: %s", recipient_email, e, exc_info=True)
            return False
        except Exception as e:
            app.logger.exception("❌ Unexpected error sending email to %s: %s", recipient_email, e)
            return False

# ========== Password-reset SendGrid wrapper (named uniquely to avoid conflicts) ==========
def _send_password_email_worker(recipient_email, subject, body):
    """
    Worker function that calls the existing send_email_notification helper.
    Kept separate so we never redefine the project's primary send_email_notification.
    """
    try:
        ok = send_email_notification(recipient_email, subject, body)
        if ok:
            logging.info("Password reset email sent to %s", recipient_email)
        else:
            logging.error("Password reset email failed (send_email_notification returned False) for %s", recipient_email)
    except Exception:
        logging.exception("Exception while sending password reset email to %s", recipient_email)

def send_password_reset_via_sendgrid(recipient_email, reset_url):
    """
    Public helper to queue a password reset email using SendGrid (via send_email_notification).
    Uses a unique function name to avoid any collisions with other senders.
    """
    if not recipient_email:
        logging.error("send_password_reset_via_sendgrid called without recipient_email")
        return None

    subject = "SwapHub — Password Reset Request"
    body = (
        "To reset your password, visit the following link:\n"
        f"{reset_url}\n\n"
        "This link will expire in 1 hour.\n"
        "If you did not request a password reset, ignore this email."
    )

    thread = threading.Thread(
        target=_send_password_email_worker,
        args=(recipient_email, subject, body),
        daemon=True
    )
    thread.start()
    return thread

# ========== Routes: forgot-password and reset-password (using SendGrid wrapper) ==========
@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        conn = None
        cursor = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)

            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()

            if not user:
                flash('No account found with that email address.', 'danger')
                return redirect(url_for('forgot_password'))

            # Generate token and store in DB
            token = secrets.token_urlsafe(32)
            expires_at = datetime.now(UTC) + timedelta(hours=1)

            cursor.execute(
                "INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (%s, %s, %s)",
                (user['id'], token, expires_at)
            )
            conn.commit()

            # Build reset URL
            reset_url = url_for('reset_password', token=token, _external=True)

            # Queue SendGrid email using the uniquely named helper
            try:
                send_password_reset_via_sendgrid(email, reset_url)
                flash('Password reset email queued. Check your inbox.', 'success')
            except Exception:
                logging.exception("Failed to queue password reset email for %s", email)
                flash('Could not send reset email at this time. Try again later.', 'danger')

            return redirect(url_for('forgot_password'))

        except Exception:
            if conn:
                conn.rollback()
            logging.exception("Error in forgot_password")
            flash('An error occurred. Please try again later.', 'danger')
            return redirect(url_for('forgot_password'))
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    return render_template('forgot_password.html')


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



            


_redis_client = None
_redis_lock = threading.Lock()

def get_redis():
    """Return a shared redis.StrictRedis instance or None."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    with _redis_lock:
        if _redis_client is None:
            url = os.getenv('REDIS_URL')
            if not url:
                logging.warning("REDIS_URL not set – using in-memory fallback")
                return None
            try:
                _redis_client = redis.from_url(url, decode_responses=False)
                logging.info("Connected to Redis: %s", url.split('@')[-1])
            except Exception as e:
                logging.error("Redis connection failed: %s", e)
                _redis_client = None
    return _redis_client

# --------------------------------------------------------------
# 3. TRACKING ENDPOINTS (unchanged public contract)
# --------------------------------------------------------------
@app.route('/api/track_impression', methods=['POST'])
def track_impression():
    data = request.get_json() or {}
    lid = str(data.get('listing_id', '')).strip()
    source = data.get('source', 'grid')

    if not lid:
        return jsonify(success=False, error='Missing listing_id'), 400

    r = get_redis()
    if not r:                     # ---- local fallback ----
        with cache_lock:
            impressions_cache.setdefault(lid, {'impressions': 0, 'carousel_impressions': 0})
            impressions_cache[lid]['impressions'] += 1
            if source == 'carousel':
                impressions_cache[lid]['carousel_impressions'] += 1
    else:                         # ---- Redis ----
        key = f"imp:{lid}"
        r.hincrby(key, 'impressions', 1)
        if source == 'carousel':
            r.hincrby(key, 'carousel_impressions', 1)

    logging.info(f"Impression tracked: listing_id={lid}, source={source}")
    return jsonify(success=True)


@app.route('/api/track_click', methods=['POST'])
def track_click():
    data = request.get_json() or {}
    lid = str(data.get('listing_id', '')).strip()
    source = data.get('source', 'grid')

    if not lid:
        return jsonify(success=False, error='Missing listing_id'), 400

    r = get_redis()
    if not r:                     # ---- local fallback ----
        with clicks_cache_lock:
            clicks_cache.setdefault(lid, {'clicks': 0, 'carousel_clicks': 0})
            clicks_cache[lid]['clicks'] += 1
            if source == 'carousel':
                clicks_cache[lid]['carousel_clicks'] += 1
    else:                         # ---- Redis ----
        key = f"clk:{lid}"
        r.hincrby(key, 'clicks', 1)
        if source == 'carousel':
            r.hincrby(key, 'carousel_clicks', 1)

    return jsonify(success=True)

# --------------------------------------------------------------
# 4. FLUSH FUNCTIONS (run inside app context)
def _flush_impressions_redis(r):
    keys = r.keys("imp:*")
    if not keys:
        logging.info("No impression keys in Redis.")
        return

    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1. Get valid listing_ids as INTEGERS
        cur.execute("SELECT listing_id FROM listings")
        valid_ids = {int(row[0]) for row in cur.fetchall()}  # ← int()

        flushed = 0
        skipped = 0
        for key in keys:
            raw_lid = key.decode().split(':', 1)[1]
            try:
                lid = int(raw_lid)  # ← Convert Redis string to int
            except ValueError:
                logging.warning(f"Invalid listing_id in Redis key: {key.decode()}")
                r.delete(key)
                continue

            if lid not in valid_ids:
                logging.debug(f"Skipping impression for deleted listing_id={lid}")
                r.delete(key)
                skipped += 1
                continue

            data = r.hgetall(key)
            impressions = int(data.get(b'impressions', 0))
            carousel = int(data.get(b'carousel_impressions', 0))

            if impressions == 0 and carousel == 0:
                r.delete(key)
                continue

            cur.execute("""
                INSERT INTO listing_metrics (listing_id, impressions, carousel_impressions)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  impressions = impressions + %s,
                  carousel_impressions = carousel_impressions + %s
            """, (lid, impressions, carousel, impressions, carousel))
            r.delete(key)
            flushed += 1

        conn.commit()
        logging.info(f"Flushed {flushed} impression records from Redis. Skipped {skipped} deleted listings.")

    except Exception as e:
        logging.exception("Redis impression flush error")
        if conn: conn.rollback()
    finally:
        if cur: cur.close()
        if conn: conn.close()


def _flush_impressions_memory():
    with cache_lock:
        to_flush = impressions_cache
        impressions_cache = {}

    if not to_flush:
        logging.info("No in-memory impressions to flush.")
        return

    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        for lid, counts in to_flush.items():
            cur.execute("""
                INSERT INTO listing_metrics (listing_id, impressions, carousel_impressions)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  impressions = impressions + %s,
                  carousel_impressions = carousel_impressions + %s
            """, (lid, counts['impressions'], counts['carousel_impressions'],
                  counts['impressions'], counts['carousel_impressions']))
        conn.commit()
        logging.info(f"Flushed {len(to_flush)} in-memory impression records.")
    except Exception as e:
        logging.exception("Memory impression flush error")
        if conn: conn.rollback()
    finally:
        if cur: cur.close()
        if conn: conn.close()


def flush_impressions():
    """Public flush – called by scheduler."""
    global impressions_cache               # <-- ADD THIS LINE
    now = datetime.utcnow()
    logging.info(f"Flushing impressions at {now}")

    r = get_redis()
    if r:
        _flush_impressions_redis(r)
    else:
        _flush_impressions_memory()


def _flush_clicks_redis(r):
    keys = r.keys("clk:*")
    if not keys:
        logging.info("No click keys in Redis.")
        return

    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1. Get valid listing_ids as INTEGERS
        cur.execute("SELECT listing_id FROM listings")
        valid_ids = {int(row[0]) for row in cur.fetchall()}  # ← int()

        # 2. Detect carousel_clicks column
        cur.execute("SHOW COLUMNS FROM listing_metrics LIKE 'carousel_clicks'")
        has_cc = cur.fetchone() is not None

        flushed = 0
        skipped = 0
        for key in keys:
            raw_lid = key.decode().split(':', 1)[1]
            try:
                lid = int(raw_lid)  # ← Convert Redis string to int
            except ValueError:
                logging.warning(f"Invalid listing_id in Redis key: {key.decode()}")
                r.delete(key)
                continue

            if lid not in valid_ids:
                logging.debug(f"Skipping click for deleted listing_id={lid}")
                r.delete(key)
                skipped += 1
                continue

            data = r.hgetall(key)
            clicks = int(data.get(b'clicks', 0))
            carousel = int(data.get(b'carousel_clicks', 0))

            if clicks == 0 and (not has_cc or carousel == 0):
                r.delete(key)
                continue

            if has_cc:
                sql = """
                    INSERT INTO listing_metrics (listing_id, clicks, carousel_clicks)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        clicks = clicks + VALUES(clicks),
                        carousel_clicks = carousel_clicks + VALUES(carousel_clicks)
                """
                cur.execute(sql, (lid, clicks, carousel))
            else:
                sql = """
                    INSERT INTO listing_metrics (listing_id, clicks)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE clicks = clicks + VALUES(clicks)
                """
                cur.execute(sql, (lid, clicks))

            r.delete(key)
            flushed += 1

        conn.commit()
        logging.info(f"Flushed {flushed} click records from Redis. Skipped {skipped} deleted listings.")

    except Exception as e:
        logging.exception("Redis click flush error")
        if conn: conn.rollback()
    finally:
        if cur: cur.close()
        if conn: conn.close()


def _flush_clicks_memory():
    with clicks_cache_lock:
        to_flush = clicks_cache
        clicks_cache = {}

    if not to_flush:
        logging.info("No in-memory clicks to flush.")
        return

    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SHOW COLUMNS FROM listing_metrics LIKE 'carousel_clicks'")
        has_cc = cur.fetchone() is not None

        for lid, counts in to_flush.items():
            clicks = counts['clicks']
            carousel = counts['carousel_clicks']

            if has_cc:
                sql = """
                    INSERT INTO listing_metrics (listing_id, clicks, carousel_clicks)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        clicks = clicks + VALUES(clicks),
                        carousel_clicks = carousel_clicks + VALUES(carousel_clicks)
                """
                cur.execute(sql, (lid, clicks, carousel))
            else:
                sql = """
                    INSERT INTO listing_metrics (listing_id, clicks)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE clicks = clicks + VALUES(clicks)
                """
                cur.execute(sql, (lid, clicks))

        conn.commit()
        logging.info(f"Flushed {len(to_flush)} in-memory click records.")
    except Exception as e:
        logging.exception("Memory click flush error")
        if conn: conn.rollback()
    finally:
        if cur: cur.close()
        if conn: conn.close()


def flush_clicks():
    """Public flush – called by scheduler."""
    global clicks_cache                    # <-- ADD THIS LINE
    now = datetime.utcnow()
    logging.info(f"Flushing clicks at {now}")

    r = get_redis()
    if r:
        _flush_clicks_redis(r)
    else:
        _flush_clicks_memory()





# Add this to your Flask app
@app.template_filter('humanize_number')
def humanize_number(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return value
        
    if value >= 1_000_000:
        return f'{value/1_000_000:.1f}M'
    if value >= 1_000:
        return f'{value/1_000:.1f}K'
    return f'{value:,}'






@app.route('/initiate_payment', methods=['POST'])
@login_required
def initiate_payment():
    try:
        data = request.get_json()
        plan = data.get('plan')
        price = int(data.get('price')) * 100  # ₵ -> pesewas
        listing_id = data.get('listing_id')

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT user_id FROM listings WHERE listing_id = %s", (listing_id,))
        listing = cursor.fetchone()
        if not listing:
            return jsonify({'error': 'Listing not found'}), 404
        if listing['user_id'] != session['user_id']:
            return jsonify({'error': 'Unauthorized'}), 403

        cursor.execute("SELECT email FROM users WHERE id = %s", (session['user_id'],))
        user = cursor.fetchone()
        if not user or not user.get('email'):
            return jsonify({'error': 'User email not found'}), 400

        payload = {
            "email": user['email'],
            "amount": price,
            "metadata": {"plan": plan, "listing_id": listing_id, "user_id": session['user_id']},
            "callback_url": url_for('payment_verification', _external=True)
        }

        headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
        response = requests.post("https://api.paystack.co/transaction/initialize", json=payload, headers=headers)
        resp_json = response.json()

        if resp_json.get("status") and "authorization_url" in resp_json["data"]:
            return jsonify({'authorization_url': resp_json["data"]["authorization_url"]})
        else:
            return jsonify({'error': resp_json.get("message", "Paystack error")}), 400

    except Exception as e:
        print("Payment error:", str(e))
        return jsonify({'error': 'Internal server error: ' + str(e)}), 500
    finally:
        cursor.close()
        conn.close()





@app.route('/payment/verify')
def payment_verification():
    reference = request.args.get('reference')
    
    if not reference:
        flash("Missing payment reference", "error")
        return redirect(url_for('dashboard'))
    
    # Verify payment with Paystack
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    response = requests.get(f"https://api.paystack.co/transaction/verify/{reference}", headers=headers)
    
    if response.status_code == 200:
        data = response.json()
        if data['data']['status'] == 'success':
            metadata = data['data']['metadata']
            
            # Update listing plan in the database
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    UPDATE listings 
                    SET plan = %s 
                    WHERE listing_id = %s AND user_id = %s
                """, (metadata['plan'], metadata['listing_id'], metadata['user_id']))
                conn.commit()
                
                flash(f'Ad is now being promoted with the {metadata["plan"]} plan', 'success')
            except Exception as e:
                conn.rollback()
                flash('Error updating listing plan. Please contact support.', 'error')
            finally:
                cursor.close()
                conn.close()
        else:
            flash('Payment failed or was not completed', 'error')
    else:
        flash('Payment verification failed', 'error')
    
    return redirect(url_for('dashboard'))




@app.route('/promote/<int:listing_id>')
@login_required
def promote_listing(listing_id):
    conn = None
    listing = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        # Verify the listing exists and belongs to the current user
        cursor.execute("SELECT * FROM listings WHERE listing_id = %s AND user_id = %s", 
                       (listing_id, session['user_id']))
        listing = cursor.fetchone()
        cursor.close()
    except Exception as e:
        app.logger.error("Error fetching listing for promotion: %s", e)
    finally:
        if conn:
            conn.close()

    if not listing:
        abort(404)

    # Define available promotion plans and their prices
    plan_prices = {
        "Diamond": 200,
        "Gold": 100,
        "Silver": 50,
        "Bronze": 20
    }
    return render_template('promote.html', listing=listing, plan_prices=plan_prices)




from flask import send_from_directory

@app.route('/service-worker.js')
def service_worker():
    return send_from_directory('static', 'service-worker.js')




@app.route('/subscribe', methods=['POST'])
@login_required
def subscribe():
    app.logger.info("Subscribe called; session user_id=%s", session.get('user_id'))
    sub = request.get_json() or {}
    app.logger.debug("Subscription payload: %s", sub)
    endpoint = sub.get('endpoint')
    keys     = sub.get('keys', {})
    p256dh   = keys.get('p256dh')
    auth_key = keys.get('auth')
    user_id  = session['user_id']

    if not (endpoint and p256dh and auth_key):
        app.logger.warning("Invalid subscription payload: %s", sub)
        return jsonify({'error': 'Invalid subscription'}), 400

    conn   = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT 1 FROM push_subscriptions WHERE user_id=%s AND endpoint=%s",
            (user_id, endpoint)
        )
        if not cursor.fetchone():
            cursor.execute("""
              INSERT INTO push_subscriptions
                (user_id, endpoint, p256dh, auth)
              VALUES (%s, %s, %s, %s)
            """, (user_id, endpoint, p256dh, auth_key))
            conn.commit()
            app.logger.info("Inserted new push subscription for user %s", user_id)
        else:
            app.logger.info("Subscription already exists for user %s", user_id)
    finally:
        cursor.close()
        conn.close()

    return jsonify({'status': 'subscribed'}), 201




@app.route('/privacy-policy')
def privacy_policy():
    """
    Serves the SwapSphere Privacy Policy page.
    Expects: templates/privacy.html
    """
    return render_template('privacy.html')


@app.route('/terms-and-conditions')
def terms_and_conditions():
    """
    Serves the SwapSphere Terms & Conditions page.
    Expects: templates/terms.html
    """
    return render_template('terms.html')



@app.before_request
def load_wishlist_count():
    if 'user_id' in session:
        conn   = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM wishlists WHERE user_id=%s",
            (session['user_id'],)
        )
        session['wishlist_count'] = cursor.fetchone()[0]
        cursor.close()
        conn.close()




@app.route('/api/wishlist/toggle', methods=['POST'])
@login_required
def toggle_wishlist():
    data       = request.get_json() or {}
    listing_id = data.get('listing_id')
    user_id    = session['user_id']
    if not listing_id:
        return jsonify(success=False, error='Missing listing_id'), 400

    conn   = get_db_connection()
    # if your default cursor returns tuples, use a normal cursor here:
    cursor = conn.cursor()

    try:
        # 1) Check if already wishlisted
        cursor.execute(
            "SELECT id FROM wishlists WHERE user_id=%s AND listing_id=%s",
            (user_id, listing_id)
        )
        existing = cursor.fetchone()
        if existing:
            wishlist_id = existing[0]  # tuple’s first element
            cursor.execute(
                "DELETE FROM wishlists WHERE id=%s",
                (wishlist_id,)
            )
            action = 'removed'
        else:
            cursor.execute(
                "INSERT INTO wishlists (user_id, listing_id) VALUES (%s,%s)",
                (user_id, listing_id)
            )
            action = 'added'

        # 2) Get fresh total count
        cursor.execute(
            "SELECT COUNT(*) FROM wishlists WHERE user_id=%s",
            (user_id,)
        )
        count_row = cursor.fetchone()
        total     = count_row[0]  # again, tuple

        conn.commit()
        return jsonify(success=True, action=action, total=total)

    except Exception as e:
        conn.rollback()
        logging.error("Wishlist toggle error: %s", e)
        return jsonify(success=False), 500

    finally:
        cursor.close()
        conn.close()



@app.route('/wishlist')
@login_required
def view_wishlist():
    user_id = session['user_id']
    conn   = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
        SELECT l.*, w.created_at AS wishlisted_at
          FROM wishlists w
          JOIN listings l ON l.listing_id = w.listing_id
         WHERE w.user_id = %s
         ORDER BY w.created_at DESC
    """, (user_id,))
    items = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template('wishlist.html', listings=items)



@app.route('/toggle_wishlist', methods=['POST'])
@login_required
def toggle_wishlist_form():
    # Grab the listing_id from the submitted form
    listing_id = request.form.get('listing_id')
    user_id    = session['user_id']
    if not listing_id:
        flash("No listing specified.", "warning")
        return redirect(url_for('view_wishlist'))

    conn   = get_db_connection()
    cursor = conn.cursor()
    try:
        # Check if already in wishlist
        cursor.execute(
            "SELECT id FROM wishlists WHERE user_id=%s AND listing_id=%s",
            (user_id, listing_id)
        )
        existing = cursor.fetchone()

        if existing:
            # remove
            cursor.execute(
                "DELETE FROM wishlists WHERE id=%s",
                (existing[0],)
            )
        else:
            # add
            cursor.execute(
                "INSERT INTO wishlists (user_id, listing_id) VALUES (%s,%s)",
                (user_id, listing_id)
            )

        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error("Wishlist form-toggle error: %s", e)
        flash("Something went wrong toggling your wishlist.", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('view_wishlist'))




@app.route('/test-push')
def test_push():
    # Hardcoded user_id (your current test user)
    user_id = 1
    send_push(
        user_id,
        "Test Notification",
        "This is a test push from your Flask app.",
        url_for('home')  # or any other URL
    )
    return 'OK'



@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    data = request.get_json()
    visitor_id = data.get('visitorId')
    current_page = data.get('currentPage', '/')
    ip = request.remote_addr
    user_id = session.get('user_id')  # matches users.id
    user_agent = request.headers.get('User-Agent')
    device_info = data.get('deviceInfo', '')
    now = datetime.utcnow()

    if not visitor_id:
        return jsonify(success=False, error='Missing visitor_id'), 400

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO visitor_activity (visitor_id, user_id, last_seen, current_page, ip_address, user_agent, device_info)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE 
            user_id=VALUES(user_id),
            last_seen=VALUES(last_seen),
            current_page=VALUES(current_page),
            ip_address=VALUES(ip_address),
            user_agent=VALUES(user_agent),
            device_info=VALUES(device_info)
    """, (visitor_id, user_id, now, current_page, ip, user_agent, device_info))

    conn.commit()
    cur.close()
    conn.close()

    return jsonify(success=True)

# Active visitors list for table
@app.route('/admin/active_visitors')
def active_visitors():
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    threshold = datetime.utcnow() - timedelta(minutes=5)
    cur.execute("""
        SELECT va.*, u.username, u.name, u.avatar, u.email
        FROM visitor_activity va
        LEFT JOIN users u ON va.user_id = u.id
        WHERE va.last_seen >= %s
    """, (threshold,))
    visitors = cur.fetchall()
    cur.close()
    conn.close()

    return jsonify(visitors)

# Additional endpoint for traffic sources
@app.route('/admin/traffic_sources')
def traffic_sources():
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    # This is simplified - in a real app you'd parse referrers from user_agent or have a separate tracking system
    cur.execute("""
        SELECT 
            CASE 
                WHEN user_agent LIKE '%Twitter%' THEN 'Social Media'
                WHEN user_agent LIKE '%Facebook%' THEN 'Social Media'
                WHEN user_agent LIKE '%Google%' THEN 'Search Engines'
                WHEN user_agent LIKE '%Bing%' THEN 'Search Engines'
                WHEN user_agent LIKE '%Yahoo%' THEN 'Search Engines'
                WHEN user_agent LIKE '%LinkedIn%' THEN 'Social Media'
                WHEN user_agent LIKE '%Mail%' THEN 'Email'
                WHEN user_agent LIKE '%Outlook%' THEN 'Email'
                WHEN user_agent LIKE '%Gmail%' THEN 'Email'
                WHEN referrer IS NULL OR referrer = '' THEN 'Direct'
                ELSE 'Referral'
            END as source,
            COUNT(DISTINCT visitor_id) as count
        FROM visitor_activity
        WHERE last_seen >= DATE_SUB(NOW(), INTERVAL 30 DAY)
        GROUP BY source
        ORDER BY count DESC
    """)
    
    result = cur.fetchall()
    cur.close()
    conn.close()
    
    labels = [row['source'] for row in result]
    values = [row['count'] for row in result]
    
    return jsonify({
        'labels': labels,
        'values': values
    })

# Endpoint for device breakdown
@app.route('/admin/device_breakdown')
def device_breakdown():
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("""
        SELECT 
            CASE 
                WHEN user_agent LIKE '%Mobile%' THEN 'Mobile'
                WHEN user_agent LIKE '%Tablet%' THEN 'Tablet'
                ELSE 'Desktop'
            END as device,
            COUNT(DISTINCT visitor_id) as count
        FROM visitor_activity
        WHERE last_seen >= DATE_SUB(NOW(), INTERVAL 30 DAY)
        GROUP BY device
        ORDER BY count DESC
    """)
    
    result = cur.fetchall()
    cur.close()
    conn.close()
    
    labels = [row['device'] for row in result]
    values = [row['count'] for row in result]
    
    return jsonify({
        'labels': labels,
        'values': values
    })

# Endpoint for top pages
@app.route('/admin/top_pages')
def top_pages():
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("""
        SELECT 
            current_page as page,
            COUNT(*) as views,
            AVG(TIMESTAMPDIFF(SECOND, first_seen, last_seen)) as avg_duration
        FROM (
            SELECT 
                visitor_id,
                current_page,
                MIN(last_seen) as first_seen,
                MAX(last_seen) as last_seen
            FROM visitor_activity
            WHERE last_seen >= DATE_SUB(NOW(), INTERVAL 30 DAY)
            GROUP BY visitor_id, current_page
        ) as sessions
        GROUP BY current_page
        ORDER BY views DESC
        LIMIT 5
    """)
    
    result = cur.fetchall()
    cur.close()
    conn.close()
    
    return jsonify(result)

# Enhanced analytics endpoint
@app.route('/admin/analytics')
def analytics():
    start = request.args.get('start')
    end = request.args.get('end')
    period = request.args.get('period', 'day')  # day, week, month, year

    if not start or not end:
        return jsonify({'error': 'Missing start or end date'}), 400

    try:
        # Parse dates in YYYY-MM-DD format
        start_dt = datetime.strptime(start, '%Y-%m-%d')
        end_dt = datetime.strptime(end, '%Y-%m-%d') + timedelta(days=1)
    except ValueError as e:
        return jsonify({'error': f'Invalid date format. Please use YYYY-MM-DD. Error: {str(e)}'}), 400

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    # Choose SQL grouping by period
    if period == 'week':
        group_by = "YEARWEEK(last_seen, 1)"
        label = "DATE_FORMAT(DATE_ADD(last_seen, INTERVAL(1 - DAYOFWEEK(last_seen)) DAY), '%Y-%m-%d') as date"
    elif period == 'month':
        group_by = "DATE_FORMAT(last_seen, '%Y-%m')"
        label = "DATE_FORMAT(last_seen, '%Y-%m') as date"
    elif period == 'year':
        group_by = "YEAR(last_seen)"
        label = "YEAR(last_seen) as date"
    else:  # default: day
        group_by = "DATE(last_seen)"
        label = "DATE(last_seen) as date"

    # Chart: unique visitors and page views per period
    cur.execute(f"""
        SELECT 
            {label}, 
            COUNT(DISTINCT visitor_id) as unique_visitors,
            COUNT(*) as page_views
        FROM visitor_activity
        WHERE last_seen BETWEEN %s AND %s
        GROUP BY {group_by}
        ORDER BY date
    """, (start_dt, end_dt))
    chart_data = cur.fetchall()

    # Summary metrics for the whole range
    cur.execute("""
        SELECT 
            COUNT(DISTINCT visitor_id) as total_unique,
            COUNT(*) as page_views,
            AVG(session_duration) as avg_session,
            (SUM(CASE WHEN page_count = 1 THEN 1 ELSE 0 END) / COUNT(*)) * 100 as bounce_rate
        FROM (
            SELECT 
                visitor_id,
                COUNT(*) as page_count,
                TIMESTAMPDIFF(SECOND, MIN(last_seen), MAX(last_seen)) as session_duration
            FROM visitor_activity
            WHERE last_seen BETWEEN %s AND %s
            GROUP BY visitor_id
        ) as sessions
    """, (start_dt, end_dt))
    summary = cur.fetchone()

    # Count active visitors
    threshold = datetime.utcnow() - timedelta(minutes=5)
    cur.execute("""
        SELECT COUNT(DISTINCT visitor_id) as active
        FROM visitor_activity
        WHERE last_seen >= %s
    """, (threshold,))
    active = cur.fetchone()['active']

    cur.close()
    conn.close()

    return jsonify({
        'summary': summary,
        'chart': chart_data,
        'active': active
    })





def get_time_left(end_dt):
    now = datetime.utcnow()
    diff = end_dt - now
    if diff.total_seconds() <= 0:
        return "Closed"
    days = diff.days
    hours = diff.seconds // 3600
    return f"{days}d {hours}h"

app.jinja_env.globals.update(get_time_left=get_time_left)



@app.route('/auctions')
def auctions():
    cnx = get_db_connection()
    cur = cnx.cursor(dictionary=True)

    # 1. Distinct categories (only from live items for menu)
    cur.execute("""
        SELECT DISTINCT category
        FROM auction_items
        WHERE category IS NOT NULL
          AND category <> ''
        ORDER BY category
    """)
    categories = [row['category'] for row in cur.fetchall()]

    # 2. Read filters
    selected_cat = request.args.get('category', 'all')
    q = request.args.get('q', '').strip()

    # 3. Build SQL — include both live & closed, sort with CASE
    sql = """
        SELECT
            ai.*,
            COALESCE((
                SELECT MAX(bid_amount) FROM auction_bids b
                WHERE b.auction_item_id = ai.id
            ), ai.starting_bid) AS current_bid,
            (
                SELECT COUNT(*) FROM auction_bids b
                WHERE b.auction_item_id = ai.id
            ) AS bid_count
        FROM auction_items ai
        WHERE 1=1
    """
    params = []

    if selected_cat != 'all':
        sql += " AND ai.category = %s"
        params.append(selected_cat)

    if q:
        sql += " AND (ai.title LIKE %s OR ai.description LIKE %s)"
        like_q = f"%{q}%"
        params.extend([like_q, like_q])

    # Sorting: live first, then closed, each sorted by created_at DESC
    sql += """
        ORDER BY 
            CASE WHEN ai.status = 'live' THEN 0 ELSE 1 END,
            ai.created_at DESC
    """

    cur.execute(sql, params)
    items = cur.fetchall()
    now = datetime.utcnow()

    # Process items safely
    for item in items:
        et = item.get('end_time')

        if not et and item.get('auction_end'):
            if isinstance(item['auction_end'], datetime):
                et = item['auction_end']
            else:
                try:
                    et = datetime.strptime(str(item['auction_end']), "%Y-%m-%d")
                except ValueError:
                    et = None

        if isinstance(et, datetime):
            item['end_time_iso'] = et.isoformat()
            item['is_open'] = (et > now)
        else:
            item['end_time_iso'] = None
            item['is_open'] = False

    cur.close()
    cnx.close()

    return render_template(
        'auction_home.html',
        categories=categories,
        items=items,
        selected_cat=selected_cat,
        q=q,
        current_year=now.year
    )






@app.route('/auctions.json')
def auctions_json():
    """
    Returns a JSON list of live auctions, optionally filtered by:
      - category (exact match)
      - q (substring search on title or description)
    """
    # 1) Grab filters from query string
    selected_cat = request.args.get('category', 'all')
    q            = request.args.get('q', '').strip()

    # 2) Base SQL and params list
    sql = """
        SELECT
          ai.*,
          COALESCE((
            SELECT MAX(bid_amount)
            FROM auction_bids b
            WHERE b.auction_item_id = ai.id
          ), ai.starting_bid) AS current_bid,
          (
            SELECT COUNT(*)
            FROM auction_bids b
            WHERE b.auction_item_id = ai.id
          ) AS bid_count
        FROM auction_items ai
        WHERE ai.status = 'live'
    """
    params = []

    # 3) Category filter
    if selected_cat != 'all':
        sql += " AND ai.category = %s"
        params.append(selected_cat)

    # 4) Text search filter
    if q:
        sql += " AND (ai.title LIKE %s OR ai.description LIKE %s)"
        like_q = f"%{q}%"
        params.extend([like_q, like_q])

    # 5) Final ordering
    sql += " ORDER BY ai.end_time ASC;"

    # 6) Execute
    cnx = get_db_connection()
    cur = cnx.cursor(dictionary=True)
    cur.execute(sql, params)
    items = cur.fetchall()
    cur.close()
    cnx.close()

    # 7) Post‑process each item
    now = datetime.utcnow()
    for item in items:
        # Parse end_time into ISO for client‑side countdown
        et = item.get('end_time')
        if isinstance(et, str):
            try:
                et = datetime.strptime(et, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                et = None
        item['end_time_iso'] = et.isoformat() if et else ''
        # Mark open vs expired
        item['is_open'] = (et and et > now)

    # 8) Return JSON
    return jsonify(items)



@app.route('/black-friday-auctions')
def black_friday_auctions():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT ai.id, ai.title, ai.image1, ai.starting_bid, ai.start_time, ai.end_time, u.username
        FROM auction_items ai
        JOIN users u ON ai.user_id = u.id
        WHERE ai.status = 'live'
        ORDER BY ai.start_time ASC
        LIMIT 10
    """)
    auction_items = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template('black_friday_auctions.html', auction_items=auction_items)





@app.route('/auction/<int:auction_id>', methods=['GET', 'POST'])
def auction_show(auction_id):
    cnx = get_db_connection()
    cur = cnx.cursor(dictionary=True)

    # 1. Fetch item + seller
    cur.execute("""
        SELECT ai.*, u.username AS seller_username
          FROM auction_items ai
          JOIN users u ON ai.user_id = u.id
         WHERE ai.id = %s
    """, (auction_id,))
    item = cur.fetchone()
    if not item:
        cur.close()
        cnx.close()
        return "Auction not found", 404

    # 2. Handle new bid submission
    if request.method == 'POST':
        bid_amount = request.form.get('bid_amount', type=float)
        bidder_id  = session.get('user_id')
        if not bidder_id:
            # Instead of flash(), stash it in session and redirect
            session['post_login_message'] = "Please go ahead and place your bid now"
            return redirect(url_for('login', next=request.url))


        # Insert the bid
        cur.execute("""
            INSERT INTO auction_bids
              (auction_item_id, bidder_id, bid_amount, bid_time)
            VALUES (%s, %s, %s, UTC_TIMESTAMP())
        """, (auction_id, bidder_id, bid_amount))
        cnx.commit()
        flash("Your bid was placed!", "success")
        cur.close()
        cnx.close()
        return redirect(url_for('auction_show', auction_id=auction_id))

    # 3. Compute current bid & count
    cur.execute("""
        SELECT 
            IFNULL(MAX(bid_amount), %s) AS current_bid,
            COUNT(*)            AS bid_count
          FROM auction_bids
         WHERE auction_item_id = %s
    """, (item['starting_bid'], auction_id))
    bd = cur.fetchone()
    item['current_bid'] = bd['current_bid']
    item['bid_count']  = bd['bid_count']

    # 4. Highest single bid & bidder
    cur.execute("""
        SELECT b.bid_amount, b.bid_time, u.username
          FROM auction_bids b
          JOIN users u ON u.id = b.bidder_id
         WHERE b.auction_item_id = %s
         ORDER BY b.bid_amount DESC
         LIMIT 1
    """, (auction_id,))
    highest_bid = cur.fetchone()

    # 5. Recent bids for initial page render
    cur.execute("""
        SELECT
          b.bid_amount,
          b.bid_time,
          u.username,
          DATE_FORMAT(b.bid_time, '%%Y-%%m-%%d %%H:%%i') AS bid_time_display
        FROM auction_bids b
        JOIN users u ON u.id = b.bidder_id
       WHERE b.auction_item_id = %s
       ORDER BY b.bid_time DESC
       LIMIT 10
    """, (auction_id,))
    recent_bids = cur.fetchall()

    # 6. Optional feedback if closed
    feedback = None
    if item['status'] == 'closed':
        cur.execute("""
            SELECT seller_feedback, buyer_feedback
              FROM auction_feedback
             WHERE auction_item_id = %s
        """, (auction_id,))
        feedback = cur.fetchone()

    # 7. ISO timestamp for countdown
    end_time = item['end_time']
    if isinstance(end_time, datetime):
        item['end_time_iso'] = end_time.isoformat()
    else:
        # assume string
        try:
            dt = datetime.strptime(str(end_time), '%Y-%m-%d %H:%M:%S')
            item['end_time_iso'] = dt.isoformat()
        except:
            item['end_time_iso'] = ''

    # 8. Determine if auction has ended
    now = datetime.utcnow()
    is_ended = (item['status'] == 'closed') or (end_time < now)

    cur.close()
    cnx.close()

    return render_template(
        'auction_show.html',
        item=item,
        recent_bids=recent_bids,
        highest_bid=highest_bid,
        feedback=feedback,
        current_year=date.today().year,
        is_ended=is_ended
    )


@app.route('/categories')
def categories():
    cnx = get_db_connection()
    cur = cnx.cursor(dictionary=True)
    # You need a categories table; for now we fetch distinct
    cur.execute("""
      SELECT c.id, c.name, c.image,
        (SELECT COUNT(*) FROM auction_items ai WHERE ai.category_id=c.id AND ai.status='live') AS item_count
      FROM categories c
      ORDER BY c.name
    """)
    cats = cur.fetchall()
    cur.close(); cnx.close()
    return render_template(
      'categories.html',
      categories=cats,
      current_year=now_year()
    )



@app.route('/categories/<int:cat_id>')
def category_show(cat_id):
    cnx = get_db_connection()
    cur = cnx.cursor(dictionary=True)
    # show items in that category
    cur.execute("""
      SELECT ai.*, 
        COALESCE((SELECT MAX(bid_amount) FROM auction_bids b WHERE b.auction_item_id=ai.id), ai.starting_bid) AS current_bid,
        (SELECT COUNT(*) FROM auction_bids b WHERE b.auction_item_id=ai.id) AS bid_count
      FROM auction_items ai
      WHERE ai.category_id=%s AND ai.status='live'
      ORDER BY ai.end_time ASC
    """, (cat_id,))
    items = cur.fetchall()
    for it in items:
        it['end_time_iso'] = it['end_time'].isoformat()
    cur.close(); cnx.close()
    return render_template(
      'auction_home.html',  # reuse grid template
      items=items,
      current_year=now_year()
    )


# Configuration for uploads
UPLOAD_FOLDER = os.path.join('static', 'images')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'avif'}

# Ensure the upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

def allowed_file(filename):
    ext = filename.rsplit('.', 1)[-1].lower()
    return '.' in filename and ext in ALLOWED_EXT



@app.route('/sell', methods=['GET', 'POST'])
def sell():
    if 'user_id' not in session:
        flash("Please log in first", "warning")
        return redirect(url_for('sign_in'))

    if request.method == 'POST':
        conn = cur = None
        try:
            # 1) Pull in all form fields
            title = request.form.get('title', '').strip()
            description = request.form.get('description', '').strip()
            category = request.form.get('category', '').strip()
            item_condition = request.form.get('condition', '').strip()

            # ---------- Money parsing with Decimal (safe) ----------
            def parse_money(s):
                s = (s or '').strip().replace(',', '')  # remove thousands separators
                if s == '':
                    return Decimal('0.00')
                try:
                    d = Decimal(s)
                except InvalidOperation:
                    raise ValueError("Invalid money amount")
                return d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

            try:
                starting_bid = parse_money(request.form.get('starting_bid'))
                reserve_price = parse_money(request.form.get('reserve_price') or '0')
            except ValueError:
                flash("Please enter valid numeric amounts for starting/reserve price.", "danger")
                return redirect(url_for('sell'))

            # 2) Dates & times
            # Accept both 'auction_start' (new) or 'auction_date' (old)
            start_date_str = request.form.get('auction_start') or request.form.get('auction_date')
            end_date_str   = request.form.get('auction_end')   or request.form.get('auction_end_date')
            if not start_date_str or not end_date_str:
                flash("Please provide auction start and end dates.", "danger")
                return redirect(url_for('sell'))

            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date   = datetime.strptime(end_date_str,   '%Y-%m-%d').date()

            # times
            start_time_str = request.form.get('start_time')
            end_time_str   = request.form.get('end_time')
            if not start_time_str or not end_time_str:
                flash("Please provide auction start and end times.", "danger")
                return redirect(url_for('sell'))

            start_time = datetime.strptime(start_time_str, '%H:%M').time()
            end_time   = datetime.strptime(end_time_str,   '%H:%M').time()

            start_dt = datetime.combine(start_date, start_time)
            end_dt   = datetime.combine(end_date,   end_time)

            # validations
            if end_dt <= start_dt:
                flash("End datetime must come after start datetime.", "error")
                return redirect(url_for('sell'))

            span = end_dt - start_dt
            if span < timedelta(hours=24):
                flash("Auctions must run at least 24 hours.", "error")
                return redirect(url_for('sell'))
            if span > timedelta(days=14):
                flash("Auctions can run at most 14 days.", "error")
                return redirect(url_for('sell'))
            if not (8 <= start_dt.hour <= 22 and 8 <= end_dt.hour <= 22):
                flash("Auctions must start/end between 08:00 and 22:00.", "error")
                return redirect(url_for('sell'))

            # 3) Image uploads (initialize array!)
            image_paths = [None, None, None, None]
            uploaded = request.files.getlist('images') or []
            for idx, img in enumerate(uploaded[:4]):
                if img and allowed_file(img.filename):
                    filename = secure_filename(img.filename)
                    dest = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    img.save(dest)
                    image_paths[idx] = filename

            # 4) Insert
            conn = get_db_connection()
            cur = conn.cursor()

            cur.execute("""
                INSERT INTO auction_items (
                    user_id, title, description,
                    image1, image2, image3, image4,
                    starting_bid, reserve_price,
                    auction_start, auction_end,  -- DATE columns
                    start_time, end_time,        -- DATETIME columns
                    status, paid_fee,
                    category, item_condition
                ) VALUES (
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s
                )
            """, (
                session['user_id'], title, description,
                image_paths[0], image_paths[1], image_paths[2], image_paths[3],
                starting_bid, reserve_price,   # Decimal objects accepted by connector
                start_date, end_date,          # DATE
                start_dt, end_dt,              # DATETIME
                'pending', 0,
                category, item_condition
            ))

            conn.commit()
            inserted_id = getattr(cur, 'lastrowid', None)
            print("[DEBUG] inserted id:", inserted_id)

            flash("Your item has been submitted and is pending approval.", "success")
            return redirect(url_for('auctions'))

        except Exception as e:
            # log the full stack so you can see the problem
            import traceback
            print("[ERROR creating auction]", e)
            traceback.print_exc()
            flash(f"Error creating auction: {e}", "danger")
            if conn:
                conn.rollback()
            return redirect(url_for('sell'))

        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()

    # GET
    return render_template('sell.html', current_year=datetime.now().year)



# ─── ABOUT ───────────────────────────────────────────────────────────────────

@app.route('/about')
def about():
    return render_template('about.html', current_year=date.today().year)

# ─── AUTH: LOGIN / REGISTER ──────────────────────────────────────────────────
@app.route('/sign-in', methods=['GET','POST'])
def sign_in():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        pw    = request.form.get('password', '')

        # Fetch user record (including hashed password and verified flag)
        conn = get_db_connection()
        cur  = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT id, password, verified FROM users WHERE email = %s",
            (email,)
        )
        user = cur.fetchone()
        cur.close()
        conn.close()

        # Validate hash and verification
        if user and check_password_hash(user['password'], pw):
            if not user.get('verified'):
                flash("Please verify your email before logging in.", "warning")
                return redirect(url_for('sign_in'))

            session['user_id'] = user['id']
            flash("Signed in successfully", "success")
            return redirect(url_for('auctions'))
        else:
            flash("Invalid credentials", "danger")

    return render_template('login_auction.html')


@app.route('/sign-out')
def sign_out():
    session.clear()
    flash("You have been signed out", "info")
    return redirect(url_for('sign_in'))



@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email    = request.form['email']
        pw       = request.form['password']
        # hash & insert
        pw_hash = hash_password(pw)
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO users (username, email, password)
            VALUES (%s, %s, %s)
        """, (username, email, pw_hash))
        conn.commit()
        cur.close(); conn.close()
        flash("Account created—please sign in", "success")
        return redirect(url_for('sign_in'))
    return render_template('register.html')

# --- password helpers (stub: implement your own) ---
def hash_password(pw):
    import hashlib
    return hashlib.sha256(pw.encode()).hexdigest()

def verify_password(pw, pw_hash):
    import hashlib
    return hashlib.sha256(pw.encode()).hexdigest() == pw_hash




@app.route('/auction_profile', methods=['GET', 'POST'])
def auction_profile():
    if 'user_id' not in session:
        flash("Please log in to view your auction profile", "warning")
        return redirect(url_for('sign_in'))

    user_id = session['user_id']
    cnx = get_db_connection()
    cur = cnx.cursor(dictionary=True)

    if request.method == 'POST':
        auction_id = request.form['auction_id']
        role = request.form['role']
        rated_user_id = request.form['rated_user_id']
        rating = int(request.form['rating'])
        comment = request.form.get('comment', '').strip()

        # Check for duplicate ratings
        cur.execute("""
            SELECT 1 FROM auction_ratings
            WHERE auction_item_id = %s AND rater_id = %s AND role = %s
        """, (auction_id, user_id, role))
        if cur.fetchone():
            flash("You have already rated this.", "warning")
        else:
            # Insert the rating
            cur.execute("""
                INSERT INTO auction_ratings
                  (auction_item_id, rater_id, rated_user_id, role, rating, comment)
                VALUES
                  (%s, %s, %s, %s, %s, %s)
            """, (auction_id, user_id, rated_user_id, role, rating, comment))
            cnx.commit()
            flash("Rating submitted successfully!", "success")
        return redirect(url_for('auction_profile'))

    # Fetch existing ratings
    cur.execute("""
        SELECT auction_item_id, role
        FROM auction_ratings
        WHERE rater_id = %s
    """, (user_id,))
    existing_ratings = {(row['auction_item_id'], row['role']) for row in cur.fetchall()}

    # Fetch bid history
    cur.execute("""
        SELECT 
            b.auction_item_id         AS auction_id,
            ai.title                  AS auction_title,
            ai.status                 AS auction_status,
            MAX(b.bid_amount)         AS bid_amount,
            (
                SELECT b2.bid_time
                FROM auction_bids b2
                WHERE b2.auction_item_id = b.auction_item_id
                  AND b2.bidder_id = b.bidder_id
                ORDER BY b2.bid_time DESC
                LIMIT 1
            )                         AS bid_time,
            ai.user_id                AS seller_id
        FROM auction_bids b
        JOIN auction_items ai
            ON b.auction_item_id = ai.id
        WHERE b.bidder_id = %s
        GROUP BY b.auction_item_id, ai.title, ai.status, ai.user_id
        ORDER BY bid_time DESC
    """, (user_id,))
    bid_history = cur.fetchall()

    # Determine winners
    cur.execute("""
        SELECT b.auction_item_id
        FROM auction_items ai
        JOIN auction_bids b
            ON ai.id = b.auction_item_id
        WHERE ai.status = 'closed'
        GROUP BY b.auction_item_id
        HAVING
            MAX(b.bid_amount) = (
                SELECT MAX(b2.bid_amount)
                FROM auction_bids b2
                WHERE b2.auction_item_id = b.auction_item_id
            )
            AND
            MAX(CASE WHEN b.bidder_id = %s THEN b.bid_amount ELSE NULL END)
              = MAX(b.bid_amount)
    """, (user_id,))
    winners = {row['auction_item_id'] for row in cur.fetchall()}

    for bid in bid_history:
        bid['is_winner'] = (bid['auction_id'] in winners)

    # Fetch listings with winner_id
    cur.execute("""
        SELECT
            ai.*,
            (
              SELECT u.username
              FROM auction_bids bb
              JOIN users u ON bb.bidder_id = u.id
              WHERE bb.auction_item_id = ai.id
              ORDER BY bb.bid_amount DESC
              LIMIT 1
            ) AS winner_username,
            (
              SELECT u.email
              FROM auction_bids bb
              JOIN users u ON bb.bidder_id = u.id
              WHERE bb.auction_item_id = ai.id
              ORDER BY bb.bid_amount DESC
              LIMIT 1
            ) AS winner_email,
            (
              SELECT u.contact
              FROM auction_bids bb
              JOIN users u ON bb.bidder_id = u.id
              WHERE bb.auction_item_id = ai.id
              ORDER BY bb.bid_amount DESC
              LIMIT 1
            ) AS winner_contact,
            (
              SELECT bb.bidder_id
              FROM auction_bids bb
              WHERE bb.auction_item_id = ai.id
              ORDER BY bb.bid_amount DESC
              LIMIT 1
            ) AS winner_id
        FROM auction_items ai
        WHERE ai.user_id = %s
        ORDER BY ai.auction_start DESC, ai.start_time DESC
    """, (user_id,))
    listings = cur.fetchall()

    now = datetime.utcnow()

    for it in listings:
        # Prefer full datetime end_time if present
        et = it.get('end_time')  # DATETIME column
        if et:
            end_dt = et
        else:
            # fallback to auction_end (DATE) if present → treat as end of day
            ae = it.get('auction_end')
            if ae:
                try:
                    # ae may be date object or string
                    if isinstance(ae, str):
                        d = datetime.strptime(ae, "%Y-%m-%d").date()
                    else:
                        d = ae
                    end_dt = datetime.combine(d, dtime(23, 59, 59))
                except Exception:
                    end_dt = None
            else:
                end_dt = None

        # Decide display status
        if end_dt:
            it['display_status'] = 'closed' if end_dt < now else 'live'
        else:
            # if we can't determine end datetime, fall back to DB status
            it['display_status'] = it.get('status', 'pending')

    cur.close()
    cnx.close()

    return render_template(
        'auction_profile.html',
        bid_history=bid_history,
        listings=listings,
        existing_ratings=existing_ratings,
        current_year=date.today().year
    )



# --- Email (OTP) helper ---
SMTP_HOST    = "smtp.gmail.com"
SMTP_PORT    = 587
SENDER_EMAIL = "Derickbill3@gmail.com"
SENDER_PWD   = "bxyw odgw iwvl tpad"

def send_otp(to_email, code):
    subject = "Your SwapHub Verification Code"
    body    = f"Your verification code is: {code}\n\nThis expires in 15 minutes."
    msg     = f"Subject: {subject}\n\n{body}"
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PWD)
        server.sendmail(SENDER_EMAIL, to_email, msg)

# --- Signup route ---
@app.route("/signup_auction", methods=["GET", "POST"])
def signup_auction():
    if request.method == "POST":
        # collect form data
        username         = request.form.get("username", "").strip()
        name             = request.form.get("name", "").strip()
        email            = request.form.get("email", "").strip().lower()
        contact          = request.form.get("contact", "").strip()
        location         = request.form.get("location", "").strip()
        password         = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        # check required fields
        missing = [
            fld for fld, val in
            [("Username", username), ("Full Name", name),
             ("Email", email), ("Contact", contact),
             ("Location", location),
             ("Password", password), ("Confirm Password", confirm_password)]
            if not val
        ]
        if missing:
            flash(f"Missing field(s): {', '.join(missing)}", "danger")
            return render_template("signup_auction.html")

        # enforce password length
        if len(password) < 10:
            flash("Password must be at least 10 characters long.", "danger")
            return render_template("signup_auction.html")

        # confirm passwords match
        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template("signup_auction.html")

        pw_hash = generate_password_hash(password)

        try:
            conn = get_db_connection()
            cur  = conn.cursor()

            # check for existing email
            cur.execute("SELECT 1 FROM users WHERE email = %s LIMIT 1", (email,))
            if cur.fetchone():
                flash("An account with that email already exists.", "warning")
                return render_template("signup_auction.html")

            # insert user (note the added 'location' column)
            cur.execute("""
                INSERT INTO users
                  (username, name, email, contact, location, password, account_status, verified)
                VALUES (%s,%s,%s,%s,%s,%s,'pending',0)
            """, (username, name, email, contact, location, pw_hash))
            user_id = cur.lastrowid

            # create and send OTP
            code       = ''.join(random.choices(string.digits, k=6))
            expires_at = datetime.utcnow() + timedelta(minutes=15)
            cur.execute("""
                INSERT INTO email_verifications (user_id, code, expires_at)
                VALUES (%s,%s,%s)
            """, (user_id, code, expires_at))

            conn.commit()
            send_otp(email, code)

            flash("A verification code has been sent to your e-mail.", "info")
            return redirect(url_for("verify_email", user_id=user_id))

        except Error as e:
            conn.rollback()
            flash("Error creating account: " + str(e), "danger")
        finally:
            cur.close()
            conn.close()

    # GET or on error
    return render_template("signup_auction.html")


    
# --- Verification route ---
@app.route("/verify-email/<int:user_id>", methods=["GET", "POST"])
def verify_email(user_id):
    if request.method == "POST":
        code_sub = request.form.get("code", "").strip()
        conn     = get_db_connection()
        cur      = conn.cursor(dictionary=True)
        cur.execute("""
            SELECT code
              FROM email_verifications
             WHERE user_id = %s
               AND expires_at >= UTC_TIMESTAMP()
             ORDER BY created_at DESC
             LIMIT 1
        """, (user_id,))
        rec = cur.fetchone()

        if rec and rec["code"] == code_sub:
            cur.execute("""
                UPDATE users
                   SET verified = 1, account_status = 'active'
                 WHERE id = %s
            """, (user_id,))
            cur.execute("DELETE FROM email_verifications WHERE user_id = %s", (user_id,))
            conn.commit()
            flash("Your account has been verified! You can now log in.", "success")
            return redirect(url_for("sign_in"))
        else:
            flash("Invalid or expired code. Please try again.", "danger")

        cur.close()
        conn.close()

    return render_template("verify_email.html", user_id=user_id)




def parse_money_str(s):
    s = (s or '').strip().replace(',', '')
    if s == '':
        return Decimal('0.00')
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError("Invalid money amount")
    return d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

@app.route('/auction_edit/<int:auction_id>', methods=['GET', 'POST'])
def auction_edit(auction_id):
    if 'user_id' not in session:
        flash("Please log in to edit auctions.", "warning")
        return redirect(url_for('sign_in'))

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM auction_items WHERE id = %s AND user_id = %s", (auction_id, session['user_id']))
        item = cur.fetchone()

        if not item:
            flash("Auction not found or you do not have permission to edit it.", "danger")
            return redirect(url_for('auction_profile'))

        # allow editing for 'live' but block only 'closed'
        if item.get('status') == 'closed':
            flash("Closed auctions cannot be edited.", "warning")
            return redirect(url_for('auction_profile'))

        if request.method == 'POST':
            # Basic fields
            title = request.form.get('title', '').strip()
            description = request.form.get('description', '').strip()
            category = request.form.get('category', '').strip()
            # frontend uses name="condition" -> we map to DB item_condition
            item_condition = request.form.get('condition', '').strip()
            location = request.form.get('location', '').strip()

            # Money parsing (safe)
            try:
                starting_bid = parse_money_str(request.form.get('starting_bid'))
                reserve_price = parse_money_str(request.form.get('reserve_price') or '0')
            except ValueError:
                flash("Please enter valid numeric amounts for starting/reserve price.", "danger")
                return redirect(url_for('auction_edit', auction_id=auction_id))

            # Dates & times
            try:
                start_date = datetime.strptime(request.form.get('auction_start'), '%Y-%m-%d').date()
                end_date = datetime.strptime(request.form.get('auction_end'), '%Y-%m-%d').date()
                start_time = datetime.strptime(request.form.get('start_time'), '%H:%M').time()
                end_time = datetime.strptime(request.form.get('end_time'), '%H:%M').time()
            except Exception:
                flash("Invalid date/time format. Please use the provided controls.", "danger")
                return redirect(url_for('auction_edit', auction_id=auction_id))

            start_dt = datetime.combine(start_date, start_time)
            end_dt = datetime.combine(end_date, end_time)

            # validations (same as sell)
            if end_dt <= start_dt:
                flash("End datetime must come after start datetime.", "error")
                return redirect(url_for('auction_edit', auction_id=auction_id))
            span = end_dt - start_dt
            if span < timedelta(hours=24):
                flash("Auctions must run at least 24 hours.", "error")
                return redirect(url_for('auction_edit', auction_id=auction_id))
            if span > timedelta(days=14):
                flash("Auctions can run at most 14 days.", "error")
                return redirect(url_for('auction_edit', auction_id=auction_id))
            if not (8 <= start_dt.hour <= 22 and 8 <= end_dt.hour <= 22):
                flash("Auctions must start/end between 08:00 and 22:00.", "error")
                return redirect(url_for('auction_edit', auction_id=auction_id))

            # Images: preserve existing filenames unless removed/replaced
            image_slots = [
                item.get('image1'), item.get('image2'),
                item.get('image3'), item.get('image4')
            ]

            # removals: checkboxes named remove_image_1 .. remove_image_4
            for i in range(4):
                if request.form.get(f'remove_image_{i+1}') in ('1', 'on', 'true'):
                    # optionally delete file from disk here if you want cleanup
                    image_slots[i] = None

            # uploaded replacements: files come in request.files.getlist('images')
            uploaded_files = request.files.getlist('images') or []
            # Fill slots in order with uploaded files — replace first available slot
            upload_idx = 0
            for i in range(4):
                if upload_idx >= len(uploaded_files):
                    break
                f = uploaded_files[upload_idx]
                upload_idx += 1
                if f and allowed_file(f.filename):
                    filename = secure_filename(f.filename)
                    dest = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    f.save(dest)
                    image_slots[i] = filename

            # Update DB
            cur.execute("""
                UPDATE auction_items
                SET title=%s, description=%s,
                    image1=%s, image2=%s, image3=%s, image4=%s,
                    starting_bid=%s, reserve_price=%s,
                    auction_start=%s, auction_end=%s,
                    start_time=%s, end_time=%s,
                    category=%s, item_condition=%s, location=%s
                WHERE id=%s AND user_id=%s
            """, (
                title, description,
                image_slots[0], image_slots[1], image_slots[2], image_slots[3],
                starting_bid, reserve_price,
                start_date, end_date,
                start_dt, end_dt,
                category, item_condition, location,
                auction_id, session['user_id']
            ))
            conn.commit()
            flash("Auction updated successfully.", "success")
            return redirect(url_for('auction_profile'))

        # GET: prepare explicit images dict and render template
        images = {
            'image1': item.get('image1'),
            'image2': item.get('image2'),
            'image3': item.get('image3'),
            'image4': item.get('image4')
        }
        return render_template('auction_edit.html', item=item, images=images, current_year=datetime.now().year)

    finally:
        cur.close()
        conn.close()




@app.route('/auction_delete/<int:auction_id>', methods=['POST'])
def auction_delete(auction_id):
    if 'user_id' not in session:
        flash("Please log in first", "warning")
        return redirect(url_for('sign_in'))

    user_id = session['user_id']
    cnx = get_db_connection()
    cur = cnx.cursor(dictionary=True)

    try:
        cur.execute("SELECT id, status FROM auction_items WHERE id = %s AND user_id = %s", (auction_id, user_id))
        row = cur.fetchone()
        if not row:
            flash("Auction not found or you are not the owner.", "danger")
            return redirect(url_for('auction_profile'))

        if row['status'] in ('live', 'closed'):
            flash("Cannot delete auctions that are live or closed.", "warning")
            return redirect(url_for('auction_profile'))

        # safe delete: delete row (or you can soft-delete by updating status)
        cur.execute("DELETE FROM auction_items WHERE id = %s AND user_id = %s", (auction_id, user_id))
        cnx.commit()
        flash("Auction deleted.", "success")
        return redirect(url_for('auction_profile'))

    except Exception as e:
        cnx.rollback()
        import traceback; traceback.print_exc()
        flash(f"Error deleting auction: {e}", "danger")
        return redirect(url_for('auction_profile'))
    finally:
        cur.close(); cnx.close()






@app.route('/admin/usage')
def admin_usage():
    return render_template('admin_usage.html')



@app.route('/keepalive')
def keepalive():
    return 'OK', 200










# app.py (after you define app and routes)
from apscheduler.schedulers.background import BackgroundScheduler
from jobs import check_ad_performance_alerts

scheduler = BackgroundScheduler()

def _run_with_context(job_func):
    """Wrap any job so it runs inside Flask app context."""
    def wrapper():
        with app.app_context():
            job_func()
    return wrapper

# ------------------------------------------------------------------
# 5a – Existing job (keep exactly as you had it)
# ------------------------------------------------------------------
from jobs import check_ad_performance_alerts

scheduler.add_job(
    check_ad_performance_alerts,
    'interval',
    minutes=2,
    id='ad_metrics_alerts',
    replace_existing=True
)

# ------------------------------------------------------------------
# 5b – Impression / Click flush jobs (wrapped for context)
# ------------------------------------------------------------------
scheduler.add_job(
    _run_with_context(flush_impressions),
    trigger='interval',
    minutes=2,
    id='flush_impressions',
    replace_existing=True,
    next_run_time=datetime.utcnow()
)

scheduler.add_job(
    _run_with_context(flush_clicks),
    trigger='interval',
    minutes=2,
    id='flush_clicks',
    replace_existing=True,
    next_run_time=datetime.utcnow()
)

# ------------------------------------------------------------------
# 5c – Start the scheduler **once** (after app is created)
# ------------------------------------------------------------------
scheduler.start()
logging.info("BackgroundScheduler started with all jobs.")



import atexit
atexit.register(lambda: scheduler.shutdown())

if __name__ == '__main__':
    app.run(debug=True, port=5000)
