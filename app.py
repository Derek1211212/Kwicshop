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
from flask import Flask, render_template, url_for, abort, request, session, redirect, flash, jsonify, current_app, Response
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
import time
import fnmatch
from collections import defaultdict

import cloudinary
import cloudinary.uploader
from twilio.rest import Client
from urllib.parse import quote_plus
import atexit
from slugify import slugify
from flask_login import current_user
import math
from html import escape








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



cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)


@app.context_processor
def inject_user():
    return dict(is_logged_in='user_id' in session)





# ─── 1) Cache Configuration ────────────────────────────────────────────────
cache = Cache(config={
    'CACHE_TYPE': 'SimpleCache',
    'CACHE_DEFAULT_TIMEOUT': 300
})
cache.init_app(app)

# ─── 2) Database Configuration ─────────────────────────────────────────────
dbconfig = {
    "host":        os.getenv('DB_HOST', 'localhost'),
    "user":        os.getenv('DB_USER', 'root'),
    "password":    os.getenv('DB_PASSWORD', ''),
    "database":    os.getenv('DB_DATABASE', ''),
    "port":        int(os.getenv('DB_PORT', 3306)),
    "charset":     'utf8mb4',
    "collation":   'utf8mb4_unicode_ci',
    "use_unicode": True
}

# ─── 3) Determine worker count safely ──────────────────────────────────────
try:
    WEB_CONCURRENCY = int(os.getenv('WEB_CONCURRENCY', '1'))
    if WEB_CONCURRENCY < 1:
        raise ValueError
except ValueError:
    WEB_CONCURRENCY = 1

# ─── 4) SAFE pool limits (MySQL-friendly) ──────────────────────────────────
# Default MySQL max_connections ≈ 151
TOTAL_APP_CONN = int(os.getenv('TOTAL_APP_CONN', '100'))
MAX_PER_POOL   = int(os.getenv('MAX_PER_POOL', '10'))
MIN_PER_POOL   = int(os.getenv('MIN_PER_POOL', '3'))

raw_size  = TOTAL_APP_CONN // WEB_CONCURRENCY
pool_size = max(MIN_PER_POOL, min(raw_size, MAX_PER_POOL))

logger.info(
    f"DB Pool → workers={WEB_CONCURRENCY}, "
    f"raw_per_worker={raw_size}, pool_size={pool_size}"
)

# ─── 5) Create ONE pool per process ────────────────────────────────────────
cnxpool = pooling.MySQLConnectionPool(
    pool_name="mypool",
    pool_size=pool_size,
    pool_reset_session=True,
    **dbconfig
)

# ─── 6) SAFE wrapper connection (routes remain unchanged) ──────────────────
class SafePooledConnection:
    """
    Wraps mysql.connector connection to GUARANTEE
    it returns to the pool even if the developer forgets.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        if self._conn:
            try:
                self._conn.close()  # return to pool
            finally:
                self._conn = None

    def __del__(self):
        self.close()


def get_db_connection():
    """
    RETURNS EXACTLY WHAT YOUR ROUTES EXPECT.
    NO ROUTE CHANGES REQUIRED.
    """
    return SafePooledConnection(cnxpool.get_connection())


# ─── 7) Graceful shutdown cleanup ──────────────────────────────────────────
def shutdown_pool():
    try:
        cnxpool._remove_connections()
    except Exception:
        pass

atexit.register(shutdown_pool)





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





def _get_category_counts(cursor, *, search, deal_type, location):
    """
    Returns dict: { category_name: count }
    Counts respect current filters.
    """

    base_q = """
        SELECT l.category, COUNT(*) AS total
        FROM listings l
        WHERE 1=1
    """
    params = []

    if search:
        like = f"%{search}%"
        base_q += " AND (l.title LIKE %s OR l.description LIKE %s OR l.category LIKE %s)"
        params += [like, like, like]

    if deal_type and deal_type != 'All':
        base_q += " AND l.deal_type = %s"
        params.append(deal_type)

    if location:
        base_q += """
            AND l.location IS NOT NULL
            AND (l.location = %s OR SOUNDEX(l.location) = SOUNDEX(%s) OR l.location LIKE %s)
        """
        params += [location, location, f"%{location}%"]

    base_q += " GROUP BY l.category"

    cursor.execute(base_q, tuple(params))

    return {row['category']: row['total'] for row in cursor.fetchall()}







# -------------------------------
# SEARCH SYNONYMS (GLOBAL)
# -------------------------------
SYNONYMS = {
    # ----- Electronics -----
    "phone": ["iphone", "android", "mobile", "smartphone", "cellphone", "samsung", "xiaomi", "huawei", "oneplus", "nokia", "tecno", "infinix"],
    "tablet": ["ipad", "android tablet", "surface", "galaxy tab", "kindle fire", "note pad", "tab"],
    "tv": ["television", "smart tv", "oled", "led", "lcd", "plasma", "sony tv", "samsung tv", "lg tv", "philips tv", "tcl tv"],
    "laptop": ["notebook", "macbook", "pc", "ultrabook", "chromebook", "dell laptop", "hp laptop", "asus laptop", "lenovo", "acer"],
    "desktop": ["pc", "workstation", "gaming pc", "imac", "all-in-one"],
    "camera": ["dslr", "mirrorless", "canon", "nikon", "sony alpha", "go pro", "action camera", "polaroid", "camera lens", "canon eos", "nikon dslr"],
    "headphones": ["earphones", "earbuds", "airpods", "beats", "sony headphones", "bose", "audio", "headset", "gaming headset"],
    "speaker": ["bluetooth speaker", "home speaker", "bose speaker", "jbl", "sound system", "audio system", "subwoofer"],
    "printer": ["scanner", "all-in-one printer", "hp printer", "canon printer", "epson", "laser printer", "inkjet printer"],

    # ----- Vehicles -----
    "car": ["vehicle", "auto", "automobile", "sedan", "hatchback", "suv", "jeep", "truck", "van", "4x4", "coupe", "mercedes", "bmw", "toyota", "honda", "nissan", "ford", "chevrolet"],
    "motorbike": ["bike", "scooter", "harley", "yamaha", "kawasaki", "duke", "honda bike", "suzuki", "motorcycle"],
    "bicycle": ["bike", "mountain bike", "road bike", "mtb", "cycle", "electrical bike", "ebike", "bmx"],

    # ----- Home Appliances -----
    "fridge": ["refrigerator", "freezer", "lg fridge", "samsung fridge", "whirlpool fridge", "mini fridge", "bar fridge"],
    "washing machine": ["washer", "laundry machine", "front load washer", "top load washer", "bosch washer", "lg washer", "samsung washer"],
    "microwave": ["oven", "microwave oven", "panasonic microwave", "lg microwave", "convection oven"],
    "air conditioner": ["ac", "split ac", "window ac", "haier ac", "lg ac", "cooler", "aircon", "evaporative cooler"],
    "fan": ["ceiling fan", "table fan", "stand fan", "ventilator", "desk fan"],

    # ----- Furniture -----
    "sofa": ["couch", "settee", "divan", "sectional", "love seat", "recliner", "futon"],
    "bed": ["mattress", "bunk bed", "queen bed", "king bed", "single bed", "sofa bed", "cot"],
    "table": ["dining table", "coffee table", "desk", "work table", "study table"],
    "chair": ["armchair", "office chair", "dining chair", "stool", "recliner chair", "bean bag"],

    # ----- Fashion -----
    "shoes": ["sneakers", "trainers", "boots", "heels", "sandals", "footwear", "nike", "adidas", "puma", "reebok", "slippers", "loafer"],
    "clothes": ["apparel", "garments", "t-shirt", "shirt", "pants", "jeans", "dress", "skirt", "hoodie", "jacket", "coat", "sweater", "trousers", "shorts"],
    "bag": ["backpack", "handbag", "purse", "laptop bag", "shoulder bag", "duffel", "tote", "messenger bag", "clutch"],

    # ----- Gaming -----
    "console": ["playstation", "ps5", "xbox", "nintendo switch", "gaming console", "ps4", "xbox series x", "xbox series s", "nintendo wii"],
    "game": ["video game", "xbox game", "ps5 game", "nintendo game", "pc game", "playstation game", "switch game"],

    # ----- Sports & Outdoors -----
    "bicycle gear": ["helmet", "gloves", "lights", "water bottle", "bike pump"],
    "fitness": ["dumbbells", "treadmill", "exercise bike", "yoga mat", "resistance bands", "home gym", "elliptical"],
    "sports equipment": ["football", "soccer ball", "basketball", "tennis racket", "badminton", "golf club", "cricket bat"],

    # ----- Toys & Kids -----
    "toy": ["kids toy", "lego", "action figure", "doll", "board game", "puzzle", "remote control car", "stuffed animal"],
    "baby gear": ["stroller", "car seat", "crib", "high chair", "baby monitor"],

    # ----- Books & Stationery -----
    "book": ["novel", "comic", "textbook", "magazine", "manga", "guide", "manual", "storybook"],
    "stationery": ["pen", "pencil", "notebook", "marker", "folder", "eraser", "highlighter"],

    # ----- Watches & Accessories -----
    "watch": ["smartwatch", "analog watch", "digital watch", "apple watch", "fitbit", "g-shock", "timex"],
    "accessory": ["addon", "extra", "attachment", "peripheral", "gear", "equipment", "belt", "hat", "scarf", "gloves", "jewelry"],

    # ----- Automotive Parts -----
    "car part": ["engine", "brake", "tire", "wheel", "battery", "bumper", "mirror", "headlight", "taillight", "exhaust", "gearbox"],

    # ----- Miscellaneous -----
    "gift": ["present", "surprise", "giveaway", "reward", "item"],
    "home decor": ["painting", "vase", "rug", "curtain", "lamp", "mirror", "clock"],
    "tool": ["drill", "hammer", "screwdriver", "wrench", "pliers", "saw", "toolkit", "hand tool", "power tool"],
    "pet": ["dog", "cat", "bird", "fish", "rabbit", "hamster", "pet food", "pet supplies"],

    # ----- Swap/Trading Generic Terms -----
    "exchange": ["swap", "trade", "barter", "give and take", "trade-in"],
    "wanted": ["desired", "looking for", "need", "required", "interest in"],
}











# 3) The home route
@app.route('/')
def home():
    # ---------------- MODE AWARE REQUEST PARSING ----------------
    mode = request.args.get('mode', 'buy')  # 'buy' or 'swap'

    if mode == 'swap':
        search = request.args.get('want', '').strip()
        have   = request.args.get('have', '').strip()
    else:
        search = request.args.get('search', '').strip()
        have   = ''

    selected_category = request.args.get('category', 'All')
    deal_type_filter  = request.args.get('deal_type', 'All')
    location_q        = request.args.get('location', '').strip()
    page              = max(1, request.args.get('page', 1, type=int))

    DEFAULT_PER_PAGE = 40
    MAX_PER_PAGE     = 200
    per_page = min(MAX_PER_PAGE, request.args.get('per_page', DEFAULT_PER_PAGE, type=int))
    offset   = (page - 1) * per_page

    user_logged_in     = 'user_id' in session
    user_subscribed    = False
    carousel_listings  = []
    listings           = []
    suggestion_listings = []
    show_suggestions   = False
    total_pages        = 0
    category_counts    = {}

    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # 1) Carousel
        carousel_listings = _get_carousel(cursor)

        # ---------------- MAIN LISTINGS (MODE CONTROLLED) ----------------
        if mode == 'swap' and have:
            listings, total_pages = _smart_match_listings(
                cursor,
                have=have,
                want=search,
                per_page=per_page,
                offset=offset
            )
        else:
            listings, total_pages = _get_main_listings(
                cursor,
                search=search,
                category=selected_category,
                deal_type=deal_type_filter,
                location=location_q,
                per_page=per_page,
                offset=offset,
            )

        # ---------------- SUGGESTIONS (MODE AWARE) ----------------
        if not listings and search:
            show_suggestions = True
            suggestion_listings = _get_suggestions(
                cursor,
                search,
                deal_type=('Swap Deal' if mode == 'swap' else None)
            )

        # 4) Compute category counts
        cursor.execute("""
            SELECT category, COUNT(*) as count
            FROM listings
            GROUP BY category
        """)
        category_counts = {row['category']: row['count'] for row in cursor.fetchall()}

        # 5) Attach images & offers (uses listings.image1–image4)
        all_items = listings + suggestion_listings
        _attach_offered_items(cursor, all_items)  # attaches images + offers + defaults

        # 6) Wishlist logic
        if user_logged_in and all_items:
            uid = session['user_id']
            cursor.execute("SELECT 1 FROM push_subscriptions WHERE user_id=%s LIMIT 1", (uid,))
            user_subscribed = cursor.fetchone() is not None

            visible_ids = [x['listing_id'] for x in all_items]
            if visible_ids:
                ph = ",".join(["%s"] * len(visible_ids))
                cursor.execute(
                    f"SELECT listing_id FROM wishlists WHERE user_id=%s AND listing_id IN ({ph})",
                    (uid, *visible_ids),
                )
                wish_ids = {r['listing_id'] for r in cursor.fetchall()}
            else:
                wish_ids = set()

            for item in all_items:
                item['is_wishlisted'] = item['listing_id'] in wish_ids
        else:
            for item in all_items:
                item['is_wishlisted'] = False

        # Featured listing
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
            total_pages=total_pages,
            category_counts=category_counts
        )

    except Exception as e:
        logging.error("Error in home(): %s", e, exc_info=True)
        if conn: conn.rollback()
        raise
    finally:
        if cursor: cursor.close()
        if conn: conn.close()




# ———————————————————————— HELPERS ————————————————————————



def _similarity(a, b):
    if not a or not b:
        return 0.0

    a = a.lower()
    b = b.lower()

    # Token set similarity
    a_tokens = set(a.split())
    b_tokens = set(b.split())

    token_overlap = len(a_tokens & b_tokens) / max(len(a_tokens | b_tokens), 1)

    # Character similarity
    from difflib import SequenceMatcher
    char_sim = SequenceMatcher(None, a, b).ratio()

    return (token_overlap * 0.5) + (char_sim * 0.5)




def _smart_match_listings(cursor, have, want, per_page, offset):
    """
    Two modes:
    - If `have` is provided: fuzzy matching (existing behavior).
    - If only `want` is provided: targeted SQL to RETURN ONLY listings that INVOLVE `want`
      and attach only offered_items that match `want`.
    """
    want = (want or "").strip()
    have = (have or "").strip()

    # ----------------- WANT-ONLY MODE -----------------
    if not have and want:
        like = f"%{want}%"

        count_q = """
            SELECT COUNT(DISTINCT l.listing_id) AS total
            FROM listings l
            WHERE l.deal_type = 'Swap Deal'
              AND (
                    l.desired_swap LIKE %s
                 OR l.title LIKE %s
                 OR l.description LIKE %s
                 OR EXISTS (
                      SELECT 1 FROM offered_items o
                      WHERE o.listing_id = l.listing_id
                        AND (o.title LIKE %s OR o.description LIKE %s)
                 )
              )
        """
        cursor.execute(count_q, (like, like, like, like, like))
        total = cursor.fetchone()['total']
        if not total:
            return [], 0

        page_q = """
            SELECT l.listing_id, l.title, l.description, l.category, l.deal_type, l.`Plan`,
                   l.image_url, l.image1,   # ← added image_url
                   l.price, l.required_cash, l.additional_cash, l.desired_swap,
                   l.location, l.contact, u.username, IFNULL(m.impressions,0) AS impressions,
                   l.created_at
            FROM listings l
            JOIN users u ON l.user_id = u.id
            LEFT JOIN listing_metrics m ON l.listing_id = m.listing_id
            WHERE l.deal_type = 'Swap Deal'
              AND (
                    l.desired_swap LIKE %s
                 OR l.title LIKE %s
                 OR l.description LIKE %s
                 OR EXISTS (
                      SELECT 1 FROM offered_items o
                      WHERE o.listing_id = l.listing_id
                        AND (o.title LIKE %s OR o.description LIKE %s)
                 )
              )
            ORDER BY
              CASE l.`Plan`
                WHEN 'Diamond' THEN 5
                WHEN 'Gold' THEN 4
                WHEN 'Silver' THEN 3
                WHEN 'Bronze' THEN 2
                ELSE 1
              END DESC,
              l.created_at DESC
            LIMIT %s OFFSET %s
        """
        cursor.execute(page_q, (like, like, like, like, like, per_page, offset))
        listings = cursor.fetchall()

        if listings:
            ids = [l['listing_id'] for l in listings]
            ph = ",".join(["%s"] * len(ids))

            cursor.execute(
                f"""
                SELECT *
                FROM offered_items
                WHERE listing_id IN ({ph})
                  AND (title LIKE %s OR description LIKE %s)
                """,
                tuple(ids) + (like, like)
            )
            rows = cursor.fetchall()
            offers = {}
            for r in rows:
                offers.setdefault(r['listing_id'], []).append(r)

            for l in listings:
                l['offers'] = offers.get(l['listing_id'], [])
                l.setdefault('required_cash', 0)
                l.setdefault('additional_cash', 0)
                l.setdefault('desired_swap', l.get('desired_swap') or '')
                l.setdefault('price', l.get('price', 0))
                l.setdefault('location', l.get('location', ''))
                l.setdefault('contact', l.get('contact', ''))

        total_pages = (total + per_page - 1) // per_page
        return listings, total_pages

    # ----------------- FUZZY MODE (UNCHANGED LOGIC) -----------------
    base_q = """
        SELECT l.listing_id, l.title, l.description, l.category, l.deal_type, l.Plan,
               l.image_url, l.image1,   # ← added image_url
               l.price, l.required_cash, l.additional_cash, l.desired_swap,
               l.location, l.contact, u.username, l.created_at,
               IFNULL(m.impressions,0) AS impressions
        FROM listings l
        JOIN users u ON l.user_id = u.id
        LEFT JOIN listing_metrics m ON l.listing_id = m.listing_id
        WHERE l.deal_type = 'Swap Deal'
    """
    cursor.execute(base_q)
    raw = cursor.fetchall()

    offered_map = {}
    if raw:
        ids = [r["listing_id"] for r in raw]
        ph = ",".join(["%s"] * len(ids))
        cursor.execute(f"SELECT * FROM offered_items WHERE listing_id IN ({ph})", tuple(ids))
        for o in cursor.fetchall():
            offered_map.setdefault(o["listing_id"], []).append(o)

    import datetime
    now = datetime.datetime.now()
    plan_weights = {"Diamond": 5, "Gold": 4, "Silver": 3, "Bronze": 2, "Free": 1}
    scored = []

    for l in raw:
        have_score = 0.0
        if have:
            have_score = max(have_score, _similarity(have, l.get("title", "")))
            have_score = max(have_score, _similarity(have, l.get("description", "") or ""))
            for item in offered_map.get(l["listing_id"], []):
                t = f"{item.get('title','')} {item.get('description','')}"
                have_score = max(have_score, _similarity(have, t))

        want_score = _similarity(want, l.get("desired_swap") or "") if want else 0.0
        plan_score = plan_weights.get(l.get("Plan"), 1) / 5.0
        days_old = (now - l.get("created_at", now)).days if l.get("created_at") else 365
        recency_score = max(0, 1 - (days_old / 30))
        l["match_score"] = round((have_score*0.45 + want_score*0.45 + plan_score*0.07 + recency_score*0.03) * 100)
        l['offers'] = offered_map.get(l['listing_id'], [])
        scored.append(l)

    scored.sort(key=lambda x: x['match_score'], reverse=True)
    total_pages = (len(scored) + per_page - 1) // per_page
    return scored[offset:offset + per_page], total_pages









def _get_carousel(cursor):
    cursor.execute("""
        SELECT listing_id, image_url, title, `Plan`
        FROM listings
        ORDER BY created_at DESC
        LIMIT 20
    """)
    raw = cursor.fetchall()
    PLAN_WEIGHTS = {'Diamond':5,'Gold':4,'Silver':3,'Bronze':2,'Free':1}
    weighted = [i for i, r in enumerate(raw) for _ in range(PLAN_WEIGHTS.get(r['Plan'],1))]
    if not weighted:
        return []

    jitter = random.randrange(len(weighted))
    offset = (read_offset() + jitter) % len(weighted)
    write_offset((offset + 1) % len(weighted))

    seen, ordered = set(), []
    for idx in weighted[offset:] + weighted[:offset]:
        if idx not in seen:
            seen.add(idx)
            ordered.append(raw[idx])
        if len(ordered) == 5:
            break

    for c in ordered:
        raw_image = c.get('image_url')
        if raw_image and raw_image.startswith('http'):
            c['banner_image'] = raw_image
        else:
            name = raw_image or 'placeholder.jpg'
            c['banner_image'] = url_for('static', filename=f'images/{name}')

    return ordered



def _get_main_listings(cursor, search, category, deal_type, location, per_page, offset):
    import re

    base_q = """
        SELECT l.listing_id, l.title, l.description, l.category, l.deal_type, l.`Plan`,
               l.image_url, l.image1, l.image2, l.image3, l.image4,   # ← added image_url
               l.price, l.required_cash, l.additional_cash,
               l.desired_swap, l.desired_swap_description, l.location, l.contact, u.username,
               IFNULL(m.impressions,0) AS impressions
        FROM listings l
        JOIN users u ON l.user_id = u.id
        LEFT JOIN listing_metrics m ON l.listing_id = m.listing_id
        WHERE 1=1
    """
    params = []

    # ---------------- SEARCH TOKENS ----------------
    token_clauses = []
    if search:
        tokens = [t for t in re.split(r'\s+', search.strip()) if len(t) > 0]
        for t in tokens:
            like = f"%{t}%"
            token_clauses.append(
                "(l.title LIKE %s OR l.description LIKE %s OR l.category LIKE %s "
                "OR l.desired_swap LIKE %s OR l.desired_swap_description LIKE %s)"
            )
            params += [like, like, like, like, like]

    if token_clauses:
        base_q += " AND (" + " OR ".join(token_clauses) + ")"

    if category != 'All':
        base_q += " AND l.category=%s"
        params.append(category)

    if deal_type and deal_type != 'All':
        base_q += " AND l.deal_type=%s"
        params.append(deal_type)

    if location:
        base_q += " AND l.location IS NOT NULL AND (l.location=%s OR SOUNDEX(l.location)=SOUNDEX(%s) OR l.location LIKE %s)"
        params += [location, location, f"%{location}%"]

    plan_case = "CASE l.`Plan` WHEN 'Diamond' THEN 5 WHEN 'Gold' THEN 4 WHEN 'Silver' THEN 3 ELSE 1 END"
    order = f"ORDER BY {plan_case} DESC, l.created_at DESC"

    # ---------------- COUNT QUERY ----------------
    count_q = "SELECT COUNT(*) AS total FROM listings l WHERE 1=1"
    count_params = []

    if token_clauses:
        count_q += " AND (" + " OR ".join(token_clauses) + ")"
        count_params += params[:len(token_clauses) * 5]  # 5 placeholders per token

    # Add remaining filters
    idx = len(count_params)
    if category != 'All':
        count_q += " AND l.category=%s"
        count_params.append(category)
    if deal_type and deal_type != 'All':
        count_q += " AND l.deal_type=%s"
        count_params.append(deal_type)
    if location:
        count_q += " AND l.location IS NOT NULL AND (l.location=%s OR SOUNDEX(l.location)=SOUNDEX(%s) OR l.location LIKE %s)"
        count_params += [location, location, f"%{location}%"]

    # Execute count query
    cursor.execute(count_q, tuple(count_params))
    total = cursor.fetchone()['total']

    # ---------------- FETCH LISTINGS ----------------
    cursor.execute(base_q + " " + order + " LIMIT %s OFFSET %s", tuple(params + [per_page, offset]))
    listings = cursor.fetchall()
    listings = _rotate_weighted(listings)

    total_pages = (total + per_page - 1) // per_page
    return listings, total_pages







import re
def _get_suggestions(cursor, search, deal_type=None):
    import re
    listings = []
    tokens = [t for t in re.split(r'\s+', search) if len(t) > 1]
    if tokens:
        conds, params = [], []
        for t in tokens:
            like = f"%{t}%"
            conds.append(
                "(l.title LIKE %s OR l.description LIKE %s OR l.category LIKE %s "
                "OR l.desired_swap LIKE %s OR l.desired_swap_description LIKE %s)"
            )
            params += [like, like, like, like, like]

        where_clause = " OR ".join(conds)
        q = f"""
            SELECT l.listing_id, l.title, l.description, l.category, l.deal_type, l.`Plan`,
                   l.image_url, l.image1, l.image2, l.image3, l.image4,   # ← added image_url
                   l.price, l.required_cash, l.additional_cash, l.desired_swap,
                   l.location, l.contact, u.username, IFNULL(m.impressions,0) AS impressions
            FROM listings l
            JOIN users u ON l.user_id = u.id
            LEFT JOIN listing_metrics m ON l.listing_id = m.listing_id
            WHERE {where_clause}
        """
        if deal_type and deal_type != 'All':
            q += " AND l.deal_type=%s"
            params.append(deal_type)

        q += " ORDER BY m.impressions DESC, l.created_at DESC LIMIT 24"
        cursor.execute(q, tuple(params))
        listings = cursor.fetchall()
        _attach_offered_items(cursor, listings)

    if not listings:
        # fallback top listings
        q = """
            SELECT l.listing_id, l.title, l.description, l.category, l.deal_type, l.`Plan`,
                   l.image1, l.image2, l.image3, l.image4,
                   l.price, l.required_cash, l.additional_cash, l.desired_swap,
                   l.location, l.contact, u.username, IFNULL(m.impressions,0) AS impressions
            FROM listings l
            JOIN users u ON l.user_id = u.id
            LEFT JOIN listing_metrics m ON l.listing_id = m.listing_id
        """
        if deal_type and deal_type != 'All':
            q += " WHERE l.deal_type=%s ORDER BY m.impressions DESC, l.created_at DESC LIMIT 24"
            cursor.execute(q, (deal_type,))
        else:
            q += " ORDER BY m.impressions DESC, l.created_at DESC LIMIT 24"
            cursor.execute(q)
        listings = cursor.fetchall()
        _attach_offered_items(cursor, listings)

    return listings







def _rotate_weighted(listings):
    if not listings:
        return []
    weights = {'Diamond':5,'Gold':4,'Silver':3,'Free':1}
    idxs = [i for i, l in enumerate(listings) for _ in range(weights.get(l['Plan'],1))]
    if not idxs:
        return listings
    jitter = random.randrange(len(idxs))
    offset = (read_offset() + jitter) % len(idxs)
    write_offset((offset + 1) % len(idxs))
    seen, out = set(), []
    for i in idxs[offset:] + idxs[:offset]:
        if i not in seen:
            seen.add(i)
            out.append(listings[i])
        if len(out) == len(listings):
            break
    return out


def _attach_offered_items(cursor, listings):
    """
    Attaches offered items and main images to listings.
    Previously relied on a non-existent `listing_images` table.
    Now uses `image1`–`image4` columns in `listings`.
    """
    if not listings:
        return {}

    # Map listing_id → offers
    listing_ids = [l['listing_id'] for l in listings]
    ph = ",".join(["%s"] * len(listing_ids))

    # Fetch offered items
    cursor.execute(f"SELECT * FROM offered_items WHERE listing_id IN ({ph})", tuple(listing_ids))
    offered_rows = cursor.fetchall()
    offers_map = {}
    for o in offered_rows:
        offers_map.setdefault(o['listing_id'], []).append(o)

    # Attach images
    for l in listings:
        # Determine main image
        l['image_url'] = l.get('image_url') or l.get('image1') or l.get('image2') or l.get('image3') or l.get('image4') \
                         or url_for('static', filename='images/placeholder.jpg')
        # Attach offered items
        l['offers'] = offers_map.get(l['listing_id'], [])
        # Safe defaults
        l.setdefault('required_cash', 0)
        l.setdefault('additional_cash', 0)
        l.setdefault('desired_swap', l.get('desired_swap') or '')
        l.setdefault('price', l.get('price', 0))
        l.setdefault('location', l.get('location', ''))
        l.setdefault('contact', l.get('contact', ''))

    return offers_map





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

        # Step 4: Fetch similar listings by category (exclude current listing)
        app.logger.debug("Fetching similar listings by category")
        cursor.execute("""
            SELECT l.listing_id, l.title, l.description, l.category,
                   l.image_url, l.image1, l.image2, l.image3, l.image4,
                   l.price, l.deal_type, l.condition, l.location,
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






