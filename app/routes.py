import base64
from datetime import datetime, timedelta, timezone
from functools import wraps
import json
import mimetypes
import os
import re
import urllib.parse
from flask import Blueprint, Response, jsonify, redirect, request, send_file
from flask_jwt_extended import (
    create_access_token,
    get_jwt,
    get_jwt_identity,
    jwt_required,
)

from app.course_service import (
    build_student_course_roadmap,
    ensure_student_enrolled,
)
from app.email_service import (
    build_contact_notification_html,
    build_welcome_email_html,
    get_or_create_system_config,
    send_system_email,
)
from app.models import (
    ContactMessage,
    Course,
    CourseModule,
    Enrollment,
    Lesson,
    LessonResource,
    MarketingLead,
    PublicResource,
    ResourceDownloadLog,
    SessionBooking,
    StudentLessonOverride,
    StudentLessonProgress,
    User,
    db,
)
from app.calendar_service import (
    build_calendar_service,
    compute_available_slots,
    create_calendar_event,
    delete_calendar_event,
    exchange_oauth_code_for_token,
    extract_oauth_client_info,
    generate_google_oauth_url,
    get_booking_config,
    get_user_calendar_ids,
    has_calendar_client_id,
    is_calendar_ready,
    parse_working_hours,
    test_calendar_connection,
)
from app.storage_service import (
    ensure_protected_storage_dir,
    generate_s3_presigned_url,
    get_storage_config,
    is_s3_ready,
    sanitize_filename,
    store_uploaded_media,
    test_s3_connection,
    create_media_access_token,
    verify_media_access_token,
)

api_bp = Blueprint("api", __name__)


def admin_required(fn):
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        claims = get_jwt()
        if claims.get("role") != "admin":
            return (
                jsonify(
                    {
                        "error": "Acceso denegado. Se requieren permisos de administrador."
                    }
                ),
                403,
            )
        return fn(*args, **kwargs)

    return wrapper


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "curso").lower()).strip("-")
    return slug or f"curso-{int(datetime.now(timezone.utc).timestamp())}"


# ==========================================================
# 1. AUTENTICACIÓN (/api/auth/*)
# ==========================================================
@api_bp.route("/auth/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Por favor, introduce tu email y contraseña."}), 400

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return (
            jsonify(
                {"error": "Credenciales inválidas. Revisa tu email o contraseña."}
            ),
            401,
        )

    if not user.is_active:
        return (
            jsonify(
                {
                    "error": "Tu cuenta está desactivada temporalmente. Contacta con la consulta."
                }
            ),
            403,
        )

    access_token = create_access_token(
        identity=str(user.id),
        additional_claims={
            "role": user.role,
            "email": user.email,
            "name": user.name or user.email.split("@")[0],
        },
    )

    return (
        jsonify(
            {
                "access_token": access_token,
                "user": user.to_dict(),
            }
        ),
        200,
    )


@api_bp.route("/auth/me", methods=["GET"])
@jwt_required()
def get_current_user():
    user_id = get_jwt_identity()
    user = User.query.get(int(user_id))
    if not user or not user.is_active:
        return jsonify({"error": "Usuario no encontrado o inactivo."}), 404
    return jsonify({"user": user.to_dict()}), 200


# ==========================================================
# 2. CONTACTO PÚBLICO LANDING PAGE (/api/contact)
# ==========================================================
@api_bp.route("/contact", methods=["POST"])
def submit_contact():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()
    message = (data.get("message") or "").strip()

    if not name or not email or not message:
        return (
            jsonify(
                {
                    "error": "Por favor completa tu nombre, correo electrónico y tu situación actual."
                }
            ),
            400,
        )

    contact_msg = ContactMessage(
        name=name,
        email=email,
        phone=phone,
        message=message,
    )
    db.session.add(contact_msg)
    db.session.commit()

    sys_cfg = get_or_create_system_config()
    admin_user = User.query.filter_by(role="admin").first()
    target_email = sys_cfg.smtp_user or (
        admin_user.email if admin_user else "admin@vuelveati.com"
    )

    html_body = build_contact_notification_html(name, email, phone, message)
    email_sent, email_log = send_system_email(
        to_email=target_email,
        subject=f"Nueva consulta en Vuelve a Ti de {name}",
        html_body=html_body,
    )

    return (
        jsonify(
            {
                "message": "Hemos recibido tu mensaje con total confidencialidad. Te responderemos en menos de 24 horas.",
                "contact": contact_msg.to_dict(),
                "email_dispatched": email_sent,
                "email_log": email_log,
            }
        ),
        201,
    )


# ==========================================================
# 3. PANEL DE ADMINISTRACIÓN: CONFIGURACIÓN DE CORREO
# ==========================================================
@api_bp.route("/admin/email-settings", methods=["GET"])
@admin_required
def get_email_settings():
    config = get_or_create_system_config()
    return jsonify({"settings": config.to_dict()}), 200


@api_bp.route("/admin/email-settings", methods=["PUT"])
@admin_required
def update_email_settings():
    config = get_or_create_system_config()
    data = request.get_json() or {}

    config.smtp_host = (data.get("smtp_host") or "smtp.gmail.com").strip()
    config.smtp_port = int(data.get("smtp_port") or 587)
    config.smtp_user = (data.get("smtp_user") or "").strip()
    config.smtp_password = data.get("smtp_password") or ""
    config.use_gmail_api = bool(data.get("use_gmail_api", False))
    config.gmail_token_json = data.get("gmail_token_json") or ""

    db.session.commit()
    return (
        jsonify(
            {
                "message": "Configuración de correo guardada correctamente en MySQL.",
                "settings": config.to_dict(),
            }
        ),
        200,
    )


@api_bp.route("/admin/email-settings/test", methods=["POST"])
@admin_required
def test_email_connection():
    data = request.get_json() or {}
    recipient = (data.get("test_recipient") or data.get("smtp_user") or "").strip()

    if not recipient:
        claims = get_jwt()
        recipient = claims.get("email", "admin@vuelveati.com")

    test_html = """
    <div style="font-family: 'Helvetica Neue', Arial, sans-serif; background-color: #FAF8F5; padding: 32px; color: #2C3E35;">
      <div style="max-width: 540px; margin: 0 auto; background: #FFFFFF; border-radius: 16px; padding: 32px; border: 1px solid #E6E1DA;">
        <h2 style="color: #4A6B5D; margin-top: 0;">✓ Prueba de Conexión Exitosa</h2>
        <p style="font-size: 15px; line-height: 1.6;">
          El servicio de correo de la plataforma <strong>Vuelve a Ti</strong> está correctamente configurado y listo para enviar credenciales de bienvenida a tus alumnos y notificaciones del formulario de contacto.
        </p>
      </div>
    </div>
    """

    success, detailed_log = send_system_email(
        to_email=recipient,
        subject="✓ Prueba de conexión de correo · Vuelve a Ti",
        html_body=test_html,
        override_config=data,
    )

    status_code = 200 if success else 400
    return (
        jsonify(
            {
                "success": success,
                "recipient": recipient,
                "message": (
                    f"Correo de prueba enviado con éxito a {recipient}."
                    if success
                    else "No se pudo completar el envío del correo de prueba. Revisa el registro técnico."
                ),
                "log": detailed_log,
            }
        ),
        status_code,
    )


# ==========================================================
# 4. PANEL DE ADMINISTRACIÓN: ALMACENAMIENTO S3 / CLOUDFLARE R2
# ==========================================================
@api_bp.route("/admin/storage-settings", methods=["GET"])
@admin_required
def get_storage_settings_route():
    config = get_or_create_system_config()
    return (
        jsonify(
            {
                "settings": config.to_dict(),
                "s3_ready": is_s3_ready(config.to_dict()),
            }
        ),
        200,
    )


@api_bp.route("/admin/storage-settings", methods=["PUT"])
@admin_required
def update_storage_settings_route():
    config = get_or_create_system_config()
    data = request.get_json() or {}

    config.s3_enabled = bool(data.get("s3_enabled", False))
    config.s3_provider = (data.get("s3_provider") or "cloudflare_r2").strip()
    config.s3_endpoint_url = (data.get("s3_endpoint_url") or "").strip().rstrip("/")
    config.s3_region = (data.get("s3_region") or "auto").strip() or "auto"
    config.s3_bucket_name = (data.get("s3_bucket_name") or "").strip()
    config.s3_access_key_id = (data.get("s3_access_key_id") or "").strip()
    config.s3_secret_access_key = (data.get("s3_secret_access_key") or "").strip()
    config.presigned_url_expiration = max(
        300, int(data.get("presigned_url_expiration") or 1800)
    )

    db.session.commit()
    return (
        jsonify(
            {
                "message": "Configuración de almacenamiento S3 / Cloudflare R2 guardada en MySQL.",
                "settings": config.to_dict(),
                "s3_ready": is_s3_ready(config.to_dict()),
            }
        ),
        200,
    )


@api_bp.route("/admin/storage-settings/test", methods=["POST"])
@admin_required
def test_storage_settings_route():
    data = request.get_json() or {}
    ok, msg, detailed_log = test_s3_connection(data)
    return (
        jsonify(
            {
                "success": ok,
                "message": msg,
                "log": detailed_log,
            }
        ),
        200 if ok else 400,
    )


# ==========================================================
# 4.1. PANEL ADMIN: CONFIGURACIÓN DE AGENDA / GOOGLE CALENDAR
# ==========================================================
@api_bp.route("/admin/calendar-settings", methods=["GET"])
@admin_required
def get_calendar_settings():
    cfg = get_booking_config()
    callback_url = f"{request.host_url.rstrip('/')}/api/admin/calendar/oauth-callback"
    return (
        jsonify(
            {
                "settings": cfg,
                "calendar_ready": is_calendar_ready(cfg),
                "has_client_credentials": has_calendar_client_id(cfg),
                "callback_url": callback_url,
            }
        ),
        200,
    )


@api_bp.route("/admin/calendar/oauth-url", methods=["GET"])
@admin_required
def get_calendar_oauth_url():
    """Devuelve la URL oficial de Google para iniciar el flujo OAuth2 con 1 clic."""
    cfg = get_booking_config()

    # Si se pasa un token_json temporal en query params, usarlo
    temp_json = request.args.get("calendar_token_json")
    if temp_json:
        cfg = dict(cfg)
        cfg["calendar_use_gmail_token"] = False
        cfg["calendar_token_json"] = temp_json

    callback_url = f"{request.host_url.rstrip('/')}/api/admin/calendar/oauth-callback"
    redirect_uri = request.args.get("redirect_uri") or callback_url

    # Obtener el origen del frontend para redirigir tras la autorización
    frontend_origin = request.headers.get("Origin") or request.referrer or ""
    if frontend_origin:
        try:
            parsed = urllib.parse.urlparse(frontend_origin)
            frontend_origin = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass
    if not frontend_origin:
        parsed_host = urllib.parse.urlparse(request.host_url)
        frontend_origin = f"{parsed_host.scheme}://{parsed_host.hostname}:5173"

    state_obj = {"frontend": frontend_origin, "redirect_uri": redirect_uri}
    state_b64 = base64.urlsafe_b64encode(
        json.dumps(state_obj).encode("utf-8")
    ).decode("utf-8")

    try:
        auth_url = generate_google_oauth_url(cfg, redirect_uri, state=state_b64)
        return (
            jsonify(
                {
                    "auth_url": auth_url,
                    "redirect_uri": redirect_uri,
                    "frontend_origin": frontend_origin,
                }
            ),
            200,
        )
    except Exception as exc:
        return jsonify({"error": str(exc), "redirect_uri": redirect_uri}), 400


