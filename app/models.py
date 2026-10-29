from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(120), nullable=True, default="Alumno/a")
    email = db.Column(db.String(150), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(
        db.Enum("admin", "user", name="user_roles"),
        nullable=False,
        default="user",
    )
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    enrollments = db.relationship(
        "Enrollment",
        backref="user",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name or self.email.split("@")[0],
            "email": self.email,
            "role": self.role,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "is_active": bool(self.is_active),
        }


class SystemConfig(db.Model):
    __tablename__ = "system_config"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    smtp_host = db.Column(db.String(150), nullable=True, default="smtp.gmail.com")
    smtp_port = db.Column(db.Integer, nullable=True, default=587)
    smtp_user = db.Column(db.String(150), nullable=True, default="")
    smtp_password = db.Column(db.String(255), nullable=True, default="")
    use_gmail_api = db.Column(db.Boolean, nullable=False, default=False)
    gmail_token_json = db.Column(db.Text, nullable=True, default="")

    # Configuración S3 / Cloudflare R2
    s3_enabled = db.Column(db.Boolean, nullable=False, default=False)
    s3_provider = db.Column(db.String(50), nullable=True, default="cloudflare_r2")
    s3_endpoint_url = db.Column(db.String(255), nullable=True, default="")
    s3_region = db.Column(db.String(80), nullable=True, default="auto")
    s3_bucket_name = db.Column(db.String(150), nullable=True, default="")
    s3_access_key_id = db.Column(db.String(255), nullable=True, default="")
    s3_secret_access_key = db.Column(db.String(255), nullable=True, default="")
    presigned_url_expiration = db.Column(db.Integer, nullable=True, default=1800)

    # Agenda de sesiones: Google Calendar + Google Meet
    calendar_enabled = db.Column(db.Boolean, nullable=False, default=False)
    calendar_use_gmail_token = db.Column(db.Boolean, nullable=False, default=True)
    calendar_token_json = db.Column(db.Text, nullable=True, default="")
    calendar_id = db.Column(db.String(255), nullable=True, default="primary")
    calendar_timezone = db.Column(db.String(64), nullable=True, default="Europe/Madrid")
    session_duration_minutes = db.Column(db.Integer, nullable=True, default=60)
    booking_working_hours = db.Column(db.Text, nullable=True, default="")
    booking_min_notice_hours = db.Column(db.Integer, nullable=True, default=24)
    booking_cancel_limit_hours = db.Column(db.Integer, nullable=True, default=24)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "smtp_host": self.smtp_host or "smtp.gmail.com",
            "smtp_port": self.smtp_port or 587,
            "smtp_user": self.smtp_user or "",
            "smtp_password": self.smtp_password or "",
            "use_gmail_api": bool(self.use_gmail_api),
            "gmail_token_json": self.gmail_token_json or "",
            "s3_enabled": bool(self.s3_enabled),
            "s3_provider": self.s3_provider or "cloudflare_r2",
            "s3_endpoint_url": self.s3_endpoint_url or "",
            "s3_region": self.s3_region or "auto",
            "s3_bucket_name": self.s3_bucket_name or "",
            "s3_access_key_id": self.s3_access_key_id or "",
            "s3_secret_access_key": self.s3_secret_access_key or "",
            "presigned_url_expiration": int(self.presigned_url_expiration or 1800),
            "calendar_enabled": bool(self.calendar_enabled),
            "calendar_use_gmail_token": bool(
                True if self.calendar_use_gmail_token is None else self.calendar_use_gmail_token
            ),
            "calendar_token_json": self.calendar_token_json or "",
            "calendar_id": self.calendar_id or "primary",
            "calendar_timezone": self.calendar_timezone or "Europe/Madrid",
            "session_duration_minutes": int(self.session_duration_minutes or 60),
            "booking_working_hours": self.booking_working_hours or "",
            "booking_min_notice_hours": int(
                24 if self.booking_min_notice_hours is None else self.booking_min_notice_hours
            ),
            "booking_cancel_limit_hours": int(
                24 if self.booking_cancel_limit_hours is None else self.booking_cancel_limit_hours
            ),
        }