# ====== Twilio client (for phone OTP) ======
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_VERIFY_SERVICE_SID = os.getenv("TWILIO_VERIFY_SERVICE_SID")

twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


# ====== Regex + helpers reused in signup ======
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE_RE = re.compile(r"^[0-9 \-]{6,}$")


def _clean(s):
    return (s or "").strip()


# Alias to keep your older helper name happy
def get_db_conn():
    return get_db_connection()


# ====== User helpers ======
def normalize_contact(country_code: str, phone_number: str) -> str:
    """
    Normalizes to the format you used in create_user:
    contact = country_code + phone_without_leading_zero
    """
    phone_number = (phone_number or "").strip()
    phone_number = "".join(phone_number.split())  # remove spaces

    if phone_number.startswith("0"):
        phone_number = phone_number[1:]

    return (country_code or "") + phone_number


def get_user_by_contact(contact: str):
    """Lookup a user by 'contact' (phone). Returns dict or None."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, contact, account_status FROM users WHERE contact = %s LIMIT 1",
            (contact,),
        )
        return cursor.fetchone()
    except Exception as e:
        logging.error(f"Error fetching user by contact {contact}: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def authenticate_user(email, password):
    """Securely verify user credentials against DB using password hash."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            """
            SELECT id, email, password, account_status
            FROM users
            WHERE email = %s
            """,
            (email,),
        )
        user = cursor.fetchone()

        if not user:
            return None

        # Check account status
        if user.get("account_status") == "Suspended":
            return "suspended"

        if user and check_password_hash(user["password"], password):
            # Return minimal safe subset
            return {
                "id": user["id"],
                "email": user["email"],
                "account_status": user.get("account_status"),
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
            INSERT INTO users (username, email, contact, password, role, created_at, account_status)
            VALUES (%s, %s, %s, %s, 'Customer', NOW(), 'Active')
        """
        username = form["username"]
        email = form["email"]
        country_code = form["country_code"]
        phone_number = form["phone_number"].strip()

        contact = normalize_contact(country_code, phone_number)

        hashed_password = generate_password_hash(form["password"])

        cursor.execute(sql, (username, email, contact, hashed_password))
        conn.commit()

        user_id = cursor.lastrowid
        return {"id": user_id, "username": username, "email": email, "contact": contact}

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


def _flash_duplicate_reason(email, username, phone_number=None):
    """
    Check for duplicate email/username/contact and flash specific message.
    country_code removed - no longer used.
    Returns True if duplicate found/flashed, False otherwise.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Email check (priority)
        if email:
            cursor.execute("SELECT 1 FROM users WHERE email = %s LIMIT 1", (email,))
            if cursor.fetchone():
                flash("An account with this email already exists.", "error")
                return True

        # Username check
        if username:
            cursor.execute("SELECT 1 FROM users WHERE username = %s LIMIT 1", (username,))
            if cursor.fetchone():
                flash("Username is already taken.", "error")
                return True

        # Contact / phone check
        if phone_number:
            cursor.execute("SELECT 1 FROM users WHERE contact = %s LIMIT 1", (phone_number,))
            if cursor.fetchone():
                flash("This phone number is already registered.", "error")
                return True

        # No duplicates
        return False

    except Exception as e:
        logging.exception("Error in duplicate check")
        flash("Error checking for existing account. Please try again.", "error")
        return True  # block insert on error

    finally:
        cursor.close()
        conn.close()


# ====== Login required decorator ======
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to access this page", "danger")
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)

    return decorated_function


# =====================================================
#                      LOGIN (EMAIL)
# =====================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    # Determine where to go after login: first from query, then form, then home
    next_url = request.args.get("next") or request.form.get("next") or url_for("home")

    # Initialize or retrieve the per-email failure counts
    session.setdefault("failed_logins", {})

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Please enter both email and password", "danger")
            return redirect(url_for("login", next=next_url))

        # Authenticate credentials & account status
        user = authenticate_user(email, password)
        if user == "suspended":
            flash(
                "Your account is suspended. Please email swapsphere@gmail.com to request reactivation.",
                "danger",
            )
            return redirect(url_for("login", next=next_url))

        if user:
            # Successful login
            session["failed_logins"].pop(email, None)
            session["user_id"] = user["id"]
            session.permanent = True

            # If there's a post-login message, flash it now
            post_msg = session.pop("post_login_message", None)
            if post_msg:
                flash(post_msg, "success")

            logging.info(f"User {user['id']} logged in successfully")
            return redirect(next_url)
        else:
            # Failed login: increment count
            failed = session["failed_logins"].get(email, 0) + 1
            session["failed_logins"][email] = failed
            logging.warning(f"Failed login attempt {failed} for email: {email}")

            # Provide warnings or suspend as needed
            if failed == 3:
                flash(
                    "Warning: One more failed attempt will lock your account.",
                    "warning",
                )
            elif failed >= 4:
                try:
                    conn = get_db_connection()
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE users SET account_status = 'Suspended' WHERE email = %s",
                        (email,),
                    )
                    conn.commit()
                    cur.close()
                    conn.close()
                    logging.warning(
                        f"User account suspended due to repeated failures: {email}"
                    )
                except Exception as e:
                    logging.error(f"Error suspending account {email}: {e}")
                flash(
                    "Your account has been suspended due to multiple failed login attempts. "
                    "Please email swapsphere@gmail.com to request reactivation.",
                    "danger",
                )
            else:
                flash("Invalid email or password", "danger")

        # On any failure, stay on login
        return redirect(url_for("login", next=next_url))

    # GET request: render form.
    return render_template("login.html", next_url=next_url)



print("DEBUG SID:", os.getenv("TWILIO_ACCOUNT_SID"))
print("DEBUG TOKEN:", os.getenv("TWILIO_AUTH_TOKEN"))
print("DEBUG VERIFY:", os.getenv("TWILIO_VERIFY_SERVICE_SID"))




# =====================================================
#                PHONE LOGIN (TWILIO) STEP 1
# =====================================================
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


# =====================================================
#                PHONE LOGIN (TWILIO) STEP 2
# =====================================================
@app.route("/login/phone/verify", methods=["GET", "POST"])
def login_phone_verify():
    contact = session.get("phone_login_contact")
    next_url = session.get("phone_login_next", url_for("home"))

    if not contact:
        flash("Please start by entering your phone number.", "danger")
        return redirect(url_for("login_phone", next=next_url))

    if request.method == "POST":
        code = _clean(request.form.get("code"))

        if not code:
            flash("Please enter the verification code.", "danger")
            return redirect(url_for("login_phone_verify"))

        if not twilio_client or not TWILIO_VERIFY_SERVICE_SID:
            flash(
                "Phone login is currently unavailable. Please use email/password.",
                "danger",
            )
            return redirect(url_for("login", next=next_url))

        try:
            to_number = contact
            if not to_number.startswith("+"):
                to_number = "+" + to_number

            result = twilio_client.verify.v2.services(
                TWILIO_VERIFY_SERVICE_SID
            ).verification_checks.create(to=to_number, code=code)

            logging.info(f"Twilio verification check for {to_number}: status={result.status}")

            if result.status != "approved":
                flash("Invalid or expired verification code. Please try again.", "danger")
                return redirect(url_for("login_phone_verify"))

            # Approved → log user in
            user = get_user_by_contact(contact)
            if not user:
                flash("We couldn't find that account anymore.", "danger")
                return redirect(url_for("login_phone"))

            if user.get("account_status") == "Suspended":
                flash(
                    "Your account is suspended. Please email swapsphere@gmail.com to request reactivation.",
                    "danger",
                )
                return redirect(url_for("login_phone"))

            session["user_id"] = user["id"]
            session.permanent = True

            session.pop("phone_login_contact", None)
            session.pop("phone_login_next", None)

            logging.info(f"User {user['id']} logged in via phone.")
            return redirect(next_url)

        except Exception as e:
            logging.error(f"Error verifying OTP for {contact}: {e}")
            flash("We couldn't verify that code. Please try again.", "danger")
            return redirect(url_for("login_phone_verify"))

    return render_template("login_phone_verify.html", next_url=next_url)


# =====================================================
#                 GOOGLE OAUTH LOGIN
#       (assumes you configured `google` client)
# =====================================================

oauth = OAuth(app)

google = oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    access_token_url="https://oauth2.googleapis.com/token",
    api_base_url="https://www.googleapis.com/oauth2/v2/",
    client_kwargs={
        "scope": "email profile"
    }
)




def get_or_create_user(google_id, email, username, avatar=None):
    conn = get_db_connection()
    try:
        cur = conn.cursor(dictionary=True)

        # 1️⃣ Try finding user by google_id
        cur.execute("SELECT * FROM users WHERE google_id = %s", (google_id,))
        user = cur.fetchone()
        if user:
            return user

        # 2️⃣ Try finding user by email
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cur.fetchone()

        if user:
            # Link Google account
            cur.execute(
                "UPDATE users SET google_id = %s, avatar = COALESCE(avatar, %s) WHERE id = %s",
                (google_id, avatar, user["id"]),
            )
            conn.commit()
            cur.execute("SELECT * FROM users WHERE id = %s", (user["id"],))
            return cur.fetchone()

        # 3️⃣ New user – create
        cur.execute(
            """
            INSERT INTO users
              (google_id, email, username, avatar, role, password, created_at, account_status)
            VALUES (%s, %s, %s, %s, 'Customer', NULL, NOW(), 'Active')
            """,
            (google_id, email, username, avatar),
        )
        conn.commit()
        new_id = cur.lastrowid
        cur.execute("SELECT * FROM users WHERE id = %s", (new_id,))
        return cur.fetchone()

    finally:
        cur.close()
        conn.close()



@app.route("/login/google")
def google_login():
    next_url = request.args.get("next")
    session["oauth_next"] = next_url

    redirect_uri = url_for("google_callback", _external=True)
    return google.authorize_redirect(redirect_uri)




@app.route("/login/google/callback")
def google_callback():
    try:
        token = google.authorize_access_token()
        resp = google.get("userinfo")
        userinfo = resp.json()

        if not userinfo or "id" not in userinfo:
            abort(400, "Google authentication failed")

        google_id = userinfo["id"]
        email = userinfo.get("email")
        name = userinfo.get("name")
        avatar = userinfo.get("picture")

        user = get_or_create_user(
            google_id=google_id,
            email=email,
            username=name,
            avatar=avatar
        )

        session["user_id"] = user["id"]
        session.permanent = True

        return redirect(url_for("home"))
    except Exception as e:
        current_app.logger.error(f"Google login error: {str(e)}")
        flash("An error occurred during login. Please try again.", "error")
        return redirect(url_for("login"))








SECURITY_QUESTIONS = [
    "What was the name of your first pet?",
    "In what city were you born?",
    "What is your mother's maiden name?",
    "What was your first school name?",
    "What is the name of your favorite childhood teacher?",
    "What was your first car model?",
]



@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username       = _clean(request.form.get("username"))
        email          = _clean(request.form.get("email")).lower()
        country_code   = _clean(request.form.get("country_code"))          # for normalization only
        raw_phone      = _clean(request.form.get("phone_number"))
        password       = request.form.get("password") or ""
        sec_question   = request.form.get("security_question", "").strip()
        sec_answer     = request.form.get("security_answer", "").strip().lower()

        # Normalize phone
        digits = ''.join(filter(str.isdigit, raw_phone))
        if digits.startswith('0'):
            digits = digits[1:]
        full_contact = f"+{country_code.replace('+', '')}{digits}" if country_code and digits else digits

        # Validation
        errors = []
        if len(username) < 3:
            errors.append("Username must be at least 3 characters.")
        if not EMAIL_RE.match(email):
            errors.append("Please enter a valid email address.")
        if not country_code:
            errors.append("Please select your country code.")
        if not digits or len(digits) < 7:
            errors.append("Phone number looks invalid or too short.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if not sec_question or sec_question not in SECURITY_QUESTIONS:
            errors.append("Please select a valid security question.")
        if not sec_answer or len(sec_answer) < 3:
            errors.append("Security answer must be at least 3 characters.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "signup.html",
                form_prefill=dict(
                    username=username,
                    email=email,
                    country_code=country_code,
                    phone_number=raw_phone,
                ),
                questions=SECURITY_QUESTIONS,
            )

        hashed_pw  = generate_password_hash(password)
        hashed_ans = generate_password_hash(sec_answer)

        conn = None
        cursor = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO users (
                    username, email, contact, password,
                    security_question, security_answer_hash
                ) VALUES (%s, %s, %s, %s, %s, %s)
            """, (username, email, full_contact, hashed_pw,
                  sec_question, hashed_ans))

            conn.commit()

            flash("Account created successfully! Please log in.", "success")
            return redirect(url_for("login"))

        except mysql.connector.Error as err:
            if conn:
                conn.rollback()
            if getattr(err, "errno", None) == 1062:
                # This call now works after updating the function
                _flash_duplicate_reason(email, username, phone_number=full_contact)
            else:
                flash(f"Database error: {err}", "error")
            return render_template(
                "signup.html",
                form_prefill=dict(
                    username=username,
                    email=email,
                    country_code=country_code,
                    phone_number=raw_phone,
                ),
                questions=SECURITY_QUESTIONS,
            )

        except Exception as ex:
            if conn:
                conn.rollback()
            flash(f"Unexpected error: {ex}", "error")
            return render_template(
                "signup.html",
                form_prefill=dict(
                    username=username,
                    email=email,
                    country_code=country_code,
                    phone_number=raw_phone,
                ),
                questions=SECURITY_QUESTIONS,
            )

        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    return render_template("signup.html", questions=SECURITY_QUESTIONS)