@api_bp.route("/admin/calendar/oauth-callback", methods=["GET"])
def calendar_oauth_callback():
    """Callback de Google OAuth2. Canjea el código por tokens y redirige al panel admin."""
    code = request.args.get("code")
    error = request.args.get("error")
    state_raw = request.args.get("state")

    frontend_base = "http://localhost:5173"
    redirect_uri = f"{request.host_url.rstrip('/')}/api/admin/calendar/oauth-callback"
    if state_raw:
        try:
            state_json = base64.urlsafe_b64decode(
                state_raw.encode("utf-8")
            ).decode("utf-8")
            state_data = json.loads(state_json)
            frontend_base = state_data.get("frontend") or frontend_base
            redirect_uri = state_data.get("redirect_uri") or redirect_uri
        except Exception:
            pass

    frontend_target = f"{frontend_base.rstrip('/')}/admin/calendar-settings"

    if error:
        return redirect(
            f"{frontend_target}?google_error={urllib.parse.quote(error)}"
        )

    if not code:
        return redirect(
            f"{frontend_target}?google_error={urllib.parse.quote('No se recibió código de autorización de Google.')}"
        )

    cfg = get_booking_config()
    try:
        tokens = exchange_oauth_code_for_token(cfg, code, redirect_uri)
        config = get_or_create_system_config()
        config.calendar_token_json = json.dumps(tokens, indent=2)
        config.calendar_enabled = True
        config.calendar_use_gmail_token = False
        db.session.commit()
        return redirect(f"{frontend_target}?google_connected=true")
    except Exception as exc:
        return redirect(
            f"{frontend_target}?google_error={urllib.parse.quote(str(exc))}"
        )


@api_bp.route("/admin/calendar-settings", methods=["PUT"])
@admin_required
def update_calendar_settings():
    config = get_or_create_system_config()
    data = request.get_json() or {}

    config.calendar_enabled = bool(data.get("calendar_enabled", False))
    config.calendar_use_gmail_token = bool(
        data.get("calendar_use_gmail_token", True)
    )
    config.calendar_token_json = data.get("calendar_token_json") or ""
    config.calendar_id = (data.get("calendar_id") or "primary").strip() or "primary"
    config.calendar_timezone = (
        data.get("calendar_timezone") or "Europe/Madrid"
    ).strip() or "Europe/Madrid"
    config.session_duration_minutes = max(
        15, int(data.get("session_duration_minutes") or 60)
    )

    wh = data.get("booking_working_hours")
    if wh is not None:
        if isinstance(wh, (dict, list)):
            wh = json.dumps(wh)
        config.booking_working_hours = wh

    config.booking_min_notice_hours = max(
        0, int(data.get("booking_min_notice_hours", 24))
    )
    config.booking_cancel_limit_hours = max(
        0, int(data.get("booking_cancel_limit_hours", 24))
    )

    db.session.commit()
    cfg = get_booking_config()
    return (
        jsonify(
            {
                "message": "Configuración de Google Calendar y horario laboral guardada.",
                "settings": cfg,
                "calendar_ready": is_calendar_ready(cfg),
            }
        ),
        200,
    )


@api_bp.route("/admin/calendar-settings/test", methods=["POST"])
@admin_required
def test_calendar_settings():
    data = request.get_json() or {}
    ok, msg, detailed_log = test_calendar_connection(data)
    return (
        jsonify(
            {
                "success": ok,
                "message": msg,
                "log": detailed_log,
            }
        ),
        200 if ok else 400,
    )


@api_bp.route("/admin/calendar/calendars", methods=["GET"])
@admin_required
def admin_list_google_calendars():
    cfg = get_booking_config()
    if not is_calendar_ready(cfg):
        return jsonify({"calendars": []}), 200
    try:
        service = build_calendar_service(cfg)
        cals = get_user_calendar_ids(service, cfg.get("calendar_id") or "primary")
        return jsonify({"calendars": cals}), 200
    except Exception as exc:
        return jsonify({"error": str(exc), "calendars": []}), 500


# ==========================================================
# 4.2. PANEL ADMIN: GESTIÓN DE SESIONES RESERVADAS
# ==========================================================
@api_bp.route("/admin/bookings", methods=["GET"])
@admin_required
def list_admin_bookings():
    query = SessionBooking.query.order_by(SessionBooking.start_at.desc())
    status_filter = request.args.get("status")
    if status_filter in ("confirmed", "cancelled"):
        query = query.filter_by(status=status_filter)
    user_id = request.args.get("user_id")
    if user_id and user_id.isdigit():
        query = query.filter_by(user_id=int(user_id))

    bookings = query.all()
    return (
        jsonify({"bookings": [b.to_dict(include_relations=True) for b in bookings]}),
        200,
    )


@api_bp.route("/admin/bookings/<int:booking_id>", methods=["DELETE"])
@admin_required
def admin_cancel_booking(booking_id: int):
    booking = SessionBooking.query.get_or_404(booking_id)
    if booking.status == "cancelled":
        return jsonify({"message": "La sesión ya estaba cancelada."}), 200

    cfg = get_booking_config()
    cal_msg = ""
    if booking.google_event_id:
        ok, cal_msg = delete_calendar_event(cfg, booking.google_event_id)

    booking.status = "cancelled"
    booking.cancelled_by = "admin"
    booking.cancelled_at = datetime.now(timezone.utc)
    db.session.commit()

    # Notificar al alumno por correo
    if booking.user and booking.user.email:
        lesson_name = booking.lesson.title if booking.lesson else "Sesión individual"
        cancel_html = f"""
        <div style="font-family: 'Helvetica Neue', Arial, sans-serif; background-color: #FAF8F5; padding: 32px; color: #2C3E35;">
          <div style="max-width: 540px; margin: 0 auto; background: #FFFFFF; border-radius: 16px; padding: 32px; border: 1px solid #E6E1DA;">
            <h2 style="color: #D9826C; margin-top: 0;">Sesión cancelada</h2>
            <p style="font-size: 15px; line-height: 1.6;">
              Hola {booking.user.name or ''}, la psicóloga ha cancelado la sesión individual correspondiente a <strong>{lesson_name}</strong>.
            </p>
            <p style="font-size: 14px; color: #2C3E35;">
              Puedes volver a acceder a tu campus para elegir un nuevo horario libre.
            </p>
          </div>
        </div>
        """
        send_system_email(
            to_email=booking.user.email,
            subject="Cancelación de sesión · Vuelve a Ti",
            html_body=cancel_html,
        )

    return (
        jsonify(
            {
                "message": f"Sesión cancelada correctamente. {cal_msg}",
                "booking": booking.to_dict(include_relations=True),
            }
        ),
        200,
    )


@api_bp.route("/admin/storage/presign-upload", methods=["POST"])
@admin_required
def presign_direct_upload():
    """
    Genera una Presigned PUT URL (s3v4) para subir vídeos pesados directamente
    desde el navegador del Admin hacia Cloudflare R2 / AWS S3 si tiene CORS activo,
    o informa si debe usar la subida asistida por servidor.
    """
    data = request.get_json() or {}
    filename = sanitize_filename(data.get("filename") or "video.mp4")
    folder = (data.get("folder") or "courses/videos").strip("/")
    content_type = data.get("content_type") or "video/mp4"

    cfg = get_storage_config()
    if not is_s3_ready(cfg):
        return (
            jsonify(
                {
                    "s3_enabled": False,
                    "message": "S3 / Cloudflare R2 aún no está activo; se usará subida directa al servidor.",
                }
            ),
            200,
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    object_key = f"{folder}/{timestamp}_{filename}"
    put_url = generate_s3_presigned_url(
        method="PUT",
        object_key=object_key,
        expires_in=3600,
        cfg=cfg,
    )
    return (
        jsonify(
            {
                "s3_enabled": True,
                "upload_url": put_url,
                "object_key": object_key,
                "storage_provider": "s3",
                "content_type": content_type,
            }
        ),
        200,
    )


@api_bp.route("/admin/storage/upload", methods=["POST"])
@admin_required
def upload_media_endpoint():
    """
    Recibe un archivo (vídeo o recurso PDF/Word/Presentación/Audio) desde el panel Admin
    y lo sube a Cloudflare R2 / S3 (si está configurado) o al almacenamiento protegido local.
    """
    if "file" not in request.files:
        return jsonify({"error": "No se ha enviado ningún archivo."}), 400

    file_obj = request.files["file"]
    if not file_obj or not file_obj.filename:
        return jsonify({"error": "El archivo seleccionado no es válido."}), 400

    folder = (request.form.get("folder") or "courses/media").strip("/")
    ok, meta, log_msg = store_uploaded_media(file_obj, folder_prefix=folder)
    if not ok:
        return jsonify({"error": log_msg}), 400

    # Generar URL firmada de previsualización inmediata para el editor en vivo
    try:
        cfg = get_storage_config()
        if meta.get("storage_provider") == "s3" and is_s3_ready(cfg):
            meta["preview_url"] = generate_s3_presigned_url(
                method="GET",
                object_key=meta["object_key"],
                expires_in=7200,
                response_content_disposition="inline",
                response_content_type=meta.get("mime_type") or "application/octet-stream",
                cfg=cfg,
            )
        else:
            tok = create_media_access_token(
                user_id=0,
                object_key=meta["object_key"],
                mime_type=meta.get("mime_type") or "application/octet-stream",
                disposition="inline",
                filename=meta.get("original_filename") or "archivo",
                expires_in=86400,
            )
            meta["preview_url"] = f"{request.host_url.rstrip('/')}/api/campus/media-stream/{tok}"
    except Exception:
        meta["preview_url"] = ""

    return (
        jsonify(
            {
                "message": log_msg,
                "media": meta,
            }
        ),
        201,
    )


# ==========================================================
# 5. PANEL DE ADMINISTRACIÓN: GESTIÓN DE CURSOS, MÓDULOS, CLASES Y RECURSOS
# ==========================================================
@api_bp.route("/admin/courses", methods=["GET"])
@admin_required
def admin_list_courses():
    courses = Course.query.order_by(Course.id.asc()).all()
    result = []
    for c in courses:
        c_dict = c.to_dict(include_modules=True)
        c_dict["enrolled_students_count"] = Enrollment.query.filter_by(
            course_id=c.id
        ).count()
        result.append(c_dict)
    return (
        jsonify(
            {
                "courses": result,
                "s3_ready": is_s3_ready(),
            }
        ),
        200,
    )


@api_bp.route("/admin/courses", methods=["POST"])
@admin_required
def admin_create_course():
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "El título del curso es obligatorio."}), 400

    base_slug = slugify(data.get("slug") or title)
    slug = base_slug
    counter = 2
    while Course.query.filter_by(slug=slug).first():
        slug = f"{base_slug}-{counter}"
        counter += 1

    drip_mode = (data.get("drip_mode") or "weekly_days").strip()
    if drip_mode not in ("weekly_days", "interval_days", "immediate"):
        drip_mode = "weekly_days"

    weekdays_raw = data.get("drip_weekdays")
    if isinstance(weekdays_raw, list):
        drip_weekdays = ",".join(str(int(d)) for d in weekdays_raw)
    else:
        drip_weekdays = str(weekdays_raw if weekdays_raw is not None else "0").strip() or "0"

    course = Course(
        title=title,
        slug=slug,
        subtitle=(data.get("subtitle") or "").strip(),
        description=(data.get("description") or "").strip(),
        is_active=bool(data.get("is_active", True)),
        drip_mode=drip_mode,
        drip_weekdays=drip_weekdays,
        drip_interval_days=max(1, int(data.get("drip_interval_days") or 7)),
    )
    db.session.add(course)
    db.session.commit()

    # Si se solicita matricular a los alumnos actuales automáticamente
    if data.get("enroll_existing_students", True):
        students = User.query.filter_by(role="user").all()
        for stu in students:
            if not Enrollment.query.filter_by(
                user_id=stu.id, course_id=course.id
            ).first():
                db.session.add(
                    Enrollment(
                        user_id=stu.id,
                        course_id=course.id,
                        enrolled_at=datetime.now(timezone.utc),
                        status="active",
                    )
                )
        db.session.commit()

    return (
        jsonify(
            {
                "message": f"Curso '{course.title}' creado correctamente.",
                "course": course.to_dict(include_modules=True),
            }
        ),
        201,
    )


