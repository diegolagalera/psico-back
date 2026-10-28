import os
from datetime import timedelta
from urllib.parse import quote_plus
from dotenv import load_dotenv

load_dotenv()


class Config:
    DB_HOST = os.getenv("DB_HOST") or os.getenv("MYSQLHOST", "localhost")
    DB_PORT = os.getenv("DB_PORT") or os.getenv("MYSQLPORT", "3306")
    DB_NAME = os.getenv("DB_NAME") or os.getenv("MYSQLDATABASE") or os.getenv("MYSQL_DATABASE", "psicologia")
    DB_USER = os.getenv("DB_USER") or os.getenv("MYSQLUSER", "psicologiauser")
    DB_PASSWORD = os.getenv("DB_PASSWORD") or os.getenv("MYSQLPASSWORD") or os.getenv("MYSQL_ROOT_PASSWORD", "Psicologiapassword123.")

    encoded_password = quote_plus(DB_PASSWORD)

    _raw_db_url = (
        os.getenv("DATABASE_URL")
        or os.getenv("MYSQL_URL")
        or f"mysql+pymysql://{DB_USER}:{encoded_password}@{DB_HOST}:{DB_PORT}/{DB_NAME}?charset=utf8mb4"
    )
    if _raw_db_url.startswith("mysql://"):
        _raw_db_url = _raw_db_url.replace("mysql://", "mysql+pymysql://", 1)

    SQLALCHEMY_DATABASE_URI = _raw_db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
    }

    JWT_SECRET_KEY = os.getenv(
        "JWT_SECRET_KEY", "vuelve-a-ti-super-secret-jwt-key-2026-psicologia"
    )
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=12)

    ADMIN_DEFAULT_EMAIL = os.getenv("ADMIN_DEFAULT_EMAIL", "admin@vuelveati.com")
    ADMIN_DEFAULT_PASSWORD = os.getenv("ADMIN_DEFAULT_PASSWORD", "Admin1234!")
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")