@app.route('/logout', methods=['GET', 'POST'])
def logout():
    session.pop('user_id', None)
    return redirect(url_for('home'))






def send_email_notification(recipient_email, subject, body):
    """
    Send email using MailerSend's SMTP relay (TLS on port 587).
    Returns True on success, False on failure. Does not raise.
    """

    if not recipient_email:
        app.logger.warning("send_email_notification called without recipient_email")
        return False

    # ==============================
    # 🔐 MAILERSEND SMTP SETTINGS
    # ==============================
    SMTP_SERVER = os.environ.get("MAILERSEND_SMTP_HOST", "smtp.mailersend.net")
    SMTP_PORT = int(os.environ.get("MAILERSEND_SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("MAILERSEND_SMTP_USER")       # e.g. MS_xxx...
    SMTP_PASSWORD = os.environ.get("MAILERSEND_SMTP_PASSWORD")

    if not SMTP_USER or not SMTP_PASSWORD:
        app.logger.error("MailerSend SMTP credentials are not set in environment")
        return False

    # ==============================
    # 📧 SENDER ADDRESS
    # ==============================
    # Must be a verified/allowed sender in your MailerSend domain.
    sender_email = app.config.get("FROM_EMAIL", os.environ.get("FROM_EMAIL", "no-reply@test-51ndgwvkvwqlzqx8.mlsender.net"))
    sender_name = app.config.get("FROM_NAME", os.environ.get("FROM_NAME", "SwapHub Notifications"))

    # ==============================
    # ✉️ BUILD THE EMAIL MESSAGE
    # ==============================
    msg = EmailMessage()
    msg["From"] = f"{sender_name} <{sender_email}>"
    msg["To"] = recipient_email
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        app.logger.info("Connecting to MailerSend SMTP server %s:%s ...", SMTP_SERVER, SMTP_PORT)

        # Create SMTP connection with explicit socket timeout
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
            server.ehlo()
            # Upgrade connection to TLS
            server.starttls()
            server.ehlo()
            # Login with MailerSend SMTP credentials
            server.login(SMTP_USER, SMTP_PASSWORD)
            # Send the email message
            server.send_message(msg)

        app.logger.info("✅ MailerSend SMTP: Email successfully sent to %s", recipient_email)
        return True

    except (smtplib.SMTPException, socket.timeout, ConnectionRefusedError) as e:
        app.logger.error(
            "❌ MailerSend SMTP error sending to %s: %s",
            recipient_email,
            e,
            exc_info=True,
        )
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

        # 5) Bulk-fetch wishlist counts
        listing_ids = [l['listing_id'] for l in listings]
        wl_counts = {}
        if listing_ids:
            ph = ','.join(['%s'] * len(listing_ids))
            cursor.execute(
                f"SELECT listing_id, COUNT(*) AS cnt FROM wishlists WHERE listing_id IN ({ph}) GROUP BY listing_id",
                tuple(listing_ids)
            )
            wl_counts = {r['listing_id']: r['cnt'] for r in cursor.fetchall()}

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

        # 7) Calculate TOTAL VIEWS and TOTAL FAVORITES
        total_views = 0
        total_favorites = 0

        if listing_ids:
            ph = ','.join(['%s'] * len(listing_ids))

            # Total Views = SUM of impressions
            cursor.execute(
                f"SELECT COALESCE(SUM(impressions), 0) AS total FROM listing_metrics WHERE listing_id IN ({ph})",
                tuple(listing_ids)
            )
            total_views = cursor.fetchone()['total']

            # Total Favorites = SUM of wishlist entries
            total_favorites = sum(wl_counts.get(lid, 0) for lid in listing_ids)

        # 8) Fetch proposals for the dashboard
        cursor.execute("""
            SELECT p.*, l.title AS listing_title, u.username AS sender_username, u.contact AS sender_contact
            FROM proposals p
            JOIN listings l ON p.listing_id = l.listing_id
            JOIN users u ON p.user_id = u.id
            WHERE l.user_id = %s
        """, (user_id,))
        proposals = cursor.fetchall()

        unique_titles = list({p['listing_title'] for p in proposals})

        # 9) Promotion plan prices (for sidebar or modal)
        plan_prices = {
            'Diamond': 100,
            'Gold':     70,
            'Silver':   40,
            'Bronze':   20
        }

        # 10) Render
        return render_template(
            'dashboard.html',
            user=user,
            listings=listings,
            proposals=proposals,
            unique_titles=unique_titles,
            plan_prices=plan_prices,
            total_views=total_views,          # ← NOW PASSED
            total_favorites=total_favorites   # ← NOW PASSED
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




@app.route('/listings/<int:listing_id>/edit', methods=['POST'])
@login_required
def update_listing(listing_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # 1) Verify listing exists + ownership
        cursor.execute("SELECT * FROM listings WHERE listing_id = %s", (listing_id,))
        existing = cursor.fetchone()

        if not existing or existing['user_id'] != session['user_id']:
            flash('You do not have permission to edit this listing', 'danger')
            return redirect(url_for('dashboard'))

        deal_type = existing['deal_type']

        # 2) Helper for listing fields: keep old if nothing posted
        def get_listing_field(name, default_key=None):
            """
            Read a field from request.form, fallback to existing listing column.
            default_key lets us map different form name -> listing column name.
            """
            key = default_key or name
            val = request.form.get(name)
            if val is None or str(val).strip() == '':
                return existing.get(key)
            return str(val).strip()

        # 3) Common listing fields (both Outright and Swap)
        category          = get_listing_field('category')
        location          = get_listing_field('location')
        contact           = get_listing_field('contact')
        desired_swap      = get_listing_field('desired_swap')
        desired_swap_desc = get_listing_field('desired_swap_description')
        required_cash     = request.form.get('required_cash')   or existing.get('required_cash')
        additional_cash   = request.form.get('additional_cash') or existing.get('additional_cash')

        # optional extra fields – will only change if you add matching inputs
        plan          = request.form.get('plan')          or existing.get('Plan')
        plan_duration = request.form.get('plan_duration') or existing.get('Plan Duration')
        status        = request.form.get('status')        or existing.get('status')
        swap_notes    = request.form.get('swap_notes')    or existing.get('swap_notes')

        # 4) For OUTRIGHT SALES
        if deal_type == 'Outright Sales':
            title       = get_listing_field('title')
            description = get_listing_field('description')
            condition   = get_listing_field('condition', default_key='condition')
            price       = get_listing_field('price')

            # ---- Handle listing images for Outright using upload_to_cloudinary ----
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

                if file and file.filename and allowed_file(file.filename):
                    try:
                        # Use your helper (same style as create_store)
                        image_url = upload_to_cloudinary(file, 'listings')
                        # If helper returns None for any reason, keep old
                        images_to_save[db_col] = image_url or existing.get(db_col)
                    except Exception as e:
                        app.logger.exception(f"Cloudinary upload failed for listing image {form_field}: {e}")
                        images_to_save[db_col] = existing.get(db_col)
                else:
                    images_to_save[db_col] = existing.get(db_col)

            cursor.execute(
                """
                UPDATE listings
                SET
                  title=%s,
                  description=%s,
                  category=%s,
                  location=%s,
                  contact=%s,
                  `condition`=%s,
                  price=%s,
                  desired_swap=%s,
                  desired_swap_description=%s,
                  required_cash=%s,
                  additional_cash=%s,
                  image_url=%s,
                  image1=%s,
                  image2=%s,
                  image3=%s,
                  image4=%s,
                  Plan=%s,
                  `Plan Duration`=%s,
                  status=%s,
                  swap_notes=%s
                WHERE listing_id = %s
                """,
                [
                    title, description, category, location, contact,
                    condition, price,
                    desired_swap, desired_swap_desc,
                    required_cash, additional_cash,
                    images_to_save['image_url'],
                    images_to_save['image1'],
                    images_to_save['image2'],
                    images_to_save['image3'],
                    images_to_save['image4'],
                    plan, plan_duration, status, swap_notes,
                    listing_id
                ]
            )

        # 5) For SWAP DEAL
        else:
            # a) Update non-title/description/condition/images fields in listings
            cursor.execute(
                """
                UPDATE listings
                SET
                  category=%s,
                  location=%s,
                  contact=%s,
                  desired_swap=%s,
                  desired_swap_description=%s,
                  required_cash=%s,
                  additional_cash=%s,
                  Plan=%s,
                  `Plan Duration`=%s,
                  status=%s,
                  swap_notes=%s
                WHERE listing_id = %s
                """,
                [
                    category, location, contact,
                    desired_swap, desired_swap_desc,
                    required_cash, additional_cash,
                    plan, plan_duration, status, swap_notes,
                    listing_id
                ]
            )

            # b) Load existing offered_items (to reuse old images when no new upload)
            cursor.execute(
                """
                SELECT
                  item_id,
                  title,
                  description,
                  `condition`,
                  image1, image2, image3, image4
                FROM offered_items
                WHERE listing_id = %s
                ORDER BY item_id ASC
                """,
                (listing_id,)
            )
            old_items = cursor.fetchall()

            old_by_slot = {}
            for idx, row in enumerate(old_items, start=1):
                old_by_slot[idx] = row

            # c) Clear existing offered_items; we will reinsert
            cursor.execute("DELETE FROM offered_items WHERE listing_id = %s", (listing_id,))

            requested_count = min(int(request.form.get('offered_items_count', 0) or 0), 2)

            primary_listing_data = {
                'title':       existing.get('title'),
                'description': existing.get('description'),
                'condition':   existing.get('condition'),
                'price':       existing.get('price'),
                'image1':      existing.get('image1'),
                'image2':      existing.get('image2'),
                'image3':      existing.get('image3'),
                'image4':      existing.get('image4'),
            }

            for slot in range(1, requested_count + 1):
                old = old_by_slot.get(slot, {})

                otitle = request.form.get(f'offered_title_{slot}') or old.get('title')
                if not otitle:
                    continue

                odesc = request.form.get(f'offered_description_{slot}') or old.get('description', '')
                ocond = request.form.get(f'offered_condition_{slot}')   or old.get('condition', '')
                ovalue = request.form.get(f'offered_value_{slot}')

                # Handle images for this offered item (upload_to_cloudinary)
                oimgs = []
                for img_idx in range(1, 5):
                    key_file   = f'offered_image_{slot}_{img_idx}'
                    key_remove = f'remove_image_{slot}_{img_idx}'

                    file   = request.files.get(key_file)
                    remove = (request.form.get(key_remove) == 'true')

                    old_img = old.get(f'image{img_idx}')

                    if remove:
                        new_img = None
                    elif file and file.filename and allowed_file(file.filename):
                        try:
                            new_url = upload_to_cloudinary(file, 'offers')
                            new_img = new_url or old_img
                        except Exception as e:
                            app.logger.exception(
                                f"Cloudinary upload failed for offered_image_{slot}_{img_idx}: {e}"
                            )
                            new_img = old_img
                    else:
                        new_img = old_img

                    oimgs.append(new_img)

                # Insert offered_items row
                cursor.execute(
                    """
                    INSERT INTO offered_items
                      (listing_id, title, description, `condition`, image1, image2, image3, image4)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        listing_id,
                        otitle,
                        odesc,
                        ocond,
                        oimgs[0], oimgs[1], oimgs[2], oimgs[3]
                    )
                )

                # Sync PRIMARY item (slot 1) into listings
                if slot == 1:
                    primary_listing_data['title']       = otitle
                    primary_listing_data['description'] = odesc
                    primary_listing_data['condition']   = ocond

                    if ovalue and str(ovalue).strip() != '':
                        primary_listing_data['price'] = str(ovalue).strip()

                    primary_listing_data['image1'] = oimgs[0]
                    primary_listing_data['image2'] = oimgs[1]
                    primary_listing_data['image3'] = oimgs[2]
                    primary_listing_data['image4'] = oimgs[3]

            # d) Write primary item into listings
            cursor.execute(
                """
                UPDATE listings
                SET
                  title=%s,
                  description=%s,
                  `condition`=%s,
                  price=%s,
                  image_url=%s,
                  image1=%s,
                  image2=%s,
                  image3=%s,
                  image4=%s
                WHERE listing_id=%s
                """,
                [
                    primary_listing_data['title'],
                    primary_listing_data['description'],
                    primary_listing_data['condition'],
                    primary_listing_data['price'],
                    primary_listing_data['image1'],  # image_url = image1
                    primary_listing_data['image1'],
                    primary_listing_data['image2'],
                    primary_listing_data['image3'],
                    primary_listing_data['image4'],
                    listing_id
                ]
            )

        # 6) Commit & redirect
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





from datetime import datetime, timedelta

PLAN_DURATIONS = {
    'Bronze': 21,     # 3 weeks
    'Silver': 30,     # 1 month
    'Gold': 60,       # 2 months
    'Diamond': 90,    # 3 months
}

@app.route('/listings', methods=['POST'])
@login_required
def create_listing():
    logger.debug(f"Processing listing for user_id: {session['user_id']}")

    # 1) Common data
    dt = request.form.get('deal_type', 'Swap Deal')
    if dt == 'Swap Deal':
        deal_type = 'Swap Deal'
    elif dt == 'Outright Sales':
        deal_type = 'Outright Sales'
    elif dt == 'Service Offer':
        deal_type = 'Service Offer'
    else:
        deal_type = 'Outright Sales'  # fallback

    title = request.form['title'].strip()
    # description is taken from different fields depending on type
    category = request.form['category'].strip()
    location = request.form['location'].strip()
    contact = request.form['contact'].strip()
    plan = request.form.get('plan', 'Free')
    logger.debug(f"Common data: deal_type={deal_type}, title={title}, category={category}, plan={plan}")
    description = ""

    # 2) Gather main images — used by Outright Sales AND Service Offer
    main_images = []
    if deal_type in ('Outright Sales', 'Service Offer'):
        for f in request.files.getlist('images[]'):
            if f and allowed_file(f.filename):
                try:
                    upload_result = cloudinary.uploader.upload(
                        f,
                        folder="swaphub/listings",
                        resource_type="image"
                    )
                    image_url = upload_result.get("secure_url")
                    main_images.append(image_url)
                except Exception as e:
                    logger.exception(f"Cloudinary upload failed for main image: {e}")
                    flash("Error uploading images. Please try again.", "error")
                    return redirect(url_for('dashboard'))

                if len(main_images) >= 5:
                    break

        if not main_images:
            flash("At least one image is required for this listing type.", "error")
            logger.error("No images uploaded for sale/service")
            return redirect(url_for('dashboard'))

    logger.debug(f"Main images: {main_images}")

    # 3) Swap-offer fields (only for Swap Deal)
    offered_items = []
    if deal_type == 'Swap Deal':
        off_titles = request.form.getlist('offer_title[]')
        off_conds = request.form.getlist('offer_condition[]')
        off_descs = request.form.getlist('offer_description[]')
        logger.debug(f"Offer data: titles={off_titles}, conditions={off_conds}, descriptions={off_descs}")

        if not (1 <= len(off_conds) <= 2):
            flash("Offer between 1 and 2 items.", "error")
            return redirect(url_for('dashboard'))

        offer_image_files_1 = request.files.getlist('offer_images_1[]')
        offer_image_files_2 = request.files.getlist('offer_images_2[]')
        image_lists = [offer_image_files_1, offer_image_files_2][:len(off_conds)]

        def save_files_to_cloudinary(file_list):
            out = []
            for f in file_list:
                if f and allowed_file(f.filename):
                    try:
                        upload_result = cloudinary.uploader.upload(
                            f,
                            folder="swaphub/offers",
                            resource_type="image"
                        )
                        out.append(upload_result.get("secure_url"))
                    except Exception as e:
                        logger.exception(f"Cloudinary upload failed: {e}")
                        flash("Error uploading images. Please try again.", "error")
                        return None
            return out

        for i in range(len(off_conds)):
            if not off_conds[i].strip() or not off_descs[i].strip():
                flash(f"Item {i+1} must have a condition and description.", "error")
                return redirect(url_for('dashboard'))

            item_title = off_titles[i].strip() if i < len(off_titles) and off_titles[i].strip() else title
            raw_images = image_lists[i][:4] if i < len(image_lists) else []
            item_images = save_files_to_cloudinary(raw_images)
            if item_images is None:
                return redirect(url_for('dashboard'))
            if not item_images:
                flash(f"Item {i+1} must have at least one image.", "error")
                return redirect(url_for('dashboard'))

            offered_items.append({
                'title': item_title,
                'condition': off_conds[i],
                'description': off_descs[i],
                'images': item_images + [None] * (4 - len(item_images))
            })

    # 4) Deal-type specific fields
    desired_swap = None
    desired_swap_description = None
    additional_cash = None
    required_cash = None
    price = None
    condition = None

    if deal_type == 'Swap Deal':
        desired_swap = (request.form.get('desired_swap') or '').strip()
        desired_swap_description = request.form.get('swap_notes', '').strip()

        additional_cash_raw = (request.form.get('additional_cash') or '').replace(',', '').strip()
        required_cash_raw = (request.form.get('required_cash') or '').replace(',', '').strip()

        if additional_cash_raw:
            try:
                additional_cash = Decimal(additional_cash_raw).quantize(Decimal('0.01'))
            except InvalidOperation:
                flash("Invalid value for additional cash.", "error")
                return redirect(url_for('dashboard'))

        if required_cash_raw:
            try:
                required_cash = Decimal(required_cash_raw).quantize(Decimal('0.01'))
            except InvalidOperation:
                flash("Invalid value for required cash.", "error")
                return redirect(url_for('dashboard'))

        if not desired_swap:
            flash("Desired item is required for swap.", "error")
            return redirect(url_for('dashboard'))

        condition = offered_items[0]['condition'] if offered_items else None

    elif deal_type == 'Outright Sales':
        price_raw = (request.form.get('price') or '').replace(',', '').strip()
        if price_raw:
            try:
                price = Decimal(price_raw).quantize(Decimal('0.01'))
            except InvalidOperation:
                flash("Invalid value for price.", "error")
                return redirect(url_for('dashboard'))
        condition = request.form.get('condition')
        description = request.form.get('description', '').strip()

    elif deal_type == 'Service Offer':
        contact_for_price = request.form.get('contact_for_price') == '1'
        if not contact_for_price:
            price_raw = (request.form.get('price') or '').replace(',', '').strip()
            if price_raw:
                try:
                    price = Decimal(price_raw).quantize(Decimal('0.01'))
                except InvalidOperation:
                    flash("Invalid value for price.", "error")
                    return redirect(url_for('dashboard'))
        # else: price remains None → "Contact for Price"
        condition = None
        description = request.form.get('description', '').strip()

    # 5) Combine description (for swap only we append offered items)
    if deal_type == 'Swap Deal':
        joined_offers = "\n\n".join([item['description'] for item in offered_items])
        combined_description = f"{description}\n\n{joined_offers}" if joined_offers else description
    else:
        combined_description = description

    # 6) Compute expires_at early
    expires_at = None
    if plan in PLAN_DURATIONS:
        expires_at = datetime.utcnow() + timedelta(days=PLAN_DURATIONS[plan])

    # 7) PAYSTACK flow for paid plans
    if plan != 'Free':
        pending_price = f"{price:.2f}" if isinstance(price, Decimal) else price
        pending_additional_cash = f"{additional_cash:.2f}" if isinstance(additional_cash, Decimal) else additional_cash
        pending_required_cash = f"{required_cash:.2f}" if isinstance(required_cash, Decimal) else required_cash

        session['pending_listing'] = {
            'user_id': session['user_id'],
            'title': title,
            'description': combined_description,
            'category': category,
            'desired_swap': desired_swap,
            'desired_swap_description': desired_swap_description,
            'additional_cash': pending_additional_cash,
            'required_cash': pending_required_cash,
            'location': location,
            'contact': contact,
            'main_images': main_images if deal_type in ('Outright Sales', 'Service Offer') else 
                           (offered_items[0]['images'][:4] if offered_items else []),
            'plan': plan,
            'deal_type': deal_type,
            'price': pending_price,
            'offered_items': offered_items,
            'condition': condition,               # added
            'expires_at': expires_at.isoformat() if expires_at else None
        }

        plan_fees = {'Bronze': 20, 'Silver': 50, 'Gold': 100, 'Diamond': 200}
        return redirect(url_for('paystack_payment', plan=plan, amount=plan_fees.get(plan, 0)))

    # 8) Free plan → direct insert
    conn = get_db_connection()
    cursor = conn.cursor()

    listing_params = [
        session['user_id'], title, combined_description, category,
        desired_swap, desired_swap_description,
        additional_cash, required_cash,
        condition,
        location, contact
    ]

    # Images
    if deal_type in ('Outright Sales', 'Service Offer'):
        listing_params += main_images + [None] * (5 - len(main_images))
    else:  # Swap
        swap_images = offered_items[0]['images'][:4] if offered_items else []
        listing_params += swap_images + [None] * (5 - len(swap_images))

    listing_params += [plan, deal_type, price, expires_at]

    cursor.execute("""
        INSERT INTO listings (
            user_id, title, description, category,
            desired_swap, desired_swap_description,
            additional_cash, required_cash,
            `condition`, location, contact,
            image_url, image1, image2, image3, image4,
            plan, deal_type, price, expires_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, listing_params)

    lid = cursor.lastrowid
    logger.debug(f"Inserted listing with ID: {lid}")

    # 9) Insert offered items (only swap)
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

    conn.commit()
    cursor.close()
    conn.close()

    flash("Listing created successfully!", "success")
    return redirect(url_for('dashboard'))






@app.route('/paystack_payment')
@login_required
def paystack_payment():
    plan = request.args.get('plan')
    amount = request.args.get('amount', type=float)

    if not plan or not amount:
        flash("Invalid payment request.", "error")
        return redirect(url_for('home'))

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT email FROM users WHERE id = %s", (session['user_id'],))  # ← Fixed: 'id'
    user = cur.fetchone()
    cur.close()
    conn.close()

    if not user:
        flash("User not found.", "error")
        return redirect(url_for('home'))

    payload = {
        "email": user['email'],
        "amount": int(amount * 100),
        "metadata": session.get('pending_listing'),
        "callback_url": url_for('paystack_verify', _external=True)
    }

    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }

    resp = requests.post(
        "https://api.paystack.co/transaction/initialize",
        json=payload,
        headers=headers
    ).json()

    if resp.get('status'):
        return redirect(resp['data']['authorization_url'])

    flash("Payment initialization failed.", "error")
    return redirect(url_for('home'))







@app.route('/paystack_verify')
@login_required
def paystack_verify():
    ref = request.args.get('reference')
    if not ref:
        flash("Missing payment reference.", "error")
        return redirect(url_for('home'))

    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    resp = requests.get(f"https://api.paystack.co/transaction/verify/{ref}", headers=headers).json()

    if not (resp.get('status') and resp['data']['status'] == 'success'):
        flash("Payment failed.", "error")
        return redirect(url_for('home'))

    p = resp['data']['metadata']
    if not p:
        flash("Invalid payment metadata.", "error")
        return redirect(url_for('home'))

    # Extract data
    user_id = p['user_id']
    store_id = p.get('store_id')
    store_slug = p.get('store_slug')
    title = p['title']
    description = p['description']
    category = p['category']
    location = p['location']
    contact = p['contact']
    deal_type = p['deal_type']
    price_str = p.get('price')
    price = Decimal(price_str) if price_str else None
    main_images = p.get('main_images', [])
    offered_items = p.get('offered_items', [])
    desired_swap = p.get('desired_swap', '')
    plan = p.get('plan', 'Free')
    condition = p.get('condition')   # may be None for service

    # Swap-specific
    desired_condition = p.get('desired_condition', '')
    swap_notes = p.get('desired_swap_description', '')
    required_cash_str = p.get('required_cash')
    additional_cash_str = p.get('additional_cash')
    required_cash = Decimal(required_cash_str) if required_cash_str else None
    additional_cash = Decimal(additional_cash_str) if additional_cash_str else None

    conn = get_db_connection()
    cur = conn.cursor()

    try:
        if deal_type in ('Outright Sales', 'Service Offer'):
            padded_images = (main_images + [None] * 5)[:5]

            sql = """
                INSERT INTO listings
                (user_id, store_id, title, description, category, location, contact,
                 price, deal_type, Plan, `condition`, image_url, image1, image2, image3, image4)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            values = (
                user_id, store_id, title, description, category, location, contact,
                price, deal_type, plan, condition,
                padded_images[0], padded_images[1], padded_images[2], padded_images[3], padded_images[4]
            )

            cur.execute(sql, values)
            listing_id = cur.lastrowid

        else:  # Swap Deal
            preview_images = offered_items[0]['images'][:5] if offered_items else []
            padded_preview = (preview_images + [None] * 5)[:5]

            sql = """
                INSERT INTO listings
                (user_id, store_id, title, description, category, location, contact,
                 deal_type, desired_swap, `condition`, desired_swap_description,
                 required_cash, additional_cash, Plan, image_url, image1, image2, image3, image4)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            values = (
                user_id, store_id, title, description, category, location, contact,
                deal_type, desired_swap, desired_condition, swap_notes,
                required_cash, additional_cash, plan,
                padded_preview[0], padded_preview[1], padded_preview[2], padded_preview[3], padded_preview[4]
            )

            cur.execute(sql, values)
            listing_id = cur.lastrowid

            # Insert offered items
            if offered_items:
                for item in offered_items:
                    padded_item = (item['images'] + [None] * 4)[:4]
                    cur.execute("""
                        INSERT INTO offered_items
                        (listing_id, title, description, `condition`, image1, image2, image3, image4)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        listing_id,
                        item['title'], item['description'], item['condition'],
                        padded_item[0], padded_item[1], padded_item[2], padded_item[3]
                    ))

        conn.commit()
        flash("Payment successful! Your promoted listing has been created.", "success")

    except Exception as e:
        conn.rollback()
        logger.exception("Error saving paid listing")
        flash("Payment succeeded but listing creation failed. Contact support.", "error")

    finally:
        cur.close()
        conn.close()

    session.pop('pending_listing', None)

    if store_id:
        return redirect(url_for('store_home', store_id=store_id))
    else:
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
        Send email using MailerSend's SMTP relay (TLS on port 587 by default).
        Returns True on success, False on failure. Does not raise.
        """

        if not recipient_email:
            app.logger.warning("send_email_notification called without recipient_email")
            return False

        # ==============================
        # 🔐 MAILERSEND SMTP SETTINGS
        # ==============================
        SMTP_SERVER = os.environ.get("MAILERSEND_SMTP_HOST", "smtp.mailersend.net")
        SMTP_PORT = int(os.environ.get("MAILERSEND_SMTP_PORT", "587"))
        SMTP_USER = os.environ.get("MAILERSEND_SMTP_USER")       # e.g. MS_xxx...
        SMTP_PASSWORD = os.environ.get("MAILERSEND_SMTP_PASSWORD")

        # 🔍 DEBUG: just to confirm values are being read (remove later)
        app.logger.info("MailerSend SMTP user: %s", SMTP_USER)
        app.logger.info("MailerSend SMTP password length: %d", len(SMTP_PASSWORD or ""))

        if not SMTP_USER or not SMTP_PASSWORD:
            app.logger.error("MailerSend SMTP credentials are not set in environment")
            return False

        # ==============================
        # 📧 SENDER ADDRESS
        # ==============================
        # Use a domain-based or verified sender email in your MailerSend account.
        sender_email = app.config.get("FROM_EMAIL", os.environ.get("FROM_EMAIL", "no-reply@test-51ndgwvkvwqlzqx8.mlsender.net"))

        # Optional: sender display name
        sender_name = app.config.get("FROM_NAME", os.environ.get("FROM_NAME", "SwapHub Notifications"))

        # ==============================
        # ✉️ BUILD THE EMAIL MESSAGE
        # ==============================
        msg = EmailMessage()
        msg["From"] = f"{sender_name} <{sender_email}>"
        msg["To"] = recipient_email
        msg["Subject"] = subject
        msg.set_content(body)

        try:
            app.logger.info("Connecting to MailerSend SMTP server %s:%s ...", SMTP_SERVER, SMTP_PORT)

            # Create SMTP connection with explicit socket timeout
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
                server.ehlo()
                # Upgrade connection to TLS
                server.starttls()
                server.ehlo()
                # Login with MailerSend SMTP credentials
                server.login(SMTP_USER, SMTP_PASSWORD)
                # Send the email message
                server.send_message(msg)

            app.logger.info("✅ MailerSend SMTP: Email successfully sent to %s", recipient_email)
            return True

        except (smtplib.SMTPException, socket.timeout, ConnectionRefusedError) as e:
            app.logger.error(
                "❌ MailerSend SMTP error sending to %s: %s",
                recipient_email,
                e,
                exc_info=True,
            )
            return False
        except Exception as e:
            app.logger.exception("❌ Unexpected error sending email to %s: %s", recipient_email, e)
            return False

# ========== Password-reset SendGrid wrapper (named uniquely to avoid conflicts) ==========
# ========== Password-reset MailerSend wrapper (named uniquely to avoid conflicts) ==========

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
            logging.error(
                "Password reset email failed (send_email_notification returned False) for %s",
                recipient_email,
            )
    except Exception:
        logging.exception(
            "Exception while sending password reset email to %s", recipient_email
        )


def send_password_reset_via_mailersend(recipient_email, reset_url):
    """
    Public helper to queue a password reset email using MailerSend (via send_email_notification).
    Uses a unique function name to avoid any collisions with other senders.
    """
    if not recipient_email:
        logging.error("send_password_reset_via_mailersend called without recipient_email")
        return None

    subject = "Your SwapHub password reset request"

    body = (
        "Hello,\n\n"
        "We received a request to reset the password for your SwapHub account "
        "associated with this email address.\n\n"
        "To choose a new password, please click the link below or paste it into your browser:\n"
        f"{reset_url}\n\n"
        "For your security, this link will expire in 1 hour.\n"
        "If you did not request a password reset, you can safely ignore this message "
        "and your account password will remain unchanged.\n\n"
        "Best regards,\n"
        "The SwapHub Team\n"
        "support@swaphub.example\n"
    )

    thread = threading.Thread(
        target=_send_password_email_worker,
        args=(recipient_email, subject, body),
        daemon=True,
    )
    thread.start()
    return thread



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



            


_redis_client = None          # real redis.StrictRedis or InMemoryRedis
_redis_lock   = threading.Lock()


# ----------------------------------------------------------------------
# In-memory fallback (now with hash support)
# ----------------------------------------------------------------------
class InMemoryRedis:
    """
    Tiny Redis-compatible shim that stores everything in a dict.
    Supports the subset used by the app:
        set, get, delete, incr, decr, expire, keys
        hset, hget, hincrby, hdel, hgetall, hexists
        pipeline (no-op)
    """
    def __init__(self):
        self._store   = {}      # key → value  (strings)
        self._hashes  = defaultdict(dict)   # key → {field: value}
        self._expires = {}      # key → expiry timestamp
        self._lock    = threading.RLock()

    # ------------------------------------------------------------------
    # String commands
    # ------------------------------------------------------------------
    def set(self, key, value, ex=None, **kw):
        with self._lock:
            self._store[key] = value
            if ex is not None:
                self._expires[key] = time.time() + ex
            elif key in self._expires:
                del self._expires[key]
        return True

    def get(self, key):
        with self._lock:
            self._expire_if_needed(key)
            return self._store.get(key)

    def delete(self, *keys):
        with self._lock:
            deleted = 0
            for k in keys:
                if k in self._store:
                    self._store.pop(k, None)
                    self._expires.pop(k, None)
                    deleted += 1
                if k in self._hashes:
                    self._hashes.pop(k, None)
                    deleted += 1
            return deleted

    def incr(self, key, amount=1):
        with self._lock:
            self._expire_if_needed(key)
            val = int(self._store.get(key, 0)) + amount
            self._store[key] = str(val)
            return val

    def decr(self, key, amount=1):
        return self.incr(key, -amount)

    def expire(self, key, seconds):
        with self._lock:
            if key not in self._store and key not in self._hashes:
                return False
            self._expires[key] = time.time() + seconds
            return True

    def keys(self, pattern="*"):
        with self._lock:
            self._clean_expired()
            if pattern == "*":
                return list(self._store.keys()) + list(self._hashes.keys())
            return [k for k in set(self._store) | set(self._hashes)
                    if fnmatch.fnmatch(k, pattern)]

    # ------------------------------------------------------------------
    # Hash commands (the ones you hit)
    # ------------------------------------------------------------------
    def hset(self, name, key, value):
        """hset hash field value"""
        with self._lock:
            self._expire_if_needed(name)
            self._hashes[name][key] = value
            return 1

    def hget(self, name, key):
        with self._lock:
            self._expire_if_needed(name)
            return self._hashes[name].get(key)

    def hincrby(self, name, key, increment=1):
        """hincrby hash field amount"""
        with self._lock:
            self._expire_if_needed(name)
            cur = int(self._hashes[name].get(key, 0))
            new = cur + increment
            self._hashes[name][key] = str(new)
            return new

    def hdel(self, name, *keys):
        with self._lock:
            self._expire_if_needed(name)
            deleted = 0
            for k in keys:
                if k in self._hashes[name]:
                    del self._hashes[name][k]
                    deleted += 1
            return deleted

    def hgetall(self, name):
        with self._lock:
            self._expire_if_needed(name)
            return dict(self._hashes[name])

    def hexists(self, name, key):
        with self._lock:
            self._expire_if_needed(name)
            return key in self._hashes[name]

    # ------------------------------------------------------------------
    # Helper: expire handling
    # ------------------------------------------------------------------
    def _expire_if_needed(self, key):
        if key in self._expires and self._expires[key] < time.time():
            self._store.pop(key, None)
            self._hashes.pop(key, None)
            self._expires.pop(key, None)

    def _clean_expired(self):
        now = time.time()
        for k, ts in list(self._expires.items()):
            if ts < now:
                self._store.pop(k, None)
                self._hashes.pop(k, None)
                self._expires.pop(k, None)

    # ------------------------------------------------------------------
    # Pipeline (no-op)
    # ------------------------------------------------------------------
    def pipeline(self):
        return self

    def execute(self):
        return []

    def __repr__(self):
        return f"<InMemoryRedis keys={len(self._store)+len(self._hashes)}>"

# ----------------------------------------------------------------------
# Public accessor
# ----------------------------------------------------------------------
def get_redis():
    """
    Return a shared redis client.
    * Real redis.StrictRedis when REDIS_URL works
    * InMemoryRedis fallback otherwise (local dev, broken server, etc.)
    """
    global _redis_client

    if _redis_client is not None:
        return _redis_client

    with _redis_lock:
        if _redis_client is None:
            url = os.getenv('REDIS_URL')

            # 1. No URL → in-memory
            if not url:
                logging.info("REDIS_URL not set – using in-memory cache")
                _redis_client = InMemoryRedis()
                return _redis_client

            # 2. URL present → try real Redis
            try:
                client = redis.from_url(url, decode_responses=False)
                client.ping()                     # verify connectivity
                logging.info("Connected to Redis: %s", url.split('@')[-1])
                _redis_client = client
            except Exception as exc:               # pragma: no cover
                logging.error(
                    "Redis connection failed (%s) – falling back to in-memory cache",
                    exc
                )
                _redis_client = InMemoryRedis()

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
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # 1. Wishlist
    cursor.execute("""
        SELECT l.*, w.created_at AS wishlisted_at
          FROM wishlists w
          JOIN listings l ON l.listing_id = w.listing_id
         WHERE w.user_id = %s
         ORDER BY w.created_at DESC
    """, (user_id,))
    wishlist_items = cursor.fetchall()

    # 2. Similar items for each
    similar_listings = {}
    for item in wishlist_items:
        listing_id = item['listing_id']

        # Try same category
        cursor.execute("""
            SELECT l.*
              FROM listings l
             WHERE l.category = %s
               AND l.listing_id != %s
               AND l.user_id != %s
               AND l.listing_id NOT IN (SELECT listing_id FROM wishlists WHERE user_id = %s)
             ORDER BY RAND()
             LIMIT 3
        """, (item['category'], listing_id, user_id, user_id))
        sim = cursor.fetchall()

        # Fallback: random
        if not sim:
            cursor.execute("""
                SELECT l.*
                  FROM listings l
                 WHERE l.user_id != %s
                   AND l.listing_id NOT IN (SELECT listing_id FROM wishlists WHERE user_id = %s)
                 ORDER BY RAND()
                 LIMIT 3
            """, (user_id, user_id))
            sim = cursor.fetchall()

        similar_listings[listing_id] = sim

    # --- normalize images for wishlist items and similar listings ---
    def _normalize_image(record):
        # Prefer explicit image_url if present, else image1
        raw = record.get('image_url') or record.get('image1')

        if raw and str(raw).startswith('http'):
            # Cloudinary / external URL stored directly
            record['image_url'] = raw
        else:
            # Local filename in static/images or fallback placeholder
            name = raw or 'placeholder.jpg'
            record['image_url'] = url_for('static', filename=f'images/{name}')

    for item in wishlist_items:
        _normalize_image(item)

    for sims in similar_listings.values():
        for sim in sims:
            _normalize_image(sim)

    cursor.close()
    conn.close()

    return render_template(
        'wishlist.html',
        listings=wishlist_items,
        similar_listings=similar_listings
    )




@app.route('/wishlist/remove', methods=['POST'])
@login_required
def remove_from_wishlist():
    user_id = session['user_id']
    listing_id = request.form.get('listing_id')
    if not listing_id:
        flash('Invalid request.', 'danger')
        return redirect(url_for('view_wishlist'))

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "DELETE FROM wishlists WHERE user_id = %s AND listing_id = %s",
            (user_id, listing_id)
        )
        conn.commit()
        flash('Item removed from wishlist.', 'success')
    except Exception as e:
        conn.rollback()
        flash('Could not remove item.', 'danger')
    finally:
        cur.close()
        conn.close()
    return redirect(url_for('view_wishlist'))




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






# Set upload folder inside static/images
UPLOAD_FOLDER = os.path.join('static', 'images')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'avif'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def upload_to_cloudinary(file, folder):
    """
    Uploads file to Cloudinary
    Returns secure URL or None
    """
    if not file or not file.filename:
        return None

    if not allowed_file(file.filename):
        return None

    try:
        result = cloudinary.uploader.upload(
            file,
            folder=folder,
            resource_type="image",
            use_filename=True,
            unique_filename=True
        )
        return result.get("secure_url")
    except Exception as e:
        print("Cloudinary upload error:", e)
        return None





@app.route('/create-store', methods=['GET', 'POST'])
@login_required
def create_store():
    user_id = session.get('user_id')
    if not user_id:
        # Safety net (should not reach here with @login_required)
        return jsonify({"success": False, "message": "Please log in"}), 401

    if request.method == 'GET':
        return render_template('create_store.html')

    # ── POST handling ────────────────────────────────────────────────────────
    name        = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    location    = request.form.get('location', '').strip()
    contact     = request.form.get('contact', '').strip()
    store_type  = request.form.get('store_type', '').strip()

    # Basic server-side required fields check
    missing = []
    if not name:        missing.append("Store name")
    if not store_type:  missing.append("Store type")
    if not location:    missing.append("Location")
    if not contact:     missing.append("Contact")

    if missing:
        return jsonify({
            "success": False,
            "message": f"Missing required fields: {', '.join(missing)}"
        }), 400

    logo_file   = request.files.get('logo')
    banner_file = request.files.get('banner')

    # ── Upload handling – extract URL from dict, keep function unchanged ──
    logo_url = None
    if logo_file and logo_file.filename:
        result = upload_to_cloudinary(logo_file, folder='stores/logos')
        if isinstance(result, dict) and result.get('success'):
            logo_url = result.get('url')  # or result['url'] — same thing
        else:
            print("Logo upload failed:", result)

    banner_url = None
    if banner_file and banner_file.filename:
        result = upload_to_cloudinary(banner_file, folder='stores/banners')
        if isinstance(result, dict) and result.get('success'):
            banner_url = result.get('url')
        else:
            print("Banner upload failed:", result)

    # Slug & link
    slug = slugify(name)
    store_link = f"{request.host_url.rstrip('/')}/store/{slug}"

    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            INSERT INTO stores 
            (user_id, name, slug, logo, banner, description, location, contact, store_type, store_link)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            user_id,
            name,
            slug,
            logo_url,       # ← string or None
            banner_url,     # ← string or None
            description,
            location,
            contact,
            store_type,
            store_link
        ))

        conn.commit()

        new_store_id = cur.lastrowid 

        # Return JSON response that the frontend fetch expects
        return jsonify({
            "success": True,
            "message": "Store created successfully!",
            "redirect": url_for('store_home', store_id=new_store_id, _external=False)
            # or use absolute path if your frontend needs it:
            # "redirect": f"/store/{new_store_id}"
        }), 200

    except Exception as e:
        conn.rollback()

        error_str = str(e).lower()

        if "duplicate" in error_str or "unique" in error_str or "1062" in error_str:  # 1062 = MySQL duplicate entry code
            return jsonify({
                "success": False,
                "message": "A store with this name already exists. Please choose a different name.",
                "field": "name"   # optional – helps frontend highlight the field if you want
            }), 409   # or 400

        else:
            print("Create store error:", error_str)
            return jsonify({
                "success": False,
                "message": "Failed to create store. Please try again."
            }), 500

    finally:
        cur.close()
        conn.close()


# Helper function (add this somewhere in your app if you don't already have it)
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in {'png', 'jpg', 'jpeg', 'gif', 'webp'}






@app.route('/store/<int:store_id>')
def store_home(store_id):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    
    try:
        # Fetch the store by its numeric ID
        cur.execute("SELECT * FROM stores WHERE store_id = %s", (store_id,))
        store = cur.fetchone()
        
        if not store:
            flash("Store not found.", "error")
            return redirect(url_for('home'))
        
        # Optional ownership check – only the owner can view this dashboard
        if 'user_id' in session and store['user_id'] != session['user_id']:
            flash("You don't have permission to view this store.", "error")
            return redirect(url_for('home'))
        
        # Fetch current promo (assuming max one active promo per store)
        cur.execute("""
            SELECT 
                promo_id, media_type, media_url, description, 
                button_text, button_link, frequency, active,
                start_date, end_date
            FROM store_promos
            WHERE store_id = %s
            LIMIT 1
        """, (store_id,))
        promo = cur.fetchone() or {}  # empty dict if no promo exists
        
        # Category counts for the store's listings
        cur.execute("""
            SELECT category, COUNT(*) AS total 
            FROM listings 
            WHERE store_id = %s 
            GROUP BY category
        """, (store_id,))
        category_counts = cur.fetchall()
        
        # Total listing metrics (views/clicks across all time)
        cur.execute("""
            SELECT 
                COALESCE(SUM(lm.impressions), 0) AS views,
                COALESCE(SUM(lm.clicks), 0) AS clicks
            FROM listings l
            LEFT JOIN listing_metrics lm ON l.listing_id = lm.listing_id
            WHERE l.store_id = %s
        """, (store_id,))
        totals = cur.fetchone()
        
        # Store metrics – last 30 days
        cur.execute("""
            SELECT 
                COALESCE(SUM(views), 0) AS total_views,
                COALESCE(SUM(clicks), 0) AS total_clicks,
                COALESCE(SUM(chats), 0) AS total_chats,
                COALESCE(SUM(swaps), 0) AS total_swaps,
                COALESCE(SUM(sales), 0) AS total_sales
            FROM store_metrics 
            WHERE store_id = %s 
            AND dt >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
        """, (store_id,))
        store_metrics = cur.fetchone()
        
        # Previous 30-day period (days 30–60 ago) for comparison
        cur.execute("""
            SELECT 
                COALESCE(SUM(views), 0) AS prev_views,
                COALESCE(SUM(clicks), 0) AS prev_clicks,
                COALESCE(SUM(chats), 0) AS prev_chats,
                COALESCE(SUM(swaps), 0) AS prev_swaps,
                COALESCE(SUM(sales), 0) AS prev_sales
            FROM store_metrics 
            WHERE store_id = %s 
            AND dt >= DATE_SUB(CURDATE(), INTERVAL 60 DAY)
            AND dt < DATE_SUB(CURDATE(), INTERVAL 30 DAY)
        """, (store_id,))
        prev_metrics = cur.fetchone()
        
        # Helper to calculate percentage change
        def calculate_change(current, previous):
            if previous == 0:
                return 100 if current > 0 else 0
            return round(((current - previous) / previous) * 100, 2)
        
        # Build metrics dictionary with change percentages
        if store_metrics and prev_metrics:
            metrics_with_change = {
                'views': {
                    'current': store_metrics['total_views'],
                    'change': calculate_change(store_metrics['total_views'], prev_metrics['prev_views'])
                },
                'clicks': {
                    'current': store_metrics['total_clicks'],
                    'change': calculate_change(store_metrics['total_clicks'], prev_metrics['prev_clicks'])
                },
                'chats': {
                    'current': store_metrics['total_chats'],
                    'change': calculate_change(store_metrics['total_chats'], prev_metrics['prev_chats'])
                },
                'swaps': {
                    'current': store_metrics['total_swaps'],
                    'change': calculate_change(store_metrics['total_swaps'], prev_metrics['prev_swaps'])
                },
                'sales': {
                    'current': store_metrics['total_sales'],
                    'change': calculate_change(store_metrics['total_sales'], prev_metrics['prev_sales'])
                }
            }
        else:
            metrics_with_change = {
                'views': {'current': 0, 'change': 0},
                'clicks': {'current': 0, 'change': 0},
                'chats': {'current': 0, 'change': 0},
                'swaps': {'current': 0, 'change': 0},
                'sales': {'current': 0, 'change': 0}
            }
        
        # Top 5 products by impressions
        cur.execute("""
            SELECT 
                l.listing_id,
                l.title,
                l.image1,
                COALESCE(lm.impressions, 0) AS impressions,
                COALESCE(lm.clicks, 0) AS clicks,
                ROUND(
                    CASE 
                        WHEN COALESCE(lm.impressions, 0) = 0 THEN 0
                        ELSE (COALESCE(lm.clicks, 0) * 100.0) / COALESCE(lm.impressions, 1)
                    END, 1
                ) AS ctr
            FROM listings l
            LEFT JOIN listing_metrics lm ON l.listing_id = lm.listing_id
            WHERE l.store_id = %s
            ORDER BY lm.impressions DESC
            LIMIT 5
        """, (store_id,))
        top_by_impressions = cur.fetchall()
        
        # Top 5 products by clicks
        cur.execute("""
            SELECT 
                l.listing_id,
                l.title,
                l.image1,
                COALESCE(lm.impressions, 0) AS impressions,
                COALESCE(lm.clicks, 0) AS clicks,
                ROUND(
                    CASE 
                        WHEN COALESCE(lm.impressions, 0) = 0 THEN 0
                        ELSE (COALESCE(lm.clicks, 0) * 100.0) / COALESCE(lm.impressions, 1)
                    END, 1
                ) AS ctr
            FROM listings l
            LEFT JOIN listing_metrics lm ON l.listing_id = lm.listing_id
            WHERE l.store_id = %s
            ORDER BY lm.clicks DESC
            LIMIT 5
        """, (store_id,))
        top_by_clicks = cur.fetchall()
        
        # Render the dashboard template with all collected data
        return render_template(
            'store_home.html',
            store=store,
            promo=promo,
            category_counts=category_counts,
            totals=totals or {'views': 0, 'clicks': 0},
            metrics=metrics_with_change,
            top_by_impressions=top_by_impressions,
            top_by_clicks=top_by_clicks,
            now=datetime.utcnow()
        )
    
    except Exception as e:
        current_app.logger.error(f"Error in store_home (store_id={store_id}): {str(e)}")
        flash("An error occurred loading the dashboard.", "error")
        return redirect(url_for('home'))
    
    finally:
        cur.close()
        conn.close()








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
    cur.execute("SELECT store_id, user_id FROM stores WHERE slug=%s", (slug,))
    store = cur.fetchone()

    if not store or store['user_id'] != session['user_id']:
        cur.close()
        conn.close()
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    video = request.files.get('tour_video')
    allowed_extensions = {'mp4', 'mov', 'webm', 'avi'}

    if video and video.filename:
        ext = video.filename.rsplit('.', 1)[1].lower() if '.' in video.filename else ''
        if ext not in allowed_extensions:
            return jsonify({'success': False, 'message': 'Invalid video format'}), 400

        # Save in static/videos/
        video_path = save_file(video, 'videos')  # This will return /static/videos/unique.mp4

        if video_path:
            cur.execute("""
                UPDATE stores 
                SET tour_video = %s 
                WHERE store_id = %s
            """, (video_path, store['store_id']))
            conn.commit()
            cur.close()
            conn.close()
            return jsonify({'success': True, 'video_url': video_path})

    cur.close()
    conn.close()
    return jsonify({'success': False, 'message': 'No valid video uploaded'}), 400














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



PLAN_PRICES = {
    'Free': 0,
    'Silver': 40,
    'Gold': 70,
    'Diamond': 100
}




@app.route('/store/add-item', methods=['POST'])
@login_required
def store_add_item():
    user_id = session['user_id']

    # ── First connection: check if user has a store ──
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True, buffered=True)

    try:
        cur.execute("""
            SELECT store_id, slug 
            FROM stores 
            WHERE user_id = %s 
            LIMIT 1
        """, (user_id,))
        
        store_row = cur.fetchone()

        if not store_row:
            return jsonify({
                "success": False,
                "message": "You don't have a store yet. Please create one first."
            }), 400

        store_id = store_row['store_id']
        store_slug = store_row['slug']

    finally:
        cur.close()
        conn.close()

    # ── Get common form data ───────────────────────────────────────────────
    deal_type = request.form.get('deal_type')
    plan = request.form.get('plan', 'Free')

    location = request.form.get('location', '').strip()
    contact = request.form.get('contact', '').strip()

    if not location or not contact:
        return jsonify({"success": False, "message": "Location and contact are required."}), 400

    categories = [c.strip() for c in request.form.getlist('category') if c and c.strip()]
    category = categories[0] if categories else ""

    if not category:
        return jsonify({"success": False, "message": "Category is required."}), 400

    # Prepare common variables
    main_images = []
    price = None
    offered_items = []
    title = ""
    description = ""
    condition = None  # not used for service

    # ── Service Offer ──────────────────────────────────────────────────────
    if deal_type == 'Service Offer':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()

        if not title or not description:
            return jsonify({"success": False, "message": "Service name and description are required."}), 400

        contact_for_price = request.form.get('contact_for_price') == '1'

        if not contact_for_price:
            price_raw = request.form.get('price', '').strip()
            if not price_raw:
                return jsonify({"success": False, "message": "Please enter a price or select 'Contact for Price'."}), 400
            try:
                price = Decimal(price_raw)
                if price <= 0:
                    return jsonify({"success": False, "message": "Service price must be greater than zero when fixed."}), 400
            except:
                return jsonify({"success": False, "message": "Invalid price format."}), 400
        # else: price remains None → Contact for Price

        # Images are optional for services
        for f in request.files.getlist('images[]'):
            if f and allowed_file(f.filename):
                try:
                    upload = cloudinary.uploader.upload(f, folder="swaphub/listings")
                    main_images.append(upload['secure_url'])
                except Exception as e:
                    print("Cloudinary upload error (service):", e)

    # ── Outright Sales ─────────────────────────────────────────────────────
    elif deal_type == 'Outright Sales':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()

        if not title or not description:
            return jsonify({"success": False, "message": "Title and description are required for sale."}), 400

        price_raw = request.form.get('price', '').strip()
        if not price_raw:
            return jsonify({"success": False, "message": "Price is required for sale."}), 400
        
        try:
            price = Decimal(price_raw)
        except:
            return jsonify({"success": False, "message": "Invalid price format."}), 400

        # Handle images (required for sales)
        for f in request.files.getlist('images[]'):
            if f and allowed_file(f.filename):
                try:
                    upload = cloudinary.uploader.upload(f, folder="swaphub/listings")
                    main_images.append(upload['secure_url'])
                except Exception as e:
                    print("Cloudinary upload error:", e)

        if not main_images:
            return jsonify({"success": False, "message": "At least one image is required."}), 400

    # ── Swap Deal ──────────────────────────────────────────────────────────
    elif deal_type == 'Swap Deal':
        desired_swap = request.form.get('desired_swap', '').strip()
        if not desired_swap:
            return jsonify({"success": False, "message": "Desired item is required for swap."}), 400

        offer_titles = request.form.getlist('offer_title[]')
        offer_conds = request.form.getlist('offer_condition[]')
        offer_descs = request.form.getlist('offer_description[]')

        if not offer_titles:
            return jsonify({"success": False, "message": "At least one offered item is required."}), 400

        for i in range(len(offer_titles)):
            images = []
            file_key = f'offer_images_{i+1}[]'
            for f in request.files.getlist(file_key):
                if f and allowed_file(f.filename):
                    try:
                        upload = cloudinary.uploader.upload(f, folder="swaphub/offers")
                        images.append(upload['secure_url'])
                    except Exception as e:
                        print("Cloudinary upload error (offer item):", e)

            if not images:
                return jsonify({
                    "success": False,
                    "message": f"Images required for offered item #{i+1}."
                }), 400

            item_title = offer_titles[i].strip()
            item_desc = offer_descs[i].strip()

            offered_items.append({
                "title": item_title,
                "condition": offer_conds[i],
                "description": item_desc,
                "images": images
            })

            # Use first item as main listing preview
            if i == 0:
                title = item_title or "Swap Deal"
                description = item_desc or "Looking to swap items"

    else:
        return jsonify({"success": False, "message": "Invalid deal type."}), 400

    # ── PAID PLAN - Store in session and redirect to payment ──────────────
    if plan != 'Free':
        session['pending_listing'] = {
            "source": "store",
            "store_id": store_id,
            "store_slug": store_slug,
            "user_id": user_id,
            "title": title,
            "description": description,
            "category": category,
            "location": location,
            "contact": contact,
            "deal_type": deal_type,
            "price": str(price) if price is not None else None,
            "main_images": main_images,
            "offered_items": offered_items,
            "desired_swap": request.form.get('desired_swap', ''),
            "desired_condition": request.form.get('desired_condition', ''),
            "swap_notes": request.form.get('swap_notes', ''),
            "required_cash": request.form.get('required_cash'),
            "additional_cash": request.form.get('additional_cash'),
            "plan": plan,
            "condition": condition  # None for service
        }

        return jsonify({
            "success": True,
            "redirect": url_for('paystack_payment', plan=plan, amount=PLAN_PRICES[plan], _external=True)
        })

    # ── FREE PLAN - Save directly to database ─────────────────────────────
    conn = get_db_connection()
    cur = conn.cursor(buffered=True)

    try:
        if deal_type in ('Outright Sales', 'Service Offer'):
            padded_images = (main_images + [None] * 5)[:5]
            cur.execute("""
                INSERT INTO listings
                (user_id, store_id, title, description, category, location, contact,
                 price, deal_type, Plan, image_url, image1, image2, image3, image4)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                user_id, store_id, title, description, category, location, contact,
                str(price) if price is not None else None,
                deal_type, plan,
                padded_images[0], padded_images[1], padded_images[2], padded_images[3], padded_images[4]
            ))

        else:  # Swap Deal
            desired_swap = request.form.get('desired_swap', '')
            desired_condition = request.form.get('desired_condition', '')
            swap_notes = request.form.get('swap_notes', '')
            required_cash = request.form.get('required_cash') or None
            additional_cash = request.form.get('additional_cash') or None

            preview_images = offered_items[0]['images'][:5] if offered_items else []
            padded_preview = (preview_images + [None] * 5)[:5]

            cur.execute("""
                INSERT INTO listings
                (user_id, store_id, title, description, category, location, contact,
                 deal_type, desired_swap, `condition`, swap_notes,
                 required_cash, additional_cash, Plan,
                 image_url, image1, image2, image3, image4)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                user_id, store_id, title, description, category, location, contact,
                deal_type, desired_swap, desired_condition, swap_notes,
                required_cash, additional_cash, plan,
                padded_preview[0], padded_preview[1], padded_preview[2], padded_preview[3], padded_preview[4]
            ))

        listing_id = cur.lastrowid

        # Save offered items (only for swaps)
        if deal_type == 'Swap Deal':
            for item in offered_items:
                padded_item_images = (item['images'] + [None] * 4)[:4]
                cur.execute("""
                    INSERT INTO offered_items
                    (listing_id, title, description, `condition`, image1, image2, image3, image4)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    listing_id,
                    item['title'], item['description'], item['condition'],
                    padded_item_images[0], padded_item_images[1],
                    padded_item_images[2], padded_item_images[3]
                ))

        conn.commit()

        # ─── NOTIFY FOLLOWERS ─────────────────────────────────────────────
        if listing_id:
            try:
                # Get followers of this store
                notif_cur = conn.cursor(dictionary=True)
                notif_cur.execute(
                    "SELECT user_id FROM follows WHERE store_id = %s",
                    (store_id,)
                )
                followers = notif_cur.fetchall()
                notif_cur.close()

                if followers:
                    message = f"New item: {title}"
                    insert_data = [(f['user_id'], store_id, listing_id, message) for f in followers]
                    notif_cur = conn.cursor()
                    notif_cur.executemany(
                        "INSERT INTO notifications (user_id, store_id, listing_id, message) VALUES (%s, %s, %s, %s)",
                        insert_data
                    )
                    conn.commit()
                    notif_cur.close()
            except Exception as notif_e:
                # Log the error but don't interrupt the user – the listing is already saved
                print(f"Failed to send notifications: {notif_e}")
                # The transaction for notifications will be rolled back when the connection closes
                pass

        return jsonify({
            "success": True,
            "message": "Item added successfully!",
            "redirect": url_for('store_home', store_id=store_id)
        })

    except Exception as e:
        conn.rollback()
        print("Database error while saving listing:", str(e))
        return jsonify({
            "success": False,
            "message": "Failed to save listing. Please try again."
        }), 500

    finally:
        cur.close()
        conn.close()




# Add custom filter
@app.template_filter('from_json')
def from_json_filter(value):
    """Safely convert JSON string to Python object, return empty list if invalid"""
    if value is None or value == '':
        return []
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []




@app.route('/store/<slug>/edit', methods=['GET', 'POST'])
@login_required
def edit_store(slug):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    # Fetch store - only if it belongs to the logged-in user
    cur.execute("""
        SELECT * FROM stores 
        WHERE slug = %s AND user_id = %s
    """, (slug, session['user_id']))

    store = cur.fetchone()

    if not store:
        flash("Store not found or you don't have permission to edit it.", "danger")
        cur.close()
        conn.close()
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        # Basic fields (slug is NOT editable)
        name = (request.form.get('name') or store['name'] or '').strip()
        location = (request.form.get('location') or store.get('location') or '').strip()
        contact = (request.form.get('contact') or store.get('contact') or '').strip()
        description = (request.form.get('description') or store.get('description') or '').strip()
        store_type = (request.form.get('store_type') or store.get('store_type') or '').strip()

        # Delivery options (checkboxes -> JSON)
        delivery_options = request.form.getlist('delivery_options')
        delivery_json = json.dumps(delivery_options) if delivery_options else None

        # Existing media (keep unless removed/replaced)
        logo_url = store.get('logo')
        banner_url = store.get('banner')
        tour_video_url = store.get('tour_video')

        # Remove media if requested (checkboxes)
        if request.form.get('remove_logo') == '1':
            logo_url = None

        if request.form.get('remove_banner') == '1':
            banner_url = None

        if request.form.get('remove_tour_video') == '1':
            tour_video_url = None

        # Upload new media (replaces existing, unless user chose "remove")
        if logo_url is not None and 'logo' in request.files and request.files['logo'].filename:
            try:
                logo_url = save_file(request.files['logo'], 'logos')
            except Exception as e:
                flash(f"Failed to upload logo: {str(e)}", "danger")

        if banner_url is not None and 'banner' in request.files and request.files['banner'].filename:
            try:
                banner_url = save_file(request.files['banner'], 'banners')
            except Exception as e:
                flash(f"Failed to upload banner: {str(e)}", "danger")

        if tour_video_url is not None and 'tour_video' in request.files and request.files['tour_video'].filename:
            try:
                tour_video_url = save_file(request.files['tour_video'], 'videos')
            except Exception as e:
                flash(f"Failed to upload tour video: {str(e)}", "danger")

        try:
            cur.execute("""
                UPDATE stores 
                SET 
                    name = %s,
                    location = %s,
                    contact = %s,
                    description = %s,
                    store_type = %s,
                    delivery_options = %s,
                    logo = %s,
                    banner = %s,
                    tour_video = %s,
                    updated_at = NOW()
                WHERE store_id = %s
            """, (
                name,
                location,
                contact,
                description,
                store_type,
                delivery_json,
                logo_url,
                banner_url,
                tour_video_url,
                store['store_id']
            ))

            conn.commit()
            flash("Store updated successfully!", "success")
            return redirect(url_for('store_home', store_id=store['store_id']))

        except Exception as e:
            conn.rollback()
            flash(f"Failed to update store: {str(e)}", "danger")
            print("Store update error:", e)

    # Convert JSON delivery_options back to list for checkboxes
    cur.close()
    conn.close()

    current_delivery = json.loads(store['delivery_options']) if store.get('delivery_options') else []

    return render_template(
        'edit_store.html',
        store=store,
        current_delivery=current_delivery
    )





@app.route('/shops')
def all_shops():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    search = request.args.get('search', '').strip()
    location = request.args.get('location', '').strip()
    store_type = request.args.get('store_type', '').strip()

    # Base query
    query = """
        SELECT *,
               CASE 
                   WHEN Plan = 'Diamond' THEN 4
                   WHEN Plan = 'Gold'    THEN 3
                   WHEN Plan = 'Silver'  THEN 2
                   ELSE 1  -- Basic or NULL
               END AS plan_priority
        FROM stores
        WHERE is_active = 1
    """
    params = []

    # Search filters
    if search:
        query += " AND name LIKE %s"
        params.append(f"%{search}%")

    if location:
        query += " AND location LIKE %s"
        params.append(f"%{location}%")

    if store_type:
        query += " AND store_type = %s"
        params.append(store_type)

    # Sorting – most important first
    query += """
        ORDER BY 
            plan_priority DESC,          -- Diamond (4) first, then Gold (3), Silver (2), Basic (1)
            trust_score DESC,            -- Higher trust first within same plan
            verified DESC,               -- Verified (1) before non-verified (0)
            created_at DESC              -- Newest first as final tie-breaker
    """

    cursor.execute(query, params)
    stores = cursor.fetchall()

    # Fetch distinct store types for filter dropdown
    cursor.execute("""
        SELECT DISTINCT store_type
        FROM stores
        WHERE store_type IS NOT NULL AND store_type != ''
        ORDER BY store_type
    """)
    store_types = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        'shops.html',
        stores=stores,
        store_types=store_types,
        search=search,
        location=location,
        store_type=store_type
    )



def datetimeformat(value, format='%Y'):
    if value is None:
        value = datetime.now()
    return value.strftime(format)

# Add the filter
app.jinja_env.filters['date'] = datetimeformat



@app.route('/store/<slug>')
def store_detail(slug):
    """
    Display a single store page with:
    - Store info + Meet the Seller card
    - Floating WhatsApp button
    - Active product listings (including swap deal offers)
    - Rating form (conditional on login & not owner)
    - List of all existing ratings/comments
    - Promotion popup (if configured and active)
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # 1. Fetch store data by slug – including color_theme
        cursor.execute("""
            SELECT 
                store_id,
                user_id,
                name,
                slug,
                logo,
                banner,
                tour_video,
                description,
                location,
                contact,
                delivery_options,
                verified,
                store_type,
                rating_avg,
                rating_count,
                created_at,
                color_theme
            FROM stores
            WHERE slug = %s 
              AND is_active = 1
            LIMIT 1
        """, (slug,))
        
        store = cursor.fetchone()
        if not store:
            abort(404)

        # Normalize & defaults
        store['store_id']          = int(store['store_id'])
        store['user_id']           = int(store['user_id']) if store.get('user_id') else None
        store['name']              = store.get('name', 'Unnamed Store')
        store['description']       = store.get('description', 'No description available.')
        store['location']          = store.get('location', 'Location not specified')
        store['contact']           = store.get('contact', None)
        store['delivery_options']  = store.get('delivery_options', [])  
        store['tour_video']        = store.get('tour_video', None)
        store['verified']          = bool(store.get('verified', False))
        store['store_type']        = store.get('store_type', 'General')
        store['rating_avg']        = float(store.get('rating_avg') or 0.0)
        store['rating_count']      = int(store.get('rating_count') or 0)
        store['color_theme']       = store.get('color_theme') or 'default'

        # Format join date
        store['join_date'] = store['created_at'].strftime("%b %Y") if store.get('created_at') else "Unknown"

        # 2. Fetch seller username + contact
        cursor.execute("""
            SELECT 
                username,
                contact
            FROM users
            WHERE id = %s
            LIMIT 1
        """, (store['user_id'],))
        
        user_row = cursor.fetchone()
        store['seller_username'] = user_row['username'] if user_row and user_row.get('username') else 'Unknown Seller'
        store['vendor_contact']  = user_row['contact'] if user_row and user_row.get('contact') else None

        # 3. Login & ownership status
        is_logged_in = 'user_id' in session
        is_owner = is_logged_in and session.get('user_id') == store['user_id']

        # 4. Fetch active promotion popup
        cursor.execute("""
            SELECT 
                media_type,
                media_url,
                description,
                button_text,
                button_link,
                frequency
            FROM store_promos
            WHERE store_id = %s 
              AND active = 1
              AND (start_date IS NULL OR start_date <= CURDATE())
              AND (end_date IS NULL OR end_date >= CURDATE())
            LIMIT 1
        """, (store['store_id'],))
        promo = cursor.fetchone() or {}
        promo['active'] = bool(promo.get('media_url'))

        # 5. Fetch active listings
        cursor.execute("""
            SELECT 
                listing_id,
                title,
                description,
                image_url,
                image1,
                image2,
                image3,
                image4,
                category,
                price,
                deal_type,
                `condition`,
                desired_swap,
                required_cash,
                additional_cash,
                swap_notes,
                created_at
            FROM listings
            WHERE store_id = %s 
              AND status = 'Active'
            ORDER BY created_at DESC
        """, (store['store_id'],))
        
        listings = cursor.fetchall()

        # ---------- NEW LOGIC: Add is_new flag ----------
        now = datetime.now()
        seven_days_ago = now - timedelta(days=7)

        for listing in listings:
            # Check if created_at is within last 7 days
            listing['is_new'] = listing['created_at'] >= seven_days_ago

            if listing.get('deal_type') == 'Swap Deal':
                cursor.execute("""
                    SELECT 
                        item_id,
                        title,
                        description,
                        `condition`,
                        image_url,
                        image1,
                        image2,
                        image3,
                        image4
                    FROM offered_items
                    WHERE listing_id = %s
                    ORDER BY item_id
                """, (listing['listing_id'],))
                listing['offers'] = cursor.fetchall()
            else:
                listing['offers'] = []

            images = [img for img in [
                listing.get('image_url'),
                listing.get('image1'),
                listing.get('image2'),
                listing.get('image3'),
                listing.get('image4')
            ] if img]
            listing['main_image'] = images[0] if images else '/static/images/placeholder.jpg'

            try: listing['price_float'] = float(listing['price']) if listing['price'] else 0.0
            except: listing['price_float'] = 0.0

            try: listing['required_cash_float'] = float(listing['required_cash']) if listing['required_cash'] is not None else 0.0
            except: listing['required_cash_float'] = 0.0

            try: listing['additional_cash_float'] = float(listing['additional_cash']) if listing['additional_cash'] is not None else 0.0
            except: listing['additional_cash_float'] = 0.0

        categories_set = {l.get('category') for l in listings if l.get('category')}
        sorted_categories = sorted(categories_set)

        # 6. Fetch ratings
        cursor.execute("""
            SELECT 
                rating,
                comment,
                created_at
            FROM store_ratings
            WHERE store_id = %s
            ORDER BY created_at DESC
            LIMIT 100
        """, (store['store_id'],))
        ratings = cursor.fetchall()

        # 7. Render
        return render_template(
            'store_detail.html',
            store=store,
            listings=listings,
            categories=sorted_categories,
            ratings=ratings,
            is_logged_in=is_logged_in,
            is_owner=is_owner,
            promo=promo,
            current_year=datetime.now().year
        )

    except Exception as e:
        current_app.logger.error(f"Error in store_detail (slug={slug}): {str(e)}")
        abort(500)

    finally:
        cursor.close()
        conn.close()



@app.route('/store/<slug>/inventory')
@login_required
def store_inventory(slug):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    # ── Fetch store info including slug ─────────────────────────
    cur.execute("""
        SELECT store_id, name, slug
        FROM stores
        WHERE slug=%s AND user_id=%s AND is_active=1
    """, (slug, session['user_id']))
    store = cur.fetchone()

    if not store:
        cur.close()
        conn.close()
        abort(403)

    # ── Fetch all listings for this store ───────────────────────
    cur.execute("""
        SELECT *
        FROM listings
        WHERE store_id=%s
        ORDER BY created_at DESC
    """, (store['store_id'],))
    listings = cur.fetchall()

    cur.close()
    conn.close()

    # ── Render inventory page ─────────────────────────────────
    return render_template(
        'store_inventory.html',
        store=store,
        listings=listings
    )




@app.route('/store/<slug>/inventory/<int:listing_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_inventory(slug, listing_id):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    # ── Verify store ownership ─────────────────────────
    cur.execute("""
        SELECT store_id, name 
        FROM stores 
        WHERE slug=%s AND user_id=%s AND is_active=1
    """, (slug, session['user_id']))
    store = cur.fetchone()

    if not store:
        cur.close()
        conn.close()
        abort(403)

    store_id = store['store_id']

    # ── Verify listing ownership ───────────────────────
    cur.execute("""
        SELECT *
        FROM listings
        WHERE listing_id=%s AND store_id=%s
    """, (listing_id, store_id))
    listing = cur.fetchone()

    if not listing:
        cur.close()
        conn.close()
        abort(404)

    # ── Load additional offered items (skip first) ────
    offers = []
    if listing['deal_type'] == 'Swap Deal':
        cur.execute("""
            SELECT *
            FROM offered_items
            WHERE listing_id=%s
            ORDER BY item_id ASC
            LIMIT 100 OFFSET 1
        """, (listing_id,))
        offers = cur.fetchall()

    # ── POST: UPDATE ─────────────────────────────────
    if request.method == 'POST':

        def upload_if_exists(field, folder):
            file = request.files.get(field)
            if file and file.filename:
                return upload_to_cloudinary(file, folder)
            return None

        # ── Update main listing ─────────────────────
        update_fields = {
            'title': request.form.get('title'),
            'description': request.form.get('description'),
            'category': request.form.get('category'),
            'condition': request.form.get('condition'),
            'location': request.form.get('location'),
            'contact': request.form.get('contact'),
            'status': request.form.get('status'),
            'price': request.form.get('price'),
            'desired_swap': request.form.get('desired_swap'),
            'required_cash': request.form.get('required_cash'),
            'additional_cash': request.form.get('additional_cash'),
            'swap_notes': request.form.get('swap_notes')
        }

        # Upload new main images
        for img in ['image_url','image1','image2','image3','image4']:
            uploaded = upload_if_exists(f"{img}_file", 'listings')
            if uploaded:
                update_fields[img] = uploaded

        # Build SQL
        set_sql = ", ".join(f"`{k}`=%s" for k,v in update_fields.items() if v is not None)
        values = [v for v in update_fields.values() if v is not None]
        values.append(listing_id)

        cur.execute(f"""
            UPDATE listings
            SET {set_sql}
            WHERE listing_id=%s
        """, values)

        # ── Swap Deal: Update offered items ─────────────
        if listing['deal_type'] == 'Swap Deal':

            # First offered item sync (main) if exists
            cur.execute("""
                SELECT item_id FROM offered_items
                WHERE listing_id=%s
                ORDER BY item_id ASC LIMIT 1
            """, (listing_id,))
            first = cur.fetchone()

            if first:
                cur.execute("""
                    UPDATE offered_items
                    SET `title`=%s, `description`=%s, `condition`=%s,
                        image_url=%s, image1=%s, image2=%s, image3=%s, image4=%s
                    WHERE item_id=%s
                """, (
                    update_fields['title'],
                    update_fields['description'],
                    update_fields['condition'],
                    request.form.get('offer_image_url_0') or listing.get('image_url'),
                    request.form.get('offer_image1_0') or listing.get('image1'),
                    request.form.get('offer_image2_0') or listing.get('image2'),
                    request.form.get('offer_image3_0') or listing.get('image3'),
                    request.form.get('offer_image4_0') or listing.get('image4'),
                    first['item_id']
                ))

            # Update additional offered items
            titles = request.form.getlist('offer_title[]')
            descs = request.form.getlist('offer_description[]')
            conds = request.form.getlist('offer_condition[]')

            for idx, offer in enumerate(offers):
                if idx < len(titles):
                    img_fields = []
                    for i,img_name in enumerate(['image_url','image1','image2','image3','image4']):
                        uploaded = upload_if_exists(f"offer_{img_name}_{idx+1}", 'offers')
                        img_fields.append(uploaded or offer.get(img_name))
                    cur.execute("""
                        UPDATE offered_items
                        SET title=%s, description=%s, `condition`=%s,
                            image_url=%s, image1=%s, image2=%s, image3=%s, image4=%s
                        WHERE item_id=%s
                    """, (
                        titles[idx], descs[idx], conds[idx],
                        *img_fields,
                        offer['item_id']
                    ))

        conn.commit()
        cur.close()
        conn.close()
        return redirect(url_for('store_inventory', slug=slug))

    cur.close()
    conn.close()
    return render_template(
        'edit_inventory.html',
        store=store,
        listing=listing,
        offers=offers
    )


    





@app.route('/api/listing/<int:listing_id>/edit', methods=['GET'])
@login_required
def get_edit_form(listing_id):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    
    # Get the listing with all images
    cur.execute("""
        SELECT 
            listing_id, title, description, 
            image_url, image1, image2, image3, image4,
            category, price, deal_type, `condition`, 
            location, contact, desired_swap, required_cash, 
            additional_cash, swap_notes, status, store_id
        FROM listings
        WHERE listing_id = %s
    """, (listing_id,))
    
    item = cur.fetchone()
    
    if not item:
        return "Item not found", 404
    
    # Verify ownership
    cur.execute("SELECT user_id FROM stores WHERE store_id = %s", (item['store_id'],))
    store = cur.fetchone()
    
    if not store or store['user_id'] != session['user_id']:
        return "Unauthorized", 403
    
    # Get offered items for swap deals
    if item['deal_type'] == 'Swap Deal':
        cur.execute("""
            SELECT 
                item_id, title, description, `condition`,
                image_url, image1, image2, image3, image4
            FROM offered_items
            WHERE listing_id = %s
            ORDER BY item_id ASC
        """, (listing_id,))
        item['offers'] = cur.fetchall()
    else:
        item['offers'] = []
    
    cur.close()
    conn.close()
    
    # Render the edit form template
    return render_template('edit_form_partial.html', item=item)




@app.route('/my-store')
@login_required
def my_store_redirect():
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    try:
        cur.execute(
            """
            SELECT store_id, slug 
            FROM stores 
            WHERE user_id = %s 
              AND is_active = 1
            LIMIT 1
            """,
            (session['user_id'],)
        )
        store = cur.fetchone()

        if store:
            # Redirect to PRIVATE DASHBOARD using store_id
            return redirect(url_for('store_home', store_id=store['store_id']))
            # If you kept the name 'store_home' for the dashboard:
            # return redirect(url_for('store_home', store_id=store['store_id']))

        else:
            flash("You don't have an active store yet.", "info")
            return redirect(url_for('create_store'))

    except Exception as e:
        current_app.logger.error(f"my_store_redirect error: {str(e)}")
        flash("Could not load your store. Please try again.", "error")
        return redirect(url_for('home'))

    finally:
        cur.close()
        conn.close()




def _inc_store_metric(store_id: int, field: str, amount: int = 1):
    """Increment one metric field for (store_id, today). Requires UNIQUE(store_id, dt)."""
    if field not in {"views", "clicks", "chats", "swaps", "sales"}:
        raise ValueError("Invalid metric field")

    conn = get_db_connection()
    cur = conn.cursor(buffered=True)
    try:
        today = date.today()

        # Requires UNIQUE KEY on (store_id, dt)
        sql = f"""
            INSERT INTO store_metrics (store_id, dt, {field})
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE {field} = COALESCE({field}, 0) + VALUES({field})
        """
        cur.execute(sql, (store_id, today, amount))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print("Store metrics error:", str(e))
        return False
    finally:
        cur.close()
        conn.close()



  


@app.route("/metrics/store/view", methods=["POST"])
def metric_store_view():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")

    try:
        store_id = int(store_id)
    except:
        return jsonify({"success": False, "message": "Invalid store_id"}), 400

    ok = _inc_store_metric(store_id, "views", 1)
    return jsonify({"success": ok})


@app.route("/metrics/store/click", methods=["POST"])
def metric_store_click():
    data = request.get_json(silent=True) or {}
    store_id = data.get("store_id")

    try:
        store_id = int(store_id)
    except:
        return jsonify({"success": False, "message": "Invalid store_id"}), 400

    ok = _inc_store_metric(store_id, "clicks", 1)
    return jsonify({"success": ok})



def _inc_listing_metric(listing_id: int, field: str, amount: int = 1):
    if field not in {"impressions", "clicks", "carousel_impressions"}:
        raise ValueError("Invalid metric field")

    conn = get_db_connection()
    cur = conn.cursor(buffered=True)
    try:
        sql = f"""
            INSERT INTO listing_metrics (listing_id, {field}, updated_at)
            VALUES (%s, %s, NOW())
            ON DUPLICATE KEY UPDATE
                {field} = COALESCE({field}, 0) + VALUES({field}),
                updated_at = NOW()
        """
        cur.execute(sql, (listing_id, amount))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print("Listing metrics error:", str(e))
        return False
    finally:
        cur.close()
        conn.close()


@app.route("/metrics/listing/impression", methods=["POST"])
def metric_listing_impression():
    data = request.get_json(silent=True) or {}
    listing_id = data.get("listing_id")

    try:
        listing_id = int(listing_id)
    except:
        return jsonify({"success": False, "message": "Invalid listing_id"}), 400

    ok = _inc_listing_metric(listing_id, "impressions", 1)
    return jsonify({"success": ok})


@app.route("/metrics/listing/click", methods=["POST"])
def metric_listing_click():
    data = request.get_json(silent=True) or {}
    listing_id = data.get("listing_id")

    try:
        listing_id = int(listing_id)
    except:
        return jsonify({"success": False, "message": "Invalid listing_id"}), 400

    ok = _inc_listing_metric(listing_id, "clicks", 1)
    return jsonify({"success": ok})
        





VERIFY_THRESHOLD       = 3.5
MIN_REVIEWS_FOR_VERIFY = 10   # or 10 — your choice
M_BAYES                = 10  # prior strength


def clamp(val, lo=0.0, hi=5.0):
    return max(lo, min(hi, float(val)))


def recompute_store_rating(store_id: int) -> bool:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT 
                ROUND(COALESCE(AVG(rating), 0), 2) AS avg_rating,
                COUNT(*) AS cnt
            FROM store_ratings 
            WHERE store_id = %s
        """, (store_id,))
        row = cur.fetchone() or {"avg_rating": 0.0, "cnt": 0}

        cur.execute("""
            UPDATE stores 
            SET 
                rating_avg   = %s,
                rating_count = %s,
                updated_at   = NOW()
            WHERE store_id = %s
        """, (row["avg_rating"], row["cnt"], store_id))

        conn.commit()
        return True

    except Exception as e:
        conn.rollback()
        print(f"Rating recalc error (store {store_id}): {e}")
        return False
    finally:
        cur.close()
        conn.close()


def recompute_store_trust_and_verification(store_id: int) -> bool:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        # 1. Load store
        cur.execute("""
            SELECT store_id, user_id, rating_avg, rating_count, is_active,
                   logo, banner, description, location, contact, 
                   store_link, created_at
            FROM stores 
            WHERE store_id = %s
        """, (store_id,))
        store = cur.fetchone()
        if not store:
            return False

        if not store["is_active"]:
            cur.execute("""
                UPDATE stores 
                SET trust_score = 0, verified = 0, updated_at = NOW() 
                WHERE store_id = %s
            """, (store_id,))
            conn.commit()
            return True

        R = float(store["rating_avg"] or 0.0)
        v = int(store["rating_count"] or 0)

        # 2. Global average rating C (fallback 4.0)
        cur.execute("""
            SELECT COALESCE(AVG(rating_avg), 4.0) AS C 
            FROM stores 
            WHERE rating_count > 0
        """)
        C = float(cur.fetchone()["C"] or 4.0)

        # 3. Bayesian adjusted rating
        denom = v + M_BAYES
        R_adj = (v / denom) * R + (M_BAYES / denom) * C
        R_adj = clamp(R_adj)

        # 4. Activity score A — only views and clicks now
        cur.execute("""
            SELECT 
                COALESCE(SUM(views), 0)  AS views,
                COALESCE(SUM(clicks), 0) AS clicks
            FROM store_metrics
            WHERE store_id = %s
              AND dt >= CURDATE() - INTERVAL 30 DAY
        """, (store_id,))
        mrow = cur.fetchone() or {"views": 0, "clicks": 0}

        # Weighted engagement (only views × 1, clicks × 3)
        engagement = (mrow["views"] * 1) + (mrow["clicks"] * 3)

        # Log scaling — same target of ~1500 points
        A = 5.0 * (math.log1p(engagement) / math.log1p(1500))
        A = clamp(A)

        # 5. Profile completeness P — removed delivery_options
        fields = ["logo", "banner", "description", "location", "contact", "store_link"]
        present = sum(1 for f in fields if store.get(f))
        P = 5.0 * (present / len(fields))
        P = clamp(P)

        # 6. Age score G (unchanged)
        days = 0
        if store["created_at"]:
            if isinstance(store["created_at"], str):
                try:
                    created = datetime.fromisoformat(store["created_at"])
                except:
                    created = None
            else:
                created = store["created_at"]
            
            if created:
                days = max(0, (datetime.utcnow() - created).days)

        G = 5.0 * min(days, 180) / 180.0
        G = clamp(G)

        # 7. Final trust score (weights unchanged)
        trust = clamp(0.60 * R_adj + 0.20 * A + 0.10 * P + 0.10 * G)

        # 8. Verification rule
        should_verify = 1 if (trust >= VERIFY_THRESHOLD and v >= MIN_REVIEWS_FOR_VERIFY) else 0

        cur.execute("""
            UPDATE stores
            SET 
                trust_score = ROUND(%s, 2),
                verified    = %s,
                updated_at  = NOW()
            WHERE store_id = %s
        """, (trust, should_verify, store_id))

        conn.commit()
        return True

    except Exception as e:
        conn.rollback()
        print(f"Trust update error (store {store_id}): {e}")
        return False
    finally:
        cur.close()
        conn.close()


@app.route("/ratings/store", methods=["POST"])
def rate_store():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "message": "Please log in to rate"}), 401

    data = request.get_json(silent=True) or {}
    try:
        store_id = int(data.get("store_id"))
        rating   = int(data.get("rating"))
        comment  = (data.get("comment") or "").strip()[:1000]  # reasonable limit
    except (ValueError, TypeError):
        return jsonify({"success": False, "message": "Invalid input"}), 400

    if not (1 <= rating <= 5):
        return jsonify({"success": False, "message": "Rating must be 1–5 stars"}), 400

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT user_id, is_active 
            FROM stores 
            WHERE store_id = %s
        """, (store_id,))
        store = cur.fetchone()

        if not store:
            return jsonify({"success": False, "message": "Store not found"}), 404

        if not store["is_active"]:
            return jsonify({"success": False, "message": "Store is inactive"}), 400

        if int(store["user_id"]) == int(user_id):
            return jsonify({"success": False, "message": "Cannot rate your own store"}), 403

        # UPSERT rating
        cur.execute("""
            INSERT INTO store_ratings (store_id, user_id, rating, comment)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                rating = VALUES(rating),
                comment = VALUES(comment),
                updated_at = NOW()
        """, (store_id, user_id, rating, comment or None))

        conn.commit()

    except Exception as e:
        conn.rollback()
        print(f"Rate store error: {e}")
        return jsonify({"success": False, "message": "Database error"}), 500
    finally:
        cur.close()
        conn.close()

    # Update aggregates & trust
    recompute_store_rating(store_id)
    recompute_store_trust_and_verification(store_id)

    # Return updated values
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT rating_avg, rating_count, verified
            FROM stores 
            WHERE store_id = %s
        """, (store_id,))
        row = cur.fetchone() or {}

        return jsonify({
            "success":      True,
            "rating_avg":   float(row.get("rating_avg") or 0.0),
            "rating_count": int(row.get("rating_count") or 0),
            "verified":     int(row.get("verified") or 0),
        })
    finally:
        cur.close()
        conn.close()


# Optional – useful for debugging or manual trigger
@app.route("/admin/recompute-store-trust", methods=["POST"])
def admin_recompute_all_trust():
    # ← Add admin auth check here in production
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT store_id FROM stores WHERE is_active = 1")
        store_ids = [r["store_id"] for r in cur.fetchall()]

        success_count = 0
        for sid in store_ids:
            if recompute_store_trust_and_verification(sid):
                success_count += 1

        return jsonify({"success": True, "updated": success_count, "total": len(store_ids)})
    except Exception as e:
        print(f"Batch trust error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        cur.close()
        conn.close()









PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")

# Plan configuration (prices in GHS, duration in days)
STORE_PLANS = {
    "Silver":   {"price": 25,  "days": 14,  "label": "Silver - 2 Weeks"},
    "Gold":     {"price": 50,  "days": 21,  "label": "Gold - 3 Weeks"},
    "Diamond":  {"price": 100, "days": 30,  "label": "Diamond - 1 Month"},
}

@app.route('/store/<slug>/boost', methods=['GET', 'POST'])
@login_required
def store_boost(slug):
    # Verify this is the user's store
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT store_id, name, slug, Plan, Plan_expiry_date 
        FROM stores 
        WHERE slug = %s AND user_id = %s
    """, (slug, session['user_id']))
    store = cur.fetchone()
    cur.close()
    conn.close()

    if not store:
        flash("Store not found or you don't have permission.", "error")
        return redirect(url_for('dashboard'))

    current_plan = store['Plan'] or "Basic"
    expiry = store['Plan_expiry_date']

    if request.method == 'POST':
        selected_plan = request.form.get('plan')

        if selected_plan not in STORE_PLANS:
            flash("Invalid plan selected.", "error")
            return redirect(url_for('store_boost', slug=slug))

        plan_info = STORE_PLANS[selected_plan]
        amount_ghs = plan_info['price']

        if amount_ghs == 0:
            # Free plan (downgrade or reset)
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                UPDATE stores 
                SET Plan = %s, Plan_expiry_date = NULL 
                WHERE store_id = %s
            """, ("Basic", store['store_id']))
            conn.commit()
            cur.close()
            conn.close()

            flash("Store plan reset to Basic.", "success")
            return redirect(url_for('store_home', slug=slug))

        # Paid plan → initialize Paystack transaction
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT email FROM users WHERE id = %s", (session['user_id'],))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if not user or not user['email']:
            flash("Cannot start payment – email not found.", "error")
            return redirect(url_for('store_boost', slug=slug))

        # Save pending promotion in session
        session['pending_store_promotion'] = {
            'store_id': store['store_id'],
            'store_slug': slug,
            'user_id': session['user_id'],
            'plan': selected_plan,
            'amount_ghs': amount_ghs,
            'days': plan_info['days']
        }

        # Paystack payload (amount in kobo = GHS × 100)
        payload = {
            "email": user['email'],
            "amount": int(amount_ghs * 100),  # e.g. 2500 for GHS 25
            "currency": "GHS",
            "reference": f"store_promo_{store['store_id']}_{int(datetime.now().timestamp())}",
            "callback_url": url_for('store_boost_verify', _external=True),
            "metadata": {
                "store_id": store['store_id'],
                "plan": selected_plan
            }
        }

        headers = {
            "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json"
        }

        resp = requests.post(
            "https://api.paystack.co/transaction/initialize",
            json=payload,
            headers=headers
        ).json()

        if resp.get('status'):
            return redirect(resp['data']['authorization_url'])
        else:
            flash("Failed to initialize payment. Please try again.", "error")
            return redirect(url_for('store_boost', slug=slug))

    # GET → show plans page
    return render_template(
        'boost.html',
        store=store,
        current_plan=current_plan,
        expiry=expiry,
        plans=STORE_PLANS
    )


@app.route('/store/boost/verify')
@login_required
def store_boost_verify():
    ref = request.args.get('reference')
    if not ref:
        flash("Payment verification failed – missing reference.", "error")
        return redirect(url_for('dashboard'))

    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    resp = requests.get(
        f"https://api.paystack.co/transaction/verify/{ref}",
        headers=headers
    ).json()

    if not resp.get('status') or resp['data']['status'] != 'success':
        flash("Payment failed or was not completed.", "error")
        return redirect(url_for('dashboard'))

    pending = session.get('pending_store_promotion')
    if not pending:
        flash("No pending promotion found.", "error")
        return redirect(url_for('dashboard'))

    # Verify this transaction belongs to this user/store
    if pending['user_id'] != session['user_id']:
        flash("Security error: transaction mismatch.", "error")
        session.pop('pending_store_promotion', None)
        return redirect(url_for('dashboard'))

    plan = pending['plan']
    days = pending['days']
    store_id = pending['store_id']

    # Calculate new expiry
    expires_at = datetime.utcnow() + timedelta(days=days)

    conn = get_db_connection()
    cur = conn.cursor()

    try:
        cur.execute("""
            UPDATE stores 
            SET Plan = %s, Plan_expiry_date = %s 
            WHERE store_id = %s AND user_id = %s
        """, (plan, expires_at, store_id, session['user_id']))

        conn.commit()

        flash(f"Store successfully boosted! {STORE_PLANS[plan]['label']} activated until {expires_at.strftime('%Y-%m-%d')}.", "success")

    except Exception as e:
        conn.rollback()
        print("Boost update error:", str(e))
        flash("Payment succeeded but failed to update store plan. Contact support.", "error")

    finally:
        cur.close()
        conn.close()
        session.pop('pending_store_promotion', None)

    return redirect(url_for('store_home', slug=pending['store_slug']))





SITE_URL = os.getenv("SITE_URL", "https://vendupp.com").rstrip("/")
PLATFORM_BRAND = os.getenv("PLATFORM_BRAND", "VendUpp")
CURRENCY = os.getenv("CURRENCY", "GHS")

# If your product pages are different, change this:
LISTING_PATH = os.getenv("LISTING_PATH", "/listing/")  # results: https://site.com/listing/<id>


def _abs_url(url: str) -> str:
    """Turn /path into https://site.com/path; keep absolute URLs as-is."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return f"{SITE_URL}{url}"
    # if stored like "uploads/x.jpg"
    return f"{SITE_URL}/{url.lstrip('/')}"