@api_bp.route("/admin/courses/<int:course_id>", methods=["PUT"])
@admin_required
def admin_update_course(course_id: int):
    course = Course.query.get_or_404(course_id)
    data = request.get_json() or {}

    if "title" in data and (data.get("title") or "").strip():
        course.title = data["title"].strip()
    if "subtitle" in data:
        course.subtitle = (data.get("subtitle") or "").strip()
    if "description" in data:
        course.description = (data.get("description") or "").strip()
    if "is_active" in data:
        course.is_active = bool(data["is_active"])
    if "drip_mode" in data and data["drip_mode"] in (
        "weekly_days",
        "interval_days",
        "immediate",
    ):
        course.drip_mode = data["drip_mode"]
    if "drip_weekdays" in data:
        w_raw = data["drip_weekdays"]
        if isinstance(w_raw, list):
            course.drip_weekdays = (
                ",".join(str(int(d)) for d in w_raw) if w_raw else "0"
            )
        else:
            course.drip_weekdays = str(w_raw or "0").strip() or "0"
    if "drip_interval_days" in data:
        course.drip_interval_days = max(1, int(data.get("drip_interval_days") or 7))

    db.session.commit()
    return (
        jsonify(
            {
                "message": "Configuración y cronograma del curso actualizados.",
                "course": course.to_dict(include_modules=True),
            }
        ),
        200,
    )


@api_bp.route("/admin/courses/<int:course_id>", methods=["DELETE"])
@admin_required
def admin_delete_course(course_id: int):
    course = Course.query.get_or_404(course_id)
    if Course.query.count() <= 1:
        return (
            jsonify(
                {
                    "error": "No puedes eliminar el único curso activo de la plataforma."
                }
            ),
            400,
        )
    db.session.delete(course)
    db.session.commit()
    return jsonify({"message": "Curso eliminado correctamente."}), 200


# --- Módulos ---
@api_bp.route("/admin/courses/<int:course_id>/modules", methods=["POST"])
@admin_required
def admin_create_module(course_id: int):
    course = Course.query.get_or_404(course_id)
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "El título del módulo es obligatorio."}), 400

    next_order = len(course.modules) + 1
    mod = CourseModule(
        course_id=course.id,
        title=title,
        subtitle=(data.get("subtitle") or "").strip(),
        order_index=int(data.get("order_index") or next_order),
    )
    db.session.add(mod)
    db.session.commit()
    return (
        jsonify(
            {
                "message": f"Módulo '{mod.title}' añadido.",
                "module": mod.to_dict(include_lessons=True),
            }
        ),
        201,
    )


@api_bp.route("/admin/modules/<int:module_id>", methods=["PUT"])
@admin_required
def admin_update_module(module_id: int):
    mod = CourseModule.query.get_or_404(module_id)
    data = request.get_json() or {}
    if "title" in data and (data.get("title") or "").strip():
        mod.title = data["title"].strip()
    if "subtitle" in data:
        mod.subtitle = (data.get("subtitle") or "").strip()
    if "order_index" in data:
        mod.order_index = max(1, int(data.get("order_index") or 1))
    db.session.commit()
    return (
        jsonify(
            {
                "message": "Módulo actualizado.",
                "module": mod.to_dict(include_lessons=True),
            }
        ),
        200,
    )


@api_bp.route("/admin/modules/<int:module_id>", methods=["DELETE"])
@admin_required
def admin_delete_module(module_id: int):
    mod = CourseModule.query.get_or_404(module_id)
    db.session.delete(mod)
    db.session.commit()
    return jsonify({"message": "Módulo eliminado correctamente."}), 200


# --- Clases (Lessons) ---
@api_bp.route("/admin/modules/<int:module_id>/lessons", methods=["POST"])
@admin_required
def admin_create_lesson(module_id: int):
    mod = CourseModule.query.get_or_404(module_id)
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "El título de la clase es obligatorio."}), 400

    next_order = len(mod.lessons) + 1
    video_key = (data.get("video_object_key") or "").strip()
    has_video = bool(data.get("has_video") and video_key)

    lesson = Lesson(
        module_id=mod.id,
        title=title,
        description=(data.get("description") or "").strip(),
        order_index=int(data.get("order_index") or next_order),
        has_video=has_video,
        video_storage_provider=(data.get("video_storage_provider") or "s3").strip(),
        video_object_key=video_key,
        video_original_name=(data.get("video_original_name") or "").strip(),
        video_mime_type=(data.get("video_mime_type") or "video/mp4").strip(),
        video_size_bytes=int(data.get("video_size_bytes") or 0),
        video_duration_seconds=int(data.get("video_duration_seconds") or 0),
        booking_enabled=bool(data.get("booking_enabled", False)),
    )
    db.session.add(lesson)
    db.session.commit()
    return (
        jsonify(
            {
                "message": f"Clase '{lesson.title}' creada correctamente.",
                "lesson": lesson.to_dict(include_resources=True),
            }
        ),
        201,
    )


@api_bp.route("/admin/lessons/<int:lesson_id>", methods=["PUT"])
@admin_required
def admin_update_lesson(lesson_id: int):
    lesson = Lesson.query.get_or_404(lesson_id)
    data = request.get_json() or {}

    if "title" in data and (data.get("title") or "").strip():
        lesson.title = data["title"].strip()
    if "description" in data:
        lesson.description = (data.get("description") or "").strip()
    if "order_index" in data:
        lesson.order_index = max(1, int(data.get("order_index") or 1))
    if "booking_enabled" in data:
        lesson.booking_enabled = bool(data["booking_enabled"])

    if "video_object_key" in data:
        video_key = (data.get("video_object_key") or "").strip()
        lesson.video_object_key = video_key
        lesson.has_video = bool(video_key)
    elif "has_video" in data and not data["has_video"]:
        lesson.has_video = False
        lesson.video_object_key = ""
        lesson.video_original_name = ""
        lesson.video_size_bytes = 0
        lesson.video_duration_seconds = 0

    if "video_storage_provider" in data:
        lesson.video_storage_provider = (
            data.get("video_storage_provider") or "s3"
        ).strip()
    if "video_original_name" in data:
        lesson.video_original_name = (data.get("video_original_name") or "").strip()
    if "video_mime_type" in data:
        lesson.video_mime_type = (data.get("video_mime_type") or "video/mp4").strip()
    if "video_size_bytes" in data:
        lesson.video_size_bytes = int(data.get("video_size_bytes") or 0)
    if "video_duration_seconds" in data:
        lesson.video_duration_seconds = int(data.get("video_duration_seconds") or 0)

    db.session.commit()
    return (
        jsonify(
            {
                "message": f"Clase '{lesson.title}' actualizada.",
                "lesson": lesson.to_dict(include_resources=True),
            }
        ),
        200,
    )


@api_bp.route("/admin/lessons/<int:lesson_id>", methods=["DELETE"])
@admin_required
def admin_delete_lesson(lesson_id: int):
    lesson = Lesson.query.get_or_404(lesson_id)
    db.session.delete(lesson)
    db.session.commit()
    return jsonify({"message": "Clase eliminada correctamente."}), 200


# --- Recursos de una Clase ---
@api_bp.route("/admin/lessons/<int:lesson_id>/resources", methods=["POST"])
@admin_required
def admin_add_lesson_resource(lesson_id: int):
    lesson = Lesson.query.get_or_404(lesson_id)
    data = request.get_json() or {}

    title = (data.get("title") or "").strip()
    object_key = (data.get("object_key") or "").strip()
    if not title or not object_key:
        return (
            jsonify(
                {
                    "error": "El título del recurso y el archivo subido son obligatorios."
                }
            ),
            400,
        )

    res = LessonResource(
        lesson_id=lesson.id,
        title=title,
        resource_type=(data.get("resource_type") or "pdf").strip(),
        storage_provider=(data.get("storage_provider") or "s3").strip(),
        object_key=object_key,
        original_filename=(data.get("original_filename") or title).strip(),
        mime_type=(data.get("mime_type") or "application/pdf").strip(),
        file_size_bytes=int(data.get("file_size_bytes") or 0),
        order_index=len(lesson.resources) + 1,
    )
    db.session.add(res)
    db.session.commit()
    return (
        jsonify(
            {
                "message": f"Recurso '{res.title}' añadido a la clase.",
                "resource": res.to_dict(),
            }
        ),
        201,
    )


@api_bp.route("/admin/resources/<int:resource_id>", methods=["DELETE"])
@admin_required
def admin_delete_lesson_resource(resource_id: int):
    res = LessonResource.query.get_or_404(resource_id)
    db.session.delete(res)
    db.session.commit()
    return jsonify({"message": "Recurso eliminado de la clase."}), 200


