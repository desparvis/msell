from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import select, and_, func, asc, desc
from sqlalchemy.orm import joinedload 
from datetime import datetime, timedelta
import os
import requests 
import cloudinary 
import cloudinary.uploader 
import cloudinary.api # Explicitly import api for folder deletion
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
app = Flask(__name__, 
            template_folder="../templates", 
            static_folder="../static")

# 2. Get the URL from the environment
raw_db_url = os.environ.get('DATABASE_URL')

# Fix the postgres prefix for SQLAlchemy
if raw_db_url and raw_db_url.startswith("postgres://"):
    raw_db_url = raw_db_url.replace("postgres://", "postgresql://", 1)

# 3. SET THE CONFIG BEFORE INITIALIZING DB
app.config['SQLALCHEMY_DATABASE_URI'] = raw_db_url or 'sqlite:///msell.db'
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'some_secret_key')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=14)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# 4. NOW Initialize SQLAlchemy
db = SQLAlchemy(app)

# --- CLOUDINARY CONFIGURATION ---
# IMPORTANT: Cloudinary access requires these three parameters.
cloudinary.config(
    cloud_name = os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key = os.getenv('CLOUDINARY_API_KEY'),
    api_secret = os.getenv('CLOUDINARY_API_SECRET')
)

# --- GLOBAL SETTINGS (Live Currency API) ---
EXCHANGE_API_URL = "https://open.er-api.com/v6/latest/"
AVAILABLE_CURRENCIES = [
    'RWF', 'USD', 'EUR', 'GBP', 'JPY', 'CNY', 'AED', 'KRW', 'CAD', 'AUD', 
    'INR', 'ZAR', 'CHF', 'SEK', 'BRL', 'KZT', 'NGN', 'KES', 'TZS', 'UGX'
]
MAIN_CURRENCIES_LIST = ['USD', 'EUR', 'GBP', 'JPY', 'CNY', 'AED', 'CAD']

# --- Cloudinary Helper Functions ---

def upload_to_cloudinary(files, product_name, user_id):
    """Uploads multiple files to Cloudinary."""
    uploaded_urls = []
    uploaded_ids = []
    
    # Folder name conversion (spaces to underscores) is correct here
    folder_name = f"msell_products/{user_id}/{product_name.replace(' ', '_')}"
    
    for file in files:
        if file.filename and file.filename != '':
            try:
                result = cloudinary.uploader.upload(
                    file, 
                    folder=folder_name,
                    unique_filename=True
                )
                uploaded_urls.append(result['secure_url'])
                uploaded_ids.append(result['public_id'])
            except Exception as e:
                print(f"Cloudinary Upload Error: {e}")
                flash("Error uploading image to Cloudinary.", 'error')
    
    # Store both URLs and Public IDs (Public IDs are required for deletion)
    return ",".join(uploaded_urls), ",".join(uploaded_ids)

def delete_from_cloudinary(public_ids):
    """
    Deletes specific image resources (files) from Cloudinary using their public IDs. 
    It is used for both incremental updates and the initial step of full product deletion.
    It does NOT delete the containing folder.
    """
    if not public_ids:
        return True # Nothing to delete, consider it a success
        
    id_list = [id.strip() for id in public_ids.split(',') if id.strip()]
    
    if id_list:
        try:
            # Calls the bulk deletion API for resources
            cloudinary.api.delete_resources(id_list)
            return True # Success
        except Exception as e:
            print(f"Cloudinary Deletion Error: {e}")
            flash("Warning: Failed to delete images from Cloudinary. They may be orphaned. The product has been removed from the database.", 'warning') 
            return False # Failure
    return True # If id_list was empty after splitting/cleaning

# --- Database Models ---

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    password_hash = db.Column(db.String(500), nullable=False)
    is_verified = db.Column(db.Boolean, default=False)
    categories = db.relationship('Category', backref='owner', lazy='dynamic')
    products = db.relationship('Product', backref='seller', lazy='dynamic')
    sales = db.relationship('Sale', backref='seller', lazy='dynamic')
    batches = db.relationship('Batch', backref='owner', lazy='dynamic')

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    products = db.relationship('Product', backref='category_info', lazy='dynamic')
    __table_args__ = (db.UniqueConstraint('name', 'user_id', name='_user_category_uc'),)

class Batch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    
    # NEW: Detailed Cost Tracking
    after_landing_total = db.Column(db.Float, default=0.0) # The total aggregate
    shipping_cost = db.Column(db.Float, default=0.0)
    tax_percent = db.Column(db.Float, default=0.0)
    tax_value = db.Column(db.Float, default=0.0)
    customs = db.Column(db.Float, default=0.0)
    declaration = db.Column(db.Float, default=0.0)
    arrival_notification = db.Column(db.Float, default=0.0)
    warehouse_storage = db.Column(db.Float, default=0.0)
    amazon_prime = db.Column(db.Float, default=0.0)
    miscellaneous = db.Column(db.Float, default=0.0)
    warehouse_usa = db.Column(db.Float, default=0.0)
    extra_costs = db.Column(db.Float, default=0.0)

    # This replaces total_adjustment_rwf for better naming
    total_adjustment_rwf = db.Column(db.Float, nullable=False) 
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    
    product_details = db.relationship('BatchProductDetail', backref='batch', lazy='dynamic')
    products = db.relationship('Product', backref='batch_info', lazy='dynamic')

    __table_args__ = (db.UniqueConstraint('name', 'user_id', name='_user_batch_uc'),)

