import csv
import io
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "mysql+pymysql://root:@127.0.0.1/alts_wealth_watch"
app.config["SECRET_KEY"] = "dev-key-change-later"
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
db = SQLAlchemy(app)


class User(db.Model):
    __tablename__ = "Users"
    user_id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(255))
    role = db.Column(db.String(20), nullable=False)
    account_status = db.Column(db.String(20), nullable=False)
    department = db.Column(db.String(100))
    position_title = db.Column(db.String(100))
    county = db.Column(db.String(50))
    reset_token = db.Column(db.String(64))
    reset_token_expiry = db.Column(db.DateTime)


class Declaration(db.Model):
    __tablename__ = "Declarations"
    declaration_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("Users.user_id"), nullable=False)
    filing_period = db.Column(db.String(20), nullable=False)
    annual_income = db.Column(db.Numeric(12, 2))
    status = db.Column(db.String(20), nullable=False)
    date_submitted = db.Column(db.DateTime)
    current_version_no = db.Column(db.Integer, default=1)
    confirmation_checked = db.Column(db.Boolean)


class Asset(db.Model):
    __tablename__ = "Assets"
    asset_id = db.Column(db.Integer, primary_key=True)
    declaration_id = db.Column(db.Integer, db.ForeignKey("Declarations.declaration_id"), nullable=False)
    asset_type = db.Column(db.String(20), nullable=False)
    description = db.Column(db.Text, nullable=False)
    self_declared_value = db.Column(db.Numeric(14, 2))


class Liability(db.Model):
    __tablename__ = "Liabilities"
    liability_id = db.Column(db.Integer, primary_key=True)
    declaration_id = db.Column(db.Integer, db.ForeignKey("Declarations.declaration_id"), nullable=False)
    liability_type = db.Column(db.String(20), nullable=False)
    description = db.Column(db.Text, nullable=False)
    amount = db.Column(db.Numeric(14, 2))


class DeclarationVersion(db.Model):
    __tablename__ = "DeclarationVersions"
    version_id = db.Column(db.Integer, primary_key=True)
    declaration_id = db.Column(db.Integer, db.ForeignKey("Declarations.declaration_id"), nullable=False)
    version_number = db.Column(db.Integer, nullable=False)
    date_created = db.Column(db.DateTime)
    change_notes = db.Column(db.Text)


class ReviewLog(db.Model):
    __tablename__ = "ReviewLog"
    log_id = db.Column(db.Integer, primary_key=True)
    declaration_id = db.Column(db.Integer, db.ForeignKey("Declarations.declaration_id"), nullable=False)
    admin_id = db.Column(db.Integer, db.ForeignKey("Users.user_id"), nullable=False)
    action = db.Column(db.String(20), nullable=False)
    comment = db.Column(db.Text)
    date_actioned = db.Column(db.DateTime)
    
    
class Valuation(db.Model):
    __tablename__ = "Valuations"
    valuation_id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("Assets.asset_id"), unique=True, nullable=False)
    ai_estimated_value = db.Column(db.Numeric(14, 2))
    gap_percentage = db.Column(db.Numeric(7, 2))
    flag_tier = db.Column(db.String(20), nullable=False)
    date_generated = db.Column(db.DateTime)
    
    
def to_decimal(value):
    return Decimal(value) if value else None


TYPE_FACTORS = {"land": 1.2, "building": 1.15, "vehicle": 0.9, "other": 1.0}

LOCATION_FACTORS = {
    "karen": 1.5, "westlands": 1.5, "kilimani": 1.45, "nairobi": 1.35,
    "kitengela": 1.25, "mombasa": 1.2, "kisumu": 1.15, "nakuru": 1.1,
    "rural": 0.8, "shamba": 0.8, "village": 0.8,
}

FLAG_ORDER = {"high_priority": 0, "moderate": 1, "pass": 2}


def estimate_value(asset_type, description, declared_value):
    factor = TYPE_FACTORS.get(asset_type, 1.0)
    text = (description or "").lower()
    for word, location_factor in LOCATION_FACTORS.items():
        if word in text:
            factor *= location_factor
            break
    return (declared_value * Decimal(str(factor))).quantize(Decimal("0.01"))


def flag_for_gap(gap):
    if gap > 60:
        return "high_priority"
    if gap >= 25:
        return "moderate"
    return "pass"