def _extract_price_number(price_raw):
    """
    Your listings.price is varchar(45).
    This tries to pull a numeric price out of strings like:
      "1200", "GHS 1,200", "1,200.50", "₵1200", "1200 GHS"
    Returns float or None.
    """
    if price_raw is None:
        return None
    s = str(price_raw).strip()
    if not s:
        return None

    # Remove common currency symbols/words, keep digits/commas/dots
    s = s.replace("₵", "").replace("GHS", "").replace("ghs", "").replace("GH₵", "")
    # Find first number-ish token
    m = re.search(r"(\d[\d,]*\.?\d*)", s)
    if not m:
        return None
    num = m.group(1).replace(",", "")
    try:
        return float(num)
    except ValueError:
        return None


def _format_price(value_float: float) -> str:
    # Google accepts "1500 GHS" or "1500.00 GHS"
    # Keep 2 decimals only if needed
    if value_float.is_integer():
        return f"{int(value_float)} {CURRENCY}"
    return f"{value_float:.2f} {CURRENCY}"


def _availability_from_status(status: str) -> str:
    # Map your listing status to Google availability
    # Adjust if your statuses differ.
    s = (status or "").lower()
    if s in {"active", "available", "published", "live"}:
        return "in_stock"
    if s in {"sold", "inactive", "disabled", "expired", "archived"}:
        return "out_of_stock"
    # default safe:
    return "in_stock"


