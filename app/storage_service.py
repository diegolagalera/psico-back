import hashlib
import hmac
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from flask import current_app

try:
    import certifi
except ImportError:
    certifi = None


def _get_ssl_context() -> ssl.SSLContext:
    """
    En macOS, Python oficial no carga los certificados raíz del sistema por defecto
    para urllib.request. Usamos el bundle CA actualizado de `certifi`.
    """
    if certifi is not None:
        try:
            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            pass
    ctx = ssl.create_default_context()
    return ctx

from app.models import SystemConfig, db

PROTECTED_STORAGE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "protected_media")
)


def ensure_protected_storage_dir() -> str:
    os.makedirs(PROTECTED_STORAGE_DIR, exist_ok=True)
    return PROTECTED_STORAGE_DIR


def sanitize_filename(filename: str) -> str:
    filename = os.path.basename(filename or "archivo")
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    return cleaned or "archivo"


def get_storage_config(override: dict | None = None) -> dict:
    cfg = SystemConfig.query.first()
    if not cfg:
        cfg = SystemConfig()
        db.session.add(cfg)
        db.session.commit()

    base = cfg.to_dict()
    if override:
        for k in (
            "s3_enabled",
            "s3_provider",
            "s3_endpoint_url",
            "s3_region",
            "s3_bucket_name",
            "s3_access_key_id",
            "s3_secret_access_key",
            "presigned_url_expiration",
        ):
            if k in override and override[k] is not None:
                base[k] = override[k]
    return base


def is_s3_ready(cfg: dict | None = None) -> bool:
    if cfg is None:
        cfg = get_storage_config()
    return bool(
        cfg.get("s3_enabled")
        and (cfg.get("s3_endpoint_url") or "").strip()
        and (cfg.get("s3_bucket_name") or "").strip()
        and (cfg.get("s3_access_key_id") or "").strip()
        and (cfg.get("s3_secret_access_key") or "").strip()
    )


# ==========================================================
# FIRMA NATIVA AWS S3 / CLOUDFLARE R2 (SigV4 - s3v4)
# ==========================================================
def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _get_signature_key(
    secret_key: str, date_stamp: str, region_name: str, service_name: str = "s3"
) -> bytes:
    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region_name)
    k_service = _sign(k_region, service_name)
    k_signing = _sign(k_service, "aws4_request")
    return k_signing