def ensure_valuations(declaration_id):
    assets = Asset.query.filter_by(declaration_id=declaration_id).all()
    for a in assets:
        if a.self_declared_value is None or a.self_declared_value <= 0:
            continue
        if Valuation.query.filter_by(asset_id=a.asset_id).first():
            continue
        estimate = estimate_value(a.asset_type, a.description, a.self_declared_value)
        gap = ((estimate - a.self_declared_value) / a.self_declared_value * 100).quantize(Decimal("0.01"))
        db.session.add(Valuation(
            asset_id=a.asset_id,
            ai_estimated_value=estimate,
            gap_percentage=gap,
            flag_tier=flag_for_gap(gap),
            date_generated=datetime.now(),
        ))

def save_declaration(status, decl=None):
    if decl is None:
        decl = Declaration(user_id=session["user_id"], filing_period="2026", status=status)
        db.session.add(decl)

    decl.annual_income = to_decimal(request.form.get("annual_income"))
    decl.status = status
    if status == "submitted":
        decl.date_submitted = datetime.now()
        decl.confirmation_checked = True
    db.session.flush()  # makes sure the declaration has its declaration_id

    # replace the old asset and liability rows with what is on the form now
    Asset.query.filter_by(declaration_id=decl.declaration_id).delete()
    Liability.query.filter_by(declaration_id=decl.declaration_id).delete()

    if request.form.get("asset_type"):
        db.session.add(Asset(
            declaration_id=decl.declaration_id,
            asset_type=request.form["asset_type"],
            description=request.form.get("asset_description", ""),
            self_declared_value=to_decimal(request.form.get("asset_value")),
        ))

    if request.form.get("liability_type"):
        db.session.add(Liability(
            declaration_id=decl.declaration_id,
            liability_type=request.form["liability_type"],
            description=request.form.get("liability_description", ""),
            amount=to_decimal(request.form.get("liability_amount")),
        ))
    

    if status == "submitted":
        db.session.add(DeclarationVersion(
            declaration_id=decl.declaration_id,
            version_number=1,
            date_created=datetime.now(),
            change_notes="Initial submission",
        ))
        ensure_valuations(decl.declaration_id)

    db.session.commit()


def declaration_form(decl):
    """Shows the form and handles Save draft / Submit, for a new or an existing draft."""
    if request.method == "POST":
        action = request.form.get("action")

        if action == "draft":
            save_declaration("draft", decl)
            flash("Draft saved.")
            return redirect(url_for("dashboard"))

        if action == "submit":
            if not request.form.get("confirm"):
                flash("Please tick the confirmation box before submitting.")
                return redirect(request.url)
            if not request.form.get("annual_income"):
                flash("Enter your annual income before submitting.")
                return redirect(request.url)
            save_declaration("submitted", decl)
            flash("Declaration submitted.")
            return redirect(url_for("dashboard"))

    asset = liability = None
    if decl:
        asset = Asset.query.filter_by(declaration_id=decl.declaration_id).first()
        liability = Liability.query.filter_by(declaration_id=decl.declaration_id).first()
    return render_template("new_declaration.html", decl=decl, asset=asset, liability=liability)


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/dbtest")
def dbtest():
    return f"Connected. Users in database: {User.query.count()}"


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]
        user = User.query.filter_by(email=email).first()
        if (user and user.account_status == "active" and user.password_hash
                and check_password_hash(user.password_hash, password)):
            session.clear()
            session["user_id"] = user.user_id
            session["role"] = user.role
            session["name"] = user.full_name
            if user.role == "admin":
               return redirect(url_for("review_queue"))
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.")
    return render_template("login.html")


@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("role") == "admin":
        return redirect(url_for("review_queue"))
    declarations = (Declaration.query.filter_by(user_id=session["user_id"])
                    .order_by(Declaration.declaration_id.desc()).all())
    return render_template("dashboard.html", declarations=declarations)


@app.route("/declaration/new", methods=["GET", "POST"])
def new_declaration():
    if "user_id" not in session:
        return redirect(url_for("login"))
    return declaration_form(None)