@app.get("/google-products.xml")
def google_products_feed():
    conn = mysql.connector.connect(**dbconfig)
    cur = conn.cursor(dictionary=True)

    try:
        # Pull listings + store info
        # Filters:
        # - status active-ish (change as needed)
        # - not expired (if expires_at is used)
        # - must have at least one image
        query = """
            SELECT
                l.listing_id,
                l.title,
                l.description,
                l.category,
                l.condition,
                l.location,
                l.status,
                l.deal_type,
                l.price,
                l.required_cash,
                l.additional_cash,
                l.image_url,
                l.image1, l.image2, l.image3, l.image4,
                l.expires_at,
                l.is_featured,
                s.store_id,
                s.name AS store_name,
                s.slug AS store_slug,
                s.verified,
                s.rating_avg,
                s.rating_count
            FROM listings l
            LEFT JOIN stores s ON s.store_id = l.store_id
            WHERE (s.is_active = 1 OR s.is_active IS NULL)
              AND (l.status IS NULL OR LOWER(l.status) NOT IN ('deleted'))
              AND (l.expires_at IS NULL OR l.expires_at > NOW())
            ORDER BY l.is_featured DESC, l.created_at DESC
            LIMIT 10000;
        """
        cur.execute(query)
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    # Build Google RSS XML feed
    xml = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">',
        '<channel>',
        f'<title>{escape(PLATFORM_BRAND)} Products</title>',
        f'<link>{escape(SITE_URL)}</link>',
        '<description>Google Merchant Center feed</description>',
    ]

    for r in rows:
        listing_id = str(r.get("listing_id", "")).strip()
        title = (r.get("title") or "").strip()
        desc = (r.get("description") or "").strip()

        # Choose best image field available
        images = []
        for k in ("image_url", "image1", "image2", "image3", "image4"):
            u = _abs_url(r.get(k) or "")
            if u and u not in images:
                images.append(u)

        if not listing_id or not title or not desc or not images:
            continue

        # Only include listings that can behave like a "sellable" product
        # If deal_type is swap-only, Google Shopping usually isn't a fit.
        deal_type = (r.get("deal_type") or "").lower().strip()
        price_float = _extract_price_number(r.get("price"))

        # Fallback: if you use required_cash/additional_cash as the price
        if price_float is None:
            # try required_cash then additional_cash
            rc = r.get("required_cash")
            ac = r.get("additional_cash")
            if rc is not None:
                try:
                    price_float = float(rc)
                except Exception:
                    price_float = None
            if price_float is None and ac is not None:
                try:
                    price_float = float(ac)
                except Exception:
                    price_float = None

        # Skip if still no price (Shopping requires it)
        if price_float is None:
            continue

        # Optional: if you want to ONLY include "sell/cash" listings, uncomment:
        # if deal_type not in ("sell", "sale", "cash", "buy", ""):
        #     continue

        store_name = (r.get("store_name") or "").strip()
        display_title = title if not store_name else f"{title} — From {store_name}"

        # Product page link
        product_link = f"{SITE_URL}{LISTING_PATH}{listing_id}"

        availability = _availability_from_status(r.get("status"))

        condition = (r.get("condition") or "new").lower().strip()
        # Google accepts: new, used, refurbished
        if condition not in ("new", "used", "refurbished"):
            condition = "new"

        category = (r.get("category") or "").strip()

        xml.append("<item>")
        xml.append(f"<g:id>{escape(listing_id)}</g:id>")
        xml.append(f"<g:title>{escape(display_title)}</g:title>")
        xml.append(f"<g:description>{escape(desc)}</g:description>")
        xml.append(f"<g:link>{escape(product_link)}</g:link>")
        xml.append(f"<g:image_link>{escape(images[0])}</g:image_link>")

        # Extra images (optional but good)
        for extra in images[1:]:
            xml.append(f"<g:additional_image_link>{escape(extra)}</g:additional_image_link>")

        xml.append(f"<g:availability>{escape(availability)}</g:availability>")
        xml.append(f"<g:condition>{escape(condition)}</g:condition>")
        xml.append(f"<g:price>{escape(_format_price(price_float))}</g:price>")

        # Brand: for marketplaces, safest is your platform as seller/brand
        xml.append(f"<g:brand>{escape(PLATFORM_BRAND)}</g:brand>")

        # Optional helpful fields
        if category:
            xml.append(f"<g:product_type>{escape(category)}</g:product_type>")

        # Useful for reporting / filtering in Google Ads
        # Feature flag as custom label
        if r.get("is_featured") in (1, True):
            xml.append("<g:custom_label_0>featured</g:custom_label_0>")
        else:
            xml.append("<g:custom_label_0>standard</g:custom_label_0>")

        # Store name as custom label (lets you promote specific shops later)
        if store_name:
            xml.append(f"<g:custom_label_1>{escape(store_name)}</g:custom_label_1>")

        xml.append("</item>")

    xml.extend(["</channel>", "</rss>"])
    return Response("\n".join(xml), mimetype="application/xml")







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






