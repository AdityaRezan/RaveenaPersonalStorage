import os
import zipfile
from datetime import datetime
from io import BytesIO
from flask import Flask, request, jsonify, send_file, render_template, redirect, url_for
from flask_mail import Mail, Message
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from cryptography.fernet import Fernet
from itsdangerous import URLSafeTimedSerializer
import qrcode
import boto3

# --- CONFIGURATION ---
# Local storage and database
STORAGE_DIR = "storage"
DB_FILE = "file_storage_app.db"

# AWS S3 configuration (optional: use local storage only if not set up)
AWS_BUCKET_NAME = "your-s3-bucket-name"
AWS_REGION = "your-region"
AWS_ACCESS_KEY = "your-access-key"
AWS_SECRET_KEY = "your-secret-key"

# Encryption key
ENCRYPTION_KEY = Fernet.generate_key()
cipher = Fernet(ENCRYPTION_KEY)

# Ensure storage directory exists
os.makedirs(STORAGE_DIR, exist_ok=True)

# Database setup
Base = declarative_base()
engine = create_engine(f"sqlite:///{DB_FILE}")
Session = sessionmaker(bind=engine)
session = Session()

# Flask app
app = Flask(__name__)

# Secret key for token generation
SECRET_KEY = "your_secret_key"
serializer = URLSafeTimedSerializer(SECRET_KEY)

# Flask-Mail configuration
app.config["MAIL_SERVER"] = "smtp.gmail.com"
app.config["MAIL_PORT"] = 587
app.config["MAIL_USE_TLS"] = True
app.config["MAIL_USERNAME"] = "your_email@gmail.com"  # Replace with your email
app.config["MAIL_PASSWORD"] = "your_password"         # Replace with your email password
mail = Mail(app)

# AWS S3 client
s3_client = boto3.client(
    "s3",
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
)

# --- DATABASE MODEL ---
class FileMetadata(Base):
    __tablename__ = "files"
    id = Column(Integer, primary_key=True)
    file_name = Column(String, nullable=False)
    file_type = Column(String, nullable=False)
    file_size = Column(Float, nullable=False)
    file_path = Column(String, nullable=False)
    upload_time = Column(DateTime, default=datetime.utcnow)
    checksum = Column(String, nullable=False)
    tags = Column(String, nullable=True)

Base.metadata.create_all(engine)

# --- HELPER FUNCTIONS ---
def compress_and_encrypt(file_path):
    """Compress and encrypt a file."""
    compressed_path = f"{file_path}.zip"
    with zipfile.ZipFile(compressed_path, "w") as zipf:
        zipf.write(file_path, os.path.basename(file_path))
    with open(compressed_path, "rb") as file:
        encrypted_data = cipher.encrypt(file.read())
    with open(compressed_path, "wb") as file:
        file.write(encrypted_data)
    return compressed_path

def upload_to_s3(file_path, file_name):
    """Upload a file to S3."""
    with open(file_path, "rb") as file:
        s3_client.upload_fileobj(file, AWS_BUCKET_NAME, file_name)

def calculate_checksum(file_path):
    """Calculate file checksum."""
    import hashlib
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

def validate_video_format(file_name):
    """Validate video file format."""
    allowed_extensions = {'mp4', 'mov', 'avi', 'mkv'}
    return file_name.split('.')[-1].lower() in allowed_extensions

# --- ROUTES ---
@app.route("/")
def index():
    """Home page to upload and view files."""
    files = session.query(FileMetadata).all()
    return render_template("index.html", files=files)

@app.route("/upload", methods=["POST"])
def upload_file():
    """Upload a file."""
    file = request.files.get("file")
    tags = request.form.get("tags", "")
    if not file:
        return jsonify({"error": "No file provided"}), 400

    file_name = file.filename
    file_type = file.content_type
    file_size = len(file.read()) / (1024 * 1024)  # Size in MB

    # Video format validation
    if file_type.startswith("video/") and not validate_video_format(file_name):
        return jsonify({"error": "Unsupported video format. Use MP4, MOV, AVI, or MKV."}), 400

    file_path = os.path.join(STORAGE_DIR, file_name)
    file.save(file_path)

    # Compress, encrypt, and upload
    compressed_path = compress_and_encrypt(file_path)
    checksum = calculate_checksum(compressed_path)
    upload_to_s3(compressed_path, file_name)

    # Store metadata
    metadata = FileMetadata(
        file_name=file_name,
        file_type=file_type,
        file_size=file_size,
        file_path=file_path,
        checksum=checksum,
        tags=tags,
    )
    session.add(metadata)
    session.commit()

    os.remove(compressed_path)  # Remove local compressed file
    return redirect(url_for("index"))

@app.route("/download/<int:file_id>")
def download_file(file_id):
    """Download a file by ID."""
    file = session.query(FileMetadata).get(file_id)
    if not file:
        return jsonify({"error": "File not found"}), 404

    # Retrieve file from S3
    download_path = os.path.join(STORAGE_DIR, file.file_name)
    s3_client.download_file(AWS_BUCKET_NAME, file.file_name, download_path)

    # Decrypt and decompress
    with open(download_path, "rb") as f:
        encrypted_data = f.read()
    decrypted_data = cipher.decrypt(encrypted_data)
    with open(download_path, "wb") as f:
        f.write(decrypted_data)

    return send_file(download_path, as_attachment=True)

@app.route("/share/<int:file_id>", methods=["POST"])
def share_file(file_id):
    """Generate a secure shareable link."""
    file = session.query(FileMetadata).get(file_id)
    if not file:
        return jsonify({"error": "File not found"}), 404

    # Generate a time-limited secure token
    token = serializer.dumps({"file_id": file_id}, salt="file-share")
    share_url = f"http://localhost:5000/download_shared/{token}"

    return jsonify({"share_url": share_url})

@app.route("/download_shared/<token>")
def download_shared(token):
    """Download a file via shareable link."""
    try:
        # Verify the token (valid for 24 hours)
        data = serializer.loads(token, salt="file-share", max_age=86400)
        file_id = data.get("file_id")
        return download_file(file_id)
    except:
        return jsonify({"error": "Invalid or expired link"}), 400

@app.route("/share_qr/<int:file_id>")
def generate_qr(file_id):
    """Generate a QR code for the shareable link."""
    file = session.query(FileMetadata).get(file_id)
    if not file:
        return jsonify({"error": "File not found"}), 404

    # Generate a shareable link
    token = serializer.dumps({"file_id": file_id}, salt="file-share")
    share_url = f"http://localhost:5000/download_shared/{token}"

    # Create QR code
    qr = qrcode.make(share_url)
    qr_io = BytesIO()
    qr.save(qr_io, format="PNG")
    qr_io.seek(0)

    return send_file(qr_io, mimetype="image/png")

# --- MAIN ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
