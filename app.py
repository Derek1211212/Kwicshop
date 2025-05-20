import markupsafe
import flask
flask.Markup = markupsafe.Markup

# Now import the rest of your modules
import os
import logging
from dotenv import load_dotenv
from flask import Flask, render_template, url_for, abort, request, session, redirect, flash, jsonify
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
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
import json
from notifications import send_push
from config import VAPID_PUBLIC_KEY




# Load environment variables from .env file
load_dotenv()

PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY')
if not PAYSTACK_SECRET_KEY:
    raise ValueError("PAYSTACK_SECRET_KEY is not set in the environment")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'))

app.config['SECRET_KEY'] = 'fa470fe714e44404511cbad16224f52777068d05bb5c29ed'

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

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS






# Database connection function
def get_db_connection():
    # Fetch database credentials from environment variables
    db_host = os.getenv('DB_HOST')
    db_user = os.getenv('DB_USER')
    db_password = os.getenv('DB_PASSWORD')
    db_database = os.getenv('DB_DATABASE')
    db_port = int(os.getenv('DB_PORT'))
    
    # Connect to the database using the credentials
    return mysql.connector.connect(
        host=db_host,
        user=db_user,
        password=db_password,
        database=db_database,
        port=db_port,
        charset='utf8mb4',
        collation='utf8mb4_unicode_ci',
        use_unicode=True
    )



@app.route('/')
def home():
    # 0) Keep these in scope for the template
    search             = request.args.get('search', '').strip()
    selected_category  = request.args.get('category', 'All')
    deal_type_filter   = request.args.get('deal_type', 'All')
    location_q         = request.args.get('location', '').strip()

    listings = []
    categories = []
    user_logged_in = 'user_id' in session
    user_subscribed = False
    conn = None

    try:
        # 1) Check push subscription
        if user_logged_in:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM push_subscriptions WHERE user_id = %s",
                        (session['user_id'],))
            user_subscribed = cur.fetchone() is not None
            cur.close()
            conn.close()
            conn = None

        # 2) Fetch matching listings
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Base query
        query = """
        SELECT
          l.*,
          u.username,
          IFNULL(m.impressions, 0) AS impressions
        FROM listings AS l
        JOIN users    AS u ON l.user_id = u.id
        LEFT JOIN listing_metrics AS m
          ON l.listing_id = m.listing_id
        WHERE 1=1
        """
        params = []

        # Text search
        if search:
            like = f'%{search}%'
            query += """
              AND (
                l.title       LIKE %s OR
                l.description LIKE %s OR
                l.category    LIKE %s
              )
            """
            params += [like, like, like]

        # Category filter
        if selected_category != 'All':
            query += " AND l.category = %s"
            params.append(selected_category)

        # Deal type filter
        if deal_type_filter != 'All':
            query += " AND l.deal_type = %s"
            params.append(deal_type_filter)

        # Ordering (with optional location boost)
        if location_q:
            query += """
              ORDER BY
                CASE l.plan
                  WHEN 'Diamond' THEN 5
                  WHEN 'Gold'    THEN 4
                  WHEN 'Silver'  THEN 3
                  WHEN 'Bronze'  THEN 2
                  ELSE 1
                END DESC,
                (l.location = %s) DESC,
                (SOUNDEX(l.location) = SOUNDEX(%s)) DESC,
                (l.location LIKE %s) DESC,
                l.created_at DESC
            """
            params += [location_q, location_q, f'%{location_q}%']
        else:
            query += """
              ORDER BY
                CASE l.plan
                  WHEN 'Diamond' THEN 5
                  WHEN 'Gold'    THEN 4
                  WHEN 'Silver'  THEN 3
                  WHEN 'Bronze'  THEN 2
                  ELSE 1
                END DESC,
                l.created_at DESC
            """

        cursor.execute(query, params)
        listings = cursor.fetchall()

        # Fetch categories for filter tags
        cursor.execute("SELECT DISTINCT category FROM listings")
        categories = [r['category'] for r in cursor.fetchall()]

        # Build static URLs for each listing and its offers
        for l in listings:
            # main image_url
            if l.get('image_url'):
                l['image_url'] = url_for('static', filename='images/' + l['image_url'], _external=True)
            else:
                l['image_url'] = url_for('static', filename='images/placeholder.jpg')

            # banner_image (for carousel) from image1 column
            if l.get('image1'):
                l['banner_image'] = url_for('static', filename='images/' + l['image1'], _external=True)
            else:
                l['banner_image'] = url_for('static', filename='images/placeholder.jpg')

            # offered items for swap deals
            if l['deal_type'] == 'Swap Deal':
                cursor.execute("""
                  SELECT item_id, title, description, image1, `condition`
                  FROM offered_items
                  WHERE listing_id = %s
                  ORDER BY item_id ASC
                """, (l['listing_id'],))
                offers = cursor.fetchall() or []
                for o in offers:
                    if o.get('image1'):
                        o['image1'] = url_for('static', filename='images/' + o['image1'])
                    else:
                        o['image1'] = url_for('static', filename='images/placeholder.jpg')
                l['offers'] = offers
            else:
                l['offers'] = []

        cursor.close()

    except Exception as e:
        logging.error("Error fetching listings: %s", e)
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

    # 3) Render the template
    featured_listing = listings[0] if listings else None
    return render_template(
        'home.html',
        listings=listings,
        featured_listing=featured_listing,
        categories=categories,
        search=search,
        selected_category=selected_category,
        deal_type_filter=deal_type_filter,
        location=location_q,
        user_logged_in=user_logged_in,
        user_subscribed=user_subscribed
    )









