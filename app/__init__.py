import os
import pymysql
from datetime import datetime, timezone
from flask import Flask, jsonify
from flask_cors import CORS
from flask_jwt_extended import JWTManager

from app.config import Config
from app.models import (
    Course,
    CourseModule,
    Enrollment,
    Lesson,
    LessonResource,
    SystemConfig,
    User,
    db,
)
from app.routes import api_bp
from app.storage_service import ensure_protected_storage_dir

jwt = JWTManager()


def ensure_mysql_database_exists() -> None:
    """
    Intenta crear la base de datos `psicologia` y asegurar las columnas nuevas
    en `system_config` antes de inicializar las tablas con SQLAlchemy.
    """
    try:
        conn = pymysql.connect(
            host=Config.DB_HOST,
            port=int(Config.DB_PORT),
            user=Config.DB_USER,
            password=Config.DB_PASSWORD,
            charset="utf8mb4",
        )
        with conn.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{Config.DB_NAME}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
            )
            cursor.execute(f"USE `{Config.DB_NAME}`;")
            # Verificar si system_config ya existe y añadir columnas S3 si faltan
            cursor.execute("SHOW TABLES LIKE 'system_config';")
            if cursor.fetchone():
                new_cols = [
                    ("s3_enabled", "TINYINT(1) NOT NULL DEFAULT 0"),
                    ("s3_provider", "VARCHAR(50) DEFAULT 'cloudflare_r2'"),
                    ("s3_endpoint_url", "VARCHAR(255) DEFAULT ''"),
                    ("s3_region", "VARCHAR(80) DEFAULT 'auto'"),
                    ("s3_bucket_name", "VARCHAR(150) DEFAULT ''"),
                    ("s3_access_key_id", "VARCHAR(255) DEFAULT ''"),
                    ("s3_secret_access_key", "VARCHAR(255) DEFAULT ''"),
                    ("presigned_url_expiration", "INT DEFAULT 1800"),
                    ("calendar_enabled", "TINYINT(1) NOT NULL DEFAULT 0"),
                    ("calendar_use_gmail_token", "TINYINT(1) NOT NULL DEFAULT 1"),
                    ("calendar_token_json", "LONGTEXT NULL"),
                    ("calendar_id", "VARCHAR(255) DEFAULT 'primary'"),
                    ("calendar_timezone", "VARCHAR(64) DEFAULT 'Europe/Madrid'"),
                    ("session_duration_minutes", "INT DEFAULT 60"),
                    ("booking_working_hours", "TEXT NULL"),
                    ("booking_min_notice_hours", "INT DEFAULT 24"),
                    ("booking_cancel_limit_hours", "INT DEFAULT 24"),
                ]
                for col_name, col_def in new_cols:
                    cursor.execute(
                        f"SHOW COLUMNS FROM `system_config` LIKE '{col_name}';"
                    )
                    if not cursor.fetchone():
                        cursor.execute(
                            f"ALTER TABLE `system_config` ADD COLUMN `{col_name}` {col_def};"
                        )

            # Columna para activar la agenda de sesiones en una clase
            cursor.execute("SHOW TABLES LIKE 'lessons';")
            if cursor.fetchone():
                cursor.execute("SHOW COLUMNS FROM `lessons` LIKE 'booking_enabled';")
                if not cursor.fetchone():
                    cursor.execute(
                        "ALTER TABLE `lessons` ADD COLUMN `booking_enabled` TINYINT(1) NOT NULL DEFAULT 0;"
                    )
        conn.commit()
        conn.close()
    except Exception as exc:
        print(
            f"[AVISO DB] No se pudo verificar/actualizar automáticamente el esquema '{Config.DB_NAME}': {exc}"
        )


def _create_sample_pdf_bytes(title: str, subtitle: str) -> bytes:
    """Genera un documento PDF real válido para los cuadernos de ejercicios iniciales."""
    safe_title = title.encode("latin-1", errors="ignore").decode("latin-1")
    safe_sub = subtitle.encode("latin-1", errors="ignore").decode("latin-1")
    stream_text = (
        f"BT /F1 18 Tf 50 750 Td ({safe_title}) Tj "
        f"/F1 12 Tf 0 -30 Td ({safe_sub}) Tj "
        f"0 -25 Td (Programa Terapeutico Vuelve a Ti - Material confidencial del alumno) Tj ET"
    )
    stream_bytes = stream_text.encode("latin-1")
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 595 842] /Contents 5 0 R >> endobj\n"
        b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        + f"5 0 obj << /Length {len(stream_bytes)} >> stream\n".encode("latin-1")
        + stream_bytes
        + b"\nendstream endobj\n"
        b"xref\n0 6\n0000000000 65535 f \n"
        b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n0\n%%EOF"
    )
    return pdf


