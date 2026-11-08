import os, secrets, markdown, time, threading
from collections import defaultdict, deque
from functools import wraps
from urllib.parse import urlparse

# Load environment variables from a local .env file (if present) BEFORE importing
# libraries that read env vars at import time (e.g. oauthlib reads
# OAUTHLIB_INSECURE_TRANSPORT). This way `python server.py` always picks up the
# values without exporting them manually each time.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, request, jsonify, redirect, url_for, make_response, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from werkzeug.security import generate_password_hash, check_password_hash
from flask_socketio import SocketIO, emit, join_room, leave_room
from authlib.integrations.flask_client import OAuth
from flask_cors import CORS
import re
import base64
from sqlalchemy import func
import io
from PIL import Image
from datetime import datetime, date, timedelta
import pytz
from flask_migrate import Migrate

# -------------------------------------------------
# App / Config
# -------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# APP_ENV=production (default) enforces strict settings. Use APP_ENV=development locally.
IS_PROD = os.environ.get("APP_ENV", "production").lower() == "production"
if IS_PROD:
    # Never allow OAuth over plain HTTP in production, even if a stray .env sets it.
    os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)

app = Flask(__name__)

# Behind a reverse proxy (nginx / Render / Railway / Fly...) trust X-Forwarded-* headers
# so that HTTPS URLs (e.g. the Google OAuth callback) are generated correctly.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

_secret = os.environ.get("SECRET_KEY", "")
if not _secret:
    if IS_PROD:
        raise RuntimeError("SECRET_KEY environment variable is required in production.")
    _secret = secrets.token_hex(32)
app.config["SECRET_KEY"] = _secret

# Same-origin by default (the server serves the frontend itself). Only set ALLOWED_ORIGINS
# (comma separated) if the frontend is hosted on a different domain.
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if ALLOWED_ORIGINS:
    CORS(app, origins=ALLOWED_ORIGINS)

_db_url = os.environ.get("DATABASE_URL", "sqlite:///app.db?timeout=15")
if _db_url.startswith("postgres://"):  # some hosts still hand out the legacy scheme
    _db_url = "postgresql://" + _db_url[len("postgres://"):]
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # reject request bodies > 12MB
app.config["SESSION_COOKIE_SECURE"] = IS_PROD
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

TOKEN_LIFETIME = timedelta(days=int(os.environ.get("TOKEN_LIFETIME_DAYS", "30")))
MIN_AGE = int(os.environ.get("MIN_AGE", "14"))
# Comma separated emails that automatically become admin when they sign up.
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}

# Google OAuth config (환경변수에서 읽음)
app.config["GOOGLE_CLIENT_ID"] = os.environ.get("GOOGLE_CLIENT_ID", "")
app.config["GOOGLE_CLIENT_SECRET"] = os.environ.get("GOOGLE_CLIENT_SECRET", "")
KST = pytz.timezone('Asia/Seoul')
db = SQLAlchemy(app)
migrate = Migrate(app, db)
# threading mode + simple-websocket: runs under gunicorn's gthread worker (no eventlet needed).
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins=ALLOWED_ORIGINS or None,
                    max_http_buffer_size=8 * 1024 * 1024)
oauth = OAuth(app)