# ==========================================================
# 6. PANEL ADMIN: SEGUIMIENTO DE ALUMNOS, CRONOGRAMA Y DESBLOQUEO MANUAL
# ==========================================================
@api_bp.route("/admin/courses/<int:course_id>/students-progress", methods=["GET"])
@admin_required
def admin_course_students_progress(course_id: int):
    course = Course.query.get_or_404(course_id)
    students = (
        User.query.filter_by(role="user").order_by(User.created_at.desc()).all()
    )

    students_summary = []
    for stu in students:
        roadmap = build_student_course_roadmap(stu.id, course)
        students_summary.append(
            {
                "user": stu.to_dict(),
                "enrollment": roadmap["enrollment"],
                "summary": roadmap["summary"],
            }
        )

    return (
        jsonify(
            {
                "course": course.to_dict(include_modules=False),
                "students": students_summary,
            }
        ),
        200,
    )


@api_bp.route(
    "/admin/courses/<int:course_id>/students/<int:user_id>/roadmap", methods=["GET"]
)
@admin_required
def admin_get_student_roadmap(course_id: int, user_id: int):
    course = Course.query.get_or_404(course_id)
    user = User.query.get_or_404(user_id)
    roadmap = build_student_course_roadmap(user.id, course)
    return (
        jsonify(
            {
                "user": user.to_dict(),
                "roadmap": roadmap,
            }
        ),
        200,
    )