@app.route('/listing/<int:listing_id>')
def listing_details(listing_id):
    conn = None
    cursor = None
    listing = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Log query execution
        app.logger.debug(f"Fetching listing with ID: {listing_id}")
        
        # 1) Fetch the main listing + owner info
        cursor.execute("""
            SELECT l.*, u.username, u.email
            FROM listings AS l
            JOIN users AS u ON l.user_id = u.id
            WHERE l.listing_id = %s
        """, (listing_id,))
        listing = cursor.fetchone()
        if not listing:
            app.logger.warning(f"No listing found for ID: {listing_id}")
            abort(404)
        
        # 2) Fetch average rating & total count
        app.logger.debug("Fetching ratings")
        cursor.execute("""
            SELECT 
                AVG(rating_value) AS avg_rating, 
                COUNT(*) AS rating_count
            FROM ratings
            WHERE listing_id = %s
        """, (listing_id,))
        rd = cursor.fetchone() or {}
        listing['avg_rating'] = float(rd.get('avg_rating') or 0)
        listing['rating_count'] = rd.get('rating_count') or 0
        
        # 3) Fetch all reviews
        app.logger.debug("Fetching reviews")
        cursor.execute("""
            SELECT r.review_id,
                   r.review_text,
                   r.created_at,
                   u.username AS reviewer
            FROM reviews AS r
            JOIN users AS u ON r.user_id = u.id
            WHERE r.listing_id = %s
            ORDER BY r.created_at DESC
        """, (listing_id,))
        listing['reviews'] = cursor.fetchall() or []
        
        # 4) Fetch offered items with escaped `condition` column
        app.logger.debug("Fetching offered items")
        cursor.execute("""
            SELECT title, description, `condition`, image1, image2, image3, image4
            FROM offered_items
            WHERE listing_id = %s
        """, (listing_id,))
        listing['offered_items'] = cursor.fetchall() or []
        
        app.logger.debug(f"Listing data: {listing}")
        
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
    
    return render_template('listing_details.html', listing=listing)





# User Authentication Helper
def authenticate_user(email, password):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id FROM users WHERE email = %s AND password = %s", (email, password))
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

# Login Route
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        # Get the next URL from the form; if not provided or empty, default to 'home'
        next_url = request.form.get('next')
        if not next_url:
            next_url = url_for('home')
        
        # Basic input validation
        if not email or not password:
            flash('Please enter both email and password', 'danger')
            return redirect(url_for('login', next=next_url))
        
        # Authenticate user
        user = authenticate_user(email, password)
        
        if user:
            # Successful login
            session['user_id'] = user['id']
            session.permanent = True  # Optional: make session persistent
            
            # Security logging
            logging.info(f"User {user['id']} logged in successfully")
            
            return redirect(next_url)
        else:
            # Failed login
            logging.warning(f"Failed login attempt for email: {email}")
            flash('Invalid email or password', 'danger')
    
    # GET request or failed POST
    # Pass along the 'next' parameter (if any) to the template
    return render_template('login.html', next_url=request.args.get('next', ''))




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




import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from twilio.rest import Client

def send_email_notification(recipient_email, subject, body):
    """
    Sends an email notification to the specified recipient.
    
    Configuration:
      - Replace 'your_email@example.com' and 'your_email_password' with your email credentials.
      - Set 'smtp.example.com' and port 587 (or another port as needed) to match your SMTP server.
    """
    sender_email = "Derickbill3@gmail.com"
    sender_password = "bxyw odgw iwvl tpad"
    smtp_server = "smtp.gmail.com"
    smtp_port = 587  # or use 465 for SSL if needed

    # Create a multipart message
    message = MIMEMultipart()
    message["From"] = sender_email
    message["To"] = recipient_email
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain"))

    try:
        # Connect to the SMTP server and send the email
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()  # Secure the connection
            server.login(sender_email, sender_password)
            server.send_message(message)
        print(f"Email sent successfully to {recipient_email}")
    except Exception as e:
        print("Error sending email:", e)
        raise e

def send_text_notification(recipient_contact, body):
    """
    Sends a text message notification using the Twilio API.
    
    Configuration:
      - Replace 'your_twilio_account_sid', 'your_twilio_auth_token', and 'your_twilio_phone_number'
        with your Twilio account details.
      - Ensure that recipient_contact is in the proper format (e.g., '+1234567890').
    """
    account_sid = "AC51155da53026cb7d1bc0f7bd7512c764"
    auth_token = "7e2c3ce168d8790f799f5f5d59087408"
    from_number = "+13252406425"  # Your Twilio phone number

    client = Client(account_sid, auth_token)

    try:
        message = client.messages.create(
            body=body,
            from_=from_number,
            to=recipient_contact
        )
        print(f"Text message sent successfully to {recipient_contact}, SID: {message.sid}")
    except Exception as e:
        print("Error sending text message:", e)
        raise e