@app.route("/declaration/<int:declaration_id>/edit", methods=["GET", "POST"])
def edit_declaration(declaration_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    decl = db.get_or_404(Declaration, declaration_id)
    # only the owner can open it, and only while it is still a draft
    if decl.user_id != session["user_id"] or decl.status != "draft":
        abort(403)
    return declaration_form(decl)


@app.route("/admin/review")
def review_queue():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("role") != "admin":
        abort(403)
    rows = (db.session.query(Declaration, User)
            .join(User, Declaration.user_id == User.user_id)
            .filter(Declaration.status == "submitted")
            .all())
    items = []
    for d, u in rows:
        ensure_valuations(d.declaration_id)
        flags = [v.flag_tier for v in Valuation.query
                 .join(Asset, Valuation.asset_id == Asset.asset_id)
                 .filter(Asset.declaration_id == d.declaration_id).all()]
        flag = min(flags, key=lambda f: FLAG_ORDER[f]) if flags else "none"
        items.append((d, u, flag))
    db.session.commit()
    items.sort(key=lambda i: FLAG_ORDER.get(i[2], 3))
    return render_template("review_queue.html", rows=items)


@app.route("/admin/review/<int:declaration_id>", methods=["GET", "POST"])
def review_declaration(declaration_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("role") != "admin":
        abort(403)
    decl = db.get_or_404(Declaration, declaration_id)
    if decl.status != "submitted":
        abort(403)
    official = db.session.get(User, decl.user_id)
    ensure_valuations(declaration_id)
    db.session.commit()
    assets = Asset.query.filter_by(declaration_id=declaration_id).all()
    asset_rows = [(a, Valuation.query.filter_by(asset_id=a.asset_id).first()) for a in assets]
    liabilities = Liability.query.filter_by(declaration_id=declaration_id).all()

    if request.method == "POST":
        action = request.form.get("action")
        comment = request.form.get("comment", "").strip()
        new_status = {"approve": "approved", "reject": "rejected",
                      "escalate": "escalated"}.get(action)
        if new_status is None:
            abort(400)
        if action == "reject" and not comment:
            flash("Please write a comment explaining the rejection.")
            return redirect(request.url)
        decl.status = new_status
        db.session.add(ReviewLog(
            declaration_id=declaration_id,
            admin_id=session["user_id"],
            action=action,
            comment=comment or None,
            date_actioned=datetime.now(),
        ))
        db.session.commit()
        flash(f"Declaration {declaration_id} {new_status}.")
        return redirect(url_for("review_queue"))

    return render_template("review_declaration.html", decl=decl, official=official,
                           asset_rows=asset_rows, liabilities=liabilities)
    
    
@app.route("/admin/officials", methods=["GET", "POST"])
def upload_officials():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("role") != "admin":
        abort(403)

    if request.method == "POST":
        file = request.files.get("csv_file")
        if not file or not file.filename.lower().endswith(".csv"):
            flash("Please choose a .csv file.")
            return redirect(request.url)
        try:
            text = file.stream.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            flash("The file could not be read. Save it as plain text (UTF-8).")
            return redirect(request.url)

        reader = csv.DictReader(io.StringIO(text))
        required = {"full_name", "email", "department", "position_title", "county"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            flash("The first row must be: full_name,email,department,position_title,county")
            return redirect(request.url)

        added = skipped = 0
        for row in reader:
            name = (row.get("full_name") or "").strip()
            email = (row.get("email") or "").strip().lower()
            if not name or "@" not in email:
                skipped += 1
                continue
            if User.query.filter_by(email=email).first():
                skipped += 1
                continue
            db.session.add(User(
                full_name=name,
                email=email,
                role="official",
                account_status="invited",
                department=(row.get("department") or "").strip(),
                position_title=(row.get("position_title") or "").strip(),
                county=(row.get("county") or "").strip(),
            ))
            added += 1
        db.session.commit()
        flash(f"{added} officials registered, {skipped} skipped (duplicate or invalid).")
        return redirect(request.url)

    officials = User.query.filter_by(role="official").all()
    return render_template("upload_officials.html", officials=officials)
    
   
@app.route("/admin/officials/<int:user_id>/link", methods=["POST"])
def create_setup_link(user_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("role") != "admin":
        abort(403)
    official = db.get_or_404(User, user_id)
    if official.role != "official" or official.account_status != "invited":
        abort(403)
    official.reset_token = secrets.token_urlsafe(32)
    official.reset_token_expiry = datetime.now() + timedelta(hours=24)
    db.session.commit()
    link = url_for("set_password", token=official.reset_token, _external=True)
    flash(f"Setup link for {official.full_name} (valid 24 hours): {link}")
    return redirect(url_for("upload_officials"))


@app.route("/set-password/<token>", methods=["GET", "POST"])
def set_password(token):
    user = User.query.filter_by(reset_token=token).first()
    if (not user or not user.reset_token_expiry
            or user.reset_token_expiry < datetime.now()):
        return render_template("set_password.html", valid=False)
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if len(password) < 8:
            flash("Password must be at least 8 characters.")
            return redirect(request.url)
        if password != confirm:
            flash("The two passwords do not match.")
            return redirect(request.url)
        user.password_hash = generate_password_hash(password)
        user.account_status = "active"
        user.reset_token = None
        user.reset_token_expiry = None
        db.session.commit()
        flash("Password set. You can now sign in.")
        return redirect(url_for("login"))
    return render_template("set_password.html", valid=True, name=user.full_name)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))