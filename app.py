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





# Load environment variables from .env file
load_dotenv()

app = Flask(__name__, template_folder="templates")

app = Flask(__name__, template_folder='Templates')
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
        port=db_port
    )

@app.route('/')
def home():
    conn = None
    listings = []
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Get search and category filter from query parameters
        search = request.args.get('search', '').strip()
        selected_category = request.args.get('category', 'All')
        deal_type_filter = request.args.get('deal_type', 'All')  # New filter for deal type
        
        # Build the query with optional filters
        query = """
            SELECT listings.*, users.username 
            FROM listings 
            JOIN users ON listings.user_id = users.id 
            WHERE 1=1
        """
        params = []
        
        if search:
            query += " AND (listings.title LIKE %s OR listings.description LIKE %s)"
            like_str = '%' + search + '%'
            params.extend([like_str, like_str])
        
        if selected_category != 'All':
            query += " AND listings.category = %s"
            params.append(selected_category)
            
        # Add deal type filter
        if deal_type_filter != 'All':
            query += " AND listings.deal_type = %s"
            params.append(deal_type_filter)
        
        # Order listings by plan paid value first (Diamond > Gold > Silver > Bronze > Free)
        # Then order by created_at in descending order
        query += """
            ORDER BY 
              (CASE listings.plan 
                WHEN 'Diamond' THEN 5 
                WHEN 'Gold' THEN 4 
                WHEN 'Silver' THEN 3 
                WHEN 'Bronze' THEN 2 
                ELSE 1 
              END) DESC, 
              listings.created_at DESC
        """
        cursor.execute(query, params)
        listings = cursor.fetchall()
        
        # Retrieve unique categories for the filter tags
        cursor.execute("SELECT DISTINCT category FROM listings")
        categories = [row['category'] for row in cursor.fetchall()]
        
        cursor.close()
    except Exception as e:
        logging.error("Error fetching listings: %s", e)
        categories = []
    finally:
        if conn:
            conn.close()
    
    # Process each listing to generate a full image URL from the image_url column
    for listing in listings:
        if listing.get('image_url'):
            listing['image_url'] = url_for('static', filename='images/' + listing['image_url'])
    
    return render_template('home.html', 
                         listings=listings, 
                         search=search,
                         selected_category=selected_category,
                         categories=categories,
                         deal_type_filter=deal_type_filter)  # Add deal_type_filter to template context




# Listing Details Route
@app.route('/listing/<int:listing_id>')
def listing_details(listing_id):
    conn = None
    listing = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Get listing details
        cursor.execute("""
            SELECT listings.*, users.username, users.email 
            FROM listings 
            JOIN users ON listings.user_id = users.id 
            WHERE listings.listing_id = %s
        """, (listing_id,))
        listing = cursor.fetchone()
        
        if listing:
            # Get average rating and count
            cursor.execute("""
                SELECT AVG(rating_value) as avg_rating, COUNT(*) as rating_count 
                FROM ratings 
                WHERE listing_id = %s
            """, (listing_id,))
            rating_data = cursor.fetchone()
            
            # Get reviews with usernames
            cursor.execute("""
                SELECT reviews.*, users.username 
                FROM reviews 
                JOIN users ON reviews.user_id = users.id 
                WHERE reviews.listing_id = %s 
                ORDER BY reviews.created_at DESC
            """, (listing_id,))
            reviews = cursor.fetchall()
            
            listing['avg_rating'] = float(rating_data['avg_rating']) if rating_data['avg_rating'] else None
            listing['rating_count'] = rating_data['rating_count']
            listing['reviews'] = reviews
            
        cursor.close()
    except Exception as e:
        logging.error("Error fetching listing details: %s", e)
    finally:
        if conn:
            conn.close()
    
    if not listing:
        abort(404)
        
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
        # Ensure all required fields (username, email, password) are included
        sql = """
            INSERT INTO users (username, email, password) 
            VALUES (%s, %s, %s)
        """
        username = form['username']
        email = form['email']
        # Make sure to hash the password before storing it!
        hashed_password = generate_password_hash(form['password'])
        
        cursor.execute(sql, (username, email, hashed_password))
        conn.commit()
        
        user_id = cursor.lastrowid
        return {'id': user_id, 'username': username, 'email': email}
        
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
            session['user_id'] = user['id']
            # Redirect to my-proposals after signup
            return redirect(url_for('home'))
        flash('Registration failed. Please try again.')
    
    return render_template('signup.html')



@app.route('/logout', methods=['GET', 'POST'])
def logout():
    session.pop('user_id', None)
    return redirect(url_for('home'))