def generate_s3_presigned_url(
    method: str,
    object_key: str,
    expires_in: int = 1800,
    response_content_disposition: str | None = None,
    response_content_type: str | None = None,
    cfg: dict | None = None,
) -> str:
    """
    Genera una URL firmada con AWS Signature Version 4 (s3v4) compatible al 100%
    con Cloudflare R2, AWS S3, DigitalOcean Spaces y Backblaze B2.
    """
    if cfg is None:
        cfg = get_storage_config()

    endpoint_url = (cfg.get("s3_endpoint_url") or "").strip().rstrip("/")
    bucket = (cfg.get("s3_bucket_name") or "").strip()
    access_key = (cfg.get("s3_access_key_id") or "").strip()
    secret_key = (cfg.get("s3_secret_access_key") or "").strip()
    region = (cfg.get("s3_region") or "auto").strip() or "auto"

    if not endpoint_url.startswith("http"):
        endpoint_url = "https://" + endpoint_url

    parsed_endpoint = urllib.parse.urlparse(endpoint_url)
    host = parsed_endpoint.netloc
    base_path = parsed_endpoint.path.rstrip("/")
    if bucket and base_path.endswith(f"/{bucket}"):
        base_path = base_path[: -len(f"/{bucket}")]

    clean_key = object_key.lstrip("/")
    encoded_key = urllib.parse.quote(clean_key, safe="/-_.~")
    canonical_uri = f"{base_path}/{urllib.parse.quote(bucket, safe='-_.~')}/{encoded_key}"
    if not canonical_uri.startswith("/"):
        canonical_uri = "/" + canonical_uri

    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"

    query_params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{credential_scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(int(expires_in)),
        "X-Amz-SignedHeaders": "host",
    }

    if response_content_disposition:
        query_params["response-content-disposition"] = response_content_disposition
    if response_content_type:
        query_params["response-content-type"] = response_content_type

    sorted_params = sorted(query_params.items(), key=lambda kv: kv[0])
    canonical_querystring = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
        for k, v in sorted_params
    )

    canonical_headers = f"host:{host}\n"
    signed_headers = "host"
    payload_hash = "UNSIGNED-PAYLOAD"

    canonical_request = "\n".join(
        [
            method.upper(),
            canonical_uri,
            canonical_querystring,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    signing_key = _get_signature_key(secret_key, date_stamp, region, "s3")
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    final_url = (
        f"{parsed_endpoint.scheme}://{host}{canonical_uri}"
        f"?{canonical_querystring}&X-Amz-Signature={signature}"
    )
    return final_url


def upload_file_to_s3(
    file_bytes: bytes,
    object_key: str,
    content_type: str = "application/octet-stream",
    cfg: dict | None = None,
) -> tuple[bool, str]:
    """
    Sube bytes directamente al bucket S3 / Cloudflare R2 utilizando una URL
    firmada PUT s3v4 (sin problemas de CORS en el navegador).
    """
    try:
        put_url = generate_s3_presigned_url(
            method="PUT",
            object_key=object_key,
            expires_in=3600,
            cfg=cfg,
        )
        req = urllib.request.Request(
            put_url,
            data=file_bytes,
            method="PUT",
            headers={
                "Content-Type": content_type or "application/octet-stream",
                "Content-Length": str(len(file_bytes)),
                "User-Agent": "VuelveATi-Storage/1.0",
            },
        )
        with urllib.request.urlopen(
            req, timeout=120, context=_get_ssl_context()
        ) as resp:
            status = resp.status
            if 200 <= status < 300:
                return True, f"Archivo subido correctamente a S3/R2 ({object_key})"
            return False, f"Respuesta inesperada de S3/R2: HTTP {status}"
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="ignore")[:500]
        except Exception:
            pass
        return False, f"Error HTTP {exc.code} al subir a S3/R2: {exc.reason}. {body}"
    except Exception as exc:
        return False, f"Error de conexión con S3/R2: {exc}"


def delete_file_from_s3(
    object_key: str, cfg: dict | None = None
) -> tuple[bool, str]:
    try:
        del_url = generate_s3_presigned_url(
            method="DELETE",
            object_key=object_key,
            expires_in=600,
            cfg=cfg,
        )
        req = urllib.request.Request(
            del_url,
            method="DELETE",
            headers={"User-Agent": "VuelveATi-Storage/1.0"},
        )
        with urllib.request.urlopen(
            req, timeout=30, context=_get_ssl_context()
        ) as resp:
            return True, f"Objeto eliminado de S3/R2 (HTTP {resp.status})"
    except Exception as exc:
        return False, f"No se pudo eliminar de S3/R2: {exc}"


def test_s3_connection(override_cfg: dict | None = None) -> tuple[bool, str, str]:
    """
    Realiza una prueba real de escritura, lectura firmada y borrado en el bucket
    Cloudflare R2 / AWS S3 y devuelve (exito, mensaje, log_detallado).
    """
    cfg = get_storage_config(override_cfg)
    logs = []
    logs.append("=== DIAGNÓSTICO DE CONEXIÓN S3 / CLOUDFLARE R2 ===")
    endpoint = (cfg.get("s3_endpoint_url") or "").strip()
    bucket = (cfg.get("s3_bucket_name") or "").strip()
    region = (cfg.get("s3_region") or "auto").strip()
    access_key = (cfg.get("s3_access_key_id") or "").strip()
    secret_key = (cfg.get("s3_secret_access_key") or "").strip()

    logs.append(f"• Proveedor: {cfg.get('s3_provider', 'cloudflare_r2')}")
    logs.append(f"• Endpoint S3: {endpoint or '(vacío)'}")
    logs.append(f"• Bucket: {bucket or '(vacío)'}")
    logs.append(f"• Región: {region}")
    logs.append(
        f"• Access Key ID: {access_key[:6] + '...' if len(access_key) > 6 else '(no configurado)'}"
    )

    if not endpoint or not bucket or not access_key or not secret_key:
        logs.append(
            "[ERROR] Faltan campos obligatorios (Endpoint URL, Bucket, Access Key ID o Secret Access Key)."
        )
        return (
            False,
            "Completa todos los campos de conexión del bucket S3 / Cloudflare R2.",
            "\n".join(logs),
        )

    test_key = "_diagnostics/vuelve_a_ti_r2_test.txt"
    test_payload = (
        f"Verificacion Vuelve a Ti S3/R2 OK - {datetime.now(timezone.utc).isoformat()}".encode(
            "utf-8"
        )
    )

    logs.append(f"\n1. Probando subida PUT firmada (s3v4) -> '{test_key}'...")
    ok_put, msg_put = upload_file_to_s3(
        test_payload, test_key, content_type="text/plain", cfg=cfg
    )
    logs.append(f"   Resultado PUT: {msg_put}")
    if not ok_put:
        return (
            False,
            "No se pudo subir el archivo de prueba al bucket. Revisa el Endpoint y las claves S3.",
            "\n".join(logs),
        )

    logs.append("\n2. Probando lectura mediante Presigned GET URL temporal...")
    try:
        get_url = generate_s3_presigned_url(
            method="GET", object_key=test_key, expires_in=120, cfg=cfg
        )
        req = urllib.request.Request(
            get_url,
            method="GET",
            headers={"User-Agent": "VuelveATi-Storage/1.0"},
        )
        with urllib.request.urlopen(
            req, timeout=20, context=_get_ssl_context()
        ) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
            logs.append(
                f"   Resultado GET: HTTP {resp.status} OK ({len(content)} bytes leídos)"
            )
    except Exception as exc:
        logs.append(f"   [ERROR GET] Falló la lectura de la URL firmada: {exc}")
        return (
            False,
            "La subida funcionó pero falló la lectura de la URL firmada.",
            "\n".join(logs),
        )

    logs.append("\n3. Limpiando archivo temporal de prueba (DELETE)...")
    ok_del, msg_del = delete_file_from_s3(test_key, cfg=cfg)
    logs.append(f"   Resultado DELETE: {msg_del}")
    logs.append(
        "\n✓ CONEXIÓN S3 / CLOUDFLARE R2 VERIFICADA CORRECTAMENTE. Listo para subir vídeos y recursos protegidos."
    )
    return (
        True,
        f"Conexión exitosa con el bucket '{bucket}' en Cloudflare R2 / S3.",
        "\n".join(logs),
    )


# ==========================================================
# TOKENS FIRMADOS PARA STREAMING / DESCARGA PROTEGIDA LOCAL
# ==========================================================
def create_media_access_token(
    user_id: int,
    object_key: str,
    mime_type: str = "video/mp4",
    disposition: str = "inline",
    filename: str = "media",
    expires_in: int = 1800,
) -> str:
    secret = current_app.config.get("JWT_SECRET_KEY", "vuelve-a-ti-secret")
    exp = int(time.time()) + int(expires_in)
    payload = {
        "u": int(user_id),
        "k": object_key,
        "m": mime_type,
        "d": disposition,
        "f": filename,
        "e": exp,
    }
    raw_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    b64_payload = urllib.parse.quote(raw_json, safe="")
    sig = hmac.new(
        secret.encode("utf-8"), b64_payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{b64_payload}.{sig}"


def verify_media_access_token(token: str) -> dict | None:
    try:
        secret = current_app.config.get("JWT_SECRET_KEY", "vuelve-a-ti-secret")
        parts = token.rsplit(".", 1)
        if len(parts) != 2:
            return None
        b64_payload, sig = parts
        expected_sig = hmac.new(
            secret.encode("utf-8"), b64_payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        raw_json = urllib.parse.unquote(b64_payload)
        payload = json.loads(raw_json)
        if int(payload.get("e", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def store_uploaded_media(
    file_storage,
    folder_prefix: str = "courses",
) -> tuple[bool, dict, str]:
    """
    Guarda un archivo subido desde el panel Admin:
    - Si S3 / Cloudflare R2 está activo y configurado, lo sube directamente al bucket S3 privado.
    - Si aún no está activo S3, lo guarda en la carpeta protegida del servidor (`protected_media/`)
      para que todo funcione sin interrupciones mientras se configura Cloudflare R2.
    """
    orig_name = sanitize_filename(file_storage.filename or "archivo")
    mime_type = file_storage.mimetype or "application/octet-stream"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    object_key = f"{folder_prefix.strip('/')}/{timestamp}_{orig_name}"

    file_bytes = file_storage.read()
    size_bytes = len(file_bytes)

    cfg = get_storage_config()
    if is_s3_ready(cfg):
        ok, log_msg = upload_file_to_s3(
            file_bytes=file_bytes,
            object_key=object_key,
            content_type=mime_type,
            cfg=cfg,
        )
        if ok:
            return (
                True,
                {
                    "storage_provider": "s3",
                    "object_key": object_key,
                    "original_filename": file_storage.filename or orig_name,
                    "mime_type": mime_type,
                    "file_size_bytes": size_bytes,
                },
                log_msg,
            )
        return False, {}, log_msg

    # Fallback en almacenamiento local protegido (fuera de static público)
    base_dir = ensure_protected_storage_dir()
    full_path = os.path.join(base_dir, object_key.replace("/", os.sep))
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "wb") as f:
        f.write(file_bytes)

    return (
        True,
        {
            "storage_provider": "local",
            "object_key": object_key,
            "original_filename": file_storage.filename or orig_name,
            "mime_type": mime_type,
            "file_size_bytes": size_bytes,
        },
        f"Guardado en almacenamiento protegido local ({object_key}). Cuando actives Cloudflare R2 se subirán a tu bucket S3.",
    )