@api_bp.route(
    "/admin/courses/<int:course_id>/students/<int:user_id>/enrollment",
    methods=["PATCH"],
)
@admin_required
def admin_update_student_enrollment(course_id: int, user_id: int):
    """
    Permite al Admin:
    - Cambiar la fecha de inicio/pago (`enrolled_at`) de un alumno para recalcular su cronograma.
    - Activar o desactivar `unlock_all_override` (desbloquear todo el curso de golpe para ese alumno).
    """
    course = Course.query.get_or_404(course_id)
    user = User.query.get_or_404(user_id)
    enrollment = ensure_student_enrolled(user.id, course.id)
    data = request.get_json() or {}

    if "enrolled_at" in data and data["enrolled_at"]:
        try:
            raw_dt = str(data["enrolled_at"]).strip()
            if len(raw_dt) == 10:
                parsed_dt = datetime.strptime(raw_dt, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            else:
                parsed_dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
            enrollment.enrolled_at = parsed_dt
        except Exception as exc:
            return jsonify({"error": f"Formato de fecha inválido: {exc}"}), 400

    if "unlock_all_override" in data:
        enrollment.unlock_all_override = bool(data["unlock_all_override"])

    db.session.commit()
    roadmap = build_student_course_roadmap(user.id, course)
    return (
        jsonify(
            {
                "message": f"Cronograma de {user.name} actualizado.",
                "roadmap": roadmap,
            }
        ),
        200,
    )


@api_bp.route(
    "/admin/courses/<int:course_id>/students/<int:user_id>/lessons/<int:lesson_id>/override",
    methods=["POST"],
)
@admin_required
def admin_override_student_lesson(course_id: int, user_id: int, lesson_id: int):
    """
    Permite al Admin habilitar o bloquear manualmente cualquier clase para un alumno concreto,
    o devolverla al modo automático del cronograma ('auto').
    """
    course = Course.query.get_or_404(course_id)
    user = User.query.get_or_404(user_id)
    lesson = Lesson.query.get_or_404(lesson_id)
    data = request.get_json() or {}
    state = (data.get("force_state") or "unlocked").strip().lower()

    existing = StudentLessonOverride.query.filter_by(
        user_id=user.id, lesson_id=lesson.id
    ).first()

    if state == "auto":
        if existing:
            db.session.delete(existing)
            db.session.commit()
        msg = f"Clase '{lesson.title}' devuelta al cronograma automático."
    elif state in ("unlocked", "locked"):
        if not existing:
            existing = StudentLessonOverride(
                user_id=user.id,
                lesson_id=lesson.id,
                force_state=state,
                note=(data.get("note") or "").strip(),
            )
            db.session.add(existing)
        else:
            existing.force_state = state
            existing.note = (data.get("note") or "").strip()
        db.session.commit()
        msg = (
            f"Clase '{lesson.title}' habilitada manualmente para {user.name}."
            if state == "unlocked"
            else f"Clase '{lesson.title}' bloqueada manualmente para {user.name}."
        )
    else:
        return jsonify({"error": "Estado inválido. Usa 'unlocked', 'locked' o 'auto'."}), 400

    roadmap = build_student_course_roadmap(user.id, course)
    return (
        jsonify(
            {
                "message": msg,
                "roadmap": roadmap,
            }
        ),
        200,
    )


# ==========================================================
# 7. PANEL DE ADMINISTRACIÓN: GESTIÓN DE USUARIOS
# ==========================================================
@api_bp.route("/admin/users", methods=["GET"])
@admin_required
def list_users():
    search = (request.args.get("search") or "").strip().lower()
    role_filter = (request.args.get("role") or "").strip()
    status_filter = (request.args.get("status") or "").strip()

    query = User.query

    if search:
        query = query.filter(
            (User.email.ilike(f"%{search}%")) | (User.name.ilike(f"%{search}%"))
        )
    if role_filter in ("admin", "user"):
        query = query.filter(User.role == role_filter)
    if status_filter == "active":
        query = query.filter(User.is_active.is_(True))
    elif status_filter == "inactive":
        query = query.filter(User.is_active.is_(False))

    users = query.order_by(User.created_at.desc()).all()
    contacts_count = ContactMessage.query.count()

    return (
        jsonify(
            {
                "users": [u.to_dict() for u in users],
                "contacts_count": contacts_count,
            }
        ),
        200,
    )


@api_bp.route("/admin/users", methods=["POST"])
@admin_required
def create_user():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip() or "Alumno/a"
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role = data.get("role") or "user"
    send_welcome = bool(data.get("send_welcome_email", True))
    course_id = data.get("course_id")

    if not email or not password:
        return (
            jsonify(
                {
                    "error": "El correo electrónico y la contraseña temporal son obligatorios."
                }
            ),
            400,
        )

    if role not in ("admin", "user"):
        return jsonify({"error": "Rol inválido. Debe ser 'admin' o 'user'."}), 400

    existing = User.query.filter_by(email=email).first()
    if existing:
        return (
            jsonify(
                {"error": f"Ya existe un usuario registrado con el email {email}."}
            ),
            409,
        )

    new_user = User(
        name=name,
        email=email,
        role=role,
        is_active=True,
    )
    new_user.set_password(password)
    db.session.add(new_user)
    db.session.commit()

    # Matricular automáticamente al nuevo alumno en el curso seleccionado (o el principal)
    # habilitando la Clase 1 del Módulo 1 hoy mismo.
    if role == "user":
        ensure_student_enrolled(
            new_user.id, int(course_id) if course_id else None
        )

    email_sent = False
    email_log = "Envío de correo omitido por selección del administrador."
    if send_welcome:
        welcome_html = build_welcome_email_html(name, email, password)
        email_sent, email_log = send_system_email(
            to_email=email,
            subject="Bienvenida/o al Programa 'Vuelve a Ti' · Tus credenciales de acceso",
            html_body=welcome_html,
        )

    return (
        jsonify(
            {
                "message": f"Usuario {email} dado de alta e inscrito en el curso (Clase 1 habilitada hoy).",
                "user": new_user.to_dict(),
                "email_sent": email_sent,
                "email_log": email_log,
            }
        ),
        201,
    )


@api_bp.route("/admin/users/<int:user_id>/toggle-status", methods=["PATCH"])
@admin_required
def toggle_user_status(user_id: int):
    user = User.query.get_or_404(user_id)
    current_admin_id = int(get_jwt_identity())

    if user.id == current_admin_id:
        return (
            jsonify(
                {"error": "No puedes desactivar tu propia cuenta de administrador."}
            ),
            400,
        )

    user.is_active = not user.is_active
    db.session.commit()
    return (
        jsonify(
            {
                "message": f"Estado de {user.email} actualizado.",
                "user": user.to_dict(),
            }
        ),
        200,
    )


@api_bp.route("/admin/users/<int:user_id>", methods=["DELETE"])
@admin_required
def delete_user(user_id: int):
    user = User.query.get_or_404(user_id)
    current_admin_id = int(get_jwt_identity())

    if user.id == current_admin_id:
        return (
            jsonify(
                {"error": "No puedes eliminar tu propia cuenta de administrador."}
            ),
            400,
        )

    db.session.delete(user)
    db.session.commit()
    return jsonify({"message": "Usuario eliminado correctamente."}), 200


@api_bp.route("/admin/contacts", methods=["GET"])
@admin_required
def list_contacts():
    messages = ContactMessage.query.order_by(ContactMessage.created_at.desc()).all()
    return jsonify({"contacts": [m.to_dict() for m in messages]}), 200


# ==========================================================
# 8. CAMPUS DEL ALUMNO (/api/campus/*)
# ==========================================================
@api_bp.route("/campus/my-courses", methods=["GET"])
@jwt_required()
def campus_get_my_courses():
    user_id = int(get_jwt_identity())
    user = User.query.get_or_404(user_id)

    # Asegurar inscripción en el primer curso activo
    ensure_student_enrolled(user.id)

    active_courses = (
        Course.query.filter_by(is_active=True).order_by(Course.id.asc()).all()
    )
    roadmaps = [
        build_student_course_roadmap(user.id, course) for course in active_courses
    ]

    return (
        jsonify(
            {
                "user": user.to_dict(),
                "courses": roadmaps,
            }
        ),
        200,
    )


def _find_lesson_state_for_user(user_id: int, lesson: Lesson) -> dict | None:
    module = CourseModule.query.get(lesson.module_id)
    if not module:
        return None
    course = Course.query.get(module.course_id)
    if not course:
        return None
    roadmap = build_student_course_roadmap(user_id, course)
    for mod_data in roadmap["modules"]:
        for les_data in mod_data["lessons"]:
            if les_data["id"] == lesson.id:
                return les_data
    return None


@api_bp.route("/campus/lessons/<int:lesson_id>/video-stream", methods=["GET"])
@jwt_required()
def campus_get_lesson_video_stream(lesson_id: int):
    """
    Verifica que el alumno tenga la clase desbloqueada (o que sea admin) y devuelve
    una URL firmada efímera (Presigned GET en S3/Cloudflare R2 con Content-Disposition: inline)
    junto con los datos de marca de agua dinámica anti-grabación.
    """
    user_id = int(get_jwt_identity())
    user = User.query.get_or_404(user_id)
    lesson = Lesson.query.get_or_404(lesson_id)

    lesson_state = _find_lesson_state_for_user(user.id, lesson)
    if user.role != "admin" and (not lesson_state or not lesson_state["is_unlocked"]):
        return (
            jsonify(
                {
                    "error": "Esta clase aún no está desbloqueada en tu cronograma semanal.",
                    "scheduled_unlock_at": (
                        lesson_state["scheduled_unlock_at"] if lesson_state else None
                    ),
                }
            ),
            403,
        )

    # Registrar último acceso a la clase
    prog = StudentLessonProgress.query.filter_by(
        user_id=user.id, lesson_id=lesson.id
    ).first()
    if not prog:
        prog = StudentLessonProgress(
            user_id=user.id,
            lesson_id=lesson.id,
            last_accessed_at=datetime.now(timezone.utc),
        )
        db.session.add(prog)
    else:
        prog.last_accessed_at = datetime.now(timezone.utc)
    db.session.commit()

    if not lesson.has_video or not lesson.video_object_key:
        return (
            jsonify(
                {
                    "has_video": False,
                    "stream_url": None,
                    "progress": prog.to_dict(),
                }
            ),
            200,
        )

    cfg = get_storage_config()
    expires_in = int(cfg.get("presigned_url_expiration") or 1800)

    if lesson.video_storage_provider == "s3" and is_s3_ready(cfg):
        stream_url = generate_s3_presigned_url(
            method="GET",
            object_key=lesson.video_object_key,
            expires_in=expires_in,
            response_content_disposition="inline",
            response_content_type=lesson.video_mime_type or "video/mp4",
            cfg=cfg,
        )
    else:
        token = create_media_access_token(
            user_id=user.id,
            object_key=lesson.video_object_key,
            mime_type=lesson.video_mime_type or "video/mp4",
            disposition="inline",
            filename=lesson.video_original_name or "clase.mp4",
            expires_in=expires_in,
        )
        stream_url = f"{request.host_url.rstrip('/')}/api/campus/media-stream/{token}"

    return (
        jsonify(
            {
                "has_video": True,
                "stream_url": stream_url,
                "mime_type": lesson.video_mime_type or "video/mp4",
                "expires_in": expires_in,
                "watermark": {
                    "email": user.email,
                    "name": user.name or "Alumno/a",
                },
                "progress": prog.to_dict(),
            }
        ),
        200,
    )


@api_bp.route("/campus/lessons/<int:lesson_id>/progress", methods=["POST"])
@jwt_required()
def campus_update_lesson_progress(lesson_id: int):
    """
    Actualiza el progreso de reproducción de vídeo y/o finalización de la clase
    del alumno en tiempo real.
    """
    user_id = int(get_jwt_identity())
    lesson = Lesson.query.get_or_404(lesson_id)
    lesson_state = _find_lesson_state_for_user(user_id, lesson)

    claims = get_jwt()
    if claims.get("role") != "admin" and (
        not lesson_state or not lesson_state["is_unlocked"]
    ):
        return jsonify({"error": "Esta clase aún está bloqueada."}), 403

    data = request.get_json() or {}
    prog = StudentLessonProgress.query.filter_by(
        user_id=user_id, lesson_id=lesson.id
    ).first()
    if not prog:
        prog = StudentLessonProgress(
            user_id=user_id,
            lesson_id=lesson.id,
            watched_seconds=0.0,
            max_watched_seconds=0.0,
            video_duration_seconds=float(lesson.video_duration_seconds or 0.0),
            video_progress_percent=0.0,
            lesson_progress_percent=0.0,
            is_completed=False,
        )
        db.session.add(prog)

    prog.last_accessed_at = datetime.now(timezone.utc)

    watched = float(data.get("watched_seconds", prog.watched_seconds or 0.0))
    duration = float(
        data.get("video_duration_seconds")
        or prog.video_duration_seconds
        or lesson.video_duration_seconds
        or 0.0
    )

    prog.watched_seconds = max(0.0, watched)
    if watched > (prog.max_watched_seconds or 0.0):
        prog.max_watched_seconds = watched

    if duration > 0:
        prog.video_duration_seconds = duration
        if not lesson.video_duration_seconds:
            lesson.video_duration_seconds = int(round(duration))
        pct = min(100.0, round((prog.max_watched_seconds / duration) * 100.0, 1))
        prog.video_progress_percent = max(prog.video_progress_percent or 0.0, pct)
        prog.lesson_progress_percent = max(
            prog.lesson_progress_percent or 0.0, prog.video_progress_percent
        )
        if prog.video_progress_percent >= 92.0 and not prog.is_completed:
            prog.is_completed = True
            prog.lesson_progress_percent = 100.0
            prog.completed_at = datetime.now(timezone.utc)

    if "is_completed" in data:
        if bool(data["is_completed"]):
            prog.is_completed = True
            prog.lesson_progress_percent = 100.0
            if not prog.completed_at:
                prog.completed_at = datetime.now(timezone.utc)
        else:
            prog.is_completed = False
            prog.completed_at = None
            prog.lesson_progress_percent = (
                prog.video_progress_percent if lesson.has_video else 0.0
            )

    db.session.commit()
    return jsonify({"progress": prog.to_dict()}), 200


@api_bp.route("/campus/resources/<int:resource_id>/download", methods=["POST"])
@api_bp.route("/campus/resources/<int:resource_id>/preview", methods=["POST"])
@jwt_required()
def campus_download_resource(resource_id: int):
    """
    Registra en MySQL que el alumno ha accedido/descargado este recurso (PDF, audio, Word, presentación, etc.),
    actualiza el % de la clase si es una clase solo de recursos, y devuelve tanto la URL firmada
    de previsualización inline (preview_url) como la de descarga directa (download_url).
    """
    user_id = int(get_jwt_identity())
    res = LessonResource.query.get_or_404(resource_id)
    lesson = Lesson.query.get_or_404(res.lesson_id)

    lesson_state = _find_lesson_state_for_user(user_id, lesson)
    claims = get_jwt()
    if claims.get("role") != "admin" and (
        not lesson_state or not lesson_state["is_unlocked"]
    ):
        return (
            jsonify(
                {
                    "error": "No puedes acceder a los recursos de una clase que aún no se ha desbloqueado."
                }
            ),
            403,
        )

    now = datetime.now(timezone.utc)
    dl_log = ResourceDownloadLog.query.filter_by(
        user_id=user_id, resource_id=res.id
    ).first()
    if not dl_log:
        dl_log = ResourceDownloadLog(
            user_id=user_id,
            resource_id=res.id,
            lesson_id=lesson.id,
            download_count=1,
            first_downloaded_at=now,
            last_downloaded_at=now,
        )
        db.session.add(dl_log)
    else:
        dl_log.download_count = int(dl_log.download_count or 1) + 1
        dl_log.last_downloaded_at = now

    # Actualizar progreso de la clase si la clase no tiene vídeo
    prog = StudentLessonProgress.query.filter_by(
        user_id=user_id, lesson_id=lesson.id
    ).first()
    if not prog:
        prog = StudentLessonProgress(
            user_id=user_id,
            lesson_id=lesson.id,
            last_accessed_at=now,
        )
        db.session.add(prog)
    else:
        prog.last_accessed_at = now

    db.session.flush()

    if not (lesson.has_video and lesson.video_object_key):
        total_res = len(lesson.resources)
        if total_res > 0:
            downloaded_res = ResourceDownloadLog.query.filter_by(
                user_id=user_id, lesson_id=lesson.id
            ).count()
            pct = min(100.0, round((downloaded_res / total_res) * 100.0, 1))
            prog.lesson_progress_percent = max(prog.lesson_progress_percent or 0.0, pct)
            if pct >= 100.0 and not prog.is_completed:
                prog.is_completed = True
                prog.completed_at = now

    db.session.commit()

    cfg = get_storage_config()
    safe_name = sanitize_filename(res.original_filename or res.title or "recurso.pdf")

    # Inferir mime_type adecuado para que el navegador renderice PDFs y Audios inline sin forzar descarga
    raw_mime = (res.mime_type or "").strip()
    guessed_mime = mimetypes.guess_type(safe_name)[0] or ""
    if not raw_mime or raw_mime == "application/octet-stream":
        if res.resource_type == "pdf" or safe_name.lower().endswith(".pdf"):
            effective_mime = "application/pdf"
        elif res.resource_type == "audio" or safe_name.lower().endswith(
            (".mp3", ".wav", ".m4a", ".ogg", ".aac")
        ):
            effective_mime = guessed_mime or "audio/mpeg"
        else:
            effective_mime = guessed_mime or "application/octet-stream"
    else:
        effective_mime = raw_mime

    if res.storage_provider == "s3" and is_s3_ready(cfg):
        download_url = generate_s3_presigned_url(
            method="GET",
            object_key=res.object_key,
            expires_in=900,
            response_content_disposition=f'attachment; filename="{safe_name}"',
            response_content_type=effective_mime,
            cfg=cfg,
        )
        preview_url = generate_s3_presigned_url(
            method="GET",
            object_key=res.object_key,
            expires_in=3600,
            response_content_disposition=f'inline; filename="{safe_name}"',
            response_content_type=effective_mime,
            cfg=cfg,
        )
    else:
        dl_token = create_media_access_token(
            user_id=user_id,
            object_key=res.object_key,
            mime_type=effective_mime,
            disposition="attachment",
            filename=safe_name,
            expires_in=900,
        )
        preview_token = create_media_access_token(
            user_id=user_id,
            object_key=res.object_key,
            mime_type=effective_mime,
            disposition="inline",
            filename=safe_name,
            expires_in=3600,
        )
        base_api = f"{request.host_url.rstrip('/')}/api/campus/media-stream"
        download_url = f"{base_api}/{dl_token}"
        preview_url = f"{base_api}/{preview_token}"

    return (
        jsonify(
            {
                "download_url": download_url,
                "preview_url": preview_url,
                "filename": safe_name,
                "mime_type": effective_mime,
                "resource_type": res.resource_type,
                "download_count": dl_log.download_count,
                "progress": prog.to_dict(),
            }
        ),
        200,
    )


@api_bp.route("/campus/media-stream/<path:token>", methods=["GET"])
def campus_serve_protected_media(token: str):
    """
    Sirve archivos protegidos locales mediante token firmado efímero con soporte
    HTTP Range (206 Partial Content) para streaming de vídeo fluido sin permitir caché pública.
    """
    payload = verify_media_access_token(token)
    if not payload:
        return (
            jsonify({"error": "El enlace seguro ha caducado o no es válido."}),
            403,
        )

    rel_key = (payload.get("k") or "").lstrip("/")
    base_dir = ensure_protected_storage_dir()
    abs_path = os.path.abspath(os.path.join(base_dir, rel_key.replace("/", os.sep)))

    if not abs_path.startswith(base_dir) or not os.path.isfile(abs_path):
        return jsonify({"error": "Archivo no encontrado en el almacenamiento."}), 404

    mime_type = (
        payload.get("m")
        or mimetypes.guess_type(abs_path)[0]
        or "application/octet-stream"
    )
    disposition = payload.get("d") or "inline"
    filename = sanitize_filename(payload.get("f") or os.path.basename(abs_path))

    file_size = os.path.getsize(abs_path)
    range_header = request.headers.get("Range", None)

    if disposition == "attachment":
        resp = send_file(
            abs_path,
            mimetype=mime_type,
            as_attachment=True,
            download_name=filename,
            conditional=True,
        )
        resp.headers["Cache-Control"] = "private, no-store, max-age=0"
        return resp

    # Streaming inline con soporte de HTTP Range (206 Partial Content) para <video>
    if range_header:
        match = re.search(r"bytes=(\d+)-(\d*)", range_header)
        if match:
            byte1 = int(match.group(1))
            byte2 = int(match.group(2)) if match.group(2) else file_size - 1
            byte2 = min(byte2, file_size - 1)
            length = byte2 - byte1 + 1

            with open(abs_path, "rb") as f:
                f.seek(byte1)
                data = f.read(length)

            resp = Response(
                data,
                206,
                mimetype=mime_type,
                direct_passthrough=True,
            )
            resp.headers.add("Content-Range", f"bytes {byte1}-{byte2}/{file_size}")
            resp.headers.add("Accept-Ranges", "bytes")
            resp.headers.add("Content-Length", str(length))
            resp.headers.add("Content-Disposition", f'inline; filename="{filename}"')
            resp.headers.add("Cache-Control", "private, no-store, max-age=0")
            return resp

    resp = send_file(
        abs_path,
        mimetype=mime_type,
        as_attachment=False,
        conditional=True,
    )
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    resp.headers["Cache-Control"] = "private, no-store, max-age=0"
    return resp


# ==========================================================
# 9. CAMPUS: RESERVAS DE SESIONES 1:1 CON LA PSICÓLOGA
# ==========================================================
@api_bp.route("/campus/lessons/<int:lesson_id>/available-slots", methods=["GET"])
@jwt_required()
def campus_get_lesson_slots(lesson_id: int):
    user_id = int(get_jwt_identity())
    user = User.query.get_or_404(user_id)
    lesson = Lesson.query.get_or_404(lesson_id)

    lesson_state = _find_lesson_state_for_user(user.id, lesson)
    if user.role != "admin" and (not lesson_state or not lesson_state["is_unlocked"]):
        return (
            jsonify(
                {
                    "error": "Esta clase aún no está desbloqueada en tu cronograma.",
                }
            ),
            403,
        )

    if not lesson.booking_enabled and user.role != "admin":
        return (
            jsonify(
                {
                    "error": "Esta clase no tiene habilitada la reserva de sesiones.",
                }
            ),
            400,
        )

    week_offset = request.args.get("week_offset", default=1, type=int)
    cfg = get_booking_config()
    slots_data = compute_available_slots(cfg, week_offset=week_offset)

    active_booking = SessionBooking.query.filter_by(
        user_id=user.id, lesson_id=lesson.id, status="confirmed"
    ).first()

    return (
        jsonify(
            {
                "lesson_id": lesson.id,
                "lesson_title": lesson.title,
                "booking_enabled": bool(lesson.booking_enabled),
                "active_booking": active_booking.to_dict() if active_booking else None,
                "booking": active_booking.to_dict() if active_booking else None,
                "range": slots_data.get("range", {}),
                "days": slots_data.get("days", []),
                "total_slots": slots_data.get("total_slots", 0),
                "calendar_connected": slots_data.get("calendar_connected", False),
                "calendar_enabled": slots_data.get("calendar_enabled", False),
                "session_duration_minutes": slots_data.get("session_duration_minutes", 60),
                "slots": slots_data,
            }
        ),
        200,
    )


@api_bp.route("/campus/lessons/<int:lesson_id>/book-session", methods=["POST"])
@jwt_required()
def campus_book_session(lesson_id: int):
    user_id = int(get_jwt_identity())
    user = User.query.get_or_404(user_id)
    lesson = Lesson.query.get_or_404(lesson_id)

    lesson_state = _find_lesson_state_for_user(user.id, lesson)
    if user.role != "admin" and (not lesson_state or not lesson_state["is_unlocked"]):
        return jsonify({"error": "Esta clase aún está bloqueada."}), 403

    if not lesson.booking_enabled and user.role != "admin":
        return jsonify({"error": "Esta clase no permite agendar sesión."}), 400

    # Comprobar si ya tiene una sesión confirmada para esta clase
    existing_booking = SessionBooking.query.filter_by(
        user_id=user.id, lesson_id=lesson.id, status="confirmed"
    ).first()
    if existing_booking:
        return (
            jsonify(
                {
                    "error": "Ya tienes una sesión confirmada para esta clase. Cancélala si deseas elegir otro horario.",
                    "booking": existing_booking.to_dict(),
                }
            ),
            409,
        )

    data = request.get_json() or {}
    start_str = data.get("start_at")
    if not start_str:
        return jsonify({"error": "Debes seleccionar un horario."}), 400

    try:
        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
    except Exception:
        return jsonify({"error": "Formato de fecha u hora no válido."}), 400

    cfg = get_booking_config()
    duration_min = max(15, int(cfg.get("session_duration_minutes") or 60))
    end_dt = start_dt + timedelta(minutes=duration_min)

    # Convertir a naive UTC para MySQL
    start_naive = start_dt.astimezone(timezone.utc).replace(tzinfo=None)
    end_naive = end_dt.astimezone(timezone.utc).replace(tzinfo=None)

    # Evitar colisiones con otras reservas activas en la BD
    overlap = SessionBooking.query.filter(
        SessionBooking.status == "confirmed",
        SessionBooking.start_at < end_naive,
        SessionBooking.end_at > start_naive,
    ).first()
    if overlap:
        return (
            jsonify(
                {
                    "error": "Ese horario acaba de ser ocupado. Por favor, selecciona otro hueco.",
                }
            ),
            409,
        )

    # Identificar curso
    module = CourseModule.query.get(lesson.module_id)
    course_id = module.course_id if module else None

    booking = SessionBooking(
        user_id=user.id,
        lesson_id=lesson.id,
        course_id=course_id,
        start_at=start_naive,
        end_at=end_naive,
        status="confirmed",
    )
    db.session.add(booking)
    db.session.commit()

    # Sincronizar con Google Calendar si está configurado
    cal_log = ""
    if is_calendar_ready(cfg):
        ok, event_id, meet_link, cal_log = create_calendar_event(
            cfg, booking, user, lesson.title
        )
        if ok:
            booking.google_event_id = event_id
            booking.meet_link = meet_link
            booking.calendar_synced = True
            db.session.commit()

    # Formatear fecha y hora para el email
    tz = None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(cfg.get("calendar_timezone") or "Europe/Madrid")
    except Exception:
        pass
    local_start = start_dt.astimezone(tz) if tz else start_dt
    date_str = local_start.strftime("%A, %d de %B de %Y").capitalize()
    time_str = local_start.strftime("%H:%M")

    meet_info_html = ""
    if booking.meet_link:
        meet_info_html = f"""
        <div style="background-color: #EBF3EF; border-left: 4px solid #4A6B5D; padding: 16px; border-radius: 8px; margin: 20px 0;">
          <p style="margin: 0 0 6px 0; font-weight: bold; color: #2C3E35;">Enlace de Google Meet para la videollamada:</p>
          <a href="{booking.meet_link}" style="color: #4A6B5D; font-weight: bold; text-decoration: underline;">{booking.meet_link}</a>
        </div>
        """

    confirm_html = f"""
    <div style="font-family: 'Helvetica Neue', Arial, sans-serif; background-color: #FAF8F5; padding: 36px; color: #2C3E35;">
      <div style="max-width: 580px; margin: 0 auto; background: #FFFFFF; border-radius: 16px; padding: 36px; border: 1px solid #E6E1DA;">
        <span style="background: #4A6B5D; color: #FFFFFF; padding: 4px 12px; border-radius: 99px; font-size: 11px; text-transform: uppercase; letter-spacing: 1px;">
          Sesión 1:1 Confirmada
        </span>
        <h1 style="font-size: 24px; color: #2C3E35; margin: 16px 0 12px 0;">
          Tu sesión con la psicóloga está agendada
        </h1>
        <p style="font-size: 15px; line-height: 1.6; color: #4A5B53;">
          Hola {user.name or ''}, hemos reservado tu sesión individual de 1 hora vinculada a la <strong>{lesson.title}</strong>.
        </p>
        <div style="background-color: #FAF8F5; border-left: 4px solid #D9826C; padding: 18px; border-radius: 8px; margin: 24px 0;">
          <p style="margin: 0 0 6px 0; font-size: 14px;"><strong>Fecha:</strong> {date_str}</p>
          <p style="margin: 0 0 6px 0; font-size: 14px;"><strong>Hora:</strong> {time_str} ({cfg.get('calendar_timezone', 'España')})</p>
          <p style="margin: 0; font-size: 14px;"><strong>Duración:</strong> {duration_min} minutos</p>
        </div>
        {meet_info_html}
        <p style="font-size: 13px; color: #7C8B84; line-height: 1.5;">
          Recibirás también una invitación en tu calendario. Si necesitas cancelar o cambiar tu sesión, puedes hacerlo desde tu campus hasta {cfg.get('booking_cancel_limit_hours', 24)} horas antes de la cita.
        </p>
      </div>
    </div>
    """

    try:
        send_system_email(
            to_email=user.email,
            subject=f"Confirmación de tu sesión con la psicóloga · {date_str}",
            html_body=confirm_html,
        )

        # Notificar también al admin / psicóloga
        admin_user = User.query.filter_by(role="admin").first()
        admin_target = admin_user.email if admin_user else "admin@vuelveati.com"
        admin_notice = f"""
        <div style="font-family: Arial, sans-serif; padding: 24px;">
          <h2>Nueva sesión agendada</h2>
          <p><strong>Alumno:</strong> {user.name} ({user.email})</p>
          <p><strong>Clase:</strong> {lesson.title}</p>
          <p><strong>Fecha y hora:</strong> {date_str} a las {time_str}</p>
          {f'<p><strong>Meet:</strong> <a href="{booking.meet_link}">{booking.meet_link}</a></p>' if booking.meet_link else ''}
        </div>
        """
        send_system_email(
            to_email=admin_target,
            subject=f"Nueva sesión agendada por {user.name} ({date_str})",
            html_body=admin_notice,
        )
    except Exception as email_err:
        print(f"[Aviso] No se pudieron enviar los correos de confirmación: {email_err}")

    return (
        jsonify(
            {
                "message": f"Sesión reservada con éxito para el {date_str} a las {time_str}.",
                "booking": booking.to_dict(),
                "calendar_synced": booking.calendar_synced,
            }
        ),
        201,
    )


@api_bp.route("/campus/bookings/<int:booking_id>", methods=["DELETE"])
@jwt_required()
def campus_cancel_booking(booking_id: int):
    user_id = int(get_jwt_identity())
    booking = SessionBooking.query.get_or_404(booking_id)

    if booking.user_id != user_id:
        return jsonify({"error": "No tienes permiso sobre esta reserva."}), 403

    if booking.status == "cancelled":
        return jsonify({"message": "La sesión ya estaba cancelada."}), 200

    cfg = get_booking_config()
    limit_hours = max(0, int(cfg.get("booking_cancel_limit_hours", 24)))

    now_utc = datetime.now(timezone.utc)
    start_utc = booking.start_at
    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=timezone.utc)

    hours_left = (start_utc - now_utc).total_seconds() / 3600.0
    if hours_left < limit_hours:
        return (
            jsonify(
                {
                    "error": f"Las sesiones solo pueden cancelarse con al menos {limit_hours} horas de antelación.",
                }
            ),
            400,
        )

    # Cancelar en Google Calendar si estaba sincronizado
    if booking.google_event_id:
        delete_calendar_event(cfg, booking.google_event_id)

    booking.status = "cancelled"
    booking.cancelled_by = "student"
    booking.cancelled_at = now_utc
    db.session.commit()

    # Notificar al alumno y a la psicóloga
    user = User.query.get(booking.user_id)
    lesson_name = booking.lesson.title if booking.lesson else "Sesión individual"
    cancel_html = f"""
    <div style="font-family: 'Helvetica Neue', Arial, sans-serif; background-color: #FAF8F5; padding: 32px; color: #2C3E35;">
      <div style="max-width: 540px; margin: 0 auto; background: #FFFFFF; border-radius: 16px; padding: 32px; border: 1px solid #E6E1DA;">
        <h2 style="color: #D9826C; margin-top: 0;">Has cancelado tu sesión</h2>
        <p style="font-size: 15px; line-height: 1.6;">
          Tu sesión correspondiente a <strong>{lesson_name}</strong> ha sido cancelada correctamente.
        </p>
        <p style="font-size: 14px; color: #2C3E35;">
          Cuando quieras, puedes volver a acceder a tu campus y seleccionar otro horario libre disponible.
        </p>
      </div>
    </div>
    """
    if user and user.email:
        send_system_email(
            to_email=user.email,
            subject="Has cancelado tu sesión · Vuelve a Ti",
            html_body=cancel_html,
        )

    admin_user = User.query.filter_by(role="admin").first()
    admin_target = admin_user.email if admin_user else "admin@vuelveati.com"
    send_system_email(
        to_email=admin_target,
        subject=f"Sesión cancelada por el alumno {user.name if user else ''}",
        html_body=f"<p>El alumno {user.name if user else ''} ({user.email if user else ''}) ha cancelado su sesión de {lesson_name}.</p>",
    )

    return (
        jsonify(
            {
                "message": "Sesión cancelada con éxito. Ya puedes elegir otro horario libre si lo deseas.",
                "booking": booking.to_dict(),
            }
        ),
        200,
    )