# Proposal Creation Route
@app.route('/create_proposal/<int:listing_id>', methods=['GET', 'POST'])
@login_required
def create_proposal(listing_id):
    if request.method == 'POST':
        conn   = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            # 1) Gather form data
            proposer_id          = session['user_id']
            proposed_item        = request.form['proposed_item']
            additional_cash      = request.form.get('additional_cash', 0.00)
            message              = request.form.get('message', '').strip()
            detailed_description = request.form['detailed_description']
            condition            = request.form['condition']
            phone_number         = request.form['phone_number']
            email_address        = request.form['email_address']

            # 2) Handle up to 4 image uploads
            image_filenames = []
            for i in range(1, 5):
                file = request.files.get(f'image{i}')
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    path     = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    file.save(path)
                    image_filenames.append(filename)
                else:
                    image_filenames.append(None)

            # 3) Insert the proposal
            cursor.execute("""
                INSERT INTO proposals (
                    listing_id, user_id, proposed_item,
                    additional_cash, message, status,
                    detailed_description, `condition`,
                    phone_number, Email_address,
                    image1, image2, image3, image4
                ) VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                listing_id,
                proposer_id,
                proposed_item,
                additional_cash,
                message,
                detailed_description,
                condition,
                phone_number,
                email_address,
                *image_filenames
            ))
            conn.commit()

            # 4) Lookup listing owner
            cursor.execute(
                "SELECT user_id, title FROM listings WHERE listing_id = %s",
                (listing_id,)
            )
            listing = cursor.fetchone()
            if listing:
                owner_id      = listing['user_id']
                listing_title = listing['title']

                # 5a) Send email + SMS
                cursor.execute("SELECT email, contact FROM users WHERE id = %s", (owner_id,))
                user = cursor.fetchone()
                if user:
                    try:
                        send_email_notification(
                            user['email'],
                            "New Proposal Received",
                            f"Someone just sent you a swap proposal for your listing: {listing_title}."
                        )
                        send_text_notification(
                            user['contact'],
                            f"New proposal for {listing_title}. Check your dashboard."
                        )
                    except Exception as notify_err:
                        app.logger.error("Email/SMS error: %s", notify_err)

                # 5b) Send web-push notification
                try:
                    send_push(
                        owner_id,
                        "New proposal received",
                        f"Someone just sent you a swap proposal for your listing: {listing_title}.",
                        url_for('listing_details', listing_id=listing_id)
                    )
                    app.logger.info(
                        "Push notification sent to user %s for listing %s", 
                        owner_id, listing_id
                    )
                except Exception as push_err:
                    app.logger.error("Push notification error: %s", push_err)

            flash('Your swap proposal has been submitted successfully!', 'success')
            return redirect(url_for('listing_details', listing_id=listing_id))

        except Exception as e:
            conn.rollback()
            app.logger.error("Error creating proposal: %s", e)
            flash('Error submitting proposal. Please try again.', 'danger')
        finally:
            cursor.close()
            conn.close()

    # For GET or any other method, just redirect back
    return redirect(url_for('listing_details', listing_id=listing_id))




@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        # Get user listings with proposal count and metrics
        cursor.execute("""
            SELECT 
                l.*, 
                IFNULL(m.impressions, 0) AS impressions,
                IFNULL(m.clicks, 0) AS clicks,
                (SELECT COUNT(*) FROM proposals p WHERE p.listing_id = l.listing_id) AS proposal_count
            FROM listings l
            LEFT JOIN listing_metrics m ON l.listing_id = m.listing_id
            WHERE l.user_id = %s
        """, (session['user_id'],))
        listings = cursor.fetchall()
        
        # Get proposals with sender information
        cursor.execute("""
            SELECT p.*, l.title AS listing_title, u.username AS sender_username, u.contact AS sender_contact
            FROM proposals p
            JOIN listings l ON p.listing_id = l.listing_id 
            JOIN users u ON p.user_id = u.id
            WHERE l.user_id = %s
        """, (session['user_id'],))
        proposals = cursor.fetchall()
        
        # Get unique listing titles for filter
        unique_titles = list({proposal['listing_title'] for proposal in proposals})
        
        # Get current user info
        cursor.execute("SELECT id, username, email FROM users WHERE id = %s", (session['user_id'],))
        user = cursor.fetchone()
        
        # Define promotion plans and prices
        plan_prices = {
            'Diamond': 100,
            'Gold': 70,
            'Silver': 40,
            'Bronze': 20
        }
        
        return render_template('dashboard.html', 
                            user=user,
                            listings=listings,
                            proposals=proposals,
                            unique_titles=unique_titles,
                            plan_prices=plan_prices)  # Added plan_prices here
    finally:
        cursor.close()
        conn.close()



@app.route('/listings/<int:listing_id>', methods=['DELETE'])
@login_required
def delete_listing(listing_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("DELETE FROM listings WHERE listing_id=%s AND user_id=%s", 
                      (listing_id, session['user_id']))
        conn.commit()
        return jsonify({'success': True})
    finally:
        cursor.close()
        conn.close()




@app.route('/proposals/<int:proposal_id>', methods=['PUT'])
@login_required
def update_proposal(proposal_id):
    # 1) Read & validate the new status
    status = request.json.get('status', '').lower()
    if status not in ('accepted', 'declined', 'negotiated'):
        return jsonify({'error': 'Invalid status'}), 400

    user_id = session['user_id']
    app.logger.debug("User %s requests status '%s' on proposal %s",
                     user_id, status, proposal_id)

    conn   = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # 2) Fetch proposal + listing + owner + proposer
        cursor.execute("""
            SELECT
              p.user_id   AS proposer_id,
              p.listing_id,
              l.user_id   AS owner_id,
              l.title     AS listing_title
            FROM proposals AS p
            JOIN listings  AS l ON p.listing_id = l.listing_id
            WHERE p.id = %s
        """, (proposal_id,))
        row = cursor.fetchone()

        if not row:
            app.logger.warning("Proposal %s not found", proposal_id)
            return jsonify({'error': 'Proposal not found'}), 404

        proposer_id    = row['proposer_id']
        listing_id     = row['listing_id']
        owner_id       = row['owner_id']
        listing_title  = row['listing_title']

        app.logger.debug(
            "Proposal %s → listing %s owned by %s; proposer is %s",
            proposal_id, listing_id, owner_id, proposer_id
        )

        # 3) Authorization: only listing owner may update
        if owner_id != user_id:
            app.logger.warning("User %s not authorized to update proposal %s",
                               user_id, proposal_id)
            return jsonify({'error': 'Not authorized'}), 403

        # 4) Perform the update
        cursor.execute("""
            UPDATE proposals
            SET status = %s
            WHERE id = %s
        """, (status, proposal_id))
        conn.commit()
        app.logger.info("Proposal %s status updated to '%s'", proposal_id, status)

        # 5) Choose notification content based on status
        if status == 'accepted':
            notif_title = "Proposal Accepted"
            notif_body  = f"Your proposal for '{listing_title}' was accepted!"
        elif status == 'declined':
            notif_title = "Proposal Declined"
            notif_body  = f"Your proposal for '{listing_title}' was declined."
        else:  # status == 'negotiated'
            notif_title = "Proposal Negotiated"
            notif_body  = f"Your proposal for '{listing_title}' is up for negotiation."

        # 6) Send the push notification
        try:
            send_push(
                proposer_id,
                notif_title,
                notif_body,
                url_for('listing_details', listing_id=listing_id)
            )
            app.logger.info(
                "Push sent to proposer %s for proposal %s: %s",
                proposer_id, proposal_id, notif_title
            )
        except Exception as push_err:
            app.logger.error("Push notification error: %s", push_err)

        # 7) Return success
        return jsonify({'success': True})

    finally:
        cursor.close()
        conn.close()




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
        # Get the listing to edit
        cursor.execute("""
            SELECT * FROM listings 
            WHERE listing_id = %s AND user_id = %s
        """, (listing_id, session['user_id']))
        listing = cursor.fetchone()
        
        if not listing:
            flash('Listing not found or you dont have permission to edit it', 'danger')
            return redirect(url_for('dashboard'))
            
        return render_template('edit_listing.html', listing=listing)
    finally:
        cursor.close()
        conn.close()




@app.route('/listings/<int:listing_id>/update', methods=['POST'])
@login_required
def update_listing(listing_id):
    conn    = None
    cursor  = None
    try:
        # 1) First verify ownership
        conn   = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT user_id FROM listings WHERE listing_id = %s", (listing_id,))
        row = cursor.fetchone()
        if not row or row['user_id'] != session['user_id']:
            flash('You do not have permission to edit this listing', 'danger')
            return redirect(url_for('dashboard'))

        # 2) Collect all scalar form fields
        title                     = request.form.get('title')
        description               = request.form.get('description')
        condition                 = request.form.get('condition')
        desired_swap              = request.form.get('desired_swap')
        desired_swap_description  = request.form.get('desired_swap_description')
        additional_cash           = request.form.get('additional_cash') or None
        required_cash             = request.form.get('required_cash')   or None
        location                  = request.form.get('location')
        contact                   = request.form.get('contact')

        # 3) Handle file uploads for image_url + image1–image4
        #    Save each into static/images/ and prepare an update map.
        upload_dir = os.path.join(app.root_path, 'static', 'images')
        os.makedirs(upload_dir, exist_ok=True)

        img_fields = {
            'image_url':   request.files.get('image'),
            'image1':      request.files.get('image1'),
            'image2':      request.files.get('image2'),
            'image3':      request.files.get('image3'),
            'image4':      request.files.get('image4')
        }

        # Build dynamic SET clauses
        set_clauses = [
            "title=%s",
            "description=%s",
            "`condition`=%s",
            "desired_swap=%s",
            "desired_swap_description=%s",
            "additional_cash=%s",
            "required_cash=%s",
            "location=%s",
            "contact=%s"
        ]
        params = [
            title, description, condition,
            desired_swap, desired_swap_description,
            additional_cash, required_cash,
            location, contact
        ]

        for field, file in img_fields.items():
            if file and file.filename and allowed_file(file.filename):
                # save to disk
                filename = secure_filename(file.filename)
                unique_name = f"{uuid.uuid4().hex}_{filename}"
                dest = os.path.join(upload_dir, unique_name)
                file.save(dest)

                # schedule this field for update
                set_clauses.append(f"{field}=%s")
                params.append(unique_name)

        # 4) Finalize and execute UPDATE
        params.append(listing_id)
        query = f"""
            UPDATE listings
               SET {', '.join(set_clauses)}
             WHERE listing_id=%s
        """
        cursor.execute(query, params)
        conn.commit()

        flash('Listing updated successfully!', 'success')
        return redirect(url_for('dashboard'))

    except Exception as e:
        if conn:
            conn.rollback()
        app.logger.error(f"Error in update_listing: {e}")
        flash('An error occurred while updating your listing', 'danger')
        return redirect(url_for('edit_listing', listing_id=listing_id))

    finally:
        if cursor: cursor.close()
        if conn:    conn.close()






@app.route('/my-proposals')
def my_proposals():
    # Check if user is logged in
    if 'user_id' not in session:
        # Store the current URL to redirect back after login
        return redirect(url_for('login', next=request.url))
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        # Get all proposals with listing and lister details
        cursor.execute("""
            SELECT 
                p.*,
                l.title AS listing_title,
                l.description AS listing_description,
                l.user_id AS lister_id,
                l.contact AS listing_contact,
                u.username AS lister_username,
                u.contact AS lister_contact
            FROM proposals p
            JOIN listings l ON p.listing_id = l.listing_id
            JOIN users u ON l.user_id = u.id
            WHERE p.user_id = %s
            ORDER BY p.created_at DESC
        """, (session['user_id'],))
        proposals = cursor.fetchall()
        
        return render_template('my_proposals.html', proposals=proposals)
    except Exception as e:
        print(f"Error fetching proposals: {e}")
        return render_template('my_proposals.html', proposals=None)
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

@app.route('/listings', methods=['POST'])
@login_required
def create_listing():
    # 1) Common
    dt = request.form.get('deal_type','Swap Deal')
    deal_type = dt if dt=='Swap Deal' else 'Outright Sales'
    title       = request.form['title']
    description = request.form.get('description','')
    category    = request.form['category']
    location    = request.form['location']
    contact     = request.form['contact']
    plan        = request.form.get('plan','Free')

    # 2) Main images
    main_images=[]
    for f in request.files.getlist('images[]'):
        if f and allowed_file(f.filename):
            fn   = secure_filename(f.filename)
            u    = f"{uuid.uuid4().hex}_{fn}"
            f.save(os.path.join(app.config['UPLOAD_FOLDER'],u))
            main_images.append(u)
            if len(main_images)>=5: break

    # 3) Offered items
    off_titles = request.form.getlist('offer_title[]')
    off_conds  = request.form.getlist('offer_condition[]')
    off_descs  = request.form.getlist('offer_description[]')
    files1     = request.files.getlist('offer_image1[]')
    files2     = request.files.getlist('offer_image2[]')
    files3     = request.files.getlist('offer_image3[]')
    files4     = request.files.getlist('offer_image4[]')

    def save(files):
        out=[]
        for f in files:
            if f and allowed_file(f.filename):
                fn = secure_filename(f.filename)
                u  = f"{uuid.uuid4().hex}_{fn}"
                f.save(os.path.join(app.config['UPLOAD_FOLDER'],u))
                out.append(u)
        return out

    imgs1 = save(files1)
    imgs2 = save(files2)
    imgs3 = save(files3)
    imgs4 = save(files4)

    # 4) Specifics
    desired_swap             = None
    desired_swap_description = None
    additional_cash          = None
    required_cash            = None
    price                    = None

    if deal_type=='Swap Deal':
        if not (1 <= len(off_conds) <= 3):
            flash("Offer between 1 and 3 items.", "error")
            return redirect(url_for('dashboard'))
        desired_swap             = request.form.get('desired_swap')
        desired_swap_description = request.form.get('desired_swap_description')
        additional_cash          = request.form.get('additional_cash') or None
        required_cash            = request.form.get('required_cash')   or None
    else:
        price     = request.form.get('price') or None
        off_conds = [request.form.get('condition')]
        off_descs = [request.form.get('description')]
        imgs1     = [None]; imgs2=[None]; imgs3=[None]; imgs4=[None]
        off_titles= ['']  # will fallback to title

    # 5) Plan fee
    plan_prices={'Standard':20,'Premium':50,'Diamond':100}
    fee = plan_prices.get(plan,0)
    if fee>0:
        session['pending_listing']={
          'user_id':session['user_id'], 'title':title,'description':description,
          'category':category,'location':location,'contact':contact,
          'deal_type':deal_type,'plan':plan,'price':price,
          'desired_swap':desired_swap,'desired_swap_description':desired_swap_description,
          'additional_cash':additional_cash,'required_cash':required_cash,
          'main_images':main_images,
          'off_titles':off_titles,'off_conds':off_conds,'off_descs':off_descs,
          'imgs1':imgs1,'imgs2':imgs2,'imgs3':imgs3,'imgs4':imgs4
        }
        return redirect(url_for('paystack_payment',plan=plan,amount=fee))

    # 6) Free insert
    conn   = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
      INSERT INTO listings (
        user_id,title,description,category,
        desired_swap,desired_swap_description,
        additional_cash,required_cash,
        `condition`,location,contact,
        image_url,image1,image2,image3,image4,
        plan,deal_type,price
      ) VALUES (
        %s,%s,%s,%s,
        %s,%s,%s,%s,
        %s,%s,%s,
        %s,%s,%s,%s,%s,
        %s,%s,%s
      )
    """,(
      session['user_id'],title,description,category,
      desired_swap,desired_swap_description,
      additional_cash,required_cash,
      off_conds[0],location,contact,
      *main_images,*( [None]*(5-len(main_images)) ),
      plan,deal_type,price
    ))
    lid=cursor.lastrowid

    if deal_type=='Swap Deal':
        for i in range(len(off_conds)):
            name = off_titles[i].strip() or title
            cursor.execute("""
              INSERT INTO offered_items (
                listing_id,title,description,`condition`,
                image1,image2,image3,image4
              ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,(
              lid,name,off_descs[i],off_conds[i],
              imgs1[i] if i<len(imgs1) else None,
              imgs2[i] if i<len(imgs2) else None,
              imgs3[i] if i<len(imgs3) else None,
              imgs4[i] if i<len(imgs4) else None
            ))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Listing created successfully!", "success")
    return redirect(url_for('dashboard'))

@app.route('/paystack_payment')
@login_required
def paystack_payment():
    plan   = request.args.get('plan')
    amount = request.args.get('amount',type=float)
    if amount is None or not plan:
        flash("Invalid payment parameters.", "error")
        return redirect(url_for('home'))

    amount_kobo = int(amount*100)
    conn   = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT email FROM users WHERE id=%s",(session['user_id'],))
    user=cursor.fetchone()
    cursor.close()
    conn.close()
    if not user:
        flash("User not found.", "error")
        return redirect(url_for('home'))

    payload={
      "email":user['email'],
      "amount":amount_kobo,
      "metadata":{"pending_listing":session.get('pending_listing')},
      "callback_url":url_for('paystack_verify',_external=True)
    }
    headers={
      "Authorization":"Bearer sk_test_38d38a400d7c1a34c826930691e8c23fce8dde98",
      "Content-Type":"application/json"
    }
    resp = requests.post("https://api.paystack.co/transaction/initialize",
                         json=payload,headers=headers)
    data=resp.json()
    if data.get('status'):
      return redirect(data['data']['authorization_url'])
    flash("Payment initialization failed.", "error")
    return redirect(url_for('home'))

@app.route('/paystack_verify')
@login_required
def paystack_verify():
    ref = request.args.get('reference')
    if not ref:
        flash("Payment reference missing.", "error")
        return redirect(url_for('home'))

    # Verify with Paystack
    headers = {"Authorization": "Bearer sk_test_38d38a400d7c1a34c826930691e8c23fce8dde98"}
    resp = requests.get(f"https://api.paystack.co/transaction/verify/{ref}", headers=headers)
    result = resp.json()
    if not (result.get('status') and result['data']['status'] == 'success'):
        flash("Payment verification failed.", "error")
        return redirect(url_for('home'))

    # Pull pending listing from metadata or session
    p = result['data']['metadata'].get('pending_listing') or session.pop('pending_listing', None)
    if not p:
        flash("No pending listing.", "error")
        return redirect(url_for('home'))

    # ─── Clean up numeric fields ────────────────────────────────────────────
    raw_additional = str(p.get('additional_cash', '')).strip()
    raw_required   = str(p.get('required_cash', '')).strip()
    raw_price      = str(p.get('price', '')).strip()

    additional_cash = int(float(raw_additional)) if raw_additional else None
    required_cash   = int(float(raw_required))   if raw_required   else None
    price           = int(float(raw_price))      if raw_price      else None

    # ─── Unpack arrays (with correct keys) ─────────────────────────────────
    off_conds  = p.get('off_conds', [])
    off_titles = p.get('off_titles', [])
    off_descs  = p.get('off_descs', [])
    imgs1      = p.get('imgs1', [])
    imgs2      = p.get('imgs2', [])
    imgs3      = p.get('imgs3', [])
    imgs4      = p.get('imgs4', [])

    # ─── Insert into listings ──────────────────────────────────────────────
    conn   = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
      INSERT INTO listings (
        user_id, title, description, category,
        desired_swap, desired_swap_description,
        additional_cash, required_cash,
        `condition`, location, contact,
        image_url, image1, image2, image3, image4,
        plan, deal_type, price
      ) VALUES (
        %s, %s, %s, %s,
        %s, %s, %s, %s,
        %s, %s, %s,
        %s, %s, %s, %s, %s,
        %s, %s, %s
      )
    """, (
      p['user_id'], p['title'], p['description'], p['category'],
      p.get('desired_swap'), p.get('desired_swap_description'),
      additional_cash, required_cash,
      # first offered-item condition (or None)
      off_conds[0] if off_conds else None,
      p['location'], p['contact'],
      # main_images list plus padding to 5
      *p.get('main_images', []),
      *([None] * (5 - len(p.get('main_images', [])))),
      p['plan'], p['deal_type'], price
    ))
    lid = cursor.lastrowid

    # ─── Insert each offered item ──────────────────────────────────────────
    if p['deal_type'] == 'Swap Deal':
        for i, cond in enumerate(off_conds):
            title_i = off_titles[i].strip() or p['title']
            desc_i  = off_descs[i]
            cursor.execute("""
              INSERT INTO offered_items (
                listing_id, title, description, `condition`,
                image1, image2, image3, image4
              ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
              lid, title_i, desc_i, cond,
              imgs1[i] if i < len(imgs1) else None,
              imgs2[i] if i < len(imgs2) else None,
              imgs3[i] if i < len(imgs3) else None,
              imgs4[i] if i < len(imgs4) else None
            ))

    conn.commit()
    cursor.close()
    conn.close()

    # Clean up session
    session.pop('pending_listing', None)

    flash("Your product has been listed!", "success")
    return redirect(url_for('dashboard'))




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
        return jsonify({'success': False, 'message': 'Please log in to review this listing'}), 401
    
    try:
        review_text = request.form.get('review_text', '').strip()
        if not review_text:
            raise ValueError("Review text cannot be empty")
            
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)  # Ensure dictionary=True for named columns
        
        # Get listing owner ID
        cursor.execute("SELECT user_id FROM listings WHERE listing_id = %s", (listing_id,))
        listing_owner_id = cursor.fetchone()['user_id']  # Access as dictionary
        
        # Insert review
        cursor.execute("""
            INSERT INTO reviews (listing_id, user_id, owner_id, review_text)
            VALUES (%s, %s, %s, %s)
        """, (listing_id, session['user_id'], listing_owner_id, review_text))
        
        conn.commit()
        
        # Get the new review with username to return
        cursor.execute("""
            SELECT reviews.*, users.username 
            FROM reviews 
            JOIN users ON reviews.user_id = users.id 
            WHERE reviews.review_id = LAST_INSERT_ID()
        """)
        new_review = cursor.fetchone()
        
        cursor.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': 'Review submitted successfully',
            'review': {
                'username': new_review['username'],
                'review_text': new_review['review_text'],
                'created_at': new_review['created_at'].strftime('%B %d, %Y')
            }
        })
        
    except Exception as e:
        logging.error("Error submitting review: %s", e)
        return jsonify({'success': False, 'message': str(e)}), 400






# Configure Flask-Mail (add to config)
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'blaqprophet112@gmail.com'
app.config['MAIL_PASSWORD'] = 'zwce pmol jnvm vbtz'
app.config['SECRET_KEY'] = 'fa470fe714e44404511cbad16224f52777068d05bb5c29ed'

mail = Mail(app)
serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form['email']
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Check if email exists
        cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()
        
        if user:
            # Generate token
            token = secrets.token_urlsafe(32)
            expires_at = datetime.now() + timedelta(hours=1)
            
            # Store token in database
            cursor.execute("""
                INSERT INTO password_reset_tokens (user_id, token, expires_at)
                VALUES (%s, %s, %s)
            """, (user['id'], token, expires_at))
            conn.commit()
            
            # Send email
            reset_url = url_for('reset_password', token=token, _external=True)
            msg = Message('Password Reset Request',
                          sender='blaqprophet112@gmail.com',
                          recipients=[email])
            msg.body = f'''To reset your password, visit the following link:
{reset_url}

This link will expire in 1 hour.'''
            mail.send(msg)
            
            flash('Password reset email sent! Check your inbox.', 'success')
        else:
            flash('No account found with that email address.', 'danger')
        
        cursor.close()
        conn.close()
        return redirect(url_for('forgot_password'))
    
    return render_template('forgot_password.html')

@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        # Verify token
        cursor.execute("""
            SELECT * FROM password_reset_tokens 
            WHERE token = %s AND expires_at > NOW()
        """, (token,))
        token_record = cursor.fetchone()
        
        if not token_record:
            flash('Invalid or expired token.', 'danger')
            return redirect(url_for('forgot_password'))
        
        if request.method == 'POST':
            new_password = request.form['password']
            confirm_password = request.form['confirm_password']
            
            if new_password != confirm_password:
                flash('Passwords do not match.', 'danger')
                return redirect(request.url)
            
            # Hash new password (using your existing password hashing method)
            hashed_password = generate_password_hash(new_password)
            
            # Update password
            cursor.execute("""
                UPDATE users 
                SET password = %s 
                WHERE id = %s
            """, (hashed_password, token_record['user_id']))
            
            # Delete used token
            cursor.execute("""
                DELETE FROM password_reset_tokens 
                WHERE token = %s
            """, (token,))
            
            conn.commit()
            flash('Password updated successfully! You can now login.', 'success')
            return redirect(url_for('login'))
        
    except Exception as e:
        conn.rollback()
        logging.error("Password reset error: %s", e)
        flash('Error resetting password. Please try again.', 'danger')
    finally:
        cursor.close()
        conn.close()
    
    return render_template('reset_password.html', token=token)



@app.route('/api/track_impression', methods=['POST'])
def track_impression():
    data = request.get_json() or {}
    listing_id = data.get('listing_id')
    if not listing_id:
        return jsonify({'success': False, 'error': 'Missing listing_id'}), 400

    conn   = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT * FROM listing_metrics WHERE listing_id = %s",
            (listing_id,)
        )
        existing = cursor.fetchone()
        if existing:
            cursor.execute("""
                UPDATE listing_metrics 
                   SET impressions = impressions + 1,
                       updated_at   = NOW()
                 WHERE listing_id = %s
            """, (listing_id,))
        else:
            cursor.execute("""
                INSERT INTO listing_metrics
                    (listing_id, impressions, clicks, updated_at) 
                VALUES (%s, 1, 0, NOW())
            """, (listing_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error("Error updating impression: %s", e)
        return jsonify({'success': False}), 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/track_click', methods=['POST'])
def track_click():
    data = request.get_json() or {}
    listing_id = data.get('listing_id')
    if not listing_id:
        return jsonify({'success': False, 'error': 'Missing listing_id'}), 400

    conn   = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE listing_metrics
               SET clicks = clicks + 1
             WHERE listing_id = %s
        """, (listing_id,))
        if cursor.rowcount == 0:
            cursor.execute("""
                INSERT INTO listing_metrics (listing_id, clicks, impressions, updated_at)
                VALUES (%s, 1, 0, NOW())
            """, (listing_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        logging.error("Error tracking click: %s", e)
        return jsonify({'success': False}), 500
    finally:
        cursor.close()
        conn.close()


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
        if not request.is_json:
            return jsonify({'error': 'Invalid request format'}), 400

        data = request.get_json()
        print("Received payment request data:", data)  # Debug logging

        # Validate required fields
        required_fields = ['plan', 'price', 'listing_id']
        if not all(key in data for key in required_fields):
            return jsonify({'error': 'Missing required fields'}), 400

        plan = data['plan']
        try:
            price = int(data['price']) * 100  # Convert price to kobo
        except ValueError as ve:
            print("Value error during price conversion:", str(ve))
            return jsonify({'error': 'Invalid price value'}), 400
        listing_id = data['listing_id']

        print(f"Processing payment for listing {listing_id}, plan {plan}, price {price}")  # Debug logging

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        try:
            # Verify that the listing exists and belongs to the current user
            cursor.execute("SELECT user_id FROM listings WHERE listing_id = %s", (listing_id,))
            listing = cursor.fetchone()
            if not listing:
                print(f"Listing {listing_id} not found")
                return jsonify({'error': 'Listing not found'}), 404
            if listing['user_id'] != session['user_id']:
                print(f"Listing {listing_id} doesn't belong to user {session['user_id']}")
                return jsonify({'error': 'This listing does not belong to you'}), 403

            # Get user email
            cursor.execute("SELECT email FROM users WHERE id = %s", (session['user_id'],))
            user = cursor.fetchone()
            if not user or not user.get('email'):
                print(f"User {session['user_id']} email not found")
                return jsonify({'error': 'User email not found'}), 400

            # Prepare Paystack payload
            payload = {
                "email": user['email'],
                "amount": price,
                "metadata": {
                    "plan": plan,
                    "listing_id": listing_id,
                    "user_id": session['user_id']
                },
                "callback_url": url_for('payment_verification', _external=True)
            }

            print("Sending to Paystack:", payload)  # Debug logging

            headers = {
                "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
                "Content-Type": "application/json"
            }

            response = requests.post(
                "https://api.paystack.co/transaction/initialize",
                headers=headers,
                json=payload
            )

            print("Paystack response:", response.status_code, response.text)  # Debug logging

            response_data = response.json()
            if response.status_code == 200 and response_data.get("status") is True:
                # Check for the authorization_url inside the "data" object
                if "data" in response_data and "authorization_url" in response_data["data"]:
                    return jsonify(response_data["data"])
                else:
                    error_msg = response_data.get("message", "No payment URL received")
                    print("Error: Authorization URL missing:", response_data)
                    return jsonify({'error': error_msg}), 400
            else:
                error_msg = response_data.get("message", "Payment initialization failed")
                print("Error initializing payment:", error_msg)
                return jsonify({'error': error_msg}), 400

        except Exception as db_e:
            print("Database error:", repr(db_e))
            return jsonify({'error': 'Database operation failed: ' + str(db_e)}), 500
        finally:
            cursor.close()
            conn.close()

    except Exception as e:
        print("Unexpected error:", repr(e))
        return jsonify({'error': 'Internal server error: ' + str(e)}), 500





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
        "Diamond": 100,
        "Gold": 70,
        "Silver": 40,
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





# app.py (after app = Flask(__name__) and all routes)
from apscheduler.schedulers.background import BackgroundScheduler
from jobs import check_ad_performance_alerts

scheduler = BackgroundScheduler()
scheduler.add_job(
    check_ad_performance_alerts,
    'interval',
    minutes=1,
    id='ad_metrics_alerts',
    replace_existing=True
)
scheduler.start()  # start as soon as the module loads



if __name__ == '__main__':
    app.run(debug=True)