# Proposal Creation Route
@app.route('/create_proposal/<int:listing_id>', methods=['GET', 'POST'])
@login_required
def create_proposal(listing_id):
    if request.method == 'POST':
        conn = get_db_connection()  # Uses your helper function
        cursor = conn.cursor()
        try:
            # Retrieve form fields
            proposed_item = request.form['proposed_item']
            additional_cash = request.form.get('additional_cash', 0.00)
            message = request.form.get('message', '')
            detailed_description = request.form['detailed_description']
            condition = request.form['condition']
            phone_number = request.form['phone_number']
            email_address = request.form['email_address']

            # Handle file uploads for image1 to image4
            image_filenames = []
            for i in range(1, 5):
                file = request.files.get(f'image{i}')
                if file and allowed_file(file.filename):
                    filename = secure_filename(file.filename)
                    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    file.save(filepath)
                    image_filenames.append(filename)
                else:
                    image_filenames.append(None)  # No file uploaded for this slot

            # Prepare and execute the INSERT query.
            # Notice that we enclose `condition` in backticks since it's a reserved keyword in MySQL.
            query = """
                INSERT INTO proposals (
                    listing_id, user_id, proposed_item, additional_cash, message, 
                    status, detailed_description, `condition`, phone_number, email_address, 
                    image1, image2, image3, image4
                ) VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s)
            """
            params = (
                listing_id,
                session['user_id'],
                proposed_item,
                additional_cash,
                message,
                detailed_description,
                condition,
                phone_number,
                email_address,
                *image_filenames  # Unpacks image1, image2, image3, image4
            )
            cursor.execute(query, params)
            conn.commit()
            flash('Your swap proposal has been submitted successfully!', 'success')
            return redirect(url_for('listing_details', listing_id=listing_id))
        except Exception as e:
            conn.rollback()
            app.logger.error("Error inserting proposal: %s", str(e))
            app.logger.error("Query parameters: listing_id=%s, user_id=%s, proposed_item=%s, additional_cash=%s, message=%s, detailed_description=%s, condition=%s, phone_number=%s, email_address=%s, images=%s",
                             listing_id, session.get('user_id'), proposed_item, additional_cash, message,
                             detailed_description, condition, phone_number, email_address, image_filenames)
            flash(f'Error submitting proposal: {str(e)}', 'danger')
        finally:
            cursor.close()
            conn.close()
    return redirect(url_for('listing_details', listing_id=listing_id))




@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        # Get user listings with proposal count
        cursor.execute("""
            SELECT 
                l.*, 
                (SELECT COUNT(*) FROM proposals p WHERE p.listing_id = l.listing_id) AS proposal_count
            FROM listings l
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
        
        return render_template('dashboard.html', 
                            user=user,
                            listings=listings,
                            proposals=proposals,
                            unique_titles=unique_titles)
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
    status = request.json.get('status')
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            UPDATE proposals 
            SET status=%s 
            WHERE id=%s AND listing_id IN (
                SELECT listing_id FROM listings WHERE user_id=%s
            )
        """, (status, proposal_id, session['user_id']))
        conn.commit()
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
    try:
        # Get form data
        title = request.form.get('title')
        description = request.form.get('description')
        condition = request.form.get('condition')
        desired_swap = request.form.get('desired_swap')
        desired_swap_description = request.form.get('desired_swap_description')
        additional_cash = float(request.form.get('additional_cash', 0))
        location = request.form.get('location')
        contact = request.form.get('contact')
        
        # Handle image upload
        image_url = None
        if 'image' in request.files:
            file = request.files['image']
            if file.filename != '' and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                unique_filename = f"{uuid.uuid4().hex}_{filename}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
                image_url = unique_filename

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        try:
            # Verify listing ownership
            cursor.execute("""
                SELECT user_id FROM listings 
                WHERE listing_id = %s
            """, (listing_id,))
            listing = cursor.fetchone()
            
            if not listing or listing['user_id'] != session['user_id']:
                flash('You do not have permission to edit this listing', 'danger')
                return redirect(url_for('dashboard'))
            
            # Build update query with escaped condition
            if image_url:
                query = """
                    UPDATE listings 
                    SET title=%s, description=%s, `condition`=%s, 
                        desired_swap=%s, desired_swap_description=%s,
                        additional_cash=%s, location=%s, contact=%s,
                        image_url=%s
                    WHERE listing_id=%s
                """
                params = (title, description, condition, desired_swap,
                         desired_swap_description, additional_cash, location,
                         contact, image_url, listing_id)
            else:
                query = """
                    UPDATE listings 
                    SET title=%s, description=%s, `condition`=%s, 
                        desired_swap=%s, desired_swap_description=%s,
                        additional_cash=%s, location=%s, contact=%s
                    WHERE listing_id=%s
                """
                params = (title, description, condition, desired_swap,
                         desired_swap_description, additional_cash, location,
                         contact, listing_id)
            
            cursor.execute(query, params)
            conn.commit()
            
            flash('Listing updated successfully!', 'success')
            return redirect(url_for('dashboard'))
            
        except Exception as e:
            conn.rollback()
            app.logger.error(f"Database error: {str(e)}")
            flash('Error updating listing', 'danger')
            return redirect(url_for('edit_listing', listing_id=listing_id))
            
        finally:
            cursor.close()
            conn.close()
            
    except Exception as e:
        app.logger.error(f"Error in update_listing: {str(e)}")
        flash('An error occurred', 'danger')
        return redirect(url_for('dashboard'))






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