# ==========================================================
# 10. FORO DE RECURSOS GRATUITOS & CAPTURA DE LEADS (EMAIL MARKETING)
# ==========================================================
def _resolve_public_media_url(
    storage_provider: str,
    object_key: str,
    mime_type: str = "application/octet-stream",
    disposition: str = "inline",
    filename: str = "recurso",
    expires_in: int = 7200,
) -> str:
    """
    Genera una URL firmada temporal (S3/R2 o almacenamiento local protegido)
    para servir imágenes, vídeos, audios o PDFs del Foro de Recursos.
    """
    key_clean = (object_key or "").strip()
    if not key_clean:
        return ""
    cfg = get_storage_config()
    safe_name = sanitize_filename(filename or os.path.basename(key_clean) or "recurso")
    if (storage_provider or "").strip() == "s3" and is_s3_ready(cfg):
        return generate_s3_presigned_url(
            method="GET",
            object_key=key_clean,
            expires_in=expires_in,
            response_content_disposition=f'{disposition}; filename="{safe_name}"',
            response_content_type=mime_type or "application/octet-stream",
            cfg=cfg,
        )
    token = create_media_access_token(
        user_id=0,
        object_key=key_clean,
        mime_type=mime_type or "application/octet-stream",
        disposition=disposition,
        filename=safe_name,
        expires_in=expires_in,
    )
    base_api = f"{request.host_url.rstrip('/')}/api/campus/media-stream"
    return f"{base_api}/{token}"