class ContactMessage(db.Model):
    __tablename__ = "contact_messages"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(50), nullable=True)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "phone": self.phone or "",
            "message": self.message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ==========================================================
# MODELOS DE CURSOS, MÓDULOS, CLASES, RECURSOS Y PROGRESO
# ==========================================================
class Course(db.Model):
    __tablename__ = "courses"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    title = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(200), unique=True, nullable=False, index=True)
    subtitle = db.Column(db.String(350), nullable=True, default="")
    description = db.Column(db.Text, nullable=True, default="")
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    # Configuración del cronograma (drip content)
    # 'weekly_days' -> Clase 1 el día de pago/alta, siguientes cada día de la semana en drip_weekdays (0=Lunes..6=Domingo)
    # 'interval_days' -> Clase 1 el día de pago/alta, siguientes cada drip_interval_days días
    # 'immediate' -> Todo el curso desbloqueado desde el primer día
    drip_mode = db.Column(db.String(40), nullable=False, default="weekly_days")
    drip_weekdays = db.Column(db.String(50), nullable=False, default="0")  # 0 = Lunes
    drip_interval_days = db.Column(db.Integer, nullable=False, default=7)

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    modules = db.relationship(
        "CourseModule",
        backref="course",
        cascade="all, delete-orphan",
        order_by="CourseModule.order_index.asc(), CourseModule.id.asc()",
    )

    enrollments = db.relationship(
        "Enrollment",
        backref="course",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def get_weekdays_list(self) -> list[int]:
        raw = (self.drip_weekdays or "0").split(",")
        days = []
        for item in raw:
            item = item.strip()
            if item.isdigit():
                val = int(item)
                if 0 <= val <= 6 and val not in days:
                    days.append(val)
        return sorted(days) if days else [0]

    def to_dict(self, include_modules: bool = True) -> dict:
        data = {
            "id": self.id,
            "title": self.title,
            "slug": self.slug,
            "subtitle": self.subtitle or "",
            "description": self.description or "",
            "is_active": bool(self.is_active),
            "drip_mode": self.drip_mode or "weekly_days",
            "drip_weekdays": self.drip_weekdays or "0",
            "drip_weekdays_list": self.get_weekdays_list(),
            "drip_interval_days": int(self.drip_interval_days or 7),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_modules:
            data["modules"] = [m.to_dict(include_lessons=True) for m in self.modules]
            data["total_modules"] = len(self.modules)
            data["total_lessons"] = sum(len(m.lessons) for m in self.modules)
        return data


class CourseModule(db.Model):
    __tablename__ = "course_modules"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    course_id = db.Column(
        db.Integer,
        db.ForeignKey("courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = db.Column(db.String(200), nullable=False)
    subtitle = db.Column(db.Text, nullable=True, default="")
    order_index = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    lessons = db.relationship(
        "Lesson",
        backref="module",
        cascade="all, delete-orphan",
        order_by="Lesson.order_index.asc(), Lesson.id.asc()",
    )

    def to_dict(self, include_lessons: bool = True) -> dict:
        data = {
            "id": self.id,
            "course_id": self.course_id,
            "title": self.title,
            "subtitle": self.subtitle or "",
            "order_index": self.order_index,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_lessons:
            data["lessons"] = [l.to_dict(include_resources=True) for l in self.lessons]
        return data


class Lesson(db.Model):
    __tablename__ = "lessons"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    module_id = db.Column(
        db.Integer,
        db.ForeignKey("course_modules.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True, default="")
    order_index = db.Column(db.Integer, nullable=False, default=1)

    # Vídeo opcional
    has_video = db.Column(db.Boolean, nullable=False, default=False)
    video_storage_provider = db.Column(db.String(30), nullable=False, default="s3")
    video_object_key = db.Column(db.String(500), nullable=True, default="")
    video_original_name = db.Column(db.String(255), nullable=True, default="")
    video_mime_type = db.Column(db.String(100), nullable=True, default="video/mp4")
    video_size_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    video_duration_seconds = db.Column(db.Integer, nullable=False, default=0)

    # Si está activo, al desbloquearse esta clase el alumno puede reservar
    # una sesión 1:1 con la psicóloga para la semana siguiente.
    booking_enabled = db.Column(db.Boolean, nullable=False, default=False)

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    resources = db.relationship(
        "LessonResource",
        backref="lesson",
        cascade="all, delete-orphan",
        order_by="LessonResource.order_index.asc(), LessonResource.id.asc()",
    )

    def to_dict(self, include_resources: bool = True) -> dict:
        data = {
            "id": self.id,
            "module_id": self.module_id,
            "title": self.title,
            "description": self.description or "",
            "order_index": self.order_index,
            "has_video": bool(self.has_video and self.video_object_key),
            "video_storage_provider": self.video_storage_provider or "s3",
            "video_object_key": self.video_object_key or "",
            "video_original_name": self.video_original_name or "",
            "video_mime_type": self.video_mime_type or "video/mp4",
            "video_size_bytes": int(self.video_size_bytes or 0),
            "video_duration_seconds": int(self.video_duration_seconds or 0),
            "booking_enabled": bool(self.booking_enabled),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_resources:
            data["resources"] = [r.to_dict() for r in self.resources]
        return data


class LessonResource(db.Model):
    __tablename__ = "lesson_resources"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    lesson_id = db.Column(
        db.Integer,
        db.ForeignKey("lessons.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = db.Column(db.String(200), nullable=False)
    resource_type = db.Column(
        db.String(50), nullable=False, default="pdf"
    )  # pdf, word, presentation, audio, other
    storage_provider = db.Column(db.String(30), nullable=False, default="s3")
    object_key = db.Column(db.String(500), nullable=False)
    original_filename = db.Column(db.String(255), nullable=True, default="")
    mime_type = db.Column(db.String(120), nullable=True, default="application/pdf")
    file_size_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    order_index = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "lesson_id": self.lesson_id,
            "title": self.title,
            "resource_type": self.resource_type or "pdf",
            "storage_provider": self.storage_provider or "s3",
            "object_key": self.object_key,
            "original_filename": self.original_filename or self.title,
            "mime_type": self.mime_type or "application/octet-stream",
            "file_size_bytes": int(self.file_size_bytes or 0),
            "order_index": self.order_index,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Enrollment(db.Model):
    __tablename__ = "enrollments"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    course_id = db.Column(
        db.Integer,
        db.ForeignKey("courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    enrolled_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    status = db.Column(db.String(30), nullable=False, default="active")
    unlock_all_override = db.Column(db.Boolean, nullable=False, default=False)

    __table_args__ = (
        db.UniqueConstraint("user_id", "course_id", name="uq_user_course"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "course_id": self.course_id,
            "enrolled_at": self.enrolled_at.isoformat() if self.enrolled_at else None,
            "status": self.status,
            "unlock_all_override": bool(self.unlock_all_override),
        }


class StudentLessonOverride(db.Model):
    __tablename__ = "student_lesson_overrides"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lesson_id = db.Column(
        db.Integer,
        db.ForeignKey("lessons.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 'unlocked' o 'locked'
    force_state = db.Column(db.String(20), nullable=False, default="unlocked")
    note = db.Column(db.String(255), nullable=True, default="")
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "lesson_id", name="uq_override_user_lesson"
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "lesson_id": self.lesson_id,
            "force_state": self.force_state,
            "note": self.note or "",
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class StudentLessonProgress(db.Model):
    __tablename__ = "student_lesson_progress"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lesson_id = db.Column(
        db.Integer,
        db.ForeignKey("lessons.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    watched_seconds = db.Column(db.Float, nullable=False, default=0.0)
    max_watched_seconds = db.Column(db.Float, nullable=False, default=0.0)
    video_duration_seconds = db.Column(db.Float, nullable=False, default=0.0)
    video_progress_percent = db.Column(db.Float, nullable=False, default=0.0)
    lesson_progress_percent = db.Column(db.Float, nullable=False, default=0.0)
    is_completed = db.Column(db.Boolean, nullable=False, default=False)
    completed_at = db.Column(db.DateTime, nullable=True)
    last_accessed_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "lesson_id", name="uq_progress_user_lesson"
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "lesson_id": self.lesson_id,
            "watched_seconds": round(float(self.watched_seconds or 0.0), 1),
            "max_watched_seconds": round(float(self.max_watched_seconds or 0.0), 1),
            "video_duration_seconds": round(
                float(self.video_duration_seconds or 0.0), 1
            ),
            "video_progress_percent": round(
                float(self.video_progress_percent or 0.0), 1
            ),
            "lesson_progress_percent": round(
                float(self.lesson_progress_percent or 0.0), 1
            ),
            "is_completed": bool(self.is_completed),
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
            "last_accessed_at": (
                self.last_accessed_at.isoformat() if self.last_accessed_at else None
            ),
        }


class ResourceDownloadLog(db.Model):
    __tablename__ = "resource_download_logs"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource_id = db.Column(
        db.Integer,
        db.ForeignKey("lesson_resources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lesson_id = db.Column(
        db.Integer,
        db.ForeignKey("lessons.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    download_count = db.Column(db.Integer, nullable=False, default=1)
    first_downloaded_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_downloaded_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "resource_id", name="uq_download_user_resource"
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "resource_id": self.resource_id,
            "lesson_id": self.lesson_id,
            "download_count": int(self.download_count or 1),
            "first_downloaded_at": (
                self.first_downloaded_at.isoformat()
                if self.first_downloaded_at
                else None
            ),
            "last_downloaded_at": (
                self.last_downloaded_at.isoformat()
                if self.last_downloaded_at
                else None
            ),
        }


def _iso_utc(dt: datetime | None) -> str | None:
    """MySQL guarda DATETIME sin zona: lo tratamos siempre como UTC explícito."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class SessionBooking(db.Model):
    """Sesión 1:1 reservada por un alumno desde una clase con agenda activa."""

    __tablename__ = "session_bookings"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lesson_id = db.Column(
        db.Integer,
        db.ForeignKey("lessons.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    course_id = db.Column(
        db.Integer,
        db.ForeignKey("courses.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    start_at = db.Column(db.DateTime, nullable=False, index=True)  # UTC
    end_at = db.Column(db.DateTime, nullable=False)  # UTC
    # 'confirmed' | 'cancelled'
    status = db.Column(db.String(20), nullable=False, default="confirmed", index=True)
    google_event_id = db.Column(db.String(255), nullable=True, default="")
    meet_link = db.Column(db.String(500), nullable=True, default="")
    calendar_synced = db.Column(db.Boolean, nullable=False, default=False)
    cancelled_by = db.Column(db.String(20), nullable=True)  # 'student' | 'admin'
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    cancelled_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", lazy="joined")
    lesson = db.relationship("Lesson", lazy="joined")

    def to_dict(self, include_relations: bool = False) -> dict:
        data = {
            "id": self.id,
            "user_id": self.user_id,
            "lesson_id": self.lesson_id,
            "course_id": self.course_id,
            "start_at": _iso_utc(self.start_at),
            "end_at": _iso_utc(self.end_at),
            "status": self.status,
            "google_event_id": self.google_event_id or "",
            "meet_link": self.meet_link or "",
            "calendar_synced": bool(self.calendar_synced),
            "cancelled_by": self.cancelled_by,
            "created_at": _iso_utc(self.created_at),
            "cancelled_at": _iso_utc(self.cancelled_at),
        }
        if include_relations:
            data["user"] = self.user.to_dict() if self.user else None
            data["lesson_title"] = self.lesson.title if self.lesson else ""
        return data


class PublicResource(db.Model):
    """
    Recurso público / Lead Magnet del Foro de Recursos ("Vuelve a Ti").
    Muestra un teaser abierto y desbloquea el artículo completo, vídeo, audio y PDF
    cuando el visitante introduce su correo electrónico aceptando comunicaciones de marketing.
    """

    __tablename__ = "public_resources"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(220), nullable=False)
    slug = db.Column(db.String(180), unique=True, nullable=False, index=True)
    subtitle = db.Column(db.String(320), nullable=True, default="")
    category = db.Column(
        db.String(80), nullable=False, default="Regulación Emocional"
    )
    instagram_keyword = db.Column(db.String(80), nullable=True, default="RECURSO")
    read_time_minutes = db.Column(db.Integer, nullable=False, default=5)
    is_published = db.Column(db.Boolean, nullable=False, default=True, index=True)
    is_featured = db.Column(db.Boolean, nullable=False, default=False)

    # Portada
    cover_image_url = db.Column(db.String(600), nullable=True, default="")
    cover_storage_provider = db.Column(db.String(30), nullable=True, default="")
    cover_object_key = db.Column(db.String(500), nullable=True, default="")

    # Contenido editorial (Teaser abierto + Artículo completo desbloqueable)
    teaser_text = db.Column(db.Text, nullable=True, default="")
    content_body = db.Column(db.Text, nullable=True, default="")
    key_takeaways_json = db.Column(db.Text, nullable=True, default="[]")
    gallery_images_json = db.Column(db.Text, nullable=True, default="[]")

    # Vídeo opcional (enlace externo o archivo subido)
    video_external_url = db.Column(db.String(600), nullable=True, default="")
    video_storage_provider = db.Column(db.String(30), nullable=True, default="")
    video_object_key = db.Column(db.String(500), nullable=True, default="")
    video_original_name = db.Column(db.String(255), nullable=True, default="")
    video_mime_type = db.Column(db.String(100), nullable=True, default="video/mp4")

    # Audio / Meditación opcional
    audio_title = db.Column(db.String(220), nullable=True, default="")
    audio_storage_provider = db.Column(db.String(30), nullable=True, default="")
    audio_object_key = db.Column(db.String(500), nullable=True, default="")
    audio_original_name = db.Column(db.String(255), nullable=True, default="")
    audio_mime_type = db.Column(db.String(100), nullable=True, default="audio/mpeg")

    # PDF / Cuaderno descargable opcional
    pdf_title = db.Column(db.String(220), nullable=True, default="")
    pdf_storage_provider = db.Column(db.String(30), nullable=True, default="")
    pdf_object_key = db.Column(db.String(500), nullable=True, default="")
    pdf_original_name = db.Column(db.String(255), nullable=True, default="")
    pdf_file_size_bytes = db.Column(db.BigInteger, nullable=False, default=0)

    # Analítica
    views_count = db.Column(db.Integer, nullable=False, default=0)
    unlocks_count = db.Column(db.Integer, nullable=False, default=0)

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self, unlocked: bool = False) -> dict:
        import json

        try:
            takeaways = (
                json.loads(self.key_takeaways_json)
                if self.key_takeaways_json
                else []
            )
            if not isinstance(takeaways, list):
                takeaways = []
        except Exception:
            takeaways = []

        try:
            gallery = (
                json.loads(self.gallery_images_json)
                if self.gallery_images_json
                else []
            )
            if not isinstance(gallery, list):
                gallery = []
        except Exception:
            gallery = []

        has_video = bool(
            (self.video_external_url or "").strip()
            or (self.video_object_key or "").strip()
        )
        has_audio = bool((self.audio_object_key or "").strip())
        has_pdf = bool((self.pdf_object_key or "").strip())

        data = {
            "id": self.id,
            "title": self.title,
            "slug": self.slug,
            "subtitle": self.subtitle or "",
            "category": self.category or "Regulación Emocional",
            "instagram_keyword": self.instagram_keyword or "RECURSO",
            "read_time_minutes": int(self.read_time_minutes or 5),
            "is_published": bool(self.is_published),
            "is_featured": bool(self.is_featured),
            "cover_image_url": self.cover_image_url or "",
            "cover_storage_provider": self.cover_storage_provider or "",
            "cover_object_key": self.cover_object_key or "",
            "teaser_text": self.teaser_text or "",
            "key_takeaways": takeaways,
            "has_video": has_video,
            "has_audio": has_audio,
            "has_pdf": has_pdf,
            "audio_title": self.audio_title or self.audio_original_name or "Audio guiado del recurso",
            "pdf_title": self.pdf_title or self.pdf_original_name or "Cuaderno Práctico Descargable (PDF)",
            "pdf_file_size_bytes": int(self.pdf_file_size_bytes or 0),
            "views_count": int(self.views_count or 0),
            "unlocks_count": int(self.unlocks_count or 0),
            "is_unlocked": bool(unlocked),
            "created_at": _iso_utc(self.created_at),
            "updated_at": _iso_utc(self.updated_at),
        }

        if unlocked:
            data.update(
                {
                    "content_body": self.content_body or "",
                    "gallery_images": gallery,
                    "video_external_url": self.video_external_url or "",
                    "video_storage_provider": self.video_storage_provider or "",
                    "video_object_key": self.video_object_key or "",
                    "video_original_name": self.video_original_name or "",
                    "video_mime_type": self.video_mime_type or "video/mp4",
                    "audio_storage_provider": self.audio_storage_provider or "",
                    "audio_object_key": self.audio_object_key or "",
                    "audio_original_name": self.audio_original_name or "",
                    "audio_mime_type": self.audio_mime_type or "audio/mpeg",
                    "pdf_storage_provider": self.pdf_storage_provider or "",
                    "pdf_object_key": self.pdf_object_key or "",
                    "pdf_original_name": self.pdf_original_name or "",
                }
            )
        return data


class MarketingLead(db.Model):
    """
    Suscriptor / Lead de Email Marketing captado desde el Foro de Recursos.
    Si el email ya existe, se actualiza su actividad sin duplicar la fila.
    """

    __tablename__ = "marketing_leads"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(180), unique=True, nullable=False, index=True)
    name = db.Column(db.String(140), nullable=True, default="")
    consent_marketing = db.Column(db.Boolean, nullable=False, default=True)
    consent_text = db.Column(
        db.String(500),
        nullable=True,
        default="Acepto recibir recursos gratuitos, novedades y comunicaciones de marketing de Carla Viladot.",
    )
    source_resource_id = db.Column(
        db.Integer,
        db.ForeignKey("public_resources.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_resource_title = db.Column(db.String(220), nullable=True, default="")
    source_channel = db.Column(db.String(80), nullable=True, default="instagram_reel")
    accessed_resources_json = db.Column(db.Text, nullable=True, default="[]")
    unlock_count = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    last_seen_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        import json

        try:
            accessed = (
                json.loads(self.accessed_resources_json)
                if self.accessed_resources_json
                else []
            )
            if not isinstance(accessed, list):
                accessed = []
        except Exception:
            accessed = []

        return {
            "id": self.id,
            "email": self.email,
            "name": self.name or "",
            "consent_marketing": bool(self.consent_marketing),
            "consent_text": self.consent_text or "",
            "source_resource_id": self.source_resource_id,
            "source_resource_title": self.source_resource_title or "Foro de Recursos",
            "source_channel": self.source_channel or "instagram_reel",
            "accessed_resources": accessed,
            "unlock_count": int(self.unlock_count or 1),
            "created_at": _iso_utc(self.created_at),
            "last_seen_at": _iso_utc(self.last_seen_at),
        }

