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
    MarketingLead,
    PublicResource,
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

    # Crear recursos públicos iniciales del Foro de Recursos si aún no existen
    if PublicResource.query.count() == 0:
        import json

        base_dir = ensure_protected_storage_dir()
        samples_dir = os.path.join(base_dir, "seed_resources")
        os.makedirs(samples_dir, exist_ok=True)

        seed_resources = [
            {
                "title": "Protocolo S.O.S. para Cortar la Rumiación Nocturna",
                "slug": "protocolo-sos-rumiacion-nocturna",
                "subtitle": "Guía clínica de 5 pasos + cuaderno de autorregistro para frenar el bucle mental antes de dormir y evitar escribir desde la ansiedad.",
                "category": "Ansiedad & Sueño",
                "instagram_keyword": "CALMA",
                "read_time_minutes": 6,
                "is_published": True,
                "is_featured": True,
                "cover_image_url": "https://images.unsplash.com/photo-1515377905703-c4788e51af15?auto=format&fit=crop&w=1200&q=80",
                "teaser_text": (
                    "Cuando cae la noche y desaparecen las distracciones del día, el cerebro entra en estado de hipervigilancia afectiva. "
                    "Empiezas a repasar conversaciones pasadas, buscar señales en redes sociales o imaginar escenarios que te generan un nudo en el estómago.\n\n"
                    "No es falta de fuerza de voluntad: es tu sistema nervioso buscando alivio inmediato a través del hábito del vínculo. "
                    "En este recurso descubrirás el protocolo exacto que trabajamos en consulta para desactivar la amígdala en menos de 7 minutos."
                ),
                "content_body": (
                    "## 1. Por qué tu mente se acelera justo al apagar la luz\n\n"
                    "Durante el día, tu corteza prefrontal se mantiene ocupada con tareas ejecutivas. Sin embargo, al tumbarte en la cama, "
                    "el descenso de estímulos externos deja espacio libre para que la red neuronal por defecto active memorias emocionales no resueltas.\n\n"
                    "> «No intentes responder a un pensamiento ansioso a las 11 de la noche; a esa hora tu cerebro no busca la verdad, busca dopamina rápida.»\n\n"
                    "## 2. Técnica de Descarga Cognitiva en Papel (Brain Dump Terapéutico)\n\n"
                    "Antes de meterte en la cama, dedica 4 minutos a volcar en el cuaderno descargable adjunto todas las preguntas abiertas que tu mente intenta resolver. "
                    "Al pasarlas de la memoria de trabajo al papel físico, le envías a tu cerebro la señal neurobiológica de que la información ya está a salvo.\n\n"
                    "## 3. El Suspiro Fisiológico Doble y la Regla de los 15 Minutos\n\n"
                    "Realiza dos inhalaciones seguidas por la nariz (una profunda y una segunda corta para expandir los alvéolos) seguidas de una exhalación lenta y prolongada por la boca. "
                    "Repite este ciclo 5 veces para estimular el nervio vago y reducir la frecuencia cardíaca de inmediato."
                ),
                "key_takeaways": [
                    "Diferenciar entre dolor de duelo natural y rumiación obsesiva autoprovocada.",
                    "Aplicar el bloqueo preventivo de estímulos digitales 45 minutos antes de dormir.",
                    "Completar la plantilla nocturna de descarga mental incluida en el PDF."
                ],
                "pdf_title": "Cuaderno Práctico: Protocolo S.O.S. Antirrumiación.pdf",
                "pdf_sub": "Ejercicios nocturnos de descarga cognitiva y regulacion vagal",
            },
            {
                "title": "El Mapa del Contacto Cero Real: Límites sin Culpa",
                "slug": "mapa-contacto-cero-sin-culpa",
                "subtitle": "Cómo sostener la distancia física y digital sin caer en el autoengaño ni revisar perfiles a escondidas.",
                "category": "Ruptura & Límites",
                "instagram_keyword": "LIMITES",
                "read_time_minutes": 7,
                "is_published": True,
                "is_featured": False,
                "cover_image_url": "https://images.unsplash.com/photo-1506126613408-eca07ce68773?auto=format&fit=crop&w=1200&q=80",
                "teaser_text": (
                    "El mayor error al aplicar el Contacto Cero es creer que consiste únicamente en no enviar mensajes, "
                    "mientras sigues revisando su última conexión, las historias de amigos en común o relees conversaciones antiguas.\n\n"
                    "Para que tu cerebro pueda deshacer la adicción bioquímica al vínculo, necesita abstinencia de estímulos reales y simbólicos. "
                    "En esta guía práctica veremos cómo blindar tu entorno sin sentir que estás siendo radical o injusta."
                ),
                "content_body": (
                    "## 1. El Contacto Cero no es un castigo para el otro, es medicina para ti\n\n"
                    "Muchas pacientes sienten culpa al silenciar o bloquear porque temen parecer rencorosas. "
                    "Sin embargo, poner una venda sobre una herida abierta no es un acto de hostilidad: es higiene emocional básica.\n\n"
                    "> «Cada vez que miras su perfil, reinicias el contador neuroquímico de tu abstinencia emocional.»\n\n"
                    "## 2. Los 3 Niveles de Exposición Invisible\n\n"
                    "1. **Exposición directa**: Hablar, escribir o coincidir buscando excusas.\n"
                    "2. **Exposición digital pasiva**: Ver estados, listas de visualización o fotos antiguas en tu galería.\n"
                    "3. **Exposición narrativa**: Hablar del tema durante horas con tu entorno repitiendo las mismas hipótesis.\n\n"
                    "## 3. Tu Contrato Personal de Cuidado\n\n"
                    "Descarga el PDF adjunto al final de esta página para rellenar tu plan de contingencia ante momentos de debilidad."
                ),
                "key_takeaways": [
                    "Identificar tus 3 disparadores principales de recaída digital.",
                    "Crear una carpeta oculta de transición antes de borrar recuerdos.",
                    "Firmar tu Contrato Personal de Contacto Cero incluido en el cuaderno PDF."
                ],
                "pdf_title": "Plantilla y Contrato de Contacto Cero Real.pdf",
                "pdf_sub": "Plan de contingencia y blindaje emocional paso a paso",
            },
            {
                "title": "Test y Guía de Apego Ansioso: Volver a tu Centro",
                "slug": "guia-apego-ansioso-volver-a-tu-centro",
                "subtitle": "Comprende por qué el silencio del otro activa tu alarma de abandono y cómo construir seguridad interna.",
                "category": "Autoestima & Apego",
                "instagram_keyword": "APEGO",
                "read_time_minutes": 8,
                "is_published": True,
                "is_featured": False,
                "cover_image_url": "https://images.unsplash.com/photo-1544717305-2782549b5136?auto=format&fit=crop&w=1200&q=80",
                "teaser_text": (
                    "¿Sientes que tu estado de ánimo depende por completo de la rapidez o el tono con el que te responden? "
                    "Cuando el sistema de apego ansioso se activa, confundimos intensidad y angustia con amor profundo.\n\n"
                    "En este artículo y cuaderno de autoevaluación aprenderás a detectar las protestas de apego antes de actuar por impulso."
                ),
                "content_body": (
                    "## 1. La hiperactivación del sistema de apego\n\n"
                    "El apego ansioso no es un defecto de carácter; es una estrategia aprendida en entornos donde el afecto o la validación eran intermitentes. "
                    "Tu sistema nervioso aprendió a amplificar la alerta para no perder el vínculo.\n\n"
                    "> «Volver a ti significa convertirte en el adulto seguro que sabe sostener a su propia parte herida.»\n\n"
                    "## 2. Cómo pausar la conducta de protesta\n\n"
                    "Antes de pedir confirmación externa, aplica la pausa de autorregulación de 20 minutos detallada en el cuaderno descargable."
                ),
                "key_takeaways": [
                    "Reconocer las señales físicas de la activación del apego en el pecho y estómago.",
                    "Sustituir la búsqueda de reaseguración externa por autovalidación somática.",
                    "Realizar el ejercicio práctico de los 4 pilares de identidad propia."
                ],
                "pdf_title": "Cuaderno de Autoevaluación de Apego y Seguridad Interna.pdf",
                "pdf_sub": "Test reflexivo y ejercicios de reconstruccion de autoestima",
            },
        ]

        for idx, r_spec in enumerate(seed_resources, start=1):
            filename = f"public_resource_{idx}.pdf"
            rel_key = f"seed_resources/{filename}"
            abs_path = os.path.join(base_dir, "seed_resources", filename)
            pdf_bytes = _create_sample_pdf_bytes(r_spec["pdf_title"], r_spec["pdf_sub"])
            with open(abs_path, "wb") as f:
                f.write(pdf_bytes)

            pub_res = PublicResource(
                title=r_spec["title"],
                slug=r_spec["slug"],
                subtitle=r_spec["subtitle"],
                category=r_spec["category"],
                instagram_keyword=r_spec["instagram_keyword"],
                read_time_minutes=r_spec["read_time_minutes"],
                is_published=r_spec["is_published"],
                is_featured=r_spec["is_featured"],
                cover_image_url=r_spec["cover_image_url"],
                teaser_text=r_spec["teaser_text"],
                content_body=r_spec["content_body"],
                key_takeaways_json=json.dumps(r_spec["key_takeaways"], ensure_ascii=False),
                gallery_images_json="[]",
                pdf_title=r_spec["pdf_title"],
                pdf_storage_provider="local",
                pdf_object_key=rel_key,
                pdf_original_name=r_spec["pdf_title"],
                pdf_file_size_bytes=len(pdf_bytes),
                views_count=18 * idx,
                unlocks_count=7 * idx,
            )
            db.session.add(pub_res)

        db.session.commit()


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    # Permitir subidas grandes de vídeo (hasta 2 GB)
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024

    cors_origins = Config.CORS_ORIGINS
    if cors_origins != "*":
        parsed = [o.strip().rstrip("/") for o in cors_origins.split(",") if o.strip()]
        default_domains = [
            "https://carlaviladotpsicologia.com",
            "https://www.carlaviladotpsicologia.com",
            "http://localhost:5173",
        ]
        for d in default_domains:
            if d not in parsed:
                parsed.append(d)
        cors_origins = parsed

    CORS(
        app,
        resources={r"/api/*": {"origins": cors_origins}},
        allow_headers=["Content-Type", "Authorization", "X-Requested-With", "X-Lead-Email"],
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