def _enrich_public_resource_dict(res: PublicResource, unlocked: bool = False) -> dict:
    data = res.to_dict(unlocked=unlocked)

    # Resolver portada (si subió imagen a S3/local o si puso URL externa)
    if res.cover_object_key:
        data["cover_resolved_url"] = _resolve_public_media_url(
            res.cover_storage_provider or "local",
            res.cover_object_key,
            mime_type="image/jpeg",
            disposition="inline",
            filename=os.path.basename(res.cover_object_key),
            expires_in=14400,
        )
    else:
        data["cover_resolved_url"] = res.cover_image_url or ""

    if unlocked:
        # Resolver galería de fotos inline
        resolved_gallery = []
        for item in data.get("gallery_images") or []:
            if isinstance(item, dict):
                entry = dict(item)
                if entry.get("object_key"):
                    entry["resolved_url"] = _resolve_public_media_url(
                        entry.get("storage_provider") or "local",
                        entry["object_key"],
                        mime_type=entry.get("mime_type") or "image/jpeg",
                        disposition="inline",
                        filename=entry.get("original_filename") or "imagen.jpg",
                        expires_in=14400,
                    )
                else:
                    entry["resolved_url"] = entry.get("url") or ""
                resolved_gallery.append(entry)
        data["gallery_images"] = resolved_gallery

        # Resolver Vídeo
        if res.video_object_key:
            data["video_stream_url"] = _resolve_public_media_url(
                res.video_storage_provider or "local",
                res.video_object_key,
                mime_type=res.video_mime_type or "video/mp4",
                disposition="inline",
                filename=res.video_original_name or "video.mp4",
                expires_in=7200,
            )
        else:
            data["video_stream_url"] = ""

        # Resolver Audio
        if res.audio_object_key:
            data["audio_stream_url"] = _resolve_public_media_url(
                res.audio_storage_provider or "local",
                res.audio_object_key,
                mime_type=res.audio_mime_type or "audio/mpeg",
                disposition="inline",
                filename=res.audio_original_name or "audio.mp3",
                expires_in=7200,
            )
        else:
            data["audio_stream_url"] = ""

        # Resolver PDF (tanto para leer en el navegador como para descargar)
        if res.pdf_object_key:
            pdf_fname = res.pdf_original_name or res.pdf_title or f"{res.slug}.pdf"
            if not pdf_fname.lower().endswith(".pdf"):
                pdf_fname = f"{pdf_fname}.pdf"
            data["pdf_preview_url"] = _resolve_public_media_url(
                res.pdf_storage_provider or "local",
                res.pdf_object_key,
                mime_type="application/pdf",
                disposition="inline",
                filename=pdf_fname,
                expires_in=7200,
            )
            data["pdf_download_url"] = _resolve_public_media_url(
                res.pdf_storage_provider or "local",
                res.pdf_object_key,
                mime_type="application/pdf",
                disposition="attachment",
                filename=pdf_fname,
                expires_in=7200,
            )
        else:
            data["pdf_preview_url"] = ""
            data["pdf_download_url"] = ""

    return data


def _extract_visitor_lead_email() -> str:
    email_raw = (
        request.args.get("lead_email")
        or request.headers.get("X-Lead-Email")
        or ""
    ).strip().lower()
    return email_raw


def _check_lead_unlocked(email_clean: str) -> MarketingLead | None:
    if not email_clean or "@" not in email_clean:
        return None
    lead = MarketingLead.query.filter_by(email=email_clean).first()
    if lead and lead.consent_marketing:
        return lead
    return None


def _record_lead_resource_access(lead: MarketingLead, res: PublicResource) -> None:
    now_utc = datetime.now(timezone.utc)
    lead.last_seen_at = now_utc
    try:
        accessed = (
            json.loads(lead.accessed_resources_json)
            if lead.accessed_resources_json
            else []
        )
        if not isinstance(accessed, list):
            accessed = []
    except Exception:
        accessed = []

    already_in = any(
        isinstance(x, dict) and x.get("id") == res.id for x in accessed
    )
    if not already_in:
        accessed.append(
            {
                "id": res.id,
                "title": res.title,
                "slug": res.slug,
                "unlocked_at": now_utc.isoformat(),
            }
        )
        lead.accessed_resources_json = json.dumps(accessed, ensure_ascii=False)
        lead.unlock_count = len(accessed)
        res.unlocks_count = int(res.unlocks_count or 0) + 1


@api_bp.route("/resources", methods=["GET"])
def public_list_resources():
    """
    Lista todos los recursos publicados en el Foro de Recursos.
    Si el visitante ya tiene su email guardado en caché (localStorage) y figura
    en `marketing_leads`, indica `is_lead_recognized: True`.
    """
    visitor_email = _extract_visitor_lead_email()
    lead = _check_lead_unlocked(visitor_email)
    is_unlocked = lead is not None

    resources = (
        PublicResource.query.filter_by(is_published=True)
        .order_by(PublicResource.is_featured.desc(), PublicResource.created_at.desc())
        .all()
    )
    return (
        jsonify(
            {
                "resources": [
                    _enrich_public_resource_dict(r, unlocked=is_unlocked)
                    for r in resources
                ],
                "is_lead_recognized": is_unlocked,
                "lead": lead.to_dict() if lead else None,
            }
        ),
        200,
    )


@api_bp.route("/resources/<string:slug_or_id>", methods=["GET"])
def public_get_resource_detail(slug_or_id: str):
    """
    Devuelve el detalle de un recurso por su slug o ID.
    - Si el visitante aporta un `lead_email` ya registrado con consentimiento de marketing,
      desbloquea automáticamente todo el recurso sin volver a pedir el correo.
    - Si no, devuelve solo el Teaser Editorial para mostrar la tarjeta de captura de email.
    """
    res = PublicResource.query.filter_by(slug=slug_or_id).first()
    if not res and slug_or_id.isdigit():
        res = PublicResource.query.get(int(slug_or_id))
    if not res or not res.is_published:
        return jsonify({"error": "El recurso solicitado no existe o no está disponible."}), 404

    res.views_count = int(res.views_count or 0) + 1

    visitor_email = _extract_visitor_lead_email()
    lead = _check_lead_unlocked(visitor_email)
    is_unlocked = lead is not None

    if is_unlocked and lead:
        _record_lead_resource_access(lead, res)

    db.session.commit()

    # Otros recursos recomendados
    related = (
        PublicResource.query.filter(
            PublicResource.is_published == True,
            PublicResource.id != res.id,
        )
        .order_by(PublicResource.is_featured.desc(), PublicResource.created_at.desc())
        .limit(3)
        .all()
    )

    return (
        jsonify(
            {
                "resource": _enrich_public_resource_dict(res, unlocked=is_unlocked),
                "is_unlocked": is_unlocked,
                "lead": lead.to_dict() if lead else None,
                "related_resources": [
                    _enrich_public_resource_dict(r, unlocked=False) for r in related
                ],
            }
        ),
        200,
    )