# ---------- CLOUDINARY HELPERS ----------
def upload_to_cloudinary(file, folder="store_promos", resource_type="auto"):
    try:
        timestamp = int(time.time())
        original_filename = secure_filename(file.filename)
        name_without_ext = os.path.splitext(original_filename)[0]
        public_id = f"{folder}/{name_without_ext}_{timestamp}"

        upload_result = cloudinary.uploader.upload(
            file,
            public_id=public_id,
            resource_type=resource_type,
            folder=folder,
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

# ---------- ROUTE: UPLOAD PROMO MEDIA (TEMP) ----------
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


    

# ---------- ROUTE: DELETE STORE PROMO ----------
@app.route('/store/<slug>/delete-promo', methods=['POST'])
def delete_store_promo(slug):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401

    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    try:
        cur.execute("""
            SELECT s.store_id, s.user_id, p.public_id, p.media_url 
            FROM stores s
            LEFT JOIN store_promos p ON s.store_id = p.store_id
            WHERE s.slug = %s
        """, (slug,))
        result = cur.fetchone()
        if not result:
            return jsonify({'success': False, 'message': 'Store not found'}), 404
        if result['user_id'] != session['user_id']:
            return jsonify({'success': False, 'message': 'Permission denied'}), 403
        if result.get('public_id'):
            resource_type = 'video' if result.get('media_url', '').endswith(('.mp4', '.mov', '.webm')) else 'image'
            delete_from_cloudinary(result['public_id'], resource_type)
        cur.execute("DELETE FROM store_promos WHERE store_id = %s", (result['store_id'],))
        conn.commit()
        return jsonify({'success': True, 'message': 'Promotion deleted successfully'})
    except Exception as e:
        current_app.logger.error(f"Error deleting promo: {str(e)}")
        return jsonify({'success': False, 'message': 'Delete failed'}), 500
    finally:
        cur.close()
        conn.close()



 # Custom filter: adds commas + keeps 2 decimal places
@app.template_filter('currency')
def currency_filter(value):
    try:
        # Convert to float
        value = float(value)
        # Format with commas and exactly 2 decimal places
        return "{:,.2f}".format(value)
    except (ValueError, TypeError):
        # Fallback if value can't be converted
        return str(value)       




@app.route('/store/<slug>/listing/<int:listing_id>/delete', methods=['POST'])
@login_required
def delete_store_listing(slug, listing_id):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    try:
        # Verify store ownership (same as before)
        cur.execute("SELECT store_id, user_id FROM stores WHERE slug = %s", (slug,))
        store = cur.fetchone()
        if not store:
            flash("Store not found.", "error")
            return redirect(url_for('home'))
        if store['user_id'] != session['user_id']:
            flash("Permission denied.", "error")
            return redirect(url_for('store_home', store_id=store['store_id']))

        # Verify listing belongs to store
        cur.execute("SELECT * FROM listings WHERE listing_id = %s AND store_id = %s", (listing_id, store['store_id']))
        listing = cur.fetchone()
        if not listing:
            flash("Listing not found.", "error")
            return redirect(url_for('store_inventory', slug=slug))

        # --- Delete dependent records ---
        # 1. Offers (if swap deal)
        if listing['deal_type'] == 'Swap Deal':
            # Delete offer images from Cloudinary (optional)
            cur.execute("SELECT * FROM offer_items WHERE listing_id = %s", (listing_id,))
            offers = cur.fetchall()
            for offer in offers:
                for field in ['image_url', 'image1', 'image2', 'image3', 'image4']:
                    if offer.get(field):
                        try:
                            delete_from_cloudinary(offer[field])
                        except Exception:
                            pass
            cur.execute("DELETE FROM offer_items WHERE listing_id = %s", (listing_id,))

        # 2. Listing metrics
        cur.execute("DELETE FROM listing_metrics WHERE listing_id = %s", (listing_id,))

        # 3. Notification logs (this is the missing piece)
        cur.execute("DELETE FROM notification_log WHERE listing_id = %s", (listing_id,))

        # 4. Other tables (add as needed, e.g., favorites, messages)
        # cur.execute("DELETE FROM favorites WHERE listing_id = %s", (listing_id,))
        # cur.execute("DELETE FROM messages WHERE listing_id = %s", (listing_id,))

        # --- Delete listing images from Cloudinary ---
        for field in ['image_url', 'image1', 'image2', 'image3', 'image4']:
            if listing.get(field):
                try:
                    delete_from_cloudinary(listing[field])
                except Exception as e:
                    print(f"Cloudinary deletion error: {e}")

        # --- Finally delete the listing ---
        cur.execute("DELETE FROM listings WHERE listing_id = %s", (listing_id,))

        conn.commit()
        flash("Listing deleted successfully.", "success")

    except Exception as e:
        conn.rollback()
        current_app.logger.error(f"Error deleting listing {listing_id}: {str(e)}")
        flash("An error occurred while deleting the listing.", "error")
    finally:
        cur.close()
        conn.close()

    return redirect(url_for('store_inventory', slug=slug))





@app.route('/store/<int:store_id>/follow', methods=['POST'])
def follow_store(store_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'You must be logged in'}), 401

    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "INSERT INTO follows (user_id, store_id) VALUES (%s, %s)",
            (user_id, store_id)
        )
        conn.commit()
        return jsonify({'success': True, 'message': 'Store followed'})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 400
    finally:
        cursor.close()
        conn.close()