# OpenID Connect (Google)
google = oauth.register(
    name="google",
    client_id=app.config["GOOGLE_CLIENT_ID"],
    client_secret=app.config["GOOGLE_CLIENT_SECRET"],
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

followers = db.Table('followers',
                     db.Column('follower_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
                     db.Column('followed_id', db.Integer, db.ForeignKey('user.id'), primary_key=True)
                     )


# -------------------------------------------------
# Models
# -------------------------------------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=True)  # 소셜 로그인은 비번 없을 수 있음

    # --- New Account Type Field ---
    account_type = db.Column(db.String(50), default="student", nullable=False)  # "student", "teacher", "team"

    full_name = db.Column(db.String(100), nullable=True)
    username = db.Column(db.String(80), unique=True, nullable=True, index=True)
    profile_pic = db.Column(db.Text, nullable=True)
    nationality = db.Column(db.String(100), nullable=True)
    rank = db.Column(db.String(50), default="user", nullable=False)

    # --- Role-Specific Fields ---
    dob = db.Column(db.String(20), nullable=True)  # Nullable for Team accounts
    grade = db.Column(db.String(10), nullable=True)  # For Students only
    subject = db.Column(db.String(100), nullable=True)  # For Teachers only

    # --- System Fields ---
    is_banned = db.Column(db.Boolean, default=False, nullable=False)
    ban_reason = db.Column(db.Text, nullable=True)

    show_birthday = db.Column(db.Boolean, default=True, nullable=False)
    show_social_stats = db.Column(db.Boolean, default=True, nullable=False)

    # token = db.Column(db.String(128), unique=True, index=True)
    bio = db.Column(db.Text, default="")
    provider = db.Column(db.String(50), default="local")  # local or google
    oauth_sub = db.Column(db.String(255), unique=False)  # Google sub (고유 ID)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    following = db.relationship(
        'User', secondary=followers,
        primaryjoin=(followers.c.follower_id == id),
        secondaryjoin=(followers.c.followed_id == id),
        backref=db.backref('followers', lazy='dynamic'),
        lazy='dynamic')
    lounge_memberships = db.relationship('LoungeMember', backref='user', cascade='all, delete-orphan')
    
class LoungeMessageReaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('lounge_message.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    emoji = db.Column(db.String(10), nullable=False)

    __table_args__ = (db.UniqueConstraint('message_id', 'user_id', 'emoji', name='_lounge_message_user_emoji_uc'),)

class DailyView(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)
    count = db.Column(db.Integer, default=1, nullable=False)
    # These two columns link this view count to either an Article or a Circuit
    viewable_id = db.Column(db.Integer, nullable=False)
    viewable_type = db.Column(db.String(50), nullable=False)  # Will be 'article' or 'circuit'
    # This ensures we only have one row per item per day
    __table_args__ = (db.UniqueConstraint('date', 'viewable_id', 'viewable_type', name='_daily_view_uc'),)


class Article(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    author = db.relationship("User", backref="articles")


class MessageReaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('dm.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    emoji = db.Column(db.String(10), nullable=False)

    # --- THIS IS THE CHANGE ---
    # A user can only react with the SAME EMOJI once per message.
    __table_args__ = (db.UniqueConstraint('message_id', 'user_id', 'emoji', name='_message_user_emoji_reaction_uc'),)
    # --- END OF CHANGE ---


class Conversation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # Store user IDs consistently (smaller ID first) to avoid duplicate entries
    user1_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    user2_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    # Store the theme name for this specific chat
    theme = db.Column(db.String(50), default="apex", nullable=False)

    # This ensures that a conversation between two users can only exist once
    __table_args__ = (db.UniqueConstraint('user1_id', 'user2_id', name='_user_conversation_uc'),)

class StoredAsset(db.Model):
    key = db.Column(db.String(50), primary_key=True)
    webp_base64 = db.Column(db.Text, nullable=False)

class LoungeMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    lounge_id = db.Column(db.Integer, db.ForeignKey('lounge.id'), nullable=False)
    role = db.Column(db.String(50), nullable=False, default='member')  # 'member', 'moderator', 'owner'

    # A user can only have one role per lounge
    __table_args__ = (db.UniqueConstraint('user_id', 'lounge_id', name='_user_lounge_uc'),)

class Lounge(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    owner_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    # --- ADD THESE TWO LINES ---
    cover_image = db.Column(db.Text, nullable=True)
    privacy = db.Column(db.String(50), default='public', nullable=False) # 'public', 'private', 'unlisted'

    owner = db.relationship("User", backref="owned_lounges")
    channels = db.relationship("LoungeChannel", backref="lounge", cascade="all, delete-orphan")
    members = db.relationship('LoungeMember', backref='lounge', cascade="all, delete-orphan")


class LoungeChannel(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    lounge_id = db.Column(db.Integer, db.ForeignKey('lounge.id'), nullable=False)
    permission_level = db.Column(db.String(50), default='public', nullable=False)
    
    # ▼▼▼ ADD THIS LINE ▼▼▼
    is_main = db.Column(db.Boolean, default=False, nullable=False, index=True)

    messages = db.relationship("LoungeMessage", backref="channel", cascade="all, delete-orphan")


class LoungeMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=True)
    image = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(pytz.timezone('UTC')))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    channel_id = db.Column(db.Integer, db.ForeignKey('lounge_channel.id'), nullable=False)
    message_type = db.Column(db.String(50), default='user_message', nullable=False)
    
    reactions = db.relationship('LoungeMessageReaction', backref='message', cascade='all, delete-orphan')
    author = db.relationship("User", backref="lounge_messages")


# In server.py

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    # The user who will RECEIVE the notification
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    # A short description of the event, e.g., 'new_follower'
    event_type = db.Column(db.String(50), nullable=False)
    # The user who CAUSED the event (e.g., the person who followed you)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    reference_id = db.Column(db.Integer, nullable=True)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    status = db.Column(db.String(50), default='pending', nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships to easily get user info
    user = db.relationship('User', foreign_keys=[user_id], backref='notifications')
    actor = db.relationship('User', foreign_keys=[actor_id])


class DM(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    # --- MODIFICATIONS ---
    message = db.Column(db.Text, nullable=True)  # Now nullable
    image = db.Column(db.Text, nullable=True)  # New column for image data
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    # --- END MODIFICATIONS ---

    reaction = db.Column(db.String(10), nullable=True)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)

    effect = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    sender = db.relationship("User", foreign_keys=[sender_id], backref="sent_messages")
    receiver = db.relationship("User", foreign_keys=[receiver_id], backref="received_messages")


post_likes = db.Table('post_likes',
                      db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
                      db.Column('post_id', db.Integer, db.ForeignKey('circuit_post.id'), primary_key=True)
                      )


class APIToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # This creates the link back to the User model
    user = db.relationship('User', backref='api_tokens')


class CircuitPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=False)
    image = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Foreign Keys to link posts to users and circuits
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    circuit_id = db.Column(db.Integer, db.ForeignKey("circuit.id"), nullable=False)

    # Relationships (how SQLAlchemy understands the links)
    author = db.relationship("User", backref="circuit_posts")
    circuit = db.relationship("Circuit", backref="posts")
    likes = db.relationship('User', secondary=post_likes, backref='liked_posts')


class Circuit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    host_school = db.Column(db.String(100))
    code = db.Column(db.String(10), unique=True, nullable=False)
    cover_image = db.Column(db.Text, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    owner = db.relationship("User", backref="owned_circuits")


# -------------------------------------------------
# Helpers
# -------------------------------------------------

# ---------------- Security helpers ----------------
import html as _html

# JSON keys that must NOT be HTML-escaped (credentials, emails, dates, image payloads).
#   "content" = article body: only admins/moderators can post it (trusted HTML).
#   "endpoint"/"p256dh"/"auth" = web push subscription (validated separately).
_RAW_JSON_KEYS = {"password", "email", "dob", "image", "coverImage", "profilePic", "token", "content",
                  "endpoint", "p256dh", "auth"}
_IMAGE_DATA_URL_RE = re.compile(r'^data:image/(png|jpeg|jpg|gif|webp);base64,[A-Za-z0-9+/=]+$')
MAX_CHAT_IMAGE_CHARS = 6 * 1024 * 1024  # ~4.5MB of raw image data


def clean_text(value: str) -> str:
    """Neutralise HTML/JS injection in user supplied text.

    The frontend renders most user text through innerHTML and inline onclick handlers,
    so we escape markup characters and remove the characters that could break out of
    JS string literals. The function is idempotent (clean(clean(x)) == clean(x)) so that
    edit round-trips don't double-escape.
    """
    value = _html.unescape(value).replace("\x00", "")
    value = value.replace("\\", "").replace("`", "")
    value = (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "\u2019"))
    return value


def _clean_json(obj, key=None):
    if isinstance(obj, str):
        return obj if key in _RAW_JSON_KEYS else clean_text(obj)
    if isinstance(obj, list):
        return [_clean_json(v, key) for v in obj]
    if isinstance(obj, dict):
        return {k: _clean_json(v, k) for k, v in obj.items()}
    return obj


def valid_image_data_url(value) -> bool:
    """Chat images are stored as-is, so only accept real, bounded base64 image data URLs."""
    return (isinstance(value, str) and len(value) <= MAX_CHAT_IMAGE_CHARS
            and bool(_IMAGE_DATA_URL_RE.match(value)))


@app.before_request
def sanitize_incoming_json():
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.content_length:
        data = request.get_json(force=True, silent=True)
        if data is not None:
            cleaned = _clean_json(data)
            # Flask caches the parsed body here; later request.get_json(...) calls get the cleaned copy.
            request._cached_json = (cleaned, cleaned)


@app.after_request
def add_security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if IS_PROD:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


# Simple in-memory rate limiter (per client IP + endpoint). Run a single worker process
# (required for Socket.IO anyway) so the counters are shared.
_rate_hits = defaultdict(deque)
_rate_lock = threading.Lock()


def rate_limit(max_calls: int, per_seconds: int):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            key = (request.remote_addr or "unknown", fn.__name__)
            now = time.time()
            with _rate_lock:
                hits = _rate_hits[key]
                while hits and now - hits[0] > per_seconds:
                    hits.popleft()
                if len(hits) >= max_calls:
                    return jsonify({"error": "Too many attempts. Please try again later."}), 429
                hits.append(now)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def user_from_token(token_str, allow_incomplete=False):
    """Return the user for a valid, unexpired token (and not banned), else None.

    Google sign-ups start without a username until they finish the onboarding screen.
    Such "incomplete" accounts are rejected everywhere except where allow_incomplete=True
    (/api/me and /api/onboarding).
    """
    if not token_str:
        return None
    token_obj = APIToken.query.filter_by(token=token_str).first()
    if not token_obj:
        return None
    if token_obj.created_at and datetime.utcnow() - token_obj.created_at > TOKEN_LIFETIME:
        db.session.delete(token_obj)
        db.session.commit()
        return None
    user = token_obj.user
    if not user or user.is_banned:
        return None
    if user.username is None and not allow_incomplete:
        return None
    return user


def is_safe_redirect(target) -> bool:
    """Only allow redirects back to this same site (prevents open redirect / token leak)."""
    if not target:
        return False
    parsed = urlparse(target)
    if not parsed.scheme and not parsed.netloc:
        return target.startswith("/") and not target.startswith("//")
    return parsed.netloc == request.host


def is_users_birthday(dob_str: str) -> bool:
    """Checks if the user's birthday is today in KST."""
    if not dob_str:
        return False
    try:
        # Get today's date in Korea Standard Time
        today_kst = datetime.now(KST).date()
        # Parse the user's date of birth string
        user_dob = datetime.strptime(dob_str, '%Y-%m-%d').date()
        # Compare month and day
        return user_dob.month == today_kst.month and user_dob.day == today_kst.day
    except (ValueError, TypeError):
        # Handle cases where the DOB is not a valid date string
        return False

@app.delete("/api/lounge/<int:lounge_id>")
def delete_lounge(lounge_id):
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    lounge = Lounge.query.get(lounge_id)
    if not lounge:
        return jsonify({"error": "Lounge not found"}), 404

    # --- PERMISSION CHECK: Only the owner can delete the lounge ---
    if lounge.owner_id != user.id:
        return jsonify({"error": "Forbidden: You are not the owner of this lounge."}), 403

    # Thanks to the cascade settings, deleting the lounge will also delete
    # its channels, members, messages, and reactions automatically.
    db.session.delete(lounge)
    db.session.commit()

    return jsonify({"ok": True, "message": "Lounge has been permanently deleted."})

def _post_lounge_join_message(user_id, lounge_id):
    """Helper to post a system message when a user joins a lounge."""
    user = User.query.get(user_id)
    if not user: return

    main_channel = LoungeChannel.query.filter_by(lounge_id=lounge_id, is_main=True).first()
    if not main_channel: return

    system_message = LoungeMessage(
        text=f"{user.username} joined the lounge.",
        user_id=user.id,
        channel_id=main_channel.id,
        message_type='system_event'
    )
    db.session.add(system_message)
    db.session.commit()

    message_payload = {
        "id": system_message.id, "text": system_message.text, "image": None,
        "timestamp": system_message.timestamp.isoformat(), "channel_id": main_channel.id,
        "reactions": {}, "message_type": system_message.message_type,
        "author": { "username": user.username, "fullName": user.full_name, "profilePic": user.profile_pic, "rank": user.rank }
    }
    socketio.emit('new_lounge_message', message_payload, room=f"channel_{main_channel.id}")

def get_lounge_role(user_id, lounge_id):
    membership = LoungeMember.query.filter_by(user_id=user_id, lounge_id=lounge_id).first()
    return membership.role if membership else None

def _increment_view(item_id, item_type):
    today_kst = datetime.now(KST).date()

    daily_view = DailyView.query.filter_by(
        date=today_kst,
        viewable_id=item_id,
        viewable_type=item_type
    ).first()

    if daily_view:
        daily_view.count += 1
    else:
        daily_view = DailyView(
            date=today_kst,
            viewable_id=item_id,
            viewable_type=item_type,
            count=1
        )
        db.session.add(daily_view)

    db.session.commit()
    return daily_view.count


from typing import Optional

def infer_grade_from_email(email: str) -> Optional[str]:
    """
    Infers a student's grade level based on the graduation year in their email address.
    e.g., 'student2029@school.com' -> 'G9' (in Fall 2024).
    """
    if not email:
        return None

    # Regex to find a 4-digit year (20xx) or a 2-digit year (xx).
    # We remove the word boundaries (\b) to match numbers anywhere, like '29kim'.

    # --- BEFORE CHANGE ---
    # year_match = re.search(r'\b(20\d{2}|\d{2})\b', email)

    # --- AFTER CHANGE ---
    year_match = re.search(r'(20\d{2}|\d{2})', email)  # 👈 MODIFIED LINE

    if not year_match:
        return None

    grad_year_str = year_match.group(1)
    if len(grad_year_str) == 2:
        grad_year_str = '20' + grad_year_str  # Convert '29' to '2029'

    try:
        grad_year = int(grad_year_str)
    except ValueError:
        return None

    # Calculate current grade based on standard international school year (turnover in August)
    today = datetime.now()
    current_year = today.year
    # If it's August (8) or later, the new school year has started.
    # Seniors graduating next year (current_year + 1) are now in Grade 12.
    senior_graduation_year = current_year + 1 if today.month >= 8 else current_year

    grade = 12 - (grad_year - senior_graduation_year)

    if 0 < grade <= 12:
        return f'G{grade}'
    elif grade > 12:
        return 'University'  # For alumni
    else:
        # If grade calculation results in 0 or negative, they haven't started yet or year is far future.
        return None


def auth_user(allow_incomplete=False):
    token_str = None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token_str = auth.split(" ", 1)[1].strip()
    else:
        token_str = auth.strip()  # Allow token as header value directly

    # Valid, unexpired token belonging to a non-banned user
    return user_from_token(token_str, allow_incomplete=allow_incomplete)


@app.post("/api/articles")
def create_article():
    user = auth_user()
    if not user or user.rank not in ['admin', 'moderator']:
        return jsonify({"error": "Forbidden: You do not have permission to create articles."}), 403

    data = request.get_json(force=True)
    title = data.get("title", "").strip()
    content = data.get("content", "").strip()

    if not title or not content:
        return jsonify({"error": "Title and content are required"}), 400
    if len(title) > 200:
        return jsonify({"error": "Title cannot exceed 200 characters"}), 400

    new_article = Article(
        title=title,
        content=content,
        user_id=user.id
    )
    db.session.add(new_article)
    db.session.commit()

    return jsonify({"ok": True, "message": "Article created successfully!", "article_id": new_article.id}), 201


@app.get("/api/articles")
def get_articles():
    nationality_filter = request.args.get('nationality')
    query = Article.query
    if nationality_filter:
        query = query.join(User).filter(User.nationality == clean_text(nationality_filter))

    articles = query.order_by(Article.created_at.desc()).all()
    today_kst = datetime.now(KST).date()

    # Get all of today's article views in one efficient query
    todays_views = {
        v.viewable_id: v.count
        for v in DailyView.query.filter_by(date=today_kst, viewable_type='article')
    }

    article_list = [
        {
            "id": article.id,
            "title": article.title,
            "excerpt": re.sub('<[^<]+?>', '', article.content)[:150],
            "nationalityTag": article.author.nationality,
            "author": article.author.full_name,
            "daily_views": todays_views.get(article.id, 0)  # Get view from our dictionary
        } for article in articles
    ]
    return jsonify(article_list)


@app.get("/api/article/<int:article_id>")
def get_article(article_id):
    article = Article.query.get(article_id)
    if not article:
        return jsonify({"error": "Article not found"}), 404

    today_kst = datetime.now(KST).date()
    todays_view_obj = DailyView.query.filter_by(date=today_kst, viewable_type='article', viewable_id=article.id).first()
    views_today = todays_view_obj.count if todays_view_obj else 0

    return jsonify({
        "id": article.id,
        "title": article.title,
        "content": article.content,
        "nationalityTag": article.author.nationality,
        "author": article.author.full_name,
        "daily_views": views_today  # <-- ADDED THIS
    })

@app.delete("/api/lounge/<int:lounge_id>/leave")
def leave_lounge(lounge_id):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    membership = LoungeMember.query.filter_by(user_id=user.id, lounge_id=lounge_id).first()
    if not membership:
        return jsonify({"error": "You are not a member of this lounge."}), 404

    if membership.role == 'owner':
        return jsonify({"error": "Owners cannot leave a lounge. You must delete it instead."}), 403

    db.session.delete(membership)

    # --- THIS IS THE FIX ---
    # Find the designated main channel to post the system message.
    main_channel = LoungeChannel.query.filter_by(lounge_id=lounge_id, is_main=True).first()
    
    if main_channel:
        system_message = LoungeMessage(
            text=f"{user.username} left the lounge.",
            user_id=user.id,
            channel_id=main_channel.id, # Use the main channel's ID
            message_type='system_event'
        )
        db.session.add(system_message)
        db.session.commit()

        message_payload = {
            "id": system_message.id, "text": system_message.text, "image": None,
            "timestamp": system_message.timestamp.isoformat(), "channel_id": main_channel.id,
            "reactions": {}, "message_type": system_message.message_type,
            "author": { "username": user.username, "fullName": user.full_name, "profilePic": user.profile_pic, "rank": user.rank }
        }
        socketio.emit('new_lounge_message', message_payload, room=f"channel_{main_channel.id}")
    else:
        # If no main channel, just commit the membership deletion
        db.session.commit()
    # --- END OF FIX ---

    return jsonify({"ok": True, "message": "You have left the lounge."})

@app.get("/api/dm/history/<username>")
def get_dm_history(username):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    other_user = User.query.filter_by(username=username).first()
    if not other_user: return jsonify({"error": "User not found"}), 404

    DM.query.filter_by(sender_id=other_user.id, receiver_id=user.id, is_read=False).update({"is_read": True})
    db.session.commit()

    # Find conversation theme
    user1_id, user2_id = sorted((user.id, other_user.id))
    conversation = Conversation.query.filter_by(user1_id=user1_id, user2_id=user2_id).first()
    current_theme = conversation.theme if conversation else "apex"

    # Get all messages in the conversation
    messages = DM.query.filter(
        or_(
            (DM.sender_id == user.id) & (DM.receiver_id == other_user.id),
            (DM.sender_id == other_user.id) & (DM.receiver_id == user.id)
        )
    ).order_by(DM.created_at.asc()).all()

    conversation_start_info = None
    if messages:
        first_message = messages[0]
        starter_username = first_message.sender.username
        starter_is_me = (starter_username == user.username)
        conversation_start_info = {
            "starter_name": "You" if starter_is_me else first_message.sender.full_name,
            "timestamp": first_message.created_at.isoformat()
        }
    # --- ▲▲▲ END OF NEW LOGIC ▲▲

    # --- NEW: Efficiently fetch all reactions for this conversation ---
    message_ids = [msg.id for msg in messages]
    all_reactions = db.session.query(
        MessageReaction.message_id,
        MessageReaction.emoji,
        func.count(MessageReaction.user_id)
    ).filter(MessageReaction.message_id.in_(message_ids)).group_by(MessageReaction.message_id,
                                                                   MessageReaction.emoji).all()

    reactions_map = {}
    for msg_id, emoji, count in all_reactions:
        if msg_id not in reactions_map:
            reactions_map[msg_id] = {}
        reactions_map[msg_id][emoji] = count
    # --- END NEW LOGIC ---

    message_list = [
        {
            "id": msg.id,
            "sender": "me" if msg.sender_id == user.id else other_user.username,
            "text": msg.message,
            "image": msg.image,
            "is_deleted": msg.is_deleted,
            "time": msg.created_at.isoformat() + "Z",
            "reactions": reactions_map.get(msg.id, {})  # Get reactions from our map
        } for msg in messages
    ]

    return jsonify({"messages": message_list, "theme": current_theme})


@app.put("/api/dm/message/<int:message_id>/react")
def react_to_message(message_id):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    emoji = data.get("emoji")

    dm = DM.query.get(message_id)
    if not dm or (dm.sender_id != user.id and dm.receiver_id != user.id):
        return jsonify({"error": "Message not found"}), 404

    # --- NEW TOGGLE LOGIC ---
    # Check if this specific reaction from this user already exists.
    existing_reaction = MessageReaction.query.filter_by(
        message_id=message_id,
        user_id=user.id,
        emoji=emoji
    ).first()

    if existing_reaction:
        # If it exists, the user is toggling it OFF. So we delete it.
        db.session.delete(existing_reaction)
    else:
        # If it doesn't exist, the user is toggling it ON. So we add it.
        new_reaction = MessageReaction(message_id=message_id, user_id=user.id, emoji=emoji)
        db.session.add(new_reaction)
    # --- END OF NEW LOGIC ---

    db.session.commit()

    # After changes, get the new aggregated reactions for this message
    reactions_agg = db.session.query(
        MessageReaction.emoji,
        func.count(MessageReaction.user_id)
    ).filter_by(message_id=message_id).group_by(MessageReaction.emoji).all()

    reactions_payload = {emoji: count for emoji, count in reactions_agg}

    # Notify both users in the chat with the new, full reaction object
    socketio.emit("message_updated", {"message_id": dm.id, "reactions": reactions_payload}, room=f"user_{dm.sender_id}")
    socketio.emit("message_updated", {"message_id": dm.id, "reactions": reactions_payload},
                  room=f"user_{dm.receiver_id}")

    return jsonify({"ok": True})


@app.get("/api/users/for-chat")
def get_users_for_chat():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    # Define weights for our relevance score
    POST_WEIGHT = 2
    FOLLOWER_WEIGHT = 3

    # This query calculates a "relevance_score" for each user
    # It joins the User table with CircuitPost and followers to get counts.
    users_with_scores = db.session.query(
        User,
        (func.count(func.distinct(CircuitPost.id)) * POST_WEIGHT + \
         func.count(func.distinct(followers.c.follower_id)) * FOLLOWER_WEIGHT).label('relevance_score')
    ).outerjoin(CircuitPost, User.id == CircuitPost.user_id) \
        .outerjoin(followers, User.id == followers.c.followed_id) \
        .filter(
        User.id != user.id,
        User.username != "ANNOUNCEMENTS"
    ).group_by(User.id).order_by(db.desc('relevance_score')).all()

    # Format the data for the frontend
    user_list = [
        {
            "username": u.username,
            "fullName": u.full_name,
            "rank": u.rank,
            "profile_pic": u.profile_pic,
            "account_type": u.account_type
        } for u, score in users_with_scores
    ]

    return jsonify(user_list)


@app.get("/api/dm/message/<int:message_id>/reaction/<emoji>")
def get_reaction_users(message_id, emoji):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    # Find all reactions matching the message and emoji
    reactions = MessageReaction.query.filter_by(message_id=message_id, emoji=emoji).all()

    # Get the usernames of the users who reacted
    user_ids = [r.user_id for r in reactions]
    users = User.query.filter(User.id.in_(user_ids)).all()
    usernames = [u.username for u in users]

    return jsonify(usernames)


@app.put("/api/dm/message/<int:message_id>/edit")
def edit_message(message_id):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    new_text = data.get("new_text", "").strip()

    dm = DM.query.get(message_id)
    # Security: Only the sender can edit their own message
    if not dm or dm.sender_id != user.id:
        return jsonify({"error": "Message not found or you cannot edit it"}), 404
    if not new_text:
        return jsonify({"error": "Message cannot be empty"}), 400

    dm.message = new_text
    db.session.commit()

    socketio.emit("message_updated", {"message_id": dm.id, "text": dm.message}, room=f"user_{dm.sender_id}")
    socketio.emit("message_updated", {"message_id": dm.id, "text": dm.message}, room=f"user_{dm.receiver_id}")

    return jsonify({"ok": True})


@app.delete("/api/dm/message/<int:message_id>")
def delete_message(message_id):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    dm = DM.query.get(message_id)
    # Security: Only the sender can delete their own message
    if not dm or dm.sender_id != user.id:
        return jsonify({"error": "Message not found or you cannot delete it"}), 404

    dm.is_deleted = True
    dm.message = None  # Clear message content
    dm.image = None  # Clear image content
    dm.reaction = None  # Clear reaction
    db.session.commit()

    socketio.emit("message_updated", {"message_id": dm.id, "is_deleted": True}, room=f"user_{dm.sender_id}")
    socketio.emit("message_updated", {"message_id": dm.id, "is_deleted": True}, room=f"user_{dm.receiver_id}")

    return jsonify({"ok": True})


@app.put("/api/dm/conversation/<username>/theme")
def set_conversation_theme(username):
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    other_user = User.query.filter_by(username=username).first()
    if not other_user:
        return jsonify({"error": "User not found"}), 404

    data = request.get_json(force=True)
    theme = data.get("theme")
    if theme not in ["apex", "colorless"]:  # Add more themes here in the future
        return jsonify({"error": "Invalid theme"}), 400

    # Find or create the conversation entry
    user1_id = min(user.id, other_user.id)
    user2_id = max(user.id, other_user.id)
    conversation = Conversation.query.filter_by(user1_id=user1_id, user2_id=user2_id).first()

    if not conversation:
        conversation = Conversation(user1_id=user1_id, user2_id=user2_id)
        db.session.add(conversation)

    conversation.theme = theme
    db.session.commit()

    return jsonify({"ok": True, "message": f"Theme set to {theme}"})



@app.get("/api/notifications")
def get_notifications():
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    notifications = Notification.query.filter_by(user_id=user.id).order_by(Notification.created_at.desc()).limit(20).all()

    notif_list = []
    for notif in notifications:
        message = "A new notification."
        actor_username = notif.actor.username if notif.actor else None
        actionable = notif.status == 'pending' and notif.event_type in ['lounge_invite', 'lounge_access_request']

        notif_dict = {
            "id": notif.id,
            "is_read": notif.is_read,
            "timestamp": notif.created_at.isoformat(),
            "actor_username": actor_username,
            "event_type": notif.event_type,
            "status": notif.status,
            "actionable": actionable
        }

        if notif.event_type == 'new_follower' and notif.actor:
            message = f"**{notif.actor.username}** started following you!"
            notif_dict["reference_type"] = "user"

        elif notif.event_type == 'new_like' and notif.actor:
            post = CircuitPost.query.get(notif.reference_id)
            if post and post.circuit:
                message = f"**{notif.actor.username}** liked your post from **{post.circuit.title}**."
                notif_dict["reference_type"] = "post"
                notif_dict["reference_details"] = {"post_id": post.id, "circuit_id": post.circuit.id, "circuit_title": post.circuit.title, "circuit_host": post.circuit.host_school}
            else:
                message = f"**{notif.actor.username}** liked your post."

        elif notif.event_type == 'lounge_access_accepted' and notif.actor:
            lounge = Lounge.query.get(notif.reference_id)
            if lounge:
                message = f"**{notif.actor.username}** accepted your request to join **{lounge.name}**."
                notif_dict["reference_type"] = "lounge"
                notif_dict["reference_details"] = {"lounge_id": lounge.id, "lounge_name": lounge.name}
            else:
                message = f"Your request to join a lounge was accepted."
        
        notif_dict["message"] = message
        notif_list.append(notif_dict)

    return jsonify(notif_list)


@app.post("/api/posts/<int:post_id>/like")
def like_post(post_id):
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    post = CircuitPost.query.get(post_id)
    if not post:
        return jsonify({"error": "Post not found"}), 404

    if user in post.likes:
        return jsonify({"error": "You already liked this post"}), 409

    post.likes.append(user)

    if post.author.id != user.id:
        notification = Notification(
            user_id=post.author.id,
            event_type='new_like',
            actor_id=user.id,
            # 👇 THIS IS THE FIX 👇
            reference_id=post.id  # Add the ID of the post that was liked
        )
        db.session.add(notification)
        socketio.emit("new_notification", room=f"user_{post.author.id}")

    db.session.commit()
    return jsonify({"ok": True, "likes": len(post.likes)})


@app.post("/api/lounges")
def create_lounge():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()
    privacy = data.get("privacy", "public")
    cover_image_data_url = data.get("coverImage")
    processed_image_data = None

    if not name:
        return jsonify({"error": "Lounge name is required"}), 400
    if len(name) > 20:
        return jsonify({"error": "Lounge name cannot exceed 20 characters"}), 400
    if len(description) > 600:
        return jsonify({"error": "Lounge description cannot exceed 600 characters"}), 400
    if privacy not in ['public', 'private', 'unlisted']:
        return jsonify({"error": "Invalid privacy setting"}), 400

    if cover_image_data_url and cover_image_data_url.startswith('data:image'):
        try:
            header, encoded = cover_image_data_url.split(",", 1)
            image_bytes = base64.b64decode(encoded)
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.thumbnail((512, 512))
                output_buffer = io.BytesIO()
                img.save(output_buffer, format='WEBP', quality=85)
                webp_image_bytes = output_buffer.getvalue()
            base64_webp = base64.b64encode(webp_image_bytes).decode('utf-8')
            processed_image_data = f"data:image/webp;base64,{base64_webp}"
        except Exception as e:
            print(f"Could not process lounge cover image: {e}")
    
    # --- THIS IS THE FIX ---
    # 1. Create the main lounge object
    new_lounge = Lounge(
        name=name,
        description=description,
        owner_id=user.id,
        privacy=privacy,
        cover_image=processed_image_data
    )

    # 2. Create the related objects
    owner_membership = LoungeMember(user=user, lounge=new_lounge, role='owner')
    general_channel = LoungeChannel(name="general", lounge=new_lounge, is_main=True)
    random_channel = LoungeChannel(name="random", lounge=new_lounge)

    # 3. Add ALL objects to the session and commit ONCE.
    # SQLAlchemy will handle the IDs and relationships correctly.
    db.session.add_all([new_lounge, owner_membership, general_channel, random_channel])
    db.session.commit()
    # --- END OF FIX ---

    return jsonify({
        "ok": True,
        "message": "Lounge created successfully!",
        "lounge": {"id": new_lounge.id, "name": new_lounge.name}
    }), 201

# In server.py, add this new endpoint

@app.put("/api/lounge/channel/<int:channel_id>/set-main")
def set_main_channel(channel_id):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    target_channel = LoungeChannel.query.get(channel_id)
    if not target_channel: return jsonify({"error": "Channel not found"}), 404

    # Permission Check
    user_role = get_lounge_role(user.id, target_channel.lounge_id)
    if user_role not in ['owner', 'moderator']:
        return jsonify({"error": "Forbidden: You do not have permission to do this."}), 403

    # Find the current main channel in this lounge
    current_main = LoungeChannel.query.filter_by(lounge_id=target_channel.lounge_id, is_main=True).first()
    
    # This is a transaction: unset the old main and set the new main
    if current_main:
        current_main.is_main = False
    
    target_channel.is_main = True
    db.session.commit()

    return jsonify({"ok": True, "message": f"#{target_channel.name} is now the main channel."})

@app.get("/api/lounge/<int:lounge_id>/members")
def get_lounge_members(lounge_id):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    # This query joins LoungeMember and User tables to get full details
    members = db.session.query(
        User, LoungeMember.role
    ).join(
        LoungeMember, User.id == LoungeMember.user_id
    ).filter(
        LoungeMember.lounge_id == lounge_id
    ).all()

    if not members:
        return jsonify([])

    member_list = [
        {
            "username": member_user.username,
            "fullName": member_user.full_name,
            "profile_pic": member_user.profile_pic,
            "rank": member_user.rank,
            "role": role
        } for member_user, role in members
    ]
    return jsonify(member_list)

# In server.py, add this new function

@app.post("/api/lounge/<int:lounge_id>/request-access")
def request_lounge_access(lounge_id):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    lounge = Lounge.query.get(lounge_id)
    if not lounge: return jsonify({"error": "Lounge not found"}), 404
    if lounge.privacy != 'private': return jsonify({"error": "This lounge is not private."}), 400
    if get_lounge_role(user.id, lounge_id): return jsonify({"error": "You are already a member."}), 409

    # Prevent spamming requests by checking for an existing pending one
    existing_request = Notification.query.filter_by(
        actor_id=user.id,
        event_type='lounge_access_request',
        reference_id=lounge_id,
        status='pending'
    ).first()
    if existing_request:
        return jsonify({"error": "You have already requested to join this lounge."}), 409

    # Create the notification for the lounge owner
    notification = Notification(
        user_id=lounge.owner_id,
        event_type='lounge_access_request',
        actor_id=user.id,
        reference_id=lounge_id,
        status='pending'
    )
    db.session.add(notification)
    db.session.commit()
    socketio.emit("new_notification", room=f"user_{lounge.owner_id}")

    return jsonify({"ok": True, "message": "Your request has been sent to the lounge owner."})

@app.put("/api/lounge/<int:lounge_id>")
def edit_lounge(lounge_id):
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    lounge = Lounge.query.get(lounge_id)
    if not lounge:
        return jsonify({"error": "Lounge not found"}), 404

    # --- PERMISSION CHECK: Only the owner can edit ---
    if lounge.owner_id != user.id:
        return jsonify({"error": "Forbidden: You are not the owner of this lounge."}), 403

    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()
    cover_image_data_url = data.get("coverImage") # Can be null or Base64
    processed_image_data = lounge.cover_image # Keep existing if not changed

    # Validate inputs
    if not name:
        return jsonify({"error": "Lounge name cannot be empty"}), 400
    if len(name) > 20:
        return jsonify({"error": "Lounge name cannot exceed 20 characters"}), 400
    if len(description) > 600:
        return jsonify({"error": "Lounge description cannot exceed 600 characters"}), 400

    # Process new cover image if provided
    if cover_image_data_url and cover_image_data_url.startswith('data:image'):
        try:
            # (Re-use the same image processing logic from create_lounge)
            header, encoded = cover_image_data_url.split(",", 1)
            image_bytes = base64.b64decode(encoded)
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.thumbnail((512, 512))
                output_buffer = io.BytesIO()
                img.save(output_buffer, format='WEBP', quality=85)
                webp_image_bytes = output_buffer.getvalue()
            base64_webp = base64.b64encode(webp_image_bytes).decode('utf-8')
            processed_image_data = f"data:image/webp;base64,{base64_webp}"
        except Exception as e:
            print(f"Could not process edited lounge cover image: {e}")
            # Optionally return an error or just skip updating the image
    elif cover_image_data_url is None: # Allow explicitly removing the image
        processed_image_data = None


    # Update the lounge object
    lounge.name = name
    lounge.description = description
    lounge.cover_image = processed_image_data

    db.session.commit()

    return jsonify({
        "ok": True,
        "message": "Lounge updated successfully!",
        "lounge": { # Return updated details
            "id": lounge.id,
            "name": lounge.name,
            "description": lounge.description,
            "cover_image": lounge.cover_image,
            "privacy": lounge.privacy # Privacy isn't editable here
        }
    })

# Add a simple endpoint to get details needed for editing
@app.get("/api/lounge/<int:lounge_id>/details")
def get_lounge_details_for_edit(lounge_id):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    lounge = Lounge.query.get(lounge_id)
    if not lounge: return jsonify({"error": "Lounge not found"}), 404

    # Only owner needs full details for editing
    if lounge.owner_id != user.id:
         return jsonify({"error": "Forbidden"}), 403 # Or return limited info if needed elsewhere

    return jsonify({
        "id": lounge.id,
        "name": lounge.name,
        "description": lounge.description,
        "cover_image": lounge.cover_image,
        "privacy": lounge.privacy
    })


@app.get("/api/lounges")
def get_lounges():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    # Get IDs of lounges the user is a member of
    user_lounge_ids = {m.lounge_id for m in LoungeMember.query.filter_by(user_id=user.id).all()}

    # Fetch all lounges initially
    all_lounges = Lounge.query.order_by(Lounge.created_at.desc()).all()

    # --- THIS IS THE FIX ---
    # Filter the list: Keep public lounges OR lounges the user is a member of
    visible_lounges = [
        lounge for lounge in all_lounges
        if lounge.privacy in ['public', 'private'] or lounge.id in user_lounge_ids
    ]
    # --- END OF FIX ---

    # Now, build the list using only the visible lounges
    lounge_list = [
        {
            "id": lounge.id,
            "name": lounge.name,
            "description": lounge.description,
            "cover_image": lounge.cover_image,
            "privacy": lounge.privacy
        } for lounge in visible_lounges # Use the filtered list here
    ]
    return jsonify(lounge_list)
# In server.py, find and REPLACE your join_lounge function

@app.get("/api/lounge/<int:lounge_id>/potential-invitees")
def get_potential_invitees(lounge_id):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401
    
    # Get the search term from the request, if any
    search_term = request.args.get('q', '').strip()

    # Step 1: Find all user IDs that are ALREADY in the lounge.
    subquery = db.session.query(LoungeMember.user_id).filter(LoungeMember.lounge_id == lounge_id)

    # Step 2: Query for users who are NOT in that list of IDs.
    # Also exclude the current user and system accounts.
    query = User.query.filter(
        User.id.notin_(subquery),
        User.id != user.id,
        User.username != "ANNOUNCEMENTS"
    )

    # Step 3: If there's a search term, filter by username or full name.
    if search_term:
        search_filter = or_(
            User.username.ilike(f'%{search_term}%'),
            User.full_name.ilike(f'%{search_term}%')
        )
        query = query.filter(search_filter)

    # Step 4: Limit the results to avoid sending too much data.
    potential_invitees = query.limit(20).all()

    # Step 5: Format and return the results.
    user_list = [
        {
            "username": u.username,
            "fullName": u.full_name,
            "profile_pic": u.profile_pic,
            "rank": u.rank
        } for u in potential_invitees
    ]
    return jsonify(user_list)
# In server.py, REPLACE your existing join_lounge function

@app.get("/api/me/lounges")
def get_my_lounges():
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    # Find all lounges where the current user is a member
    my_lounges = Lounge.query.join(LoungeMember).filter(
        LoungeMember.user_id == user.id
    ).order_by(Lounge.name.asc()).all()

    lounge_list = [{
        "id": lounge.id,
        "name": lounge.name,
        "description": lounge.description,
        "cover_image": lounge.cover_image,
        "privacy": lounge.privacy
    } for lounge in my_lounges]

    return jsonify(lounge_list)


@app.post("/api/lounge/<int:lounge_id>/join")
def join_lounge(lounge_id):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    lounge = Lounge.query.get(lounge_id)
    if not lounge: return jsonify({"error": "Lounge not found"}), 404
    
    if lounge.privacy == 'private':
        return jsonify({"error": "This lounge is private and requires an invitation to join."}), 403

    if LoungeMember.query.filter_by(user_id=user.id, lounge_id=lounge.id).first():
        return jsonify({"error": "You are already a member of this lounge."}), 409

    db.session.add(LoungeMember(user_id=user.id, lounge_id=lounge.id, role='member'))
    
    # --- THIS IS THE FIX ---
    # Instead of looking for '#general', find the channel marked as main.
    main_channel = LoungeChannel.query.filter_by(lounge_id=lounge.id, is_main=True).first()
    
    if main_channel:
        system_message = LoungeMessage(
            text=f"{user.username} joined the lounge.",
            user_id=user.id,
            channel_id=main_channel.id, # Use the main channel's ID
            message_type='system_event'
        )
        db.session.add(system_message)
        db.session.commit()

        message_payload = {
            "id": system_message.id,
            "text": system_message.text,
            "image": None,
            "timestamp": system_message.timestamp.isoformat(),
            "channel_id": main_channel.id, # Use the main channel's ID
            "reactions": {},
            "message_type": system_message.message_type,
            "author": {
                "username": user.username, "fullName": user.full_name,
                "profilePic": user.profile_pic, "rank": user.rank
            }
        }
        socketio.emit('new_lounge_message', message_payload, room=f"channel_{main_channel.id}")
    else:
        # If no main channel is found for some reason, just commit the membership change.
        db.session.commit()
    # --- END OF FIX ---
    
    return jsonify({"ok": True, "message": f"Welcome to {lounge.name}!"})

@app.get("/api/lounge/<int:lounge_id>/channels")
def get_lounge_channels(lounge_id):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    lounge = Lounge.query.get(lounge_id)
    if not lounge: return jsonify({"error": "Lounge not found"}), 404
    
    user_role = get_lounge_role(user.id, lounge_id)
    is_member = user_role is not None
    
    if lounge.privacy in ['private', 'unlisted'] and not is_member:
        # For BOTH private and unlisted, if not a member, return empty channels.
        # Include the privacy status so the frontend knows how to render the overlay.
        return jsonify({
            "channels": [],
            "user_role": None,
            "is_member": False,
            "privacy": lounge.privacy # Crucial for frontend logic
        })

    query = LoungeChannel.query.filter_by(lounge_id=lounge.id)
    if not is_member or user_role not in ['owner', 'moderator']:
        query = query.filter(LoungeChannel.permission_level != 'mods_only_view')
    
    channels = query.order_by(LoungeChannel.is_main.desc(), LoungeChannel.name.asc()).all()
    
    return jsonify({
        # --- ▼▼▼ THIS IS THE FIX ▼▼▼ ---
        # We now include the "is_main" flag for each channel.
        "channels": [{"id": c.id, "name": c.name, "permission_level": c.permission_level, "is_main": c.is_main} for c in channels],
        # --- ▲▲▲ END OF FIX ▲▲▲ ---
        "user_role": user_role,
        "is_member": is_member
    })




# Modify the message fetching endpoint to get messages from a CHANNEL
@app.get("/api/lounge/channel/<int:channel_id>/messages")
def get_lounge_channel_messages(channel_id):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    channel = LoungeChannel.query.get(channel_id)
    if not channel: return jsonify({"error": "Channel not found"}), 404
    if channel.lounge.privacy == 'private' and not get_lounge_role(user.id, channel.lounge_id):
        return jsonify({"error": "Forbidden: this lounge is private."}), 403

    messages = LoungeMessage.query.filter_by(channel_id=channel_id).order_by(LoungeMessage.timestamp.asc()).limit(100).all()

    # --- NEW: Efficiently fetch all reactions for these messages ---
    message_ids = [msg.id for msg in messages]
    all_reactions = db.session.query(
        LoungeMessageReaction.message_id,
        LoungeMessageReaction.emoji,
        func.count(LoungeMessageReaction.user_id)
    ).filter(LoungeMessageReaction.message_id.in_(message_ids)).group_by(LoungeMessageReaction.message_id, LoungeMessageReaction.emoji).all()
    
    reactions_map = {}
    for msg_id, emoji, count in all_reactions:
        if msg_id not in reactions_map: reactions_map[msg_id] = {}
        reactions_map[msg_id][emoji] = count
    # --- END NEW ---

    message_list = []
    for msg in messages:
        if not msg.author: continue
        message_list.append({
            "id": msg.id,
            "text": msg.text,
            "image": msg.image,
            "timestamp": msg.timestamp.isoformat(),
            "reactions": reactions_map.get(msg.id, {}), # <-- ADD THIS
            "message_type": msg.message_type,
            "author": {
                "username": msg.author.username,
                "fullName": msg.author.full_name,
                "profilePic": msg.author.profile_pic,
                "rank": msg.author.rank
            }
        })
    return jsonify(message_list)




# In server.py, find and MODIFY the socketio event handlers

@socketio.on('send_lounge_message')
def handle_send_lounge_message(data):
    user = user_from_token(socket_connections.get(request.sid))
    if not user or not isinstance(data, dict): return

    channel_id = data.get('channel_id')
    channel = LoungeChannel.query.get(channel_id)
    if not channel: return

    membership = LoungeMember.query.filter_by(user_id=user.id, lounge_id=channel.lounge_id).first()
    
    # --- MODIFIED MEMBERSHIP/PRIVACY LOGIC ---
    if not membership:
        # If user is not a member, check if the lounge is private.
        # If it's private, they cannot join automatically.
        if channel.lounge.privacy == 'private':
            return # Silently stop them from joining/sending a message
        
        # Otherwise (public or unlisted), add them as a new member.
        new_membership = LoungeMember(user_id=user.id, lounge_id=channel.lounge_id, role='member')
        db.session.add(new_membership)
    # --- END OF MODIFIED LOGIC ---

    user_role = get_lounge_role(user.id, channel.lounge_id)
    if channel.permission_level == 'mods_only_chat' and user_role not in ['owner', 'moderator']:
        return 
    
    text = clean_text(str(data.get('text') or '')).strip()[:4000]
    image_data = data.get('image')
    if image_data and not valid_image_data_url(image_data): return
    if not (text or image_data): return

    new_message = LoungeMessage(text=text, image=image_data, user_id=user.id, channel_id=channel_id)
    db.session.add(new_message)
    db.session.commit()

    # --- THIS IS THE FIX: Use .strftime() here as well ---
    message_payload = {
        "id": new_message.id,
        "text": new_message.text,
        "image": new_message.image,
        "timestamp": new_message.timestamp.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        "channel_id": channel_id,
        "reactions": {},
        "message_type": new_message.message_type, # Added this line
        "author": {
            "username": user.username,
            "fullName": user.full_name,
            "profilePic": user.profile_pic,
            "rank": user.rank
        }
    }
    emit('new_lounge_message', message_payload, room=f"channel_{channel_id}")


@app.get("/api/me/activity")
def get_my_activity():
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    my_posts = CircuitPost.query.filter_by(user_id=user.id).order_by(CircuitPost.created_at.desc()).limit(10).all()
    my_liked_posts = CircuitPost.query.join(post_likes).filter(post_likes.c.user_id == user.id).order_by(CircuitPost.created_at.desc()).limit(10).all()
    my_circuits = Circuit.query.filter_by(user_id=user.id).order_by(Circuit.id.desc()).limit(10).all()
    my_owned_lounges = Lounge.query.filter_by(owner_id=user.id).order_by(Lounge.created_at.desc()).limit(10).all()

    # --- THIS IS THE FIX: Add checks to ensure timestamps exist before formatting ---
    results = {
        "posts": [{"text": p.text, "circuit_title": p.circuit.title, "circuit_id": p.circuit.id, "host_school": p.circuit.host_school, "timestamp": p.created_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ') if p.created_at else None} for p in my_posts],
        "likes": [{"text": l.text, "original_author": l.author.full_name, "circuit_title": l.circuit.title, "circuit_id": l.circuit.id, "host_school": l.circuit.host_school, "timestamp": l.created_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ') if l.created_at else None} for l in my_liked_posts],
        "circuits": [{"title": c.title, "host_school": c.host_school, "id": c.id} for c in my_circuits],
        "lounges": [{"name": l.name, "id": l.id, "timestamp": l.created_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ') if l.created_at else None} for l in my_owned_lounges]
    }
    return jsonify(results)

# ADD these new handlers for joining/leaving lounge rooms
@socketio.on('join_lounge_channel')
def handle_join_lounge_channel(data):
    user = user_from_token(socket_connections.get(request.sid))
    channel_id = data.get('channel_id') if isinstance(data, dict) else None
    if not user or not channel_id:
        return
    channel = LoungeChannel.query.get(channel_id)
    if not channel:
        return
    # Private lounges: only members may listen in.
    if channel.lounge.privacy == 'private' and not get_lounge_role(user.id, channel.lounge_id):
        return
    join_room(f"channel_{channel_id}")


@socketio.on('leave_lounge_channel')
def handle_leave_lounge_channel(data):
    channel_id = data.get('channel_id')
    if channel_id:
        leave_room(f"channel_{channel_id}")


@app.post("/api/posts/<int:post_id>/unlike")
def unlike_post(post_id):
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    post = CircuitPost.query.get(post_id)
    if not post:
        return jsonify({"error": "Post not found"}), 404

    if user not in post.likes:
        return jsonify({"error": "You have not liked this post"}), 409

    post.likes.remove(user)
    db.session.commit()
    return jsonify({"ok": True, "likes": len(post.likes)})


@app.post("/api/user/<username>/follow")
def follow_user(username):
    current_user = auth_user()
    if not current_user:
        return jsonify({"error": "unauthorized"}), 401

    user_to_follow = User.query.filter_by(username=username).first()
    if not user_to_follow:
        return jsonify({"error": "User not found"}), 404

    if current_user.id == user_to_follow.id:
        return jsonify({"error": "You cannot follow yourself"}), 400

    # Check if already following
    if current_user.following.filter(followers.c.followed_id == user_to_follow.id).count() > 0:
        return jsonify({"error": "You are already following this user"}), 409

    current_user.following.append(user_to_follow)

    notification = Notification(
        user_id=user_to_follow.id,
        event_type='new_follower',
        actor_id=current_user.id
    )
    db.session.add(notification)
    socketio.emit("new_notification", room=f"user_{user_to_follow.id}")
    db.session.commit()
    return jsonify({"ok": True, "message": f"You are now following {username}"})


@app.post("/api/notifications/mark-all-read")
def mark_notifications_read():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    # Find all unread notifications for the user and update them
    Notification.query.filter_by(user_id=user.id, is_read=False).update({"is_read": True})
    db.session.commit()

    return jsonify({"ok": True})


@app.post("/api/user/<username>/unfollow")
def unfollow_user(username):
    current_user = auth_user()
    if not current_user:
        return jsonify({"error": "unauthorized"}), 401

    user_to_unfollow = User.query.filter_by(username=username).first()
    if not user_to_unfollow:
        return jsonify({"error": "User not found"}), 404

    # Check if the user is being followed
    if current_user.following.filter(followers.c.followed_id == user_to_unfollow.id).count() == 0:
        return jsonify({"error": "You are not following this user"}), 409

    current_user.following.remove(user_to_unfollow)
    db.session.commit()
    return jsonify({"ok": True, "message": f"You have unfollowed {username}"})


def issue_api_token(user: User) -> str:
    # Create a new token object linked to the user
    new_token = APIToken(
        user_id=user.id,
        token=secrets.token_hex(32)
    )
    db.session.add(new_token)
    db.session.commit()
    return new_token.token


# -------------------------------------------------
# Health
# -------------------------------------------------

@app.get("/api/circuits")
def get_circuits():
    if not auth_user():
        return jsonify({"error": "unauthorized"}), 401

    circuits = Circuit.query.all()
    today_kst = datetime.now(KST).date()

    # Get all of today's circuit views in one efficient query
    todays_views = {
        v.viewable_id: v.count
        for v in DailyView.query.filter_by(date=today_kst, viewable_type='circuit')
    }

    circuit_list = [
        {
            "id": circuit.id,
            "title": circuit.title,
            "hostSchool": circuit.host_school,
            "coverImage": circuit.cover_image,
            "code": circuit.code,
            "daily_views": todays_views.get(circuit.id, 0)
        } for circuit in circuits
    ]
    return jsonify(circuit_list)


@app.get("/health")
def health():
    return jsonify({"ok": True, "time": datetime.utcnow().isoformat()})


# -------------------------------------------------
# Auth (Local)
# -------------------------------------------------


# -------------------------------------------------
# Signup with server-side email verification
#   1) POST /api/signup         -> validates the form, emails a 6-digit code (or creates the account
#                                  right away when REQUIRE_EMAIL_VERIFICATION=false)
#   2) POST /api/signup/verify  -> checks the code and creates the account
#   3) POST /api/signup/resend  -> sends a fresh code
# -------------------------------------------------
import hmac, hashlib, smtplib, ssl
from email.message import EmailMessage

REQUIRE_EMAIL_VERIFICATION = os.environ.get("REQUIRE_EMAIL_VERIFICATION", "true").lower() != "false"
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
APP_NAME = os.environ.get("APP_NAME", "APEX")

CODE_TTL_SECONDS = 10 * 60
MAX_CODE_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60
_pending_signups = {}  # email -> {"code_hash", "fields", "expires", "attempts", "last_sent"}
_pending_lock = threading.Lock()


def _hash_code(email: str, code: str) -> str:
    return hashlib.sha256(f"{app.config['SECRET_KEY']}:{email}:{code}".encode()).hexdigest()


def send_verification_email(to_email: str, name: str, code: str) -> bool:
    if not (SMTP_HOST and SMTP_FROM):
        print("[ERROR] SMTP is not configured (SMTP_HOST / SMTP_FROM / SMTP_USER / SMTP_PASSWORD).")
        return False
    msg = EmailMessage()
    msg["Subject"] = f"{APP_NAME} verification code: {code}"
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg.set_content(
        f"Hi {name},\n\nYour {APP_NAME} verification code is: {code}\n"
        f"It expires in {CODE_TTL_SECONDS // 60} minutes. If you did not sign up, you can ignore this email.\n"
    )
    try:
        context = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=15) as server:
                if SMTP_USER:
                    server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                server.starttls(context=context)
                if SMTP_USER:
                    server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)
        return True
    except Exception as e:
        print(f"[ERROR] Could not send verification email: {type(e).__name__}")
        return False


def _create_user_from_fields(fields: dict):
    """Create the account. Returns (response, status)."""
    # Admins are only granted to emails listed in the ADMIN_EMAILS environment variable.
    new_rank = 'admin' if fields["email"] in ADMIN_EMAILS else 'user'
    if User.query.filter_by(email=fields["email"]).first():
        return jsonify({"error": "Email already registered"}), 409
    if User.query.filter_by(username=fields["username"]).first():
        return jsonify({"error": "Username is already taken"}), 409

    user = User(
        email=fields["email"],
        password_hash=generate_password_hash(fields["password"]),
        full_name=fields["full_name"],
        username=fields["username"],
        nationality=fields["nationality"],
        account_type="personal",
        rank=new_rank,
        provider="local",
        dob=fields["dob"],
    )
    try:
        db.session.add(user)
        db.session.commit()
        return jsonify({"ok": True, "message": "Account created successfully. Please log in."}), 200
    except Exception as e:
        db.session.rollback()
        print(f"[ERROR] signup commit failed: {type(e).__name__}")
        return jsonify({"error": "Could not create account. Username or email may already be in use."}), 500


def _validate_signup(data):
    """Validate the signup form. Returns (fields, None) or (None, (response, status))."""
    if not isinstance(data, dict):
        return None, (jsonify({"error": "Invalid request format"}), 400)

    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "").strip()
    full_name = str(data.get("full_name") or "").strip()
    username = str(data.get("username") or "").strip()
    nationality = str(data.get("nationality") or "").strip()
    dob_str = data.get("dob")

    if not re.match(r'^[a-z0-9!._*-]{3,20}$', username):
        return None, (jsonify({
            "error": "Username must be 3-20 characters, lowercase, and can only contain letters, numbers, and !._-*"
        }), 400)
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email) or len(email) > 255:
        return None, (jsonify({"error": "Please enter a valid email address."}), 400)
    if len(password) < 8 or len(password) > 128:
        return None, (jsonify({"error": "Password must be 8-128 characters."}), 400)
    if not full_name or len(full_name) > 100:
        return None, (jsonify({"error": "Name is required (max 100 characters)."}), 400)
    if not nationality or len(nationality) > 60:
        return None, (jsonify({"error": "Nationality is required."}), 400)

    if not dob_str:
        return None, (jsonify({"error": "Date of Birth is required."}), 400)
    try:
        dob_date = datetime.strptime(str(dob_str), '%Y-%m-%d').date()
    except ValueError:
        return None, (jsonify({"error": "Invalid Date of Birth format. Please use the date picker."}), 400)
    today = date.today()
    if dob_date > today:
        return None, (jsonify({"error": "Date of Birth cannot be in the future."}), 400)
    age = today.year - dob_date.year - ((today.month, today.day) < (dob_date.month, dob_date.day))
    if age < MIN_AGE:
        return None, (jsonify({"error": f"You must be at least {MIN_AGE} years old to join."}), 400)
    if age > 120:
        return None, (jsonify({"error": "Invalid Date of Birth."}), 400)

    if User.query.filter_by(email=email).first():
        return None, (jsonify({"error": "Email already registered"}), 409)
    if User.query.filter_by(username=username).first():
        return None, (jsonify({"error": "Username is already taken"}), 409)

    return {
        "email": email, "password": password, "full_name": full_name,
        "username": username, "nationality": nationality, "dob": str(dob_date),
    }, None


@app.post("/api/onboarding")
@rate_limit(30, 3600)
def complete_onboarding():
    """Second step of Google sign-up: choose username, password, nationality and birth date."""
    user = auth_user(allow_incomplete=True)
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    if user.username is not None:
        return jsonify({"error": "Your account is already set up."}), 400

    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid request format"}), 400
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "").strip()
    nationality = str(data.get("nationality") or "").strip()
    dob_str = data.get("dob")

    if not re.match(r'^[a-z0-9!._*-]{3,20}$', username):
        return jsonify({"error": "Username must be 3-20 characters, lowercase, and can only contain letters, numbers, and !._-*", "field": "username"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"error": "Username is already taken", "field": "username"}), 409
    if len(password) < 8 or len(password) > 128:
        return jsonify({"error": "Password must be 8-128 characters.", "field": "password"}), 400
    if not nationality or len(nationality) > 60:
        return jsonify({"error": "Nationality is required.", "field": "nationality"}), 400
    if not dob_str:
        return jsonify({"error": "Date of Birth is required.", "field": "dob"}), 400
    try:
        dob_date = datetime.strptime(str(dob_str), '%Y-%m-%d').date()
    except ValueError:
        return jsonify({"error": "Invalid Date of Birth format.", "field": "dob"}), 400
    today = date.today()
    age = today.year - dob_date.year - ((today.month, today.day) < (dob_date.month, dob_date.day))
    if dob_date > today or age > 120:
        return jsonify({"error": "Invalid Date of Birth.", "field": "dob"}), 400
    if age < MIN_AGE:
        return jsonify({"error": f"You must be at least {MIN_AGE} years old to join.", "field": "dob"}), 400

    user.username = username
    user.password_hash = generate_password_hash(password)
    user.nationality = nationality
    user.dob = str(dob_date)
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"[ERROR] onboarding commit failed: {type(e).__name__}")
        return jsonify({"error": "Username is already taken", "field": "username"}), 409
    return jsonify({"ok": True, "username": user.username})


@app.post("/api/signup")
@rate_limit(10, 3600)
def signup():
    fields, error = _validate_signup(request.get_json(force=True, silent=True))
    if error:
        return error

    if not REQUIRE_EMAIL_VERIFICATION:
        return _create_user_from_fields(fields)

    code = f"{secrets.randbelow(1000000):06d}"
    with _pending_lock:
        now = time.time()
        for k in [k for k, v in _pending_signups.items() if v["expires"] < now]:
            del _pending_signups[k]  # drop expired entries
        _pending_signups[fields["email"]] = {
            "code_hash": _hash_code(fields["email"], code),
            "fields": fields,
            "expires": now + CODE_TTL_SECONDS,
            "attempts": 0,
            "last_sent": now,
        }

    if not send_verification_email(fields["email"], fields["full_name"], code):
        with _pending_lock:
            _pending_signups.pop(fields["email"], None)
        return jsonify({"error": "Could not send the verification email. Please try again later."}), 503
    return jsonify({"ok": True, "verification_required": True, "message": "Verification code sent."})


@app.post("/api/signup/verify")
@rate_limit(30, 3600)
def signup_verify():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid request format"}), 400
    email = str(data.get("email") or "").strip().lower()
    code = str(data.get("code") or "").strip()

    with _pending_lock:
        entry = _pending_signups.get(email)
        if not entry or entry["expires"] < time.time():
            _pending_signups.pop(email, None)
            return jsonify({"error": "Verification code expired. Please sign up again."}), 400
        entry["attempts"] += 1
        if entry["attempts"] > MAX_CODE_ATTEMPTS:
            del _pending_signups[email]
            return jsonify({"error": "Too many wrong codes. Please sign up again."}), 429
        if not hmac.compare_digest(entry["code_hash"], _hash_code(email, code)):
            return jsonify({"error": "Incorrect verification code."}), 400
        fields = entry["fields"]
        del _pending_signups[email]

    return _create_user_from_fields(fields)


@app.post("/api/signup/resend")
@rate_limit(10, 3600)
def signup_resend():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid request format"}), 400
    email = str(data.get("email") or "").strip().lower()

    with _pending_lock:
        entry = _pending_signups.get(email)
        if not entry or entry["expires"] < time.time():
            _pending_signups.pop(email, None)
            return jsonify({"error": "Signup session expired. Please sign up again."}), 400
        if time.time() - entry["last_sent"] < RESEND_COOLDOWN_SECONDS:
            return jsonify({"error": "Please wait a minute before requesting another code."}), 429
        code = f"{secrets.randbelow(1000000):06d}"
        entry["code_hash"] = _hash_code(email, code)
        entry["expires"] = time.time() + CODE_TTL_SECONDS
        entry["attempts"] = 0
        entry["last_sent"] = time.time()
        name = entry["fields"]["full_name"]

    if not send_verification_email(email, name, code):
        return jsonify({"error": "Could not send the verification email. Please try again later."}), 503
    return jsonify({"ok": True, "message": "Verification code sent."})


@app.post("/api/login")
@rate_limit(15, 300)
def login():
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid request format"}), 400
    identifier = str(data.get("email") or "").strip()
    password = str(data.get("password") or "").strip()

    if not identifier or not password:
        return jsonify({"error": "username/email and password required"}), 400

    user = User.query.filter(
        or_(
            User.email == identifier.lower(),
            User.username == identifier
        )
    ).first()

    if not user or not user.password_hash or not check_password_hash(user.password_hash, password):
        return jsonify({"error": "invalid credentials"}), 401

    if user.is_banned:
        reason = f" Reason: {user.ban_reason}" if user.ban_reason else ""
        return jsonify({"error": f"This account has been banned.{reason}"}), 403

    token = issue_api_token(user)

    # --- NEW: Gather user data to return directly ---
    following_list = [u.username for u in user.following]
    user_data = {
        "id": user.id,
        "email": user.email,
        "fullName": user.full_name,
        "username": user.username,
        "nationality": user.nationality,
        "dob": user.dob,
        "bio": user.bio,
        "rank": user.rank,
        "provider": user.provider,
        "created_at": user.created_at.isoformat(),
        "has_bio": bool(user.bio),
        "following": following_list,
        "profile_pic": user.profile_pic,
        "account_type": user.account_type,
        "grade": user.grade,
        "subject": user.subject
    }
    # Return both the token and the user data object
    return jsonify({"token": token, "user": user_data})


@app.post("/api/logout")
def logout():
    # We need to get the raw token string to delete the correct entry
    token_str = None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token_str = auth.split(" ", 1)[1].strip()

    if not token_str:
        return jsonify({"error": "unauthorized"}), 401

    # Find the token in the database and delete it
    token_to_delete = APIToken.query.filter_by(token=token_str).first()
    if token_to_delete:
        db.session.delete(token_to_delete)
        db.session.commit()

    return jsonify({"ok": True})

@app.route('/')
def serve_index():
    # This tells Flask to send the index.html file from the current directory
    return send_from_directory(BASE_DIR, 'index.html')

@app.route('/terms.txt')
def serve_terms():
    # 1. Try fetching from the database first
    asset = StoredAsset.query.get('terms_html')
    if asset and asset.webp_base64: # Assuming webp_base64 holds the HTML content
        return asset.webp_base64 # Return the stored HTML directly

    # 2. Fallback: Read from file if not in DB (or if you want to keep this option)
    try:
        with open('terms.txt', 'r', encoding='utf-8') as f:
            content = f.read()
        html_content = markdown.markdown(content)
        return html_content
    except FileNotFoundError:
        return "Terms of Service not found.", 404

@app.get("/api/asset/<key>")
def get_asset(key):
    asset = StoredAsset.query.get(key)
    if not asset:
        return jsonify({"error": "Asset not found"}), 404

    try:
        header, encoded = asset.webp_base64.split(",", 1)
        mime_type = header.split(';')[0].split(':')[1]
        image_data = base64.b64decode(encoded)
        
        response = make_response(image_data)
        response.headers['Content-Type'] = mime_type
        return response
    except Exception as e:
        print(f"Error serving asset '{key}': {e}")
        return jsonify({"error": "Could not serve asset"}), 500

def seed_assets():
    assets_to_seed = {
        'logo_icon': 'apex.png',
        'logo_text': 'apex2.png',
        'default_avatar': 'default-avatar.png'
    }

    for key, file_path in assets_to_seed.items():
        if StoredAsset.query.get(key) or not os.path.exists(file_path):
            continue

        print(f"Storing asset '{key}' from '{file_path}' into the database...")
        try:
            with Image.open(file_path) as img:
                output_buffer = io.BytesIO()
                img.save(output_buffer, format='WEBP', quality=64)
                webp_image_bytes = output_buffer.getvalue()

            base64_webp = base64.b64encode(webp_image_bytes).decode('utf-8')
            data_url = f"data:image/webp;base64,{base64_webp}"
            new_asset = StoredAsset(key=key, webp_base64=data_url)
            db.session.add(new_asset)
        except Exception as e:
            print(f"Error processing asset '{key}': {e}")
    
    db.session.commit()

@app.post("/api/user/<username>/ban")
def ban_user(username):
    admin_user = auth_user()
    if not admin_user or admin_user.rank != 'admin':
        return jsonify({"error": "Forbidden: Admin access required"}), 403

    target_user = User.query.filter_by(username=username).first()
    if not target_user:
        return jsonify({"error": "Target user not found"}), 404

    reason = request.get_json(force=True).get("reason", "")
    target_user.is_banned = True
    target_user.ban_reason = reason
    APIToken.query.filter_by(user_id=target_user.id).delete()  # Log them out everywhere
    db.session.commit()
    return jsonify({"ok": True, "message": f"User {username} has been banned."})


@app.post("/api/user/<username>/unban")
def unban_user(username):
    admin_user = auth_user()
    if not admin_user or admin_user.rank != 'admin':
        return jsonify({"error": "Forbidden: Admin access required"}), 403

    target_user = User.query.filter_by(username=username).first()
    if not target_user:
        return jsonify({"error": "Target user not found"}), 404

    target_user.is_banned = False
    target_user.ban_reason = None
    db.session.commit()
    return jsonify({"ok": True, "message": f"User {username} has been unbanned."})


@app.get("/api/user/<username>/whois")
def whois_user(username):
    mod_user = auth_user()
    if not mod_user or mod_user.rank not in ['admin', 'moderator']:
        return jsonify({"error": "Forbidden: Moderator access required"}), 403

    target_user = User.query.filter_by(username=username).first()
    if not target_user:
        return jsonify({"error": "User not found"}), 404

    return jsonify({
        "username": target_user.username,
        "fullName": target_user.full_name,
        "email": target_user.email,
        "rank": target_user.rank,
        "nationality": target_user.nationality,
        "join_date": target_user.created_at.isoformat(),
        "is_banned": target_user.is_banned,
        "ban_reason": target_user.ban_reason
    })

@app.post("/api/admin/update-content")
def update_content():
    user = auth_user()
    if not user or user.rank != 'admin':
        return jsonify({"error": "Forbidden: Admin access required"}), 403

    data = request.get_json(force=True)
    content_type = data.get("content_type", "").lower()

    content_map = {
        'terms': {'db_key': 'terms_html', 'file_path': 'terms.txt', 'type': 'markdown'},
        'logo': {'db_key': 'logo_icon', 'file_path': 'apex.png', 'type': 'image'},
        'logotext': {'db_key': 'logo_text', 'file_path': 'apex2.png', 'type': 'image'}
    }

    if content_type not in content_map:
        return jsonify({"ok": False, "error": "Invalid content type specified."}), 400

    config = content_map[content_type]
    db_key = config['db_key']
    file_path = config['file_path']
    asset_type = config['type']

    if not os.path.exists(file_path):
        return jsonify({"ok": False, "error": f"Source file not found: {file_path}"}), 404

    try:
        processed_content = None
        if asset_type == 'markdown':
            with open(file_path, 'r', encoding='utf-8') as f:
                md_content = f.read()
            processed_content = markdown.markdown(md_content) # Store as HTML

        elif asset_type == 'image':
            with Image.open(file_path) as img:
                # Use similar processing as seed_assets
                # Determine quality/resize based on asset type if needed
                quality = 64
                if db_key == 'logo_icon':
                    img.thumbnail((64, 64)) # Smaller thumbnail for icon
                    quality = 75
                elif db_key == 'logo_text':
                     # Keep original size or set a max width/height if needed
                     # img.thumbnail((256, 128)) # Example resize
                     quality = 85

                output_buffer = io.BytesIO()
                # Ensure RGBA images with transparency are handled correctly for WEBP
                if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                     img.save(output_buffer, format='WEBP', quality=quality, lossless=False) # Use lossless for transparency if needed
                else:
                    img = img.convert('RGB') # Convert non-alpha images to RGB first
                    img.save(output_buffer, format='WEBP', quality=quality)

                webp_image_bytes = output_buffer.getvalue()
            base64_webp = base64.b64encode(webp_image_bytes).decode('utf-8')
            processed_content = f"data:image/webp;base64,{base64_webp}" # Store as Data URL

        if processed_content is None:
             raise ValueError("Failed to process content.")

        # Find existing asset or create a new one
        asset = StoredAsset.query.get(db_key)
        if asset:
            asset.webp_base64 = processed_content # Update existing
        else:
            asset = StoredAsset(key=db_key, webp_base64=processed_content) # Create new
            db.session.add(asset)

        db.session.commit()
        return jsonify({"ok": True, "message": f"Successfully updated '{content_type}' content in database."})

    except FileNotFoundError:
         return jsonify({"ok": False, "error": f"Source file not found during read: {file_path}"}), 404
    except Exception as e:
        db.session.rollback()
        print(f"Error updating content '{content_type}': {e}") # Log the error server-side
        return jsonify({"ok": False, "error": f"Failed to update {content_type}. Check server logs."}), 500

@app.post("/api/dm/system-send")
def system_send_dm():
    mod_user = auth_user()
    if not mod_user or mod_user.rank not in ['admin', 'moderator']:
        return jsonify({"error": "Forbidden: Moderator access required"}), 403

    system_user = User.query.filter_by(username="ANNOUNCEMENTS").first()
    if not system_user:
        return jsonify({"error": "System user not found. Critical error."}), 500

    data = request.get_json(force=True)
    message = data.get("message", "")
    to_username = data.get("to_username")  # For /warn
    broadcast = data.get("broadcast", False)  # For /broadcast

    if not message:
        return jsonify({"error": "Message is required"}), 400

    if broadcast:
        all_users = User.query.filter(User.username.isnot(None), User.username != "ANNOUNCEMENTS").all()
        for user in all_users:
            dm = DM(sender_id=system_user.id, receiver_id=user.id, message=message)
            db.session.add(dm)
        db.session.commit()
        return jsonify({"ok": True, "message": "Broadcast sent to all users."})

    elif to_username:
        target_user = User.query.filter_by(username=to_username).first()
        if not target_user:
            return jsonify({"error": f"User {to_username} not found."}), 404
        dm = DM(sender_id=system_user.id, receiver_id=target_user.id, message=message)
        db.session.add(dm)
        db.session.commit()
        return jsonify({"ok": True, "message": f"Warning sent to {to_username}."})

    else:
        return jsonify({"error": "Recipient or broadcast flag required."}), 400


@app.put("/api/circuit/<int:circuit_id>")
def edit_circuit(circuit_id):
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    circuit = Circuit.query.get(circuit_id)
    if not circuit:
        return jsonify({"error": "Circuit not found"}), 404

    if circuit.user_id != user.id and user.rank != 'admin':
        return jsonify({"error": "Forbidden: You are not the owner of this circuit."}), 403

    data = request.get_json(force=True)
    # --- THIS IS THE FIX ---
    # We define title and host_school first from the incoming data.
    title = data.get("title", circuit.title).strip()
    host_school = data.get("hostSchool", circuit.host_school).strip()

    # Now we can check their length.
    if len(title) > 30:
        return jsonify({"error": "Title cannot exceed 30 characters"}), 400
    if len(host_school) > 20:
        return jsonify({"error": "Affiliation cannot exceed 20 characters"}), 400

    # And finally, we assign the validated values back to the circuit object.
    circuit.title = title
    circuit.host_school = host_school
    # --- END OF FIX ---

    # Process a new cover image if one was sent
    cover_image_data_url = data.get("coverImage")
    if cover_image_data_url and cover_image_data_url.startswith('data:image'):
        try:
            header, encoded = cover_image_data_url.split(",", 1)
            image_bytes = base64.b64decode(encoded)
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.thumbnail((512, 512))
                output_buffer = io.BytesIO()
                img.save(output_buffer, format='WEBP', quality=85)
                webp_image_bytes = output_buffer.getvalue()
            base64_webp = base64.b64encode(webp_image_bytes).decode('utf-8')
            circuit.cover_image = f"data:image/webp;base64,{base64_webp}"
        except Exception as e:
            print(f"Could not process edited circuit cover image: {e}")

    db.session.commit()
    return jsonify({"ok": True, "message": "Circuit updated successfully!"})

@app.post("/api/lounge/<int:lounge_id>/invite")
def invite_to_lounge(lounge_id):
    inviter = auth_user()
    if not inviter: return jsonify({"error": "unauthorized"}), 401

    lounge = Lounge.query.get(lounge_id)
    if not lounge: return jsonify({"error": "Lounge not found"}), 404

    inviter_role = get_lounge_role(inviter.id, lounge_id)
    if inviter_role not in ['owner', 'moderator']:
        return jsonify({"error": "Forbidden: You do not have permission to invite users."}), 403

    data = request.get_json(force=True)
    username_to_invite = data.get("username")
    invitee = User.query.filter_by(username=username_to_invite).first()
    if not invitee: return jsonify({"error": "User to invite not found"}), 404

    if get_lounge_role(invitee.id, lounge_id):
        return jsonify({"error": "This user is already a member of the lounge."}), 409

    # Check for an existing pending invitation
    existing_invite = Notification.query.filter_by(
        user_id=invitee.id, 
        event_type='lounge_invite', 
        reference_id=lounge_id,
        status='pending'
    ).first()
    if existing_invite:
        return jsonify({"error": "An invitation has already been sent to this user."}), 409

    notification = Notification(
        user_id=invitee.id,
        actor_id=inviter.id,
        event_type='lounge_invite',
        reference_id=lounge_id,
        status='pending'
    )
    db.session.add(notification)
    db.session.commit()
    socketio.emit("new_notification", room=f"user_{invitee.id}")

    return jsonify({"ok": True, "message": f"Invitation sent to {username_to_invite}."})


@app.post("/api/notification/<int:notification_id>/respond")
def respond_to_notification(notification_id):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401
    
    notification = Notification.query.get(notification_id)
    if not notification or notification.user_id != user.id or notification.status != 'pending':
        return jsonify({"error": "Notification not found or action already taken."}), 404

    data = request.get_json(force=True)
    action = data.get("action")

    if action not in ["accept", "decline"]:
        return jsonify({"error": "Invalid action"}), 400

    if notification.event_type == 'lounge_invite':
        lounge_id = notification.reference_id
        if action == "accept":
            if not get_lounge_role(user.id, lounge_id):
                db.session.add(LoungeMember(user_id=user.id, lounge_id=lounge_id, role='member'))
                db.session.flush() # Ensure membership is saved before posting message
                _post_lounge_join_message(user.id, lounge_id) # Post "joined" message
        notification.status = 'accepted' if action == 'accept' else 'declined'

    elif notification.event_type == 'lounge_access_request':
        lounge_id = notification.reference_id
        requester_id = notification.actor_id
        lounge = Lounge.query.get(lounge_id)
        
        if not lounge or lounge.owner_id != user.id:
            return jsonify({"error": "Forbidden or lounge not found."}), 403

        if action == "accept":
            if not get_lounge_role(requester_id, lounge_id):
                db.session.add(LoungeMember(user_id=requester_id, lounge_id=lounge_id, role='member'))
                db.session.flush() # Ensure membership is saved
                _post_lounge_join_message(requester_id, lounge_id) # Post "joined" message

            # --- NEW: Notify the requester that they were accepted ---
            accepted_notification = Notification(
                user_id=requester_id,
                event_type='lounge_access_accepted',
                actor_id=user.id,
                reference_id=lounge_id,
                status='accepted'
            )
            db.session.add(accepted_notification)
            socketio.emit("new_notification", room=f"user_{requester_id}")
            # --- END NEW ---
        
        notification.status = 'accepted' if action == 'accept' else 'declined'

    notification.is_read = True
    db.session.commit()
    return jsonify({"ok": True})

@app.delete("/api/circuit/<int:circuit_id>")
def delete_circuit(circuit_id):
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    circuit = Circuit.query.get(circuit_id)
    if not circuit:
        return jsonify({"error": "Circuit not found"}), 404

    # --- PERMISSION CHECK ---
    # Only allow the owner of the circuit or an admin to delete it.
    if circuit.user_id != user.id and user.rank != 'admin':
        return jsonify({"error": "Forbidden: You do not have permission to delete this circuit."}), 403

    # --- DELETION LOGIC ---
    # First, delete all posts associated with this circuit to maintain database integrity.
    CircuitPost.query.filter_by(circuit_id=circuit.id).delete()

    # Now, delete the circuit itself.
    db.session.delete(circuit)
    db.session.commit()

    return jsonify({"ok": True, "message": "Circuit and all its posts have been deleted."})

@app.get("/api/lounge/message/<int:message_id>/reaction/<emoji>")
def get_lounge_reaction_users(message_id, emoji):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    reactions = LoungeMessageReaction.query.filter_by(message_id=message_id, emoji=emoji).all()
    user_ids = [r.user_id for r in reactions]
    users = User.query.filter(User.id.in_(user_ids)).all()
    usernames = [u.username for u in users]
    return jsonify(usernames)


@socketio.on('react_to_lounge_message')
def handle_lounge_reaction(data):
    # Use the correct authentication method for sockets
    user = user_from_token(socket_connections.get(request.sid))
    if not user or not isinstance(data, dict):
        return

    message_id = data.get('message_id')
    emoji = clean_text(str(data.get('emoji') or ''))[:10]
    if not emoji:
        return

    message = LoungeMessage.query.get(message_id)
    if not message:
        return

    # Only members (or anyone, for non-private lounges) may react
    if message.channel.lounge.privacy == 'private' and not get_lounge_role(user.id, message.channel.lounge_id):
        return

    # Check if the user has already reacted with this emoji
    existing_reaction = LoungeMessageReaction.query.filter_by(
        message_id=message_id, user_id=user.id, emoji=emoji
    ).first()

    if existing_reaction:
        # If it exists, remove it (toggle off)
        db.session.delete(existing_reaction)
    else:
        # If it doesn't exist, add it (toggle on)
        new_reaction = LoungeMessageReaction(message_id=message_id, user_id=user.id, emoji=emoji)
        db.session.add(new_reaction)
    
    db.session.commit()

    # Get the new total counts for all reactions on this message
    reactions_agg = db.session.query(
        LoungeMessageReaction.emoji, func.count(LoungeMessageReaction.user_id)
    ).filter_by(message_id=message_id).group_by(LoungeMessageReaction.emoji).all()
    
    reactions_payload = {emoji: count for emoji, count in reactions_agg}

    # Broadcast the update to everyone in the channel's room
    emit('lounge_message_updated', {
        "message_id": message_id,
        "reactions": reactions_payload
    }, room=f"channel_{message.channel_id}")


@app.get("/api/circuit/<int:circuit_id>")
def get_circuit_details(circuit_id):
    if not auth_user():
        return jsonify({"error": "unauthorized"}), 401

    circuit = Circuit.query.get(circuit_id)
    if not circuit:
        return jsonify({"error": "Circuit not found"}), 404

    today_kst = datetime.now(KST).date()
    todays_view_obj = DailyView.query.filter_by(date=today_kst, viewable_type='circuit', viewable_id=circuit.id).first()
    views_today = todays_view_obj.count if todays_view_obj else 0

    return jsonify({
        "id": circuit.id,
        "title": circuit.title,
        "hostSchool": circuit.host_school,
        "coverImage": circuit.cover_image,
        "code": circuit.code,
        "owner_username": circuit.owner.username if circuit.owner else None,
        "daily_views": views_today  # This now correctly returns today's view count
    })


@app.get("/api/circuit/<int:circuit_id>/posts")
def get_circuit_posts(circuit_id):
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    posts = CircuitPost.query.filter_by(circuit_id=circuit_id).order_by(CircuitPost.created_at.desc()).all()

    post_list = [
        {
            "id": post.id,
            "text": post.text,
            "image": post.image,
            "timestamp": post.created_at.isoformat(),
            "author": {
                "username": post.author.username,
                "fullName": post.author.full_name,
                "rank": post.author.rank
            },
            # 👇 ADD THIS LINE 👇
            "author_id": post.user_id, # So frontend knows who made the post
            "likes": len(post.likes),
            "is_liked_by_me": user in post.likes
        } for post in posts
    ]
    return jsonify(post_list)

@app.delete("/api/posts/<int:post_id>")
def delete_circuit_post(post_id):
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    post = CircuitPost.query.get(post_id)
    if not post:
        return jsonify({"error": "Post not found"}), 404

    # --- PERMISSION CHECK: Author or Admin ---
    if post.user_id != user.id and user.rank != 'admin':
        return jsonify({"error": "Forbidden: You do not have permission to delete this post."}), 403

    # Delete associated likes first (if using a direct relationship without cascade delete)
    # If your 'post_likes' table automatically cascades deletes via FK constraints, this might not be needed.
    db.session.query(post_likes).filter(post_likes.c.post_id == post_id).delete()

    db.session.delete(post)
    db.session.commit()

    return jsonify({"ok": True, "message": "Post deleted successfully."})

@app.put("/api/posts/<int:post_id>")
def edit_circuit_post(post_id):
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    post = CircuitPost.query.get(post_id)
    if not post:
        return jsonify({"error": "Post not found"}), 404

    # --- PERMISSION CHECK: Only the Author ---
    if post.user_id != user.id:
        return jsonify({"error": "Forbidden: You can only edit your own posts."}), 403

    data = request.get_json(force=True)
    new_text = data.get("text", "").strip()
    new_image_data_url = data.get("image") # Can be null, new Base64, or original URL (if unchanged)
    processed_image_data = post.image # Keep existing by default

    if not new_text and not new_image_data_url: # Must have at least text or image
         return jsonify({"error": "Post cannot be empty"}), 400
    if len(new_text) > 600:
        return jsonify({"error": "Post cannot exceed 600 characters"}), 413

    # --- Process New Image (similar to create/edit circuit/lounge) ---
    if new_image_data_url and new_image_data_url.startswith('data:image'):
        # This means a new image was uploaded
        try:
            header, encoded = new_image_data_url.split(",", 1)
            image_bytes = base64.b64decode(encoded)
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.thumbnail((1024, 1024))
                output_buffer = io.BytesIO()
                img.save(output_buffer, format='WEBP', quality=85)
                webp_image_bytes = output_buffer.getvalue()
            base64_webp = base64.b64encode(webp_image_bytes).decode('utf-8')
            processed_image_data = f"data:image/webp;base64,{base64_webp}"
        except Exception as e:
            print(f"Could not process edited post image: {e}")
            # Decide: return error or just skip image update? Skipping for now.
            processed_image_data = post.image # Revert to original on error
    elif new_image_data_url is None:
        # This means the user explicitly removed the image
        processed_image_data = None
    # If new_image_data_url is the *same* as post.image (or not provided), processed_image_data remains unchanged.

    # --- Update Post ---
    post.text = new_text
    post.image = processed_image_data
    db.session.commit()

    # --- Return Updated Post Data (Optional but good practice) ---
    return jsonify({
        "ok": True,
        "message": "Post updated successfully!",
        "post": {
            "id": post.id,
            "text": post.text,
            "image": post.image,
             # Return other fields if your UI needs them refreshed
        }
    })

@app.post("/api/circuits")
def create_circuit():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    title = data.get("title", "").strip()
    host_school = data.get("hostSchool", "").strip()

    if len(title) > 30:
        return jsonify({"error": "Title cannot exceed 30 characters"}), 400
    if len(host_school) > 20:
        return jsonify({"error": "Affiliation cannot exceed 20 characters"}), 400

    # 👇 NEW IMAGE PROCESSING LOGIC
    cover_image_data_url = data.get("coverImage")
    processed_image_data = None
    if cover_image_data_url and cover_image_data_url.startswith('data:image'):
        try:
            header, encoded = cover_image_data_url.split(",", 1)
            image_bytes = base64.b64decode(encoded)
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.thumbnail((512, 512))  # Resize to a reasonable size for a cover
                output_buffer = io.BytesIO()
                img.save(output_buffer, format='WEBP', quality=85)
                webp_image_bytes = output_buffer.getvalue()
            base64_webp = base64.b64encode(webp_image_bytes).decode('utf-8')
            processed_image_data = f"data:image/webp;base64,{base64_webp}"
        except Exception as e:
            print(f"Could not process circuit cover image: {e}")
    # 👆 END OF NEW LOGIC

    if not title or not host_school:
        return jsonify({"error": "Title and host school are required"}), 400

    while True:
        new_code = str(secrets.randbelow(900000) + 100000)
        if not Circuit.query.filter_by(code=new_code).first():
            break

    new_circuit = Circuit(
        title=title,
        host_school=host_school,
        cover_image=processed_image_data,  # Use the processed image
        code=new_code,
        user_id=user.id  # 👈 STORE THE OWNER
    )

    db.session.add(new_circuit)
    db.session.commit()

    return jsonify({
        "ok": True,
        "message": "Circuit created successfully!",
        "circuit": {
            "id": new_circuit.id,
            "title": new_circuit.title,
            "hostSchool": new_circuit.host_school,
            "code": new_circuit.code
        }
    }), 201

@app.put("/api/lounge/channel/<int:channel_id>")
def edit_lounge_channel(channel_id):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    channel = LoungeChannel.query.get(channel_id)
    if not channel: return jsonify({"error": "Channel not found"}), 404

    user_role = get_lounge_role(user.id, channel.lounge_id)
    if user_role not in ['owner', 'moderator']:
        return jsonify({"error": "Forbidden: You do not have permission to edit channels."}), 403

    data = request.get_json(force=True)
    new_name = data.get("name", "").strip()
    if not new_name or len(new_name) > 100:
        return jsonify({"error": "Invalid channel name"}), 400

    channel.name = new_name
    db.session.commit()
    return jsonify({"ok": True, "message": "Channel name updated."})


@app.post("/api/lounge/<int:lounge_id>/channels")
def create_lounge_channel(lounge_id):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    # Check if the user has permission to create channels
    user_role = get_lounge_role(user.id, lounge_id)
    if user_role not in ['owner', 'moderator']:
        return jsonify({"error": "Forbidden: You do not have permission to create channels."}), 403

    data = request.get_json(force=True)
    name = data.get("name", "").strip().lower().replace(" ", "-") # Sanitize name
    permission_level = data.get("permission_level", "public")

    if not name or not re.match(r'^[a-z0-9-]{1,50}$', name):
        return jsonify({"error": "Invalid channel name. Use letters, numbers, and hyphens only."}), 400
    if permission_level not in ['public', 'mods_only_chat', 'mods_only_view']:
        return jsonify({"error": "Invalid permission level."}), 400

    new_channel = LoungeChannel(
        name=name,
        lounge_id=lounge_id,
        permission_level=permission_level
    )
    db.session.add(new_channel)
    db.session.commit()

    return jsonify({
        "id": new_channel.id,
        "name": new_channel.name,
        "permission_level": new_channel.permission_level
    }), 201



@app.delete("/api/lounge/channel/<int:channel_id>")
def delete_lounge_channel(channel_id):
    user = auth_user()
    if not user: return jsonify({"error": "unauthorized"}), 401

    channel = LoungeChannel.query.get(channel_id)
    if not channel: return jsonify({"error": "Channel not found"}), 404

    user_role = get_lounge_role(user.id, channel.lounge_id)
    if user_role not in ['owner', 'moderator']:
        return jsonify({"error": "Forbidden: You do not have permission to delete channels."}), 403
    
    # --- REMOVED ---
    # The special check preventing the deletion of '#general' is now gone.
    # if channel.name == 'general':
    #     return jsonify({"error": "The #general channel cannot be deleted."}), 400
        
    # --- NEW LOGIC: Handle deleting the main channel ---
    if channel.is_main:
        # Find another channel in the same lounge to promote.
        # We order by name to predictably get the "next" channel alphabetically.
        next_channel = LoungeChannel.query.filter(
            LoungeChannel.lounge_id == channel.lounge_id,
            LoungeChannel.id != channel.id
        ).order_by(LoungeChannel.name.asc()).first()

        # If another channel exists, make it the new main channel.
        if next_channel:
            next_channel.is_main = True
    # --- END OF NEW LOGIC ---
        
    # The cascade="all, delete-orphan" in your LoungeChannel model will 
    # automatically handle deleting all messages in this channel.
    db.session.delete(channel)
    db.session.commit()
    return jsonify({"ok": True, "message": "Channel deleted."})

@app.post("/api/circuit/<int:circuit_id>/posts")
def create_circuit_post(circuit_id):
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    circuit = Circuit.query.get(circuit_id)
    if not circuit:
        return jsonify({"error": "Circuit not found"}), 404

    data = request.get_json(force=True)
    text = data.get("text", "").strip()
    image_data_url = data.get("image")  # Get the image from the request
    processed_image_data = None

    if not text:
        return jsonify({"error": "Post text cannot be empty"}), 400
    if len(text) > 600:
        return jsonify({"error": "Post cannot exceed 600 characters"}), 413

    # --- ADD THIS IMAGE PROCESSING BLOCK ---
    if image_data_url and image_data_url.startswith('data:image'):
        try:
            header, encoded = image_data_url.split(",", 1)
            image_bytes = base64.b64decode(encoded)
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.thumbnail((1024, 1024))  # Larger size for posts
                output_buffer = io.BytesIO()
                img.save(output_buffer, format='WEBP', quality=85)
                webp_image_bytes = output_buffer.getvalue()
            base64_webp = base64.b64encode(webp_image_bytes).decode('utf-8')
            processed_image_data = f"data:image/webp;base64,{base64_webp}"
        except Exception as e:
            print(f"Could not process post image: {e}")
    # --- END OF BLOCK ---

    new_post = CircuitPost(
        text=text,
        image=processed_image_data,  # Save the processed image
        user_id=user.id,
        circuit_id=circuit.id
    )
    db.session.add(new_post)
    db.session.commit()

    return jsonify({"ok": True, "message": "Post created successfully!"}), 201


STOP_WORDS = set([
    'a', 'about', 'above', 'after', 'again', 'against', 'all', 'am', 'an', 'and', 
    'any', 'are', 'as', 'at', 'be', 'because', 'been', 'before', 'being', 'below',
    'between', 'both', 'but', 'by', 'can', 'did', 'do', 'does', 'doing', 'down',
    'during', 'each', 'few', 'for', 'from', 'further', 'had', 'has', 'have', 
    'having', 'he', 'her', 'here', 'hers', 'herself', 'him', 'himself', 'his', 
    'how', 'i', 'if', 'in', 'into', 'is', 'it', 'its', 'itself', 'just', 'me', 
    'more', 'most', 'my', 'myself', 'no', 'nor', 'not', 'now', 'of', 'off', 'on', 
    'once', 'only', 'or', 'other', 'our', 'ours', 'ourselves', 'out', 'over', 
    'own', 's', 'same', 'she', 'should', 'so', 'some', 'such', 't', 'than', 
    'that', 'the', 'their', 'theirs', 'them', 'themselves', 'then', 'there', 
    'these', 'they', 'this', 'those', 'through', 'to', 'too', 'under', 'until', 
    'up', 'very', 'was', 'we', 'were', 'what', 'when', 'where', 'which', 'while', 
    'who', 'whom', 'why', 'will', 'with', 'you', 'your', 'yours', 'yourself', 'yourselves'
])


@app.get("/api/search")
def search():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    query = request.args.get('q', '').strip().lower()
    if not query:
        return jsonify({"error": "Search query cannot be empty"}), 400

    # 1. Tokenize the search query and remove stop words
    search_terms = [term for term in re.split(r'\s+', query) if term not in STOP_WORDS]
    if not search_terms:
        return jsonify({"users": [], "circuits": [], "lounges": [], "articles": []})

    # 2. Build flexible filter conditions for each content type
    user_filters = or_(*(
        [User.full_name.ilike(f'%{term}%') for term in search_terms] +
        [User.username.ilike(f'%{term}%') for term in search_terms]
    ))

    circuit_filters = or_(*(
        [Circuit.title.ilike(f'%{term}%') for term in search_terms] +
        [Circuit.host_school.ilike(f'%{term}%') for term in search_terms]
    ))

    lounge_filters = or_(*(
        [Lounge.name.ilike(f'%{term}%') for term in search_terms] +
        [Lounge.description.ilike(f'%{term}%') for term in search_terms]
    ))

    article_filters = or_(*(
        [Article.title.ilike(f'%{term}%') for term in search_terms] +
        [Article.content.ilike(f'%{term}%') for term in search_terms]
    ))

    # 3. Execute all search queries, limiting results to 10 per category
    users = User.query.filter(user_filters, User.username.isnot(None)).limit(10).all()
    circuits = Circuit.query.filter(circuit_filters).limit(10).all()
    lounges = Lounge.query.filter(lounge_filters).limit(10).all()
    articles = Article.query.filter(article_filters).limit(10).all()


    return jsonify({
        "users": [
            {"username": u.username, "fullName": u.full_name, "rank": u.rank, "profile_pic": u.profile_pic}
            for u in users
        ],
        "circuits": [
            {"id": c.id, "title": c.title, "host_school": c.host_school, "coverImage": c.cover_image}
            for c in circuits
        ],
        "lounges": [
            {"id": l.id, "name": l.name, "description": l.description, "cover_image": l.cover_image}
            for l in lounges
        ],
        "articles": [
            {"id": a.id, "title": a.title, "author": a.author.full_name, "nationalityTag": a.author.nationality}
            for a in articles
        ]
    })

@app.get("/api/trending")
def get_trending_items():
    # --- THIS IS THE NEW PART ---
    # Get a limit from the request args, defaulting to 5.
    # We'll fetch more for the "All Trending" page.
    limit = request.args.get('limit', 5, type=int)
    # --- END NEW PART ---

    today_kst = datetime.now(KST).date()
    yesterday_kst = today_kst - timedelta(days=1)

    todays_views_raw = DailyView.query.filter_by(date=today_kst).all()
    yesterdays_views_raw = DailyView.query.filter_by(date=yesterday_kst).all()

    todays_map = {(v.viewable_type, v.viewable_id): v.count for v in todays_views_raw}
    yesterdays_map = {(v.viewable_type, v.viewable_id): v.count for v in yesterdays_views_raw}

    trending = []
    for (item_type, item_id), today_count in todays_map.items():
        yesterday_count = yesterdays_map.get((item_type, item_id), 0)
        growth = today_count - yesterday_count
        if growth > 0:
            trending.append({
                "id": item_id,
                "type": item_type,
                "views": today_count,
                "growth": growth
            })

    trending.sort(key=lambda x: (x['growth'], x['views']), reverse=True)

    top_items = []
    # --- THIS PART NOW USES THE NEW LIMIT ---
    for item_data in trending[:limit]:
        if item_data['type'] == 'article':
            item = Article.query.get(item_data['id'])
            if item and item.author:
                top_items.append({
                    "type": "article", "id": item.id, "title": item.title,
                    "author": item.author.full_name, "nationalityTag": item.author.nationality,
                    "daily_views": item_data['views']
                })
        elif item_data['type'] == 'circuit':
            item = Circuit.query.get(item_data['id'])
            if item:
                top_items.append({
                    "type": "circuit", "id": item.id, "title": item.title,
                    "hostSchool": item.host_school, "coverImage": item.cover_image,
                    "daily_views": item_data['views']
                })
        elif item_data['type'] == 'lounge':
            item = Lounge.query.get(item_data['id'])
            if item:
                top_items.append({
                    "type": "lounge", "id": item.id, "name": item.name,
                    "description": item.description, "cover_image": item.cover_image,
                    "daily_views": item_data['views']
                })
    return jsonify(top_items)

@app.post("/api/lounge/<int:lounge_id>/view")
def increment_lounge_view(lounge_id):
    if not Lounge.query.get(lounge_id):
        return jsonify({"error": "Lounge not found"}), 404
    # This uses the same helper function as articles and circuits
    views = _increment_view(lounge_id, 'lounge')
    return jsonify({"ok": True, "views": views})

# In server.py, update the /api/me endpoint:
@app.get("/api/me")
def me():
    user = auth_user(allow_incomplete=True)
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    # Google sign-up that hasn't picked a username/password/nationality yet
    if user.username is None:
        return jsonify({
            "needs_onboarding": True,
            "email": user.email,
            "fullName": user.full_name,
            "profile_pic": user.profile_pic,
        })

    following_list = [u.username for u in user.following]
    post_count = CircuitPost.query.filter_by(user_id=user.id).count()
    follower_count = user.followers.count()
    return jsonify({
        "id": user.id,
        "email": user.email,
        "fullName": user.full_name,
        "username": user.username,
        "nationality": user.nationality,
        "dob": user.dob,
        "bio": user.bio,
        "rank": user.rank,
        "provider": user.provider,
        "created_at": user.created_at.isoformat(),
        "has_bio": bool(user.bio),
        "post_count": post_count,
        "follower_count": follower_count,
        "following": following_list,
        "profile_pic": user.profile_pic,
        # --- ADD NEW FIELDS ---
        "account_type": user.account_type,
        "grade": user.grade,
        "subject": user.subject,
        "is_birthday": is_users_birthday(user.dob),
        "show_birthday": user.show_birthday,
        "show_social_stats": user.show_social_stats 
    })


# Helper dictionary to map connection IDs to tokens
socket_connections = {}


@socketio.on("connect")
def handle_connect():
    token_str = request.args.get("token")
    if not token_str:
        return False

    user = user_from_token(token_str)
    if not user:
        return False

    socket_connections[request.sid] = token_str

    join_room(f"user_{user.id}")
    emit("system", {"msg": f"Connected as {user.email}"})


@socketio.on('disconnect')
def handle_disconnect_event():
    if request.sid in socket_connections:
        del socket_connections[request.sid]


# In server.py, update the /api/user/<username> endpoint:
@app.get("/api/user/<username>")
def get_user_profile(username):
    if not auth_user():
        return jsonify({"error": "unauthorized"}), 401

    user = User.query.filter_by(username=username).first()
    if not user:
        return jsonify({"error": "User not found"}), 404
    post_count = CircuitPost.query.filter_by(user_id=user.id).count()
    follower_count = user.followers.count()
    return jsonify({
        "username": user.username,
        "fullName": user.full_name,
        "nationality": user.nationality,
        "bio": user.bio,
        "rank": user.rank,
        "post_count": post_count,
        "follower_count": follower_count,
        "profile_pic": user.profile_pic,
        # --- ADD NEW FIELDS ---
        "account_type": user.account_type,
        "grade": user.grade,
        "subject": user.subject,
        "is_birthday": is_users_birthday(user.dob),
        "show_birthday": user.show_birthday,
        "show_social_stats": user.show_social_stats
    })


@app.post("/api/users/details")
def get_users_details():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    usernames = data.get("usernames", [])

    if not usernames:
        return jsonify([])

    # Find all users whose username is in the provided list
    users = User.query.filter(User.username.in_(usernames)).all()

    user_list = [
        {
            "username": u.username,
            "fullName": u.full_name,
            "rank": u.rank,
            "profile_pic": u.profile_pic
        } for u in users
    ]
    return jsonify(user_list)


@app.get("/api/users")
def get_users():
    current_user = auth_user()
    if not current_user:
        return jsonify({"error": "unauthorized"}), 401

    system_usernames = ["ANNOUNCEMENTS"]
    users = User.query.filter(
        User.id != current_user.id,
        User.username.notin_(system_usernames)
    ).order_by(User.created_at.desc()).limit(10).all()

    user_list = [
        {
            "username": user.username,
            # --- THIS IS THE FIX ---
            "fullName": user.full_name,  # Changed from user.fullName
            # --- END OF FIX ---
            "rank": user.rank,
            "profile_pic": user.profile_pic,
            "account_type": user.account_type,
            "nationality": user.nationality,
            "is_birthday": is_users_birthday(user.dob),
            "show_birthday": user.show_birthday
        }
        for user in users
    ]
    return jsonify(user_list)


@app.post("/api/user/<username>/rank")
def set_user_rank(username):
    # Step 1: Check if the person making the request is an admin
    admin_user = auth_user()
    if not admin_user or admin_user.rank != 'admin':
        return jsonify({"error": "Forbidden: Admin access required"}), 403

    # Step 2: Get the new rank from the request
    data = request.get_json(force=True)
    new_rank = data.get("rank")
    if new_rank not in ['user', 'moderator', 'admin']:
        return jsonify({"error": "Invalid rank provided"}), 400

    # Step 3: Find the target user and update their rank
    target_user = User.query.filter_by(username=username).first()
    if not target_user:
        return jsonify({"error": "Target user not found"}), 404

    target_user.rank = new_rank
    db.session.commit()

    return jsonify({"ok": True, "message": f"{username}'s rank has been updated to {new_rank}."})


# In server.py


@app.get("/api/dm/conversations")
def get_conversations():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    # --- NEW: Efficiently get all unread message counts in one query ---
    unread_counts_query = db.session.query(
        DM.sender_id, func.count(DM.id)
    ).filter_by(
        receiver_id=user.id, is_read=False
    ).group_by(DM.sender_id).all()
    
    unread_map = dict(unread_counts_query)
    # --- END NEW ---

    sent_dms = DM.query.filter_by(sender_id=user.id).all()
    received_dms = DM.query.filter_by(receiver_id=user.id).all()
    conversations = {}
    all_dms = sorted(sent_dms + received_dms, key=lambda dm: dm.created_at)

    for dm in all_dms:
        other_user = dm.receiver if dm.sender_id == user.id else dm.sender
        if dm.sender_id == user.id:
            last_message_text = f"You: {dm.message}" if dm.message else "You sent an image."
        else:
            last_message_text = dm.message if dm.message else "Sent you an image."

        conversations[other_user.username] = {
            "last_message": last_message_text,
            "timestamp": dm.created_at.isoformat(),
            "unread_count": unread_map.get(other_user.id, 0), # <-- ADD THIS
            "other_user": {
                "username": other_user.username,
                "fullName": other_user.full_name,
                "rank": other_user.rank,
                "profile_pic": other_user.profile_pic,
                "account_type": other_user.account_type
            }
        }

    sorted_convos = sorted(conversations.values(), key=lambda x: x['timestamp'], reverse=True)
    return jsonify(sorted_convos)


@app.post("/api/article/<int:article_id>/view")
def increment_article_view(article_id):
    if not Article.query.get(article_id):
        return jsonify({"error": "Article not found"}), 404
    views = _increment_view(article_id, 'article')
    return jsonify({"ok": True, "views": views})


@app.post("/api/circuit/<int:circuit_id>/view")
def increment_circuit_view(circuit_id):
    if not Circuit.query.get(circuit_id):
        return jsonify({"error": "Circuit not found"}), 404
    views = _increment_view(circuit_id, 'circuit')
    return jsonify({"ok": True, "views": views})


@app.put("/api/profile")  # 👈 Note the new URL
def set_profile():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    new_username = data.get("username", "").strip()

    # Check if another user already has the new username
    if new_username and new_username != user.username:
        if not re.match(r'^[a-z0-9!._*-]{3,20}$', new_username):
            return jsonify({"error": "Username must be 3-20 characters, lowercase, and can only contain letters, numbers, and !._-*"}), 400
        if User.query.filter_by(username=new_username).first():
            return jsonify({"error": "Username is already taken"}), 409
        user.username = new_username

    new_full_name = str(data.get("fullName", user.full_name) or "").strip()
    if not new_full_name or len(new_full_name) > 100:
        return jsonify({"error": "Name is required (max 100 characters)."}), 400
    user.full_name = new_full_name

    new_bio = data.get("bio", user.bio)
    if isinstance(new_bio, str) and len(new_bio) > 2000:
        return jsonify({"error": "Bio cannot exceed 2000 characters."}), 400
    user.bio = new_bio

    if data.get("nationality") is not None:
        new_nationality = str(data.get("nationality")).strip()
        if not new_nationality or len(new_nationality) > 60:
            return jsonify({"error": "Invalid nationality."}), 400
        user.nationality = new_nationality

    user.show_birthday = data.get("showBirthday", user.show_birthday)
    user.show_social_stats = data.get("showSocialStats", user.show_social_stats)

    profile_pic_data_url = data.get("profilePic")
    if profile_pic_data_url and profile_pic_data_url.startswith('data:image'):
        try:
            # 1. Parse the Data URL to get the Base64 string
            header, encoded = profile_pic_data_url.split(",", 1)

            # 2. Decode the Base64 string into bytes
            image_bytes = base64.b64decode(encoded)

            # 3. Open the image with Pillow
            with Image.open(io.BytesIO(image_bytes)) as img:
                # 4. Resize the image to a max of 256x256 to save space
                img.thumbnail((256, 256))

                # 5. Save the resized image to a buffer in WEBP format
                output_buffer = io.BytesIO()
                img.save(output_buffer, format='WEBP', quality=80)
                webp_image_bytes = output_buffer.getvalue()

            # 6. Encode the new WEBP image back to a Base64 string
            base64_webp = base64.b64encode(webp_image_bytes).decode('utf-8')

            # 7. Store the full Data URL in the database
            user.profile_pic = f"data:image/webp;base64,{base64_webp}"

        except Exception as e:
            print(f"Could not process profile image: {e}")
            # This will skip the image update if something goes wrong

    db.session.commit()
    return jsonify({
        "ok": True,
        "message": "Profile updated successfully!",
        "user": {
            "fullName": user.full_name,
            "username": user.username,
            "bio": user.bio,
            "nationality": user.nationality,
            "profile_pic": user.profile_pic
        }
    })


# -------------------------------------------------
# Auth (Google OAuth)
# -------------------------------------------------
@app.get("/auth/google/login")
def google_login():
    # 콜백 URL
    redirect_uri = url_for("google_callback", _external=True)
    # (선택) 클라이언트로 되돌아갈 URL을 쿼리로 전달 가능
    if not app.config["GOOGLE_CLIENT_ID"] or not app.config["GOOGLE_CLIENT_SECRET"]:
        return make_response("Google login is not configured on this server.", 503)
    client_redirect = request.args.get("redirect")
    if client_redirect:
        if not is_safe_redirect(client_redirect):
            return make_response("Invalid redirect target.", 400)
        # state에 넣어 왕복
        return google.authorize_redirect(redirect_uri, state=client_redirect)
    return google.authorize_redirect(redirect_uri)


@app.get("/auth/google/callback")
def google_callback():
    # 토큰 교환
    try:
        token = google.authorize_access_token()
    except Exception as e:
        return make_response(f"Google OAuth failed: {e}", 400)

    # OpenID Connect ID 토큰(또는 userinfo) 파싱
    try:
        idinfo = google.parse_id_token(token)
    except Exception:
        idinfo = token.get("userinfo", {})

    email = (idinfo.get("email") or "").lower().strip()
    if not idinfo.get("email_verified"):
        return make_response("Your Google email address is not verified.", 400)
    sub = idinfo.get("sub")  # 구글 고유 ID
    full_name = clean_text(str(idinfo.get("name") or (email.split("@")[0] if email else "User")))[:100]
    picture = idinfo.get("picture")
    if not (isinstance(picture, str) and picture.startswith("https://") and '"' not in picture and "'" not in picture):
        picture = None
    if not email:
        return make_response("No email returned from Google.", 400)

    user = User.query.filter_by(email=email).first()
    if not user:
        # New Google sign-up: the account starts WITHOUT a username. The user is sent to the
        # onboarding screen to choose a username, password, nationality and birth date
        # (POST /api/onboarding) before they can use the app.
        user = User(
            email=email,
            provider="google",
            oauth_sub=sub,
            full_name=full_name,
            username=None,
            account_type="personal",
            rank='admin' if email in ADMIN_EMAILS else 'user',
            profile_pic=picture,
        )
        db.session.add(user)
        db.session.commit()
    else:
        # 기존 로컬 계정이더라도 구글 연동 허용(필요 시 정책에 맞게 제한)
        user.provider = user.provider or "google"
        if not user.oauth_sub:
            user.oauth_sub = sub
        if not user.profile_pic and picture:
            user.profile_pic = picture
        db.session.commit()

    # The redirect target travels in "state": only trust it if it points back to this site.
    state_redirect = request.args.get("state")
    safe_redirect = state_redirect if is_safe_redirect(state_redirect) else None

    if user.is_banned:
        if safe_redirect:
            sep = "&" if "?" in safe_redirect else "?"
            return redirect(f"{safe_redirect}{sep}login_error=banned")
        return make_response("This account has been banned.", 403)

    api_token = issue_api_token(user)

    if safe_redirect:
        sep = "&" if "?" in safe_redirect else "?"
        return redirect(f"{safe_redirect}{sep}token={api_token}")

    # 기본 응답(간단 HTML: window.opener로 토큰 전달 → 팝업 로그인 시 용이)
    html = f"""
<!doctype html>
<html>
  <body>
    <script>
      (function(){{
        try {{
          if (window.opener) {{
            window.opener.postMessage({{"type":"GOOGLE_LOGIN_SUCCESS","token":"{api_token}"}}, window.location.origin);
            window.close();
          }}
        }} catch (e) {{}}
      }})();
    </script>
    <pre>Google login success. You can now close this window.</pre>
  </body>
</html>
"""
    resp = make_response(html, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


# -------------------------------------------------
# Bio APIs
# -------------------------------------------------
@app.get("/api/bio")
def get_bio():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"bio": user.bio or ""})


@app.put("/api/bio")
def set_bio():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True)
    bio = data.get("bio")
    if bio is None or not isinstance(bio, str):
        return jsonify({"error": "bio string required"}), 400
    if len(bio) > 20000:
        return jsonify({"error": "bio too long"}), 413
    user.bio = bio
    db.session.commit()
    return jsonify({"ok": True, "bio": user.bio})


# -------------------------------------------------
# DM APIs
# -------------------------------------------------
@app.post("/api/dm/send")
@rate_limit(120, 60)
def send_dm():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    receiver_username = str(data.get("to_username") or "").strip()
    message_text = str(data.get("message") or "").strip()
    image_data = data.get("image")  # Can be a Base64 string

    if not receiver_username or (not message_text and not image_data):
        return jsonify({"error": "Receiver and message/image are required"}), 400
    if len(message_text) > 4000:
        return jsonify({"error": "Message is too long (max 4000 characters)."}), 400
    if image_data and not valid_image_data_url(image_data):
        return jsonify({"error": "Invalid or too large image."}), 400

    receiver = User.query.filter_by(username=receiver_username).first()
    if not receiver:
        return jsonify({"error": "Receiver not found"}), 404

    dm = DM(
        sender_id=user.id,
        receiver_id=receiver.id,
        message=message_text, # <-- CORRECTED LINE
        image=image_data
    )
    db.session.add(dm)
    db.session.commit()

    # Update the socket event to include image data
    socketio.emit("dm_received", {
        "id": dm.id,
        "from": user.username,
        "message": dm.message,
        "image": dm.image,  # <-- ADD THIS LINE
        "created_at": dm.created_at.isoformat() + "Z"
    }, room=f"user_{receiver.id}")

    return jsonify({"ok": True, "dm_id": dm.id})

@app.put("/api/lounge/<int:lounge_id>/member/<username>/role")
def set_lounge_member_role(lounge_id, username):
    admin_user = auth_user()
    if not admin_user: return jsonify({"error": "unauthorized"}), 401

    target_user = User.query.filter_by(username=username).first()
    if not target_user: return jsonify({"error": "Target user not found"}), 404

    # Get the role of the user trying to make the change
    admin_role = get_lounge_role(admin_user.id, lounge_id)

    # Get the membership record of the user being changed
    target_membership = LoungeMember.query.filter_by(lounge_id=lounge_id, user_id=target_user.id).first()
    if not target_membership: return jsonify({"error": "User is not a member of this lounge"}), 404

    data = request.get_json(force=True)
    new_role = data.get("role")
    if new_role not in ['member', 'moderator']:
        return jsonify({"error": "Invalid role specified. Can only set to 'member' or 'moderator'."}), 400

    # --- PERMISSION CHECKS ---
    # Only the owner can promote/demote moderators.
    if target_membership.role == 'moderator' or new_role == 'moderator':
        if admin_role != 'owner':
            return jsonify({"error": "Forbidden: Only the lounge owner can manage moderators."}), 403

    # Moderators can only demote other members (this logic is implicitly handled by the check above).
    if admin_role not in ['owner', 'moderator']:
        return jsonify({"error": "Forbidden: You do not have permission to change roles."}), 403

    # An owner's role cannot be changed.
    if target_membership.role == 'owner':
        return jsonify({"error": "The lounge owner's role cannot be changed."}), 400
    
    # --- END PERMISSION CHECKS ---

    target_membership.role = new_role
    db.session.commit()

    return jsonify({"ok": True, "message": f"{username}'s role has been updated to {new_role}."})


@app.get("/api/dm/inbox")
def inbox():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    dms = DM.query.filter_by(receiver_id=user.id).order_by(DM.created_at.desc()).all()
    return jsonify([
        {"id": dm.id, "from": dm.sender.username, "message": dm.message,
         "created_at": dm.created_at.isoformat()} for dm in dms
    ])


@app.get("/api/dm/sent")
def sent():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    dms = DM.query.filter_by(sender_id=user.id).order_by(DM.created_at.desc()).all()
    return jsonify([
        {"id": dm.id, "to": dm.receiver.username, "message": dm.message,
         "created_at": dm.created_at.isoformat()} for dm in dms
    ])


# -------------------------------------------------
# Socket.IO (실시간 DM)
# -------------------------------------------------


# -------------------------------------------------
# Admin dashboard (/admin) + its API (/api/admin/*). Admin rank only.
# -------------------------------------------------
SYSTEM_USERNAMES = ["ANNOUNCEMENTS"]


def require_admin():
    """Returns (admin_user, None) or (None, error_response)."""
    user = auth_user()
    if not user:
        return None, (jsonify({"error": "unauthorized"}), 401)
    if user.rank != "admin":
        return None, (jsonify({"error": "Forbidden: Admin access required"}), 403)
    return user, None


def _iso(dt):
    return dt.isoformat() + "Z" if dt else None


@app.get("/admin")
def serve_admin_page():
    # The page itself is static; every API call it makes is checked server-side.
    resp = make_response(send_from_directory(BASE_DIR, "admin.html"))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/admin/stats")
def admin_stats():
    admin, err = require_admin()
    if err: return err
    now = datetime.utcnow()
    real_users = User.query.filter(User.username.notin_(SYSTEM_USERNAMES))
    by_nat = (db.session.query(User.nationality, func.count(User.id))
              .filter(User.nationality.isnot(None), User.username.notin_(SYSTEM_USERNAMES))
              .group_by(User.nationality).order_by(func.count(User.id).desc()).limit(10).all())
    return jsonify({
        "users_total": real_users.count(),
        "users_today": real_users.filter(User.created_at >= now - timedelta(days=1)).count(),
        "users_7d": real_users.filter(User.created_at >= now - timedelta(days=7)).count(),
        "users_banned": User.query.filter_by(is_banned=True).count(),
        "users_incomplete": User.query.filter(User.username.is_(None)).count(),
        "admins": User.query.filter_by(rank="admin").filter(User.username.notin_(SYSTEM_USERNAMES)).count(),
        "moderators": User.query.filter_by(rank="moderator").count(),
        "dms_total": DM.query.count(),
        "dms_today": DM.query.filter(DM.created_at >= now - timedelta(days=1)).count(),
        "lounges": Lounge.query.count(),
        "lounge_messages": LoungeMessage.query.count(),
        "circuits": Circuit.query.count(),
        "posts": CircuitPost.query.count(),
        "articles": Article.query.count(),
        "top_nationalities": [{"nationality": n, "count": c} for n, c in by_nat],
    })


@app.get("/api/admin/users")
def admin_list_users():
    admin, err = require_admin()
    if err: return err
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status", "all")
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except (TypeError, ValueError):
        page = 1
    per_page = 25

    query = User.query.filter(or_(User.username.is_(None), User.username.notin_(SYSTEM_USERNAMES)))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(User.username.ilike(like), User.full_name.ilike(like),
                                 User.email.ilike(like), User.nationality.ilike(like)))
    if status == "banned":
        query = query.filter(User.is_banned.is_(True))
    elif status in ("admin", "moderator", "user"):
        query = query.filter(User.rank == status)
    elif status == "incomplete":
        query = query.filter(User.username.is_(None))

    total = query.count()
    users = query.order_by(User.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({
        "total": total, "page": page, "per_page": per_page,
        "users": [{
            "id": u.id, "username": u.username, "fullName": u.full_name, "email": u.email,
            "nationality": u.nationality, "rank": u.rank, "provider": u.provider,
            "is_banned": u.is_banned, "ban_reason": u.ban_reason, "created_at": _iso(u.created_at),
            "dob": u.dob, "is_me": u.id == admin.id,
        } for u in users],
    })


def _admin_target(user_id, admin):
    """Load a user the admin may act on. Returns (user, None) or (None, error_response)."""
    target = User.query.get(user_id)
    if not target:
        return None, (jsonify({"error": "User not found"}), 404)
    if target.username in SYSTEM_USERNAMES:
        return None, (jsonify({"error": "System accounts cannot be changed."}), 400)
    if target.id == admin.id:
        return None, (jsonify({"error": "You cannot do this to your own account."}), 400)
    return target, None


@app.post("/api/admin/users/<int:user_id>/ban")
def admin_ban_user(user_id):
    admin, err = require_admin()
    if err: return err
    target, err = _admin_target(user_id, admin)
    if err: return err
    reason = str((request.get_json(force=True, silent=True) or {}).get("reason") or "")[:500]
    target.is_banned = True
    target.ban_reason = reason or None
    APIToken.query.filter_by(user_id=target.id).delete()  # log out everywhere
    db.session.commit()
    return jsonify({"ok": True})


@app.post("/api/admin/users/<int:user_id>/unban")
def admin_unban_user(user_id):
    admin, err = require_admin()
    if err: return err
    target, err = _admin_target(user_id, admin)
    if err: return err
    target.is_banned = False
    target.ban_reason = None
    db.session.commit()
    return jsonify({"ok": True})


@app.post("/api/admin/users/<int:user_id>/rank")
def admin_set_rank(user_id):
    admin, err = require_admin()
    if err: return err
    target, err = _admin_target(user_id, admin)
    if err: return err
    rank = (request.get_json(force=True, silent=True) or {}).get("rank")
    if rank not in ("user", "moderator", "admin"):
        return jsonify({"error": "Invalid rank"}), 400
    target.rank = rank
    db.session.commit()
    return jsonify({"ok": True})


@app.post("/api/admin/users/<int:user_id>/logout")
def admin_force_logout(user_id):
    admin, err = require_admin()
    if err: return err
    target, err = _admin_target(user_id, admin)
    if err: return err
    n = APIToken.query.filter_by(user_id=target.id).delete()
    db.session.commit()
    return jsonify({"ok": True, "sessions_revoked": n})


@app.get("/api/admin/lounges")
def admin_list_lounges():
    admin, err = require_admin()
    if err: return err
    counts = dict(db.session.query(LoungeMember.lounge_id, func.count(LoungeMember.id))
                  .group_by(LoungeMember.lounge_id).all())
    lounges = Lounge.query.order_by(Lounge.created_at.desc()).limit(200).all()
    return jsonify([{
        "id": l.id, "name": l.name, "description": l.description, "privacy": l.privacy,
        "owner": l.owner.username if l.owner else None, "members": counts.get(l.id, 0),
        "created_at": _iso(l.created_at),
    } for l in lounges])


@app.delete("/api/admin/lounges/<int:lounge_id>")
def admin_delete_lounge(lounge_id):
    admin, err = require_admin()
    if err: return err
    lounge = Lounge.query.get(lounge_id)
    if not lounge:
        return jsonify({"error": "Lounge not found"}), 404
    db.session.delete(lounge)  # cascades to channels, members, messages, reactions
    db.session.commit()
    return jsonify({"ok": True})


@app.get("/api/admin/circuits")
def admin_list_circuits():
    admin, err = require_admin()
    if err: return err
    counts = dict(db.session.query(CircuitPost.circuit_id, func.count(CircuitPost.id))
                  .group_by(CircuitPost.circuit_id).all())
    return jsonify([{
        "id": c.id, "title": c.title, "host": c.host_school, "code": c.code,
        "owner": c.owner.username if c.owner else None, "posts": counts.get(c.id, 0),
    } for c in Circuit.query.order_by(Circuit.id.desc()).limit(200).all()])


@app.delete("/api/admin/circuits/<int:circuit_id>")
def admin_delete_circuit(circuit_id):
    admin, err = require_admin()
    if err: return err
    circuit = Circuit.query.get(circuit_id)
    if not circuit:
        return jsonify({"error": "Circuit not found"}), 404
    post_ids = [p.id for p in CircuitPost.query.filter_by(circuit_id=circuit.id).all()]
    if post_ids:
        db.session.query(post_likes).filter(post_likes.c.post_id.in_(post_ids)).delete(synchronize_session=False)
        CircuitPost.query.filter(CircuitPost.id.in_(post_ids)).delete(synchronize_session=False)
    db.session.delete(circuit)
    db.session.commit()
    return jsonify({"ok": True})


@app.get("/api/admin/posts")
def admin_list_posts():
    admin, err = require_admin()
    if err: return err
    posts = CircuitPost.query.order_by(CircuitPost.created_at.desc()).limit(100).all()
    return jsonify([{
        "id": p.id, "text": p.text, "has_image": bool(p.image),
        "author": p.author.username if p.author else None,
        "circuit": p.circuit.title if p.circuit else None, "created_at": _iso(p.created_at),
        "likes": len(p.likes),
    } for p in posts])


@app.delete("/api/admin/posts/<int:post_id>")
def admin_delete_post(post_id):
    admin, err = require_admin()
    if err: return err
    post = CircuitPost.query.get(post_id)
    if not post:
        return jsonify({"error": "Post not found"}), 404
    db.session.query(post_likes).filter(post_likes.c.post_id == post_id).delete(synchronize_session=False)
    db.session.delete(post)
    db.session.commit()
    return jsonify({"ok": True})


@app.get("/api/admin/articles")
def admin_list_articles():
    admin, err = require_admin()
    if err: return err
    return jsonify([{
        "id": a.id, "title": a.title, "author": a.author.username if a.author else None,
        "created_at": _iso(a.created_at),
    } for a in Article.query.order_by(Article.created_at.desc()).limit(200).all()])


@app.delete("/api/admin/articles/<int:article_id>")
def admin_delete_article(article_id):
    admin, err = require_admin()
    if err: return err
    article = Article.query.get(article_id)
    if not article:
        return jsonify({"error": "Article not found"}), 404
    db.session.delete(article)
    db.session.commit()
    return jsonify({"ok": True})


@app.post("/api/admin/broadcast")
@rate_limit(10, 3600)
def admin_broadcast():
    """Send a DM from the ANNOUNCEMENTS account to every active user."""
    admin, err = require_admin()
    if err: return err
    message = str((request.get_json(force=True, silent=True) or {}).get("message") or "").strip()
    if not message:
        return jsonify({"error": "Message is required"}), 400
    if len(message) > 4000:
        return jsonify({"error": "Message is too long (max 4000 characters)."}), 400
    system_user = User.query.filter_by(username="ANNOUNCEMENTS").first()
    if not system_user:
        return jsonify({"error": "ANNOUNCEMENTS account is missing."}), 500
    recipients = User.query.filter(User.username.isnot(None), User.username.notin_(SYSTEM_USERNAMES),
                                   User.is_banned.is_(False)).all()
    dms = [DM(sender_id=system_user.id, receiver_id=u.id, message=message) for u in recipients]
    db.session.add_all(dms)
    db.session.commit()
    for dm in dms:  # same payload shape as /api/dm/send
        socketio.emit("dm_received", {"id": dm.id, "from": system_user.username, "message": dm.message,
                                      "image": None, "created_at": dm.created_at.isoformat() + "Z"},
                      room=f"user_{dm.receiver_id}")
    return jsonify({"ok": True, "sent": len(recipients)})


# -------------------------------------------------
# Web Push notifications
#   Browsers / installed PWAs / the Android app subscribe via /api/push/subscribe.
#   Whenever a DM or a Notification row is committed, a push is sent to the receiver
#   (in a background thread, so requests are never slowed down).
#   VAPID keys come from VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY, or are generated once and
#   stored in the database.
# -------------------------------------------------
import json as _json
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import event as _sa_event
from sqlalchemy.orm import Session as _SASession, object_session

try:
    from pywebpush import webpush, WebPushException
except ImportError:  # push simply stays disabled if the package is missing
    webpush = None
    WebPushException = Exception

VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:support@prototype-neo.com")
_push_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webpush")
_vapid_keys = {}
_B64URL_RE = re.compile(r'^[A-Za-z0-9_-]+=*$')


class PushSubscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    endpoint = db.Column(db.String(1000), unique=True, nullable=False)
    p256dh = db.Column(db.String(255), nullable=False)
    auth = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def load_vapid_keys():
    """Load VAPID keys from env, else from the DB, else generate and store them (once)."""
    priv, pub = os.environ.get("VAPID_PRIVATE_KEY", ""), os.environ.get("VAPID_PUBLIC_KEY", "")
    if not (priv and pub):
        a_priv, a_pub = StoredAsset.query.get("vapid_private"), StoredAsset.query.get("vapid_public")
        if a_priv and a_pub:
            priv, pub = a_priv.webp_base64, a_pub.webp_base64
        else:
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives import serialization
            key = ec.generate_private_key(ec.SECP256R1())
            priv = _b64url(key.private_numbers().private_value.to_bytes(32, "big"))
            pub = _b64url(key.public_key().public_bytes(serialization.Encoding.X962,
                                                         serialization.PublicFormat.UncompressedPoint))
            db.session.add_all([StoredAsset(key="vapid_private", webp_base64=priv),
                                StoredAsset(key="vapid_public", webp_base64=pub)])
            db.session.commit()
            print("Generated VAPID keys for web push.")
    _vapid_keys.update(private=priv, public=pub)


def _push_text(value, limit=140):
    text = _html.unescape(value or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _build_dm_push(dm):
    sender = dm.sender
    name = _push_text(sender.full_name or sender.username, 60) if sender else "APEX"
    body = _push_text(dm.message) if dm.message else "Sent you an image."
    return dm.receiver_id, {"title": name, "body": body, "tag": f"dm-{dm.sender_id}",
                            "url": f"/?dm={sender.username}" if sender else "/"}


_NOTIF_TEXT = {
    "new_follower": "{actor} started following you.",
    "new_like": "{actor} liked your post.",
    "lounge_invite": "{actor} invited you to a lounge.",
    "lounge_access_request": "{actor} wants to join your lounge.",
    "lounge_access_accepted": "{actor} accepted your lounge request.",
}


def _build_notification_push(n):
    actor = n.actor.username if n.actor and n.actor.username else "Someone"
    body = _NOTIF_TEXT.get(n.event_type, "You have a new notification.").format(actor=actor)
    return n.user_id, {"title": "APEX", "body": body, "tag": f"notif-{n.id}", "url": "/?view=notifications"}


def _deliver_push(kind, obj_id):
    """Runs in a worker thread: build the payload and send it to every device of the receiver."""
    if webpush is None:
        return
    with app.app_context():
        try:
            obj = (DM if kind == "dm" else Notification).query.get(obj_id)
            if not obj:
                return
            if kind == "dm":
                if obj.sender_id == obj.receiver_id:
                    return
                user_id, payload = _build_dm_push(obj)
            else:
                user_id, payload = _build_notification_push(obj)
            receiver = User.query.get(user_id)
            if not receiver or receiver.is_banned or receiver.username is None:
                return
            send_push_to_user(user_id, payload)
        except Exception as e:
            print(f"[push] failed to send {kind} #{obj_id}: {type(e).__name__}")
        finally:
            db.session.remove()


def send_push_to_user(user_id, payload):
    """Send one payload to all of a user's subscriptions. Returns the number delivered.
    Must be called inside an app context."""
    if webpush is None or not _vapid_keys.get("private"):
        return 0
    data = _json.dumps(payload)
    delivered = 0
    for sub in PushSubscription.query.filter_by(user_id=user_id).all():
        try:
            webpush(subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                    data=data, vapid_private_key=_vapid_keys["private"],
                    vapid_claims={"sub": VAPID_SUBJECT}, ttl=86400, timeout=10)
            delivered += 1
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):  # the browser dropped this subscription
                db.session.delete(sub)
                db.session.commit()
            else:
                print(f"[push] delivery error {status}")
        except Exception as e:
            print(f"[push] delivery error {type(e).__name__}")
    return delivered


# Queue pushes when DM / Notification rows are inserted, send them only after the commit succeeds.
@_sa_event.listens_for(DM, "after_insert")
def _queue_dm_push(mapper, connection, target):
    sess = object_session(target)
    if sess is not None:
        sess.info.setdefault("pending_push", []).append(("dm", target.id))


@_sa_event.listens_for(Notification, "after_insert")
def _queue_notification_push(mapper, connection, target):
    sess = object_session(target)
    if sess is not None:
        sess.info.setdefault("pending_push", []).append(("notification", target.id))


@_sa_event.listens_for(_SASession, "after_commit")
def _flush_push_queue(session):
    for kind, obj_id in session.info.pop("pending_push", []):
        _push_executor.submit(_deliver_push, kind, obj_id)


@_sa_event.listens_for(_SASession, "after_soft_rollback")
def _drop_push_queue(session, previous_transaction):
    session.info.pop("pending_push", None)


@app.get("/api/push/public-key")
def push_public_key():
    if webpush is None or not _vapid_keys.get("public"):
        return jsonify({"error": "Push notifications are not available on this server."}), 503
    return jsonify({"key": _vapid_keys["public"]})


@app.post("/api/push/subscribe")
@rate_limit(30, 3600)
def push_subscribe():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    endpoint = str(data.get("endpoint") or "")
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    p256dh, auth_key = str(keys.get("p256dh") or ""), str(keys.get("auth") or "")
    if not endpoint.startswith("https://") or len(endpoint) > 1000:
        return jsonify({"error": "Invalid subscription endpoint."}), 400
    if not (_B64URL_RE.match(p256dh) and _B64URL_RE.match(auth_key)) or len(p256dh) > 255 or len(auth_key) > 255:
        return jsonify({"error": "Invalid subscription keys."}), 400

    sub = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if sub:  # same browser, maybe a different account now
        sub.user_id, sub.p256dh, sub.auth = user.id, p256dh, auth_key
    else:
        db.session.add(PushSubscription(user_id=user.id, endpoint=endpoint, p256dh=p256dh, auth=auth_key))
    db.session.commit()
    return jsonify({"ok": True})


@app.post("/api/push/unsubscribe")
def push_unsubscribe():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    endpoint = str((request.get_json(force=True, silent=True) or {}).get("endpoint") or "")
    PushSubscription.query.filter_by(endpoint=endpoint, user_id=user.id).delete()
    db.session.commit()
    return jsonify({"ok": True})


@app.post("/api/push/test")
@rate_limit(10, 3600)
def push_test():
    user = auth_user()
    if not user:
        return jsonify({"error": "unauthorized"}), 401
    sent = send_push_to_user(user.id, {"title": "APEX", "body": "Notifications are working.", "tag": "test", "url": "/"})
    return jsonify({"ok": True, "delivered": sent})


# ---------------- PWA files (installable web app + service worker) ----------------
APP_NAME_SHORT = os.environ.get("APP_NAME", "APEX")
ANDROID_PACKAGE = os.environ.get("ANDROID_PACKAGE", "org.horizonatlas.apex")
# SHA-256 fingerprint of the Android app's signing certificate (android/apex-release.p12).
# Lets the Android app open full-screen without a browser address bar.
ANDROID_CERT_SHA256 = os.environ.get(
    "ANDROID_CERT_SHA256",
    "1D:24:C7:BB:53:92:A8:E0:1F:08:79:58:C0:36:97:71:33:00:27:7C:BA:F9:ED:64:72:22:41:CB:9F:34:3D:AF")
_icon_cache = {}


@app.get("/sw.js")
def service_worker():
    resp = make_response(send_from_directory(BASE_DIR, "sw.js"))
    resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.get("/manifest.webmanifest")
def web_manifest():
    resp = make_response(_json.dumps({
        "name": APP_NAME_SHORT, "short_name": APP_NAME_SHORT, "id": "/", "start_url": "/", "scope": "/",
        "display": "standalone", "background_color": "#f8f9fa", "theme_color": "#ffffff",
        "description": "Make friends from around the world.",
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }))
    resp.headers["Content-Type"] = "application/manifest+json"
    return resp


@app.get("/icons/<name>.png")
def app_icon(name):
    sizes = {"icon-192": (192, 0.92), "icon-512": (512, 0.92), "maskable-512": (512, 0.62), "apple-touch-icon": (180, 0.86)}
    if name not in sizes:
        return jsonify({"error": "not found"}), 404
    if name not in _icon_cache:
        size, fill = sizes[name]
        with Image.open(os.path.join(BASE_DIR, "apex.png")) as src:
            src = src.convert("RGBA")
            src.thumbnail((int(size * fill), int(size * fill)), Image.LANCZOS)
            canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
            canvas.alpha_composite(src, ((size - src.width) // 2, (size - src.height) // 2))
            buf = io.BytesIO()
            canvas.convert("RGB").save(buf, format="PNG", optimize=True)
            _icon_cache[name] = buf.getvalue()
    resp = make_response(_icon_cache[name])
    resp.headers["Content-Type"] = "image/png"
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/assets/<name>.png")
def transparent_logo(name):
    """Logos with the white background removed, for the dark theme."""
    sources = {"logo-text": "apex2.png", "logo-mark": "apex.png"}
    if name not in sources:
        return jsonify({"error": "not found"}), 404
    key = "clear-" + name
    if key not in _icon_cache:
        with Image.open(os.path.join(BASE_DIR, sources[name])) as src:
            img = src.convert("RGBA")
        if img.getextrema()[3][0] == 255:  # no transparency yet: key out the white background
            px = img.load()
            for y in range(img.height):
                for x in range(img.width):
                    r, g, b, a = px[x, y]
                    lightness = min(r, g, b)
                    if lightness >= 245:
                        px[x, y] = (r, g, b, 0)
                    elif lightness >= 200:  # soften the anti-aliased edge
                        px[x, y] = (r, g, b, int(a * (245 - lightness) / 45))
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        img.thumbnail((640, 640), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        _icon_cache[key] = buf.getvalue()
    resp = make_response(_icon_cache[key])
    resp.headers["Content-Type"] = "image/png"
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/.well-known/assetlinks.json")
def asset_links():
    return jsonify([{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {"namespace": "android_app", "package_name": ANDROID_PACKAGE,
                   "sha256_cert_fingerprints": [ANDROID_CERT_SHA256]},
    }])


# -------------------------------------------------
def migrate_legacy_schema():
    """Rename the legacy user.school column to user.nationality on existing databases (keeps data)."""
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    if "user" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("user")}
    if "school" in cols and "nationality" not in cols:
        with db.engine.begin() as conn:
            conn.execute(text('ALTER TABLE "user" RENAME COLUMN school TO nationality'))


def init_app_data():
    """Create tables and seed required rows. Runs at import time so it also works under gunicorn."""
    with app.app_context():
        migrate_legacy_schema()
        db.create_all()
        seed_assets()

        # System user used for announcements
        if not User.query.filter_by(username="ANNOUNCEMENTS").first():
            print("Creating ANNOUNCEMENTS user...")
            system_user = User(username="ANNOUNCEMENTS", full_name="ANNOUNCEMENTS", email="apex@announcements",
                               rank="admin")
            db.session.add(system_user)
            db.session.commit()

        bootstrap_admin()
        load_vapid_keys()


def bootstrap_admin():
    """Create the owner/admin account from environment variables on first start.

    BOOTSTRAP_ADMIN_USERNAME / BOOTSTRAP_ADMIN_PASSWORD are required; NAME, EMAIL and
    NATIONALITY are optional. Nothing happens if the username already exists, so the
    password is only used once and changing it later in the app is safe.
    """
    username = os.environ.get("BOOTSTRAP_ADMIN_USERNAME", "").strip()
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if not username or not password:
        return
    if User.query.filter_by(username=username).first():
        return
    email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower() or f"{username.lower()}@admin.local"
    if User.query.filter_by(email=email).first():
        print(f"[WARN] bootstrap admin skipped: email {email} already in use")
        return
    db.session.add(User(
        email=email,
        password_hash=generate_password_hash(password),
        full_name=os.environ.get("BOOTSTRAP_ADMIN_NAME", username).strip(),
        username=username,
        nationality=os.environ.get("BOOTSTRAP_ADMIN_NATIONALITY", "").strip() or None,
        account_type="personal",
        rank="admin",
        provider="local",
    ))
    db.session.commit()
    print(f"Created admin account '{username}'.")


init_app_data()

# -------------------------------------------------
# Entrypoints
#   Production : gunicorn -k gthread -w 1 --threads 100 -b 0.0.0.0:$PORT server:app
#   Local dev  : APP_ENV=development python server.py
# -------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