@api_bp.route("/resources/<string:slug_or_id>/unlock", methods=["POST"])
def public_unlock_resource(slug_or_id: str):
    """
    Registra o actualiza el correo electrónico del visitante para Email Marketing
    (evitando duplicados si ya existía) y desbloquea al instante el recurso completo.
    """
    res = PublicResource.query.filter_by(slug=slug_or_id).first()
    if not res and slug_or_id.isdigit():
        res = PublicResource.query.get(int(slug_or_id))
    if not res or not res.is_published:
        return jsonify({"error": "Recurso no encontrado."}), 404

    data = request.get_json() or {}
    email_clean = (data.get("email") or "").strip().lower()
    name_clean = (data.get("name") or "").strip()
    consent = bool(data.get("consent_marketing", False))
    source_channel = (data.get("source_channel") or "instagram_reel").strip()

    if not email_clean or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_clean):
        return (
            jsonify({"error": "Por favor, introduce un correo electrónico válido."}),
            400,
        )

    if not consent:
        return (
            jsonify(
                {
                    "error": "Debes aceptar el consentimiento de comunicaciones y email marketing para desbloquear el recurso gratuito."
                }
            ),
            400,
        )

    now_utc = datetime.now(timezone.utc)
    lead = MarketingLead.query.filter_by(email=email_clean).first()

    if not lead:
        accessed_init = [
            {
                "id": res.id,
                "title": res.title,
                "slug": res.slug,
                "unlocked_at": now_utc.isoformat(),
            }
        ]
        lead = MarketingLead(
            email=email_clean,
            name=name_clean,
            consent_marketing=True,
            consent_text=(
                data.get("consent_text")
                or "Acepto recibir recursos gratuitos, novedades y comunicaciones de marketing de Carla Viladot."
            ),
            source_resource_id=res.id,
            source_resource_title=res.title,
            source_channel=source_channel,
            accessed_resources_json=json.dumps(accessed_init, ensure_ascii=False),
            unlock_count=1,
            created_at=now_utc,
            last_seen_at=now_utc,
        )
        db.session.add(lead)
        res.unlocks_count = int(res.unlocks_count or 0) + 1
    else:
        lead.consent_marketing = True
        if name_clean and not lead.name:
            lead.name = name_clean
        _record_lead_resource_access(lead, res)

    db.session.commit()

    return (
        jsonify(
            {
                "message": "¡Recurso desbloqueado! Hemos guardado tu acceso en este navegador para que no tengas que volver a introducir tu correo.",
                "unlocked": True,
                "lead": lead.to_dict(),
                "resource": _enrich_public_resource_dict(res, unlocked=True),
            }
        ),
        200,
    )


# --- Endpoints de Administración del Foro de Recursos & Leads ---
@api_bp.route("/admin/public-resources", methods=["GET"])
@admin_required
def admin_list_public_resources():
    resources = PublicResource.query.order_by(
        PublicResource.is_featured.desc(), PublicResource.created_at.desc()
    ).all()
    total_leads = MarketingLead.query.count()
    total_views = sum(int(r.views_count or 0) for r in resources)
    total_unlocks = sum(int(r.unlocks_count or 0) for r in resources)

    return (
        jsonify(
            {
                "resources": [
                    _enrich_public_resource_dict(r, unlocked=True) for r in resources
                ],
                "stats": {
                    "total_resources": len(resources),
                    "total_leads": total_leads,
                    "total_views": total_views,
                    "total_unlocks": total_unlocks,
                },
            }
        ),
        200,
    )


def _apply_resource_payload(res: PublicResource, data: dict) -> None:
    if "title" in data and (data.get("title") or "").strip():
        res.title = data["title"].strip()

    if "slug" in data and (data.get("slug") or "").strip():
        desired_slug = slugify(data["slug"])
        existing = PublicResource.query.filter(
            PublicResource.slug == desired_slug, PublicResource.id != res.id
        ).first()
        if not existing:
            res.slug = desired_slug

    if "subtitle" in data:
        res.subtitle = (data.get("subtitle") or "").strip()
    if "category" in data:
        res.category = (data.get("category") or "Regulación Emocional").strip()
    if "instagram_keyword" in data:
        res.instagram_keyword = (data.get("instagram_keyword") or "RECURSO").strip().upper()
    if "read_time_minutes" in data:
        res.read_time_minutes = max(1, int(data.get("read_time_minutes") or 5))
    if "is_published" in data:
        res.is_published = bool(data["is_published"])
    if "is_featured" in data:
        res.is_featured = bool(data["is_featured"])

    if "cover_image_url" in data:
        res.cover_image_url = (data.get("cover_image_url") or "").strip()
    if "cover_storage_provider" in data:
        res.cover_storage_provider = (data.get("cover_storage_provider") or "").strip()
    if "cover_object_key" in data:
        res.cover_object_key = (data.get("cover_object_key") or "").strip()

    if "teaser_text" in data:
        res.teaser_text = data.get("teaser_text") or ""
    if "content_body" in data:
        res.content_body = data.get("content_body") or ""

    if "key_takeaways" in data:
        kt = data.get("key_takeaways")
        if isinstance(kt, list):
            res.key_takeaways_json = json.dumps(
                [str(x).strip() for x in kt if str(x).strip()], ensure_ascii=False
            )
    if "gallery_images" in data:
        gi = data.get("gallery_images")
        if isinstance(gi, list):
            res.gallery_images_json = json.dumps(gi, ensure_ascii=False)

    if "video_external_url" in data:
        res.video_external_url = (data.get("video_external_url") or "").strip()
    if "video_storage_provider" in data:
        res.video_storage_provider = (data.get("video_storage_provider") or "").strip()
    if "video_object_key" in data:
        res.video_object_key = (data.get("video_object_key") or "").strip()
    if "video_original_name" in data:
        res.video_original_name = (data.get("video_original_name") or "").strip()
    if "video_mime_type" in data:
        res.video_mime_type = (data.get("video_mime_type") or "video/mp4").strip()

    if "audio_title" in data:
        res.audio_title = (data.get("audio_title") or "").strip()
    if "audio_storage_provider" in data:
        res.audio_storage_provider = (data.get("audio_storage_provider") or "").strip()
    if "audio_object_key" in data:
        res.audio_object_key = (data.get("audio_object_key") or "").strip()
    if "audio_original_name" in data:
        res.audio_original_name = (data.get("audio_original_name") or "").strip()
    if "audio_mime_type" in data:
        res.audio_mime_type = (data.get("audio_mime_type") or "audio/mpeg").strip()

    if "pdf_title" in data:
        res.pdf_title = (data.get("pdf_title") or "").strip()
    if "pdf_storage_provider" in data:
        res.pdf_storage_provider = (data.get("pdf_storage_provider") or "").strip()
    if "pdf_object_key" in data:
        res.pdf_object_key = (data.get("pdf_object_key") or "").strip()
    if "pdf_original_name" in data:
        res.pdf_original_name = (data.get("pdf_original_name") or "").strip()
    if "pdf_file_size_bytes" in data:
        res.pdf_file_size_bytes = int(data.get("pdf_file_size_bytes") or 0)


@api_bp.route("/admin/public-resources", methods=["POST"])
@admin_required
def admin_create_public_resource():
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "El título del recurso es obligatorio."}), 400

    base_slug = slugify(data.get("slug") or title)
    slug = base_slug
    counter = 2
    while PublicResource.query.filter_by(slug=slug).first():
        slug = f"{base_slug}-{counter}"
        counter += 1

    res = PublicResource(
        title=title,
        slug=slug,
        subtitle=(data.get("subtitle") or "").strip(),
        category=(data.get("category") or "Regulación Emocional").strip(),
        instagram_keyword=(data.get("instagram_keyword") or "RECURSO").strip().upper(),
        read_time_minutes=max(1, int(data.get("read_time_minutes") or 5)),
        is_published=bool(data.get("is_published", True)),
        is_featured=bool(data.get("is_featured", False)),
    )
    db.session.add(res)
    db.session.flush()
    _apply_resource_payload(res, data)
    db.session.commit()

    return (
        jsonify(
            {
                "message": f"Recurso '{res.title}' creado correctamente.",
                "resource": _enrich_public_resource_dict(res, unlocked=True),
            }
        ),
        201,
    )


@api_bp.route("/admin/public-resources/<int:res_id>", methods=["PUT"])
@admin_required
def admin_update_public_resource(res_id: int):
    res = PublicResource.query.get_or_404(res_id)
    data = request.get_json() or {}
    _apply_resource_payload(res, data)
    res.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    return (
        jsonify(
            {
                "message": f"Recurso '{res.title}' guardado correctamente.",
                "resource": _enrich_public_resource_dict(res, unlocked=True),
            }
        ),
        200,
    )


@api_bp.route("/admin/public-resources/<int:res_id>", methods=["DELETE"])
@admin_required
def admin_delete_public_resource(res_id: int):
    res = PublicResource.query.get_or_404(res_id)
    db.session.delete(res)
    db.session.commit()
    return jsonify({"message": "Recurso eliminado correctamente."}), 200


@api_bp.route("/admin/marketing-leads", methods=["GET"])
@admin_required
def admin_list_marketing_leads():
    leads = MarketingLead.query.order_by(MarketingLead.created_at.desc()).all()
    resources = PublicResource.query.order_by(PublicResource.title.asc()).all()

    return (
        jsonify(
            {
                "leads": [l.to_dict() for l in leads],
                "resources": [
                    {"id": r.id, "title": r.title, "slug": r.slug, "unlocks_count": r.unlocks_count}
                    for r in resources
                ],
                "stats": {
                    "total_leads": len(leads),
                    "total_unlocks": sum(int(l.unlock_count or 1) for l in leads),
                },
            }
        ),
        200,
    )


@api_bp.route("/admin/marketing-leads/<int:lead_id>", methods=["DELETE"])
@admin_required
def admin_delete_marketing_lead(lead_id: int):
    lead = MarketingLead.query.get_or_404(lead_id)
    db.session.delete(lead)
    db.session.commit()
    return jsonify({"message": "Suscriptor eliminado de la lista de marketing."}), 200