def seed_initial_data() -> None:
    """
    Crea el administrador por defecto, un alumno de ejemplo, la fila inicial
    de `system_config` y el Curso 1 ("Programa Vuelve a Ti") con sus 3 módulos,
    clases y recursos de ejemplo si aún no existen.
    """
    admin_email = Config.ADMIN_DEFAULT_EMAIL.lower()
    admin = User.query.filter_by(email=admin_email).first()
    if not admin:
        admin = User(
            name="Carla Viladot (Admin)",
            email=admin_email,
            role="admin",
            is_active=True,
        )
        admin.set_password(Config.ADMIN_DEFAULT_PASSWORD)
        db.session.add(admin)

    demo_student_email = "alumno@vuelveati.com"
    student = User.query.filter_by(email=demo_student_email).first()
    if not student:
        student = User(
            name="Lucía Mendoza",
            email=demo_student_email,
            role="user",
            is_active=True,
        )
        student.set_password("Alumno1234!")
        db.session.add(student)

    if not SystemConfig.query.first():
        cfg = SystemConfig(
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            smtp_user="",
            smtp_password="",
            use_gmail_api=False,
            gmail_token_json="",
            s3_enabled=False,
            s3_provider="cloudflare_r2",
            s3_endpoint_url="",
            s3_region="auto",
            s3_bucket_name="",
            s3_access_key_id="",
            s3_secret_access_key="",
            presigned_url_expiration=1800,
        )
        db.session.add(cfg)

    db.session.commit()

    # Crear el Curso inicial si aún no hay cursos
    if Course.query.count() == 0:
        base_dir = ensure_protected_storage_dir()
        samples_dir = os.path.join(base_dir, "seed_resources")
        os.makedirs(samples_dir, exist_ok=True)

        course = Course(
            title="Programa Vuelve a Ti · Reconstrucción Personal",
            slug="programa-vuelve-a-ti",
            subtitle="Itinerario terapéutico paso a paso para superar la ruptura, recuperar tu calma y reconstruir tu identidad.",
            description=(
                "Programa clínico estructurado en módulos progresivos. "
                "El día de tu inscripción se habilita la primera clase del Módulo 1, "
                "y cada lunes se desbloquea automáticamente la siguiente sesión junto con sus cuadernos prácticos."
            ),
            is_active=True,
            drip_mode="weekly_days",
            drip_weekdays="0",  # 0 = Cada Lunes
            drip_interval_days=7,
        )
        db.session.add(course)
        db.session.flush()

        modules_spec = [
            {
                "title": "Módulo 1 · Contención & Calma",
                "subtitle": "Cortar el bucle obsesivo, gestionar los picos de ansiedad y establecer el contacto cero con dignidad.",
                "order_index": 1,
                "lessons": [
                    {
                        "title": "Clase 1.1 · Bienvenida y el Mapa del Duelo Afectivo",
                        "description": (
                            "En esta primera sesión sentamos las bases de tu refugio terapéutico. "
                            "Comprenderás por qué tu cerebro experimenta síndrome de abstinencia emocional tras la ruptura "
                            "y cómo aplicar el protocolo de emergencia ante picos de ansiedad."
                        ),
                        "order_index": 1,
                        "resources": [
                            (
                                "Cuaderno de Ejercicios 1.1 - Mapa del Duelo.pdf",
                                "pdf",
                                "Ejercicios de autorregistro emocional y mapa de disparadores",
                            ),
                            (
                                "Presentación de la Clase 1.1 - Neurobiología del Vínculo.pdf",
                                "presentation",
                                "Diapositivas de apoyo de la primera sesión",
                            ),
                        ],
                    },
                    {
                        "title": "Clase 1.2 · Contacto Cero Real: Límites sin Culpa",
                        "description": (
                            "Cómo sostener el contacto cero (físico y digital) sin caer en la hipervigilancia de redes sociales "
                            "ni en las recaídas de madrugada."
                        ),
                        "order_index": 2,
                        "resources": [
                            (
                                "Contrato Personal de Contacto Cero.pdf",
                                "pdf",
                                "Plantilla práctica de compromiso y plan de contingencia",
                            ),
                        ],
                    },
                    {
                        "title": "Clase 1.3 · Regulación Somática y Calma Nocturna",
                        "description": (
                            "Técnicas de desactivación fisiológica para recuperar el descanso y frenar la rumiación mental."
                        ),
                        "order_index": 3,
                        "resources": [
                            (
                                "Guía de Respiración y Regulación Vagal.pdf",
                                "pdf",
                                "Protocolo de 5 minutos para momentos de crisis",
                            ),
                        ],
                    },
                ],
            },
            {
                "title": "Módulo 2 · Procesamiento & Cierre",
                "subtitle": "Desmontar la idealización de la relación, soltar la culpa y entender los patrones emocionales.",
                "order_index": 2,
                "lessons": [
                    {
                        "title": "Clase 2.1 · Desmontando el Mito y la Idealización",
                        "description": (
                            "Por qué recordamos solo lo bueno cuando duele la ausencia y cómo construir una narrativa realista de la relación."
                        ),
                        "order_index": 1,
                        "resources": [
                            (
                                "Ejercicio de Contraste Realidad vs Idealización.pdf",
                                "pdf",
                                "Tabla terapéutica de balance objetivo del vínculo",
                            ),
                        ],
                    },
                    {
                        "title": "Clase 2.2 · La Carta de Cierre Emocional",
                        "description": (
                            "Cómo cerrar el ciclo internamente sin depender de una última conversación ni del perdón externo."
                        ),
                        "order_index": 2,
                        "resources": [
                            (
                                "Plantilla Carta de Despedida Terapéutica.pdf",
                                "pdf",
                                "Guía estructurada de cierre simbólico",
                            ),
                        ],
                    },
                ],
            },
            {
                "title": "Módulo 3 · Reconstrucción & Autonomía",
                "subtitle": "Recuperar tus límites, reconstruir tu autoestima y diseñar tu nuevo proyecto de vida personal.",
                "order_index": 3,
                "lessons": [
                    {
                        "title": "Clase 3.1 · Identidad Propia y Nuevos Pilares",
                        "description": (
                            "Redescubrir quién eres fuera de la pareja y reconstruir tus pilares de bienestar y proyectos personales."
                        ),
                        "order_index": 1,
                        "resources": [
                            (
                                "Mapa de Identidad y Nuevos Límites.pdf",
                                "pdf",
                                "Cuaderno de diseño de tu nueva etapa vital",
                            ),
                        ],
                    },
                ],
            },
        ]

        for m_spec in modules_spec:
            mod = CourseModule(
                course_id=course.id,
                title=m_spec["title"],
                subtitle=m_spec["subtitle"],
                order_index=m_spec["order_index"],
            )
            db.session.add(mod)
            db.session.flush()

            for l_spec in m_spec["lessons"]:
                lesson = Lesson(
                    module_id=mod.id,
                    title=l_spec["title"],
                    description=l_spec["description"],
                    order_index=l_spec["order_index"],
                    has_video=False,
                    video_storage_provider="s3",
                    video_object_key="",
                    video_original_name="",
                    video_mime_type="video/mp4",
                    video_size_bytes=0,
                    video_duration_seconds=0,
                )
                db.session.add(lesson)
                db.session.flush()

                for r_idx, (r_title, r_type, r_sub) in enumerate(
                    l_spec["resources"], start=1
                ):
                    filename = f"lesson_{lesson.id}_res_{r_idx}.pdf"
                    rel_key = f"seed_resources/{filename}"
                    abs_path = os.path.join(base_dir, "seed_resources", filename)
                    pdf_bytes = _create_sample_pdf_bytes(r_title, r_sub)
                    with open(abs_path, "wb") as f:
                        f.write(pdf_bytes)

                    res = LessonResource(
                        lesson_id=lesson.id,
                        title=r_title,
                        resource_type=r_type,
                        storage_provider="local",
                        object_key=rel_key,
                        original_filename=r_title,
                        mime_type="application/pdf",
                        file_size_bytes=len(pdf_bytes),
                        order_index=r_idx,
                    )
                    db.session.add(res)

        db.session.commit()

    # Asegurar que todos los usuarios de rol 'user' estén matriculados en el curso principal
    first_course = Course.query.order_by(Course.id.asc()).first()
    if first_course:
        all_students = User.query.filter_by(role="user").all()
        for stu in all_students:
            exists = Enrollment.query.filter_by(
                user_id=stu.id, course_id=first_course.id
            ).first()
            if not exists:
                db.session.add(
                    Enrollment(
                        user_id=stu.id,
                        course_id=first_course.id,
                        enrolled_at=stu.created_at or datetime.now(timezone.utc),
                        status="active",
                        unlock_all_override=False,
                    )
                )
        db.session.commit()


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    # Permitir subidas grandes de vídeo (hasta 2 GB)
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024

    cors_origins = Config.CORS_ORIGINS
    if cors_origins != "*" and "," in cors_origins:
        cors_origins = [o.strip() for o in cors_origins.split(",") if o.strip()]

    CORS(
        app,
        resources={r"/api/*": {"origins": cors_origins}},
        allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )

    db.init_app(app)
    jwt.init_app(app)

    app.register_blueprint(api_bp, url_prefix="/api")

    @app.route("/api/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "service": "Vuelve a Ti API"}), 200

    with app.app_context():
        ensure_mysql_database_exists()
        try:
            db.create_all()
            seed_initial_data()
        except Exception as exc:
            print(
                f"[AVISO SQLAlchemy] Revisa que MySQL 8.0 esté en ejecución con la BD '{Config.DB_NAME}': {exc}"
            )

    return app
