import os
from datetime import timedelta
from urllib.parse import quote_plus
from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str = "") -> str:
    val = os.getenv(key)
    if val is None or not str(val).strip():
        return default
    return str(val).strip()


class Config:
    DB_HOST = _env("DB_HOST") or _env("MYSQLHOST", "localhost")
    _raw_port = _env("DB_PORT") or _env("MYSQLPORT", "3306")
    DB_PORT = _raw_port if _raw_port.isdigit() else "3306"
    DB_NAME = _env("DB_NAME") or _env("MYSQLDATABASE") or _env("MYSQL_DATABASE", "psicologia")
    DB_USER = _env("DB_USER") or _env("MYSQLUSER", "psicologiauser")
    DB_PASSWORD = _env("DB_PASSWORD") or _env("MYSQLPASSWORD") or _env("MYSQL_ROOT_PASSWORD", "Psicologiapassword123.")

    encoded_password = quote_plus(DB_PASSWORD)

    _raw_db_url = (
        _env("DATABASE_URL")
        or _env("MYSQL_URL")
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

    JWT_SECRET_KEY = _env(
        "JWT_SECRET_KEY", "vuelve-a-ti-super-secret-jwt-key-2026-psicologia"
    )
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=12)

    ADMIN_DEFAULT_EMAIL = _env("ADMIN_DEFAULT_EMAIL", "admin@vuelveati.com")
    ADMIN_DEFAULT_PASSWORD = _env("ADMIN_DEFAULT_PASSWORD", "Admin1234!")
    CORS_ORIGINS = _env("CORS_ORIGINS", "*")