# NEW MODEL: Stores the historical adjustment details for each product in the batch
class BatchProductDetail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey('batch.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    product_name = db.Column(db.String(150), nullable=False)
    units_in_batch = db.Column(db.Integer, nullable=False)

    # Historical values at the time of processing:
    initial_cost_rwf = db.Column(db.Float, nullable=False)          # Total cost (units * cost/unit) before batch adj.
    batch_adjustment_rwf_total = db.Column(db.Float, nullable=False) # The proportional RWF amount added to this product's total cost
    adjustment_per_unit = db.Column(db.Float, nullable=False)
    
    # Ensures we don't duplicate a product entry within one batch
    __table_args__ = (db.UniqueConstraint('batch_id', 'product_id', name='_batch_product_detail_uc'),)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    units = db.Column(db.Integer, nullable=False)
    
    # Base purchase details
    price_foreign = db.Column(db.Float, nullable=False) 
    currency = db.Column(db.String(10), nullable=False)
    
    # We are keeping cost_price_rwf as the "Total Unit Cost"
    cost_price_rwf = db.Column(db.BigInteger, nullable=False) 
    profit_margin = db.Column(db.Float, nullable=False)
    final_price_rwf = db.Column(db.BigInteger, nullable=False) 

    status = db.Column(db.String(20), default='available', nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    image_urls = db.Column(db.Text, nullable=True) 
    image_public_ids = db.Column(db.Text, nullable=True) 

    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    batch_id = db.Column(db.Integer, db.ForeignKey('batch.id'), nullable=True)
    
    sales = db.relationship('Sale', backref='product', lazy='dynamic') 
    __table_args__ = (db.UniqueConstraint('name', 'user_id', name='_user_product_uc'),)

    category = db.relationship('Category', backref='products_in_cat')

    @property
    def total_inventory_cost(self):
        # Total value of this product sitting in the warehouse
        return (self.cost_price_rwf or 0) * (self.units or 0)


class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    units_sold = db.Column(db.Integer, nullable=False, default=1)
    sold_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # The actual price the customer paid per unit
    unit_sale_price_rwf = db.Column(db.BigInteger, nullable=False) 
    # Total for this transaction (unit_sale_price * units_sold)
    total_sale_price_rwf = db.Column(db.BigInteger, nullable=False) 
    # The cost of the product per unit AT THE TIME of sale (for profit calc)
    cost_price_at_sale = db.Column(db.BigInteger, nullable=False)

# --- Database Setup Function ---

def init_db():
    """Initializes the database by creating all defined models (tables)."""
    with app.app_context():
        # NOTE: Since the schema changed, you MUST delete msell.db before running this.
        db.create_all()
        print("SQLAlchemy database tables initialized successfully.")

# --- Helper Functions (Authentication and Inventory) ---

def is_authenticated():
    """Checks if a user is logged in via the session."""
    return 'phone' in session

def get_current_user():
    """Fetches the User object from the database based on the session phone."""
    if not is_authenticated():
        return None
    
    phone = session.get('phone')
    user = db.session.execute(select(User).filter_by(phone=phone)).scalar_one_or_none()
    return user

def get_product_inventory_data(product_id, user_id):
    """Calculates available units for a product."""
    
    initial_stock = db.session.execute(
        select(Product.units).filter(Product.id == product_id, Product.user_id == user_id)
    ).scalar_one_or_none()

    if initial_stock is None:
        return None, 0, 0
    
    total_sold = db.session.execute(
        select(func.sum(Sale.units_sold)).filter(Sale.product_id == product_id)
    ).scalar_one_or_none()
    
    total_sold = total_sold if total_sold is not None else 0
    available_units = initial_stock - total_sold
    
    return initial_stock, total_sold, available_units

def convert_to_rwf(amount, currency):
    """
    Converts a foreign amount to RWF using the open.er-api.com live rates.
    """
    currency = currency.upper()
    if currency == 'RWF':
        return amount

    try:
        url = f"{EXCHANGE_API_URL}{currency}"
        response = requests.get(url, timeout=5)
        response.raise_for_status() 
        data = response.json()
        rwf_rate = data.get('rates', {}).get('RWF')
        
        if rwf_rate is None:
            raise ValueError(f"RWF rate not found in API response for base currency {currency}")

        return amount * rwf_rate
        
    except requests.exceptions.RequestException as e:
        print(f"API Request Error for {currency}: {e}")
        flash(f"Warning: Failed to fetch live currency rate for {currency}. Using fallback rate (1 unit = 1 RWF). Check internet/API status.", 'warning')
        return amount * 1.0 
        
    except (KeyError, ValueError, TypeError) as e:
        print(f"API Data Parse Error for {currency}: {e}")
        flash(f"Warning: Error processing currency data for {currency}. Using fallback rate (1 unit = 1 RWF).", 'warning')
        return amount * 1.0

def get_products_for_batching(user_id):
    """Fetches products that are not yet assigned to a batch."""
    products = db.session.execute(
        select(Product)
        .filter(Product.user_id == user_id, Product.batch_id.is_(None))
        .order_by(Product.name)
    ).scalars().all()
    
    # We need product ID, name, units, total cost, and current selling price
    products_data = []
    for p in products:
        products_data.append({
            'id': p.id,
            'name': p.name,
            'units': p.units,
            'total_cost_rwf': round(p.cost_price_rwf, 2), # Total cost for ALL units (Acquisition cost + initial taxes)
            'final_price_rwf': round(p.final_price_rwf, 2) # Final selling price per unit
        })
    return products_data


# --- Routes ---

@app.route('/')
def landing():
    return render_template('landing.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if is_authenticated(): return redirect(url_for('dashboard'))
    if request.method == 'POST':
        phone = request.form.get('login_phone', '').strip()
        password = request.form.get('login_password', '')
        user = db.session.execute(db.select(User).filter_by(phone=phone)).scalar_one_or_none()
        if user and check_password_hash(user.password_hash, password):
            session['phone'] = user.phone; session['name'] = user.name
            flash(f"Welcome back, {user.name}!", 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Login failed. Invalid phone number or password.', 'error')
    return render_template('login.html')

@app.route('/signup', methods=['POST'])
def signup():
    if is_authenticated(): 
        return redirect(url_for('dashboard'))
    
    name = request.form.get('signup_name', '').strip()
    phone = request.form.get('signup_phone', '').strip()
    password = request.form.get('signup_password', '')

    # 1. Basic Field Check
    if not all([name, phone, password]):
        flash('All fields are required for registration.', 'error')
        return redirect(url_for('login'))

    # 2. Rwanda Phone Validation (07... and 10 digits)
    # This checks if it's all digits, length is 10, and starts with 07
    if not (phone.isdigit() and len(phone) == 10 and phone.startswith('07')):
        flash('Invalid phone format. Must be 10 digits starting with 07 (e.g., 078...)', 'error')
        return redirect(url_for('login'))

    # 3. Password Length Check
    if len(password) < 6:
        flash('Password must be at least 6 characters long.', 'error')
        return redirect(url_for('login'))

    # 4. Check for existing phone manually (Cleaner than just relying on IntegrityError)
    existing_user = db.session.execute(
        db.select(User).filter_by(phone=phone)
    ).scalar_one_or_none()

    if existing_user:
        flash('An account with this phone number already exists.', 'error')
        return redirect(url_for('login'))

    # 5. Create User
    password_hash = generate_password_hash(password)
    new_user = User(
        name=name, 
        phone=phone, 
        password_hash=password_hash
        # Note: We don't need is_verified=False anymore since we rolled that back
    )

    try:
        db.session.add(new_user)
        db.session.commit()
        flash('Account created successfully! Please log in.', 'success')
    except Exception as e:
        db.session.rollback()
        print(f"Database error during signup: {e}")
        flash('An unexpected error occurred. Please try again.', 'error')

    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    if not is_authenticated(): 
        return redirect(url_for('login'))
        
    user = get_current_user()
    if not user:
        return redirect(url_for('logout'))

    filter_type = request.args.get('filter', 'day') # Options: day, month, year
    today = datetime.utcnow()

    # --- 1. Top Cards Stats (Context Aware) ---
    if filter_type == 'year':
        start_date = today.replace(month=1, day=1, hour=0, minute=0, second=0)
    elif filter_type == 'month':
        start_date = today.replace(day=1, hour=0, minute=0, second=0)
    else: # Default to 'day'
        start_date = today.replace(hour=0, minute=0, second=0)

    # Calculate Revenue and Profit for the selected filter period
    sales_query = db.session.execute(
        select(Sale).filter(Sale.user_id == user.id, Sale.sold_at >= start_date)
    ).scalars().all()

    # These are mapped to the names your HTML expects
    sales_today_rwf = int(round(sum(s.total_sale_price_rwf for s in sales_query)))
    profit_today_rwf = int(round(sum(s.total_sale_price_rwf - (s.cost_price_at_sale * s.units_sold) for s in sales_query)))

    # --- 2. Inventory Stats (Static Totals) ---
    all_products = db.session.execute(
        select(Product).filter(Product.user_id == user.id)
    ).scalars().all()
    
    total_products = len(all_products) # FIX: Defined this to stop the UndefinedError
    total_stock = 0
    low_stock_count = 0
    total_inventory_value = 0
    
    for p in all_products:
        # Use your existing helper to get current available stock
        _, _, available = get_product_inventory_data(p.id, user.id)
        if p.status == 'available':
            total_stock += available
            # Inventory value based on what you PAID for the items currently on shelf
            total_inventory_value += int(round(p.cost_price_rwf * available))
            if 0 < available <= 5: 
                low_stock_count += 1

    # --- 3. Chart Logic (Last 7 Units of Time) ---
    chart_labels, revenue_data, profit_data = [], [], []
    
    for i in range(6, -1, -1):
        if filter_type == 'year':
            d_year = today.year - i
            label = str(d_year)
            start = datetime(d_year, 1, 1)
            end = datetime(d_year, 12, 31, 23, 59, 59)
        elif filter_type == 'month':
            # Approximate month shift
            d_date = today - timedelta(days=i*30)
            label = d_date.strftime('%b')
            start = d_date.replace(day=1, hour=0, minute=0, second=0)
            # Find end of month
            next_month = (start + timedelta(days=32)).replace(day=1)
            end = next_month - timedelta(seconds=1)
        else:
            d_date = today - timedelta(days=i)
            label = d_date.strftime('%a')
            start = d_date.replace(hour=0, minute=0, second=0)
            end = d_date.replace(hour=23, minute=59, second=59)

        day_sales = db.session.execute(
            select(Sale).filter(Sale.user_id == user.id, Sale.sold_at.between(start, end))
        ).scalars().all()
        
        chart_labels.append(label)
        revenue_data.append(int(round(sum(s.total_sale_price_rwf for s in day_sales))))
        profit_data.append(int(round(sum(s.total_sale_price_rwf - (s.cost_price_at_sale * s.units_sold) for s in day_sales))))

    # --- 4. Recent Activity (Last 5 Sales) ---
    recent_sales = db.session.execute(
        select(Sale, Product.name)
        .join(Product, Sale.product_id == Product.id)
        .filter(Sale.user_id == user.id)
        .order_by(Sale.sold_at.desc())
        .limit(5)
    ).all()
    
    recent_activity = []
    for s, name in recent_sales:
        recent_activity.append({
            'product_name': name,
            'profit': int(round(s.total_sale_price_rwf - (s.cost_price_at_sale * s.units_sold))),
            'time_ago': s.sold_at.strftime('%H:%M') if s.sold_at.date() == today.date() else s.sold_at.strftime('%b %d')
        })

    # --- 5. Delivery to Template ---
    return render_template('dashboard.html', 
                           total_products=total_products,
                           total_stock=total_stock,
                           low_stock_count=low_stock_count,
                           total_inventory_value=total_inventory_value,
                           sales_today_rwf=sales_today_rwf, # Maps to the Revenue card
                           profit_today_rwf=profit_today_rwf, # Maps to the Net Profit card
                           recent_activity=recent_activity,
                           chart_labels=chart_labels,
                           revenue_data=revenue_data,
                           profit_data=profit_data,
                           filter_type=filter_type)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if not is_authenticated(): 
        flash('You must log in to access your settings.', 'error')
        return redirect(url_for('login'))
        
    user = get_current_user()
    if not user: 
        flash('User data not found.', 'error')
        return redirect(url_for('logout')) 

    if request.method == 'POST':
        action = request.form.get('action') # New hidden field to identify which form was sent
        changes_made = False

        # --- Update Name ---
        if action == 'update_name':
            new_name = request.form.get('name', '').strip()
            if new_name and user.name != new_name:
                user.name = new_name
                session['name'] = new_name
                flash('Display name updated!', 'success')
                changes_made = True

        # --- Update Phone (Login ID) ---
        elif action == 'update_phone':
            new_phone = request.form.get('phone', '').strip()
            if new_phone and user.phone != new_phone:
                existing_user = db.session.execute(db.select(User).filter_by(phone=new_phone)).scalar_one_or_none()
                if existing_user and existing_user.id != user.id:
                    flash('This phone number is already registered.', 'error')
                else:
                    user.phone = new_phone
                    session['phone'] = new_phone
                    flash('Phone updated! Use this for your next login.', 'success')
                    changes_made = True

        # --- Update Password ---
        elif action == 'update_password':
            new_pw = request.form.get('password', '')
            if len(new_pw) < 6:
                flash('Password must be at least 6 characters.', 'error')
            else:
                user.password_hash = generate_password_hash(new_pw)
                flash('Password changed successfully!', 'success')
                changes_made = True

        if changes_made:
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                flash('Database error. Update failed.', 'error')

        return redirect(url_for('settings')) 

    return render_template('settings.html', user=user)

@app.route('/add-category', methods=['GET', 'POST'])
def add_category():
    if not is_authenticated(): 
        flash('You must log in to access this page.', 'error')
        return redirect(url_for('login'))
        
    user = get_current_user()
    if not user: 
        flash('User data not found. Please log in again.', 'error')
        return redirect(url_for('logout'))

    if request.method == 'POST':
        name = request.form.get('category_name', '').strip()
        description = request.form.get('description', '').strip()
        
        if not name: 
            flash('Category Name is required.', 'error')
            return redirect(url_for('add_category'))
            
        existing_category = db.session.execute(
            select(Category).filter(and_(Category.name == name, Category.user_id == user.id))
        ).scalar_one_or_none()
        
        if existing_category: 
            flash(f'You already have a category named "{name}". Category names must be unique for your account.', 'error')
            return redirect(url_for('add_category'))
            
        new_category = Category(name=name, description=description, user_id=user.id)
        try:
            db.session.add(new_category)
            db.session.commit()
            flash(f'Category "{name}" added successfully!', 'success')
            return redirect(url_for('add_category'))
        except Exception as e:
            db.session.rollback()
            print(f"Error saving new category: {e}")
            flash('An unexpected error occurred while saving the category.', 'error')
            
    # GET request: Fetch categories to display them
    categories = db.session.execute(
        select(Category)
        .filter(Category.user_id == user.id)
        .order_by(Category.name)
    ).scalars().all()

    # Pre-calculate product count for deletion check
    categories_with_count = []
    for cat in categories:
        # We query the count separately as the relationship is lazy='dynamic'
        product_count = db.session.execute(
            select(func.count(Product.id)).filter(Product.category_id == cat.id)
        ).scalar_one()
        
        categories_with_count.append({
            'category': cat,
            'product_count': product_count
        })

    return render_template('add_category.html', categories_data=categories_with_count)

@app.route('/update-category/<int:category_id>', methods=['POST'])
def update_category(category_id):
    if not is_authenticated(): 
        flash('Authentication required.', 'error')
        return redirect(url_for('login'))
        
    user = get_current_user()
    category = db.session.execute(
        select(Category).filter(Category.id == category_id, Category.user_id == user.id)
    ).scalar_one_or_none()

    if not category:
        flash('Category not found or access denied.', 'error')
        return redirect(url_for('add_category'))

    new_name = request.form.get('category_name', '').strip()
    new_description = request.form.get('description', '').strip()

    if not new_name:
        flash('Category name cannot be empty.', 'error')
        return redirect(url_for('add_category'))

    try:
        # Check for uniqueness if the name is changing
        if category.name != new_name:
            existing_category = db.session.execute(
                select(Category).filter(
                    and_(Category.name == new_name, Category.user_id == user.id)
                )
            ).scalar_one_or_none()
            if existing_category:
                flash(f'A category named "{new_name}" already exists.', 'error')
                return redirect(url_for('add_category'))
        
        category.name = new_name
        category.description = new_description
        db.session.commit()
        flash(f'Category "{new_name}" updated successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        print(f"Error updating category: {e}")
        flash('An unexpected error occurred during the update.', 'error')
        
    return redirect(url_for('add_category'))

@app.route('/delete-category/<int:category_id>', methods=['POST'])
def delete_category(category_id):
    if not is_authenticated(): 
        flash('Authentication required.', 'error')
        return redirect(url_for('login'))
        
    user = get_current_user()
    category = db.session.execute(
        select(Category).filter(Category.id == category_id, Category.user_id == user.id)
    ).scalar_one_or_none()

    if not category:
        flash('Category not found or access denied.', 'error')
        return redirect(url_for('add_category'))

    # Crucial Integrity Check: Does the category have any products?
    product_count = db.session.execute(
        select(func.count(Product.id)).filter(Product.category_id == category.id)
    ).scalar_one()
    
    if product_count > 0:
        flash(f'Cannot delete category "{category.name}": It is associated with {product_count} product(s). Remove the products first.', 'error')
        return redirect(url_for('add_category'))

    try:
        category_name = category.name
        db.session.delete(category)
        db.session.commit()
        flash(f'Category "{category_name}" deleted successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        print(f"Error deleting category: {e}")
        flash('An unexpected error occurred during deletion.', 'error')
        
    return redirect(url_for('add_category'))


@app.route('/add-product', methods=['GET', 'POST'])
def add_product():
    if not is_authenticated(): 
        flash('You must log in to add a product.', 'error')
        return redirect(url_for('login'))
    
    user = get_current_user()
    
    # Fetch categories for the dropdown
    categories = db.session.execute(
        select(Category).filter(Category.user_id == user.id).order_by(Category.name)
    ).scalars().all()
    
    if not categories: 
        flash('Create a category first.', 'error')
        return redirect(url_for('add_category'))

    other_currencies = [c for c in AVAILABLE_CURRENCIES if c not in MAIN_CURRENCIES_LIST]

    if request.method == 'POST':
        try:
            # 1. Capture Form Data
            name = request.form.get('name', '').strip()
            category_id = int(request.form.get('category_id'))
            units = int(request.form.get('units', 1))
            currency = request.form.get('currency', 'USD').upper()
            
            # 2. Get the "Raw" Foreign Price (Hidden input from your new frontend)
            # We strip commas just in case the JS didn't catch something
            raw_price_str = request.form.get('price_foreign', '0').replace(',', '')
            price_foreign = float(raw_price_str) if raw_price_str else 0.0

            # 3. Handle Product Images (Cloudinary)
            image_files = request.files.getlist('product_images')
            image_urls_str, image_ids_str = upload_to_cloudinary(image_files, name, user.id)

            # --- VALIDATION ---
            if not name or units <= 0 or price_foreign <= 0:
                flash('Please provide a valid name, quantity, and price.', 'error')
                return redirect(url_for('add_product'))

            # --- CALCULATIONS (THE NEW LOGIC) ---
            # Step A: Convert the unit price to RWF based on the selected currency
            unit_price_rwf = convert_to_rwf(price_foreign, currency)
            
            # Step B: Calculate Total Landing Cost (For initial add, we assume no extra batch fees yet)
            # In your new table, cost_price_rwf = Total Unit Cost
            total_unit_cost = unit_price_rwf 

            # 4. Create Product using your NEW Table Schema
            new_product = Product(
                name=name, 
                units=units, 
                price_foreign=price_foreign, 
                currency=currency,
                # New Table Columns:
                cost_price_rwf=round(total_unit_cost, 2), # This is the "Total Unit Cost"
                profit_margin=0.0,                        # Starts at 0% until markup is added
                final_price_rwf=round(total_unit_cost, 2),# Initially, sales price = cost price
                status='available',
                image_urls=image_urls_str,
                image_public_ids=image_ids_str,
                user_id=user.id,
                category_id=category_id
            )

            db.session.add(new_product)
            db.session.commit()
            
            flash(f'Product "{name}" added! Base cost per unit: RWF {new_product.cost_price_rwf:,.0f}', 'success')
            return redirect(url_for('products')) 

        except Exception as e:
            db.session.rollback()
            print(f"ADD PRODUCT ERROR: {e}")
            flash(f'An error occurred: {str(e)}', 'error')
            return redirect(url_for('add_product'))
            
    return render_template('add_product.html', 
                           categories=categories, 
                           main_currencies=MAIN_CURRENCIES_LIST, 
                           other_currencies=other_currencies)

@app.route('/products')
def products():
    """Secured route for the main Products listing page, handles sorting, category, batch filtering, and sold status."""
    if not is_authenticated():
        flash('You must log in to access this page.', 'error')
        return redirect(url_for('login'))
        
    user = get_current_user()
    if not user:
        flash('User data not found. Please log in again.', 'error')
        return redirect(url_for('logout'))

    # --- Get Query Parameters ---
    sort_by = request.args.get('sort', 'newest')
    category_filter = request.args.get('category', 'all')
    batch_filter = request.args.get('batch', 'all')
    show_sold = request.args.get('show_sold', 'true') == 'true'
    
    # Start the query - Updated to use the new Product model
    query = select(Product).filter(Product.user_id == user.id)

    # --- Fetch Categories and Batches for dropdowns ---
    categories = db.session.execute(
        select(Category).filter(Category.user_id == user.id).order_by(Category.name)
    ).scalars().all()
    
    batches = db.session.execute(
        select(Batch).filter(Batch.user_id == user.id).order_by(Batch.created_at.desc())
    ).scalars().all()

    # --- Apply Category Filtering ---
    if category_filter != 'all':
        category_obj = db.session.execute(
            select(Category).filter(Category.user_id == user.id, Category.name == category_filter)
        ).scalar_one_or_none()
        if category_obj:
            query = query.filter(Product.category_id == category_obj.id)

    # --- Apply Batch Filtering ---
    if batch_filter != 'all':
        try:
            query = query.filter(Product.batch_id == int(batch_filter))
        except ValueError:
            pass

    # --- Apply "Show Sold" Filter ---
    if not show_sold:
        query = query.filter(Product.status != 'sold')

    # --- Apply Sorting (Updated for final_price_rwf) ---
    if sort_by == 'newest':
        query = query.order_by(Product.created_at.desc())
    elif sort_by == 'oldest':
        query = query.order_by(Product.created_at.asc())
    elif sort_by == 'stock_high':
        query = query.order_by(Product.units.desc())
    elif sort_by == 'stock_low':
        query = query.order_by(Product.units.asc())
    elif sort_by == 'price_high':
        query = query.order_by(Product.final_price_rwf.desc())
    elif sort_by == 'price_low':
        query = query.order_by(Product.final_price_rwf.asc())
        
    # Execute query with joinedload to avoid N+1 issues
    products = db.session.execute(
        query.options(joinedload(Product.category_info), joinedload(Product.batch_info))
    ).scalars().all()
    
    product_data = []
    for product in products:
        # Real-time stock numbers
        initial, sold, available = get_product_inventory_data(product.id, user.id)
        
        # --- YOUR STATUS SYNC TECH (RESTORED) ---
        needs_commit = False
        if available <= 0 and product.status != 'sold':
            product.status = 'sold'
            needs_commit = True
        elif available > 0 and product.status == 'sold':
            product.status = 'available'
            needs_commit = True
            
        if needs_commit:
            db.session.add(product)
            db.session.commit()

        # Handle images
        first_image = product.image_urls.split(',')[0].strip() if product.image_urls else None

        product_data.append({
            'product': product,
            'available_units': available,
            'total_sold': sold,
            'first_image': first_image
        })
    
    return render_template('products.html', 
                            products_data=product_data,
                            categories=categories,
                            batches=batches,
                            current_sort=sort_by,
                            current_category=category_filter,
                            current_batch=batch_filter,
                            show_sold=show_sold)

@app.route('/product/<int:product_id>')
def product_detail(product_id):
    """New route for viewing detailed product information."""
    if not is_authenticated():
        flash('You must log in to access this page.', 'error'); return redirect(url_for('login'))
        
    user = get_current_user()
    if not user:
        flash('User data not found. Please log in again.', 'error'); return redirect(url_for('logout'))

    product = db.session.execute(
        select(Product)
        .filter(Product.id == product_id, Product.user_id == user.id)
        .options(joinedload(Product.category_info))
    ).scalar_one_or_none()

    if not product:
        flash('Product not found or access denied.', 'error'); return redirect(url_for('products'))
    
    # Get sales and inventory data
    initial, sold, available = get_product_inventory_data(product.id, user.id)
    
    # Get all image URLs
    images = [url.strip() for url in product.image_urls.split(',') if url.strip()]
    
    # Get all sales records
    sales_records = db.session.execute(
        select(Sale).filter(Sale.product_id == product.id).order_by(Sale.sold_at.desc())
    ).scalars().all()

    return render_template('product_detail.html', 
                           product=product,
                           available_units=available,
                           total_sold=sold,
                           images=images,
                           sales_records=sales_records)


@app.route('/sell-unit/<int:product_id>', methods=['POST'])
def sell_unit(product_id):
    if not is_authenticated():
        return redirect(url_for('login'))
    
    user = get_current_user()
    product = db.session.execute(
        select(Product).filter(Product.id == product_id, Product.user_id == user.id)
    ).scalar_one_or_none()
    
    if not product:
        flash('Product not found.', 'error')
        return redirect(url_for('products'))

    try:
        # 1. Capture and CLEAN inputs
        units_to_sell = int(request.form.get('units_to_sell', 1))
        
        # We convert to float first to handle any decimals, then round to nearest WHOLE integer
        raw_price = request.form.get('sale_price_rwf', '0').replace(',', '')
        selling_price_per_unit = int(round(float(raw_price)))
        
        if units_to_sell <= 0 or selling_price_per_unit <= 0:
            raise ValueError
            
    except (ValueError, TypeError):
        flash('Invalid numbers. Please try again.', 'error')
        return redirect(request.referrer)

    # 2. Inventory Check
    _, _, available_units = get_product_inventory_data(product_id, user.id)
    if available_units < units_to_sell:
        flash(f'Shortage: Only {available_units} left.', 'error')
        return redirect(request.referrer)

    # 3. THE FOREVER MATH FIX
    # Force the cost price from DB to be a strict integer to kill hidden .000000004 decimals
    base_cost_per_unit = int(round(float(product.cost_price_rwf))) 
    
    # Integer - Integer = Perfect Math (e.g., 2000 - 2000 = 0, NOT -1)
    profit_per_unit = selling_price_per_unit - base_cost_per_unit
    
    total_profit_for_this_sale = profit_per_unit * units_to_sell
    total_revenue = selling_price_per_unit * units_to_sell

    # 4. Record the Sale
    new_sale = Sale(
        product_id=product.id,
        user_id=user.id,
        units_sold=units_to_sell,
        unit_sale_price_rwf=selling_price_per_unit, 
        total_sale_price_rwf=total_revenue,        
        cost_price_at_sale=base_cost_per_unit       
    )

    try:
        db.session.add(new_sale)
        
        # If stock hits zero, mark as sold
        if available_units - units_to_sell <= 0:
            product.status = 'sold'
        
        db.session.commit()
        
        # Flash message using clean integer formatting
        flash(f'Sold {units_to_sell} units. Total Profit: {total_profit_for_this_sale:,.0f} RWF', 'success')
        
    except Exception as e:
        db.session.rollback()
        print(f"CRITICAL SALE ERROR: {e}")
        flash('Error recording sale.', 'error')

    return redirect(request.referrer or url_for('products'))


@app.route('/delete-product/<int:product_id>', methods=['POST'])
def delete_product(product_id):
    """Deletes a product only if no units have ever been sold, and deletes images and the folder from Cloudinary."""
    if not is_authenticated():
        flash('You must be logged in to delete products.', 'error'); return redirect(url_for('login'))
        
    user = get_current_user()
    
    product = db.session.execute(
        select(Product).filter(Product.id == product_id, Product.user_id == user.id)
    ).scalar_one_or_none()
    
    if not product:
        flash('Product not found or access denied.', 'error'); return redirect(url_for('products'))

    # Check if any sales have been made
    total_sold = db.session.execute(
        select(func.count(Sale.id)).filter(Sale.product_id == product_id)
    ).scalar()
    
    if total_sold and total_sold > 0:
        flash(f'Cannot delete "{product.name}": {total_sold} units have been sold. Delete is only allowed for products with no sales history.', 'error')
        return redirect(url_for('products'))

    try:
        product_name = product.name
        
        # 1. Delete all image resources using the helper function
        cloudinary_success = delete_from_cloudinary(product.image_public_ids)

        # 2. Delete the product folder (CRITICAL STEP for full cleanup)
        folder_deleted = True
        user_id = user.id
        # Generate the folder path using the space-to-underscore convention
        folder_name = f"msell_products/{user.id}/{product_name.replace(' ', '_')}"
        
        if folder_name:
            try:
                # Attempt to delete the folder (Cloudinary requires it to be empty first, which delete_from_cloudinary ensures)
                cloudinary.api.delete_folder(folder_name)
                folder_deleted = True
            except Exception as e:
                print(f"Cloudinary Folder Deletion Error for {folder_name}: {e}")
                folder_deleted = False
                flash(f"Warning: Failed to delete Cloudinary folder for '{product_name}'. It may be empty but still visible in Cloudinary. All files were deleted.", 'warning')


        # 3. Delete product from database
        db.session.delete(product)
        db.session.commit()
        
        # 4. Provide specific success message
        if cloudinary_success and folder_deleted:
            flash(f'Product "{product_name}" successfully deleted (including all images and folder from Cloudinary).', 'success')
        elif cloudinary_success:
             # The warning is already flashed inside delete_from_cloudinary()
             flash(f'Product "{product_name}" successfully deleted from database.', 'success')
        # If cloudinary_success was False, the initial warning was already flashed by delete_from_cloudinary

    except Exception as e:
        db.session.rollback()
        print(f"Error deleting product: {e}")
        flash('An error occurred during deletion.', 'error')
        
    return redirect(url_for('products'))


@app.route('/update-product/<int:product_id>', methods=['GET', 'POST'])
def update_product(product_id):
    if not is_authenticated():
        flash('You must be logged in to update products.', 'error')
        return redirect(url_for('login'))
        
    user = get_current_user()
    product = db.session.execute(
        select(Product).filter(Product.id == product_id, Product.user_id == user.id)
    ).scalar_one_or_none()
    
    if not product:
        flash('Product not found or access denied.', 'error')
        return redirect(url_for('products'))
        
    _, total_sold, _ = get_product_inventory_data(product_id, user.id)
    categories = db.session.execute(
        select(Category).filter(Category.user_id == user.id).order_by(Category.name)
    ).scalars().all()
    
    other_currencies = [c for c in AVAILABLE_CURRENCIES if c not in MAIN_CURRENCIES_LIST]

    if request.method == 'POST':
        try:
            # --- 1. Helper Function to "Loosen Up" Number Detection ---
            def clean_num(value, default=0.0):
                if not value: return default
                try:
                    # Remove commas just in case the user types "1,000"
                    return float(str(value).replace(',', '').strip())
                except:
                    return default

            # --- 2. Image Handling (Keeping your Cloudinary logic) ---
            images_to_delete_str = request.form.get('images_to_delete', '')
            deleted_ids = []
            if images_to_delete_str:
                ids_to_delete = [id.strip() for id in images_to_delete_str.split(',') if id.strip()]
                delete_from_cloudinary(images_to_delete_str) 
                deleted_ids.extend(ids_to_delete)
                
            existing_urls = product.image_urls.split(',') if product.image_urls else []
            existing_ids = product.image_public_ids.split(',') if product.image_public_ids else []
            existing_map = {existing_ids[i].strip(): existing_urls[i].strip() for i in range(len(existing_ids)) if existing_ids[i].strip()}
            updated_ids = [id for id in existing_map.keys() if id not in deleted_ids]
            updated_urls = [existing_map[id] for id in updated_ids]
            
            new_image_files = request.files.getlist('new_product_images')
            if new_image_files and new_image_files[0].filename:
                new_urls_str, new_ids_str = upload_to_cloudinary(new_image_files, product.name, user.id)
                updated_urls.extend([url.strip() for url in new_urls_str.split(',') if url.strip()])
                updated_ids.extend([id.strip() for id in new_ids_str.split(',') if id.strip()])

            product.image_urls = ",".join(updated_urls)
            product.image_public_ids = ",".join(updated_ids)
            
            # --- 3. Update Fields with the "Clean" logic ---
            product.name = request.form.get('name', '').strip()
            product.category_id = int(request.form.get('category_id'))
            
            if total_sold == 0:
                product.units = int(clean_num(request.form.get('units'), default=1))
            
            # NO MORE CRASHING: These will now default to 0 if left empty
            product.price_foreign = clean_num(request.form.get('price_foreign'))
            product.currency = request.form.get('currency', 'RWF').upper()
            product.tax_percent = clean_num(request.form.get('tax_percent'))
            product.declaration_rwf = clean_num(request.form.get('declaration_rwf'))
            product.extra_costs_rwf = clean_num(request.form.get('extra_costs_rwf'))
            product.profit_margin = 0.0 

            # --- 4. THE AUTO-CALCULATION (Using Integers to stop the -1 error) ---
            price_per_unit_rwf = convert_to_rwf(product.price_foreign, product.currency)
            total_cogs_rwf = product.units * price_per_unit_rwf
            
            total_cost_landed = total_cogs_rwf + product.declaration_rwf + product.extra_costs_rwf
            tax_amount_rwf = total_cost_landed * (product.tax_percent / 100)
            final_total_inventory_value = total_cost_landed + tax_amount_rwf
            
            # --- FOREVER FIX: Convert to BigInt compatible whole numbers ---
            product.cost_price_rwf = int(round(final_total_inventory_value)) 
            product.final_price_rwf = int(round(final_total_inventory_value / product.units)) 

            db.session.commit()
            flash(f'Updated! Unit cost is now {product.final_price_rwf:,} RWF.', 'success')
            return redirect(url_for('products'))

        except Exception as e:
            db.session.rollback()
            print(f"Update Error: {e}")
            flash(f'Error: {str(e)}', 'error')
            return redirect(url_for('update_product', product_id=product_id))

    return render_template('update_product.html', 
                           product=product,
                           categories=categories, 
                           main_currencies=MAIN_CURRENCIES_LIST, 
                           other_currencies=other_currencies,
                           total_sold=total_sold)

# --- BATCH ROUTES ---

@app.route('/batches', methods=['GET'])
def batches():
    if not is_authenticated(): 
        return redirect(url_for('login'))
        
    user = get_current_user()

    # 1. Fetch products belonging to user that are NOT in a batch
    unbatched_products = Product.query.filter_by(
        user_id=user.id, 
        batch_id=None
    ).all()
    
    # 2. Filter for products with zero sales (using the correct 'sales' attribute)
    eligible_products = []
    for p in unbatched_products:
        # Check the 'sales' relationship defined in your model
        if p.sales.count() == 0:
            eligible_products.append(p)
    
    # 3. Fetch batches for the sidebar
    existing_batches = Batch.query.filter_by(user_id=user.id)\
                        .order_by(Batch.created_at.desc()).all()

    return render_template('batches.html', 
                           eligible_products=eligible_products,
                           existing_batches=existing_batches)

@app.route('/process-batch', methods=['POST'])
def process_batch():
    if not is_authenticated(): 
        flash('Authentication required.', 'error')
        return redirect(url_for('login'))
        
    user = get_current_user()
    
    try:
        batch_name = request.form.get('batch_name', '').strip()
        selected_product_ids = request.form.getlist('product_ids')

        # LOOSE NUMBER DETECTION (Cleans commas and empty fields)
        def get_val(field):
            val = request.form.get(field, '0').replace(',', '').strip()
            try:
                return float(val) if val else 0.0
            except ValueError:
                return 0.0

        # Capture Inputs
        simple_total = get_val('adjustment_amount')
        ship = get_val('shipping_cost')
        tax_v = get_val('tax_value')
        cust = get_val('customs')
        decl = get_val('declaration')
        arr_notif = get_val('arrival_notification')
        ware_store = get_val('warehouse_storage')
        amz = get_val('amazon_prime')
        ware_usa = get_val('warehouse_usa')
        misc = get_val('miscellaneous')
        extra = get_val('extra_costs')

        expanded_sum = (ship + tax_v + cust + decl + arr_notif + 
                        ware_store + amz + ware_usa + misc + extra)

        final_aggregate = expanded_sum if expanded_sum > 0 else simple_total

        if not batch_name or not selected_product_ids or final_aggregate <= 0:
            flash("Batch processing failed: Provide name and cost > 0.", 'error')
            return redirect(url_for('batches'))

        # Fetch Products
        product_objects = db.session.execute(
            select(Product).filter(
                Product.id.in_([int(pid) for pid in selected_product_ids]),
                Product.user_id == user.id,
                Product.batch_id.is_(None)
            )
        ).scalars().all()

        if not product_objects:
            flash("No valid products selected.", 'error')
            return redirect(url_for('batches'))

        # 5. Calculate Total Value (Denominator)
        # Using float here for the calculation, but we will round later
        total_shipment_value = sum(float(p.cost_price_rwf) * (p.units if p.units > 0 else 1) for p in product_objects)

        if total_shipment_value <= 0:
            flash("Selected products have no cost base.", 'error')
            return redirect(url_for('batches'))

        new_batch = Batch(
            name=batch_name,
            user_id=user.id,
            after_landing_total=int(round(final_aggregate)), # FORCE INT
            total_adjustment_rwf=int(round(final_aggregate)) # FORCE INT
        )
        db.session.add(new_batch)
        db.session.flush() 

        # 7. THE PRECISION DISTRIBUTION LOOP
        for product in product_objects:
            qty = product.units if product.units > 0 else 1
            
            # Use float for the middle-step math to keep it fair
            line_value = float(product.cost_price_rwf) * qty
            weight_factor = line_value / total_shipment_value
            share_of_total = weight_factor * final_aggregate
            adj_per_unit = share_of_total / qty
            
            # --- THE "FOREVER FIX" STARTS HERE ---
            old_cost = float(product.cost_price_rwf)
            
            # We FORCE the new cost to be a clean Integer
            # This kills the .333333 decimals immediately
            new_cost_rounded = int(round(old_cost + adj_per_unit))
            
            product.batch_id = new_batch.id
            product.cost_price_rwf = new_cost_rounded # Saved as BIGINT in Supabase
            
            # Update final price (the unit cost display)
            # We ensure this is also a clean whole number
            product.final_price_rwf = int(round(float(product.final_price_rwf) + adj_per_unit))
            
            # Recalculate Margin safely using our clean whole numbers
            if product.cost_price_rwf > 0:
                new_margin = ((product.final_price_rwf - product.cost_price_rwf) / product.cost_price_rwf) * 100
                product.profit_margin = round(new_margin, 2)

            # Log details (keeping it clean for the history table)
            detail = BatchProductDetail(
                batch_id=new_batch.id,
                product_id=product.id,
                product_name=product.name,
                units_in_batch=qty,
                initial_cost_rwf=int(round(old_cost)),
                batch_adjustment_rwf_total=int(round(share_of_total)),
                adjustment_per_unit=int(round(adj_per_unit))
            )
            db.session.add(detail)

        db.session.commit()
        flash(f'Batch "{batch_name}" finalized with whole-unit precision!', 'success')
        return redirect(url_for('batches'))

    except Exception as e:
        db.session.rollback()
        print(f"BATCH ERROR: {str(e)}")
        flash(f'System Error: {str(e)}', 'error')
        return redirect(url_for('batches'))

@app.route('/batch-detail/<int:batch_id>', methods=['GET'])
def batch_detail(batch_id):
    if not is_authenticated(): 
        flash('Authentication required.', 'error')
        return redirect(url_for('login'))
        
    user = get_current_user()
    batch = db.session.execute(
        select(Batch)
        .filter(Batch.id == batch_id, Batch.user_id == user.id)
    ).scalar_one_or_none()
    
    if not batch:
        flash("Batch not found or access denied.", 'error')
        return redirect(url_for('batches'))
        
    # Fetch the historical distribution details
    details = db.session.execute(
        select(BatchProductDetail, Product.final_price_rwf)
        .join(Product, BatchProductDetail.product_id == Product.id)
        .filter(BatchProductDetail.batch_id == batch_id)
    ).all()
    
    # We need to calculate the old final price before the adjustment was made.
    # New Final Price = Old Final Price + Adjustment Per Unit
    # Old Final Price = New Final Price - Adjustment Per Unit
    
    detail_data = []
    for detail, current_final_price_rwf in details:
        # Initial final selling price before adjustment
        initial_final_price_rwf = current_final_price_rwf - detail.adjustment_per_unit
        
        detail_data.append({
            'product_name': detail.product_name,
            'units': detail.units_in_batch,
            'initial_total_cost': round(detail.initial_cost_rwf, 2),
            'batch_adj_total': round(detail.batch_adjustment_rwf_total, 2),
            'adj_per_unit': round(detail.adjustment_per_unit, 2),
            'old_price_per_unit': round(initial_final_price_rwf, 2),
            'new_price_per_unit': round(current_final_price_rwf, 2),
        })

    return render_template('batch_detail.html', batch=batch, detail_data=detail_data)

from datetime import datetime, timedelta

@app.route('/sales')
def sales_history():
    if not is_authenticated():
        return redirect(url_for('login'))
    
    user = get_current_user()
    sort = request.args.get('sort', 'newest')
    filter_days = request.args.get('days', 'all')

    query = select(Sale).filter(Sale.user_id == user.id)

    if filter_days != 'all':
        try:
            days_int = int(filter_days)
            start_date = datetime.utcnow() - timedelta(days=days_int)
            query = query.filter(Sale.sold_at >= start_date)
        except ValueError:
            pass

    # Sorting logic
    if sort == 'oldest':
        query = query.order_by(Sale.sold_at.asc())
    else:
        query = query.order_by(Sale.sold_at.desc())

    sales_results = db.session.execute(query).scalars().all()

    total_revenue = 0
    total_profit = 0

    for s in sales_results:
        # 1. Calculate Revenue and Total Cost for this specific sale
        rev = s.total_sale_price_rwf
        # Make sure we use the snapshot cost per unit
        cost = s.cost_price_at_sale * s.units_sold
        
        # 2. THE FIX: Round and convert to int to kill "-1" and "-0"
        s.calculated_profit = int(round(rev - cost))
        
        # 3. Add to totals
        total_revenue += int(rev)
        total_profit += s.calculated_profit
    
    return render_template('sales.html', 
                           sales=sales_results, 
                           current_sort=sort, 
                           current_days=filter_days,
                           total_revenue=total_revenue,
                           total_profit=total_profit,
                           now=datetime.utcnow()) # This fixes your HTML header

@app.route('/batch-list')
def batch_list():
    if not is_authenticated():
        return redirect(url_for('login'))
    
    user = get_current_user()
    
    # Get Filter Params
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    
    query = select(Batch).filter(Batch.user_id == user.id)

    # Date Range Filtering Logic
    if start_date_str:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
        query = query.filter(Batch.created_at >= start_date)
    
    if end_date_str:
        # We add 23:59:59 to the end date to include the whole day
        end_date = datetime.strptime(f"{end_date_str} 23:59:59", '%Y-%m-%d %H:%M:%S')
        query = query.filter(Batch.created_at <= end_date)

    # Default to newest first
    query = query.order_by(Batch.created_at.desc())
    
    batches = db.session.execute(query).scalars().all()
    
    return render_template('batchlist.html', 
                           batches=batches, 
                           start_date=start_date_str, 
                           end_date=end_date_str)

@app.route('/init-db-once')
def init_db_once():
    with app.app_context():
        db.create_all()
    return "Database initialized on Supabase!"

@app.route('/logout')
def logout():
    """Logs out the user by clearing the session."""
    session.pop('phone', None)
    session.pop('name', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

if __name__ == '__main__':
    init_db()
    print("Starting Msell Flask application...")
    app.run(debug=True)