@app.route('/store/<int:store_id>/unfollow', methods=['POST'])
def unfollow_store(store_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Not logged in'}), 401

    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "DELETE FROM follows WHERE user_id = %s AND store_id = %s",
            (user_id, store_id)
        )
        conn.commit()
        return jsonify({'success': True, 'message': 'Store unfollowed'})
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'message': str(e)}), 400
    finally:
        cursor.close()
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




@app.route('/notifications/unread-count', methods=['GET'])
def unread_notifications_count():
    if 'user_id' not in session:
        return jsonify({'count': 0})
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = %s AND is_read = 0",
        (user_id,)
    )
    count = cursor.fetchone()[0]
    cursor.close()
    conn.close()
    return jsonify({'count': count})





@app.route('/notifications', methods=['GET'])
def get_notifications():
    if 'user_id' not in session:
        return jsonify([])

    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Get latest 20 notifications, newest first
    cursor.execute("""
        SELECT n.id, n.message, n.is_read, n.created_at,
               s.name as store_name, s.slug as store_slug,
               l.listing_id, l.title as item_title
        FROM notifications n
        JOIN stores s ON n.store_id = s.store_id
        LEFT JOIN listings l ON n.listing_id = l.listing_id
        WHERE n.user_id = %s
        ORDER BY n.created_at DESC
        LIMIT 20
    """, (user_id,))
    notifications = cursor.fetchall()

    cursor.close()
    conn.close()
    return jsonify(notifications)