@app.route('/listings', methods=['POST'])
@login_required
def create_listing():
    try:
        # Get the deal type; default to "Swap Deal" if not provided.
        deal_type = request.form.get('deal_type', 'Swap Deal')
        # Force deal_type to "Outright Sales" if it's not "Swap Deal"
        if deal_type != 'Swap Deal':
            deal_type = 'Outright Sales'
        
        # Common fields
        title = request.form.get('title')
        description = request.form.get('description')
        category = request.form.get('category')
        location = request.form.get('location')
        contact = request.form.get('contact')
        plan = request.form.get('plan')
        
        # Deal-specific fields
        if deal_type == 'Swap Deal':
            # Swap-specific fields
            desired_swap = request.form.get('desired_swap')
            desired_swap_description = request.form.get('desired_swap_description')
            additional_cash = request.form.get('additional_cash', 0)
            required_cash = request.form.get('required_cash', 0)
            condition = request.form.get('condition')
            price = None  # Not applicable for swap deals.
        else:
            # Outright Sales fields
            price = request.form.get('price')
            condition = request.form.get('sale_condition')
            # For outright sales, these swap fields are not used.
            desired_swap = None
            desired_swap_description = None
            additional_cash = None
            required_cash = None

        # File uploads handling (assuming allowed_file, secure_filename are defined)
        image_paths = []
        files = request.files.getlist('images')
        for file in files:
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                unique_name = f"{uuid.uuid4().hex}_{filename}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
                file.save(filepath)
                image_paths.append(unique_name)
                if len(image_paths) >= 5:
                    break

        conn = get_db_connection()
        cursor = conn.cursor()

        # For free plans, insert the listing immediately.
        if plan == 'Free':
            cursor.execute("""
                INSERT INTO listings (
                    user_id, title, description, category,
                    desired_swap, desired_swap_description, additional_cash,
                    required_cash, `condition`, location, contact, image_url,
                    image1, image2, image3, image4, plan, deal_type, price
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                session['user_id'],
                title,
                description,
                category,
                desired_swap,
                desired_swap_description,
                additional_cash,
                required_cash,
                condition,
                location,
                contact,
                image_paths[0] if len(image_paths) > 0 else None,
                image_paths[1] if len(image_paths) > 1 else None,
                image_paths[2] if len(image_paths) > 2 else None,
                image_paths[3] if len(image_paths) > 3 else None,
                image_paths[4] if len(image_paths) > 4 else None,
                plan,
                deal_type,
                price
            ))
            conn.commit()
            flash("Your product has been listed successfully!", "success")
            return jsonify({'success': True})
        else:
            # Paid plan: Store pending listing data in session and redirect to payment.
            session['pending_listing'] = {
                'user_id': session['user_id'],
                'title': title,
                'description': description,
                'category': category,
                'desired_swap': desired_swap,
                'desired_swap_description': desired_swap_description,
                'additional_cash': additional_cash,
                'required_cash': required_cash,
                'condition': condition,
                'location': location,
                'contact': contact,
                'image_paths': image_paths,
                'plan': plan,
                'deal_type': deal_type,
                'price': price
            }
            plan_prices = {
                'Diamond': 100,
                'Gold': 70,
                'Silver': 40,
                'Bronze': 20
            }
            amount = plan_prices.get(plan, 0)
            return redirect(url_for('paystack_payment', amount=amount, plan=plan))
    except Exception as e:
        conn.rollback()
        app.logger.error(f"Error creating listing: {str(e)}")
        return jsonify({'error': 'Server error'}), 500
    finally:
        cursor.close()
        conn.close()







@app.route('/paystack_payment')
@login_required
def paystack_payment():
    # Get the plan and amount from query parameters
    amount = request.args.get('amount', type=float)
    plan = request.args.get('plan')
    
    if not amount or not plan:
        flash("Invalid payment parameters.", "error")
        return redirect(url_for('home'))
    
    # Convert amount from GHS to kobo
    amount_kobo = int(amount * 100)
    
    # Get the user's email using the user_id in session
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT email FROM users WHERE id = %s", (session['user_id'],))
        user = cursor.fetchone()
        if not user:
            flash("User not found.", "error")
            return redirect(url_for('home'))
        email = user['email']
    except Exception as e:
        app.logger.error(f"Error fetching user email: {str(e)}")
        flash("Error fetching user data.", "error")
        return redirect(url_for('home'))
    finally:
        cursor.close()
        conn.close()
    
    # Prepare payload with callback_url
    payload = {
        "email": email,
        "amount": amount_kobo,
        "metadata": {
            "plan": plan,
            "user_id": session['user_id']
        },
        "callback_url": url_for('paystack_verify', _external=True)
    }
    
    headers = {
        "Authorization": "Bearer sk_test_38d38a400d7c1a34c826930691e8c23fce8dde98",  # Using test key for development
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post("https://api.paystack.co/transaction/initialize", json=payload, headers=headers)
        response_data = response.json()
        app.logger.info(f"Paystack init response: {response_data}")
        if response_data.get('status'):
            auth_url = response_data['data']['authorization_url']
            return redirect(auth_url)
        else:
            app.logger.error("Paystack initialization failed: " + response_data.get('message', 'Unknown error'))
            flash("Payment initialization failed. Please try again.", "error")
            return redirect(url_for('home'))
    except Exception as e:
        app.logger.error(f"Error initializing Paystack transaction: {str(e)}")
        flash("Error initializing payment. Please try again.", "error")
        return redirect(url_for('home'))





@app.route('/paystack_verify')
@login_required
def paystack_verify():
    reference = request.args.get('reference')
    if not reference:
        flash("Payment reference not provided.", "error")
        return redirect(url_for('home'))
    
    headers = {
        "Authorization": "Bearer sk_test_38d38a400d7c1a34c826930691e8c23fce8dde98",  # Use the same test key here
    }
    verify_url = f"https://api.paystack.co/transaction/verify/{reference}"
    
    try:
        response = requests.get(verify_url, headers=headers)
        response_data = response.json()
        app.logger.info(f"Paystack verify response: {response_data}")
        
        # Check if verification was successful
        if response_data.get("status") and response_data['data']['status'] == 'success':
            pending_listing = session.get('pending_listing')
            if not pending_listing:
                flash("No pending listing found.", "error")
                return redirect(url_for('home'))
            
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO listings (
                        user_id, title, description, category, 
                        desired_swap, desired_swap_description, additional_cash,
                        required_cash, `condition`, location, contact, image_url,
                        image1, image2, image3, image4, plan
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    pending_listing['user_id'],
                    pending_listing['title'],
                    pending_listing['description'],
                    pending_listing['category'],
                    pending_listing['desired_swap'],
                    pending_listing['desired_swap_description'],
                    pending_listing['additional_cash'],
                    pending_listing['required_cash'],
                    pending_listing['condition'],
                    pending_listing['location'],
                    pending_listing['contact'],
                    pending_listing['image_paths'][0] if len(pending_listing['image_paths']) > 0 else None,
                    pending_listing['image_paths'][1] if len(pending_listing['image_paths']) > 1 else None,
                    pending_listing['image_paths'][2] if len(pending_listing['image_paths']) > 2 else None,
                    pending_listing['image_paths'][3] if len(pending_listing['image_paths']) > 3 else None,
                    pending_listing['image_paths'][4] if len(pending_listing['image_paths']) > 4 else None,
                    pending_listing['plan']
                ))

                conn.commit()
                app.logger.info("Listing inserted successfully after payment.")
                flash("Your product has been listed successfully!", "success")
            except Exception as e:
                conn.rollback()
                app.logger.error(f"Error inserting listing after payment: {str(e)}")
                flash("Payment successful, but an error occurred while listing your product.", "error")
            finally:
                cursor.close()
                conn.close()
            
            # Clear pending listing data from session
            session.pop('pending_listing', None)
            return redirect(url_for('home'))
        else:
            flash("Payment verification failed. Please try again.", "error")
            return redirect(url_for('home'))
    except Exception as e:
        app.logger.error(f"Error verifying payment: {str(e)}")
        flash("Error verifying payment. Please try again.", "error")
        return redirect(url_for('home'))




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




if __name__ == '__main__':
    app.run(debug=True)