@app.route('/notifications/mark-read', methods=['POST'])
def mark_notifications_read():
    if 'user_id' not in session:
        return jsonify({'success': False}), 401

    data = request.get_json() or {}
    notification_ids = data.get('ids', [])
    user_id = session['user_id']

    print(f"Mark-read request: user_id={user_id}, ids={notification_ids}")  # Debug

    if not notification_ids:
        return jsonify({'success': True})

    conn = get_db_connection()
    cursor = conn.cursor()
    format_strings = ','.join(['%s'] * len(notification_ids))
    sql = f"UPDATE notifications SET is_read = 1 WHERE user_id = %s AND id IN ({format_strings})"
    cursor.execute(sql, (user_id, *notification_ids))
    affected = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()

    print(f"Rows updated: {affected}")  # Debug

    return jsonify({'success': True, 'updated': affected})   







@app.route('/notifications')
def notifications_page():
    """Display a full page of all notifications for the logged-in user."""
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT n.id, n.message, n.is_read, n.created_at,
               s.name as store_name, s.slug as store_slug,
               l.listing_id, l.title as item_title
        FROM notifications n
        JOIN stores s ON n.store_id = s.store_id
        LEFT JOIN listings l ON n.listing_id = l.listing_id
        WHERE n.user_id = %s
        ORDER BY n.created_at DESC
    """, (user_id,))
    notifications = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template('notifications.html', notifications=notifications)








@app.route("/googlefbaf22f94e24fef4.html")
def google_verification():
    templates_dir = os.path.join(os.path.dirname(__file__), "templates")
    return send_from_directory(templates_dir, "googlefbaf22f94e24fef4.html")



@app.route("/sitemap.xml")
def sitemap():
    static_dir = os.path.join(os.path.dirname(__file__), "templates")
    return send_from_directory(static_dir, "sitemap.xml")




@app.template_filter('format_number')
def format_number(value):
    """Format large numbers with K/M suffix"""
    if value >= 1000000:
        return f'{value/1000000:.1f}M'
    elif value >= 1000:
        return f'{value/1000:.1f}K'
    return str(value)





# ------------------------------------------------------------------
# 5c – Start the scheduler **once** (after app is created)
# ------------------------------------------------------------------
scheduler.start()
logging.info("BackgroundScheduler started with all jobs.")



import atexit
atexit.register(lambda: scheduler.shutdown())

if __name__ == '__main__':
    app.run(debug=True, port=5000)
