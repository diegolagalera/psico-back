from datetime import datetime, time as dt_time, timedelta, timezone
from app.models import (
    Course,
    CourseModule,
    Enrollment,
    Lesson,
    LessonResource,
    ResourceDownloadLog,
    SessionBooking,
    StudentLessonOverride,
    StudentLessonProgress,
    User,
    db,
)

WEEKDAY_NAMES_ES = {
    0: "Lunes",
    1: "Martes",
    2: "Miércoles",
    3: "Jueves",
    4: "Viernes",
    5: "Sábado",
    6: "Domingo",
}


def ensure_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def calculate_lesson_unlock_datetimes(
    enrolled_at: datetime,
    total_lessons: int,
    drip_mode: str = "weekly_days",
    drip_weekdays: list[int] | None = None,
    drip_interval_days: int = 7,
) -> list[datetime]:
    """
    Calcula la fecha/hora exacta de desbloqueo automático para cada clase (índice 0 .. N-1):
    - Clase 0 (primera clase del Módulo 1): SIEMPRE se desbloquea el mismo instante en que
      el alumno paga / se inscribe (`enrolled_at`).
    - Clases siguientes (índice k >= 1):
      * 'immediate': todas en `enrolled_at`.
      * 'interval_days': cada `drip_interval_days` días a partir de `enrolled_at`.
      * 'weekly_days': cada vez que llega uno de los días de la semana configurados en
        `drip_weekdays` (por defecto [0] = cada Lunes a las 00:00 UTC).
    """
    if total_lessons <= 0:
        return []

    base_dt = ensure_utc(enrolled_at)
    mode = (drip_mode or "weekly_days").strip().lower()

    if mode == "immediate":
        return [base_dt for _ in range(total_lessons)]

    if mode == "interval_days":
        interval = max(1, int(drip_interval_days or 7))
        unlock_dates = [base_dt]
        for idx in range(1, total_lessons):
            target_date = (base_dt + timedelta(days=idx * interval)).date()
            unlock_dates.append(
                datetime.combine(target_date, dt_time(0, 0, 0), tzinfo=timezone.utc)
            )
        return unlock_dates

    # Modo por defecto: 'weekly_days' (ej. cada Lunes = [0], o Lunes y Jueves = [0, 3])
    allowed_days = sorted(set(drip_weekdays or [0]))
    if not allowed_days:
        allowed_days = [0]

    unlock_dates = [base_dt]
    cursor_date = base_dt.date() + timedelta(days=1)

    while len(unlock_dates) < total_lessons:
        if cursor_date.weekday() in allowed_days:
            unlock_dates.append(
                datetime.combine(cursor_date, dt_time(0, 0, 0), tzinfo=timezone.utc)
            )
        cursor_date += timedelta(days=1)

    return unlock_dates


def ensure_student_enrolled(user_id: int, course_id: int | None = None) -> Enrollment | None:
    """
    Garantiza que el alumno tenga matrícula activa en el curso indicado (o en el
    primer curso activo si no se especifica).
    """
    if course_id:
        course = Course.query.get(course_id)
    else:
        course = Course.query.filter_by(is_active=True).order_by(Course.id.asc()).first()
        if not course:
            course = Course.query.order_by(Course.id.asc()).first()

    if not course:
        return None

    enrollment = Enrollment.query.filter_by(
        user_id=user_id, course_id=course.id
    ).first()
    if not enrollment:
        user = User.query.get(user_id)
        enrolled_time = (
            ensure_utc(user.created_at)
            if (user and user.created_at)
            else datetime.now(timezone.utc)
        )
        enrollment = Enrollment(
            user_id=user_id,
            course_id=course.id,
            enrolled_at=enrolled_time,
            status="active",
            unlock_all_override=False,
        )
        db.session.add(enrollment)
        db.session.commit()

    return enrollment


def build_student_course_roadmap(
    user_id: int,
    course: Course,
    now_dt: datetime | None = None,
) -> dict:
    """
    Construye el cronograma completo de un alumno para un curso:
    - Estado de cada módulo (activo/bloqueado, % completado, fecha de activación)
    - Estado de cada clase (desbloqueada por fecha, desbloqueada manualmente por admin,
      bloqueada manualmente o pendiente de su próximo lunes/fecha)
    - Progreso del vídeo (% visto, segundos vistos) y progreso de la clase
    - Recursos descargados por el alumno y contador de descargas
    """
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    else:
        now_dt = ensure_utc(now_dt)

    enrollment = ensure_student_enrolled(user_id, course.id)
    enrolled_at = (
        ensure_utc(enrollment.enrolled_at)
        if enrollment
        else datetime.now(timezone.utc)
    )
    unlock_all_override = bool(enrollment and enrollment.unlock_all_override)

    # Aplanar todas las clases en orden secuencial (Módulo 1 -> Clases, luego Módulo 2 -> Clases...)
    ordered_modules = sorted(
        course.modules, key=lambda m: (m.order_index or 1, m.id)
    )
    flat_lessons: list[tuple[CourseModule, Lesson]] = []
    for mod in ordered_modules:
        ordered_lessons = sorted(
            mod.lessons, key=lambda l: (l.order_index or 1, l.id)
        )
        for les in ordered_lessons:
            flat_lessons.append((mod, les))

    total_lessons = len(flat_lessons)
    scheduled_dates = calculate_lesson_unlock_datetimes(
        enrolled_at=enrolled_at,
        total_lessons=total_lessons,
        drip_mode=course.drip_mode,
        drip_weekdays=course.get_weekdays_list(),
        drip_interval_days=course.drip_interval_days,
    )

    # Cargar overrides manuales, progreso y descargas del alumno en bloque
    overrides_map = {
        ov.lesson_id: ov
        for ov in StudentLessonOverride.query.filter_by(user_id=user_id).all()
    }
    progress_map = {
        pr.lesson_id: pr
        for pr in StudentLessonProgress.query.filter_by(user_id=user_id).all()
    }
    downloads_map = {
        dl.resource_id: dl
        for dl in ResourceDownloadLog.query.filter_by(user_id=user_id).all()
    }
    bookings_map = {
        bk.lesson_id: bk
        for bk in SessionBooking.query.filter_by(
            user_id=user_id, status="confirmed"
        )
        .order_by(SessionBooking.start_at.asc())
        .all()
    }

    lesson_schedule_by_id: dict[int, dict] = {}
    unlocked_lessons_count = 0
    completed_lessons_count = 0
    total_video_percent_sum = 0.0
    total_lesson_percent_sum = 0.0
    total_resources_count = 0
    downloaded_resources_count = 0
    next_lesson_to_unlock = None

    for global_idx, (mod, les) in enumerate(flat_lessons):
        sched_dt = scheduled_dates[global_idx]
        ov = overrides_map.get(les.id)
        pr = progress_map.get(les.id)

        # Determinar si está desbloqueada y por qué motivo
        auto_unlocked = now_dt >= sched_dt
        if unlock_all_override:
            is_unlocked = True
            unlock_source = "course_override"
        elif ov and ov.force_state == "unlocked":
            is_unlocked = True
            unlock_source = "manual_admin"
        elif ov and ov.force_state == "locked":
            is_unlocked = False
            unlock_source = "locked_by_admin"
        else:
            is_unlocked = auto_unlocked
            unlock_source = "schedule" if auto_unlocked else "pending_schedule"

        if is_unlocked:
            unlocked_lessons_count += 1
        elif next_lesson_to_unlock is None:
            next_lesson_to_unlock = {
                "lesson_id": les.id,
                "lesson_title": les.title,
                "module_title": mod.title,
                "scheduled_unlock_at": sched_dt.isoformat(),
            }

        # Recursos y su estado de descarga para este alumno
        resources_data = []
        lesson_downloaded_count = 0
        for res in les.resources:
            total_resources_count += 1
            dl_log = downloads_map.get(res.id)
            if dl_log:
                downloaded_resources_count += 1
                lesson_downloaded_count += 1
            r_dict = res.to_dict()
            r_dict["downloaded"] = bool(dl_log)
            r_dict["download_count"] = int(dl_log.download_count) if dl_log else 0
            r_dict["last_downloaded_at"] = (
                dl_log.last_downloaded_at.isoformat()
                if (dl_log and dl_log.last_downloaded_at)
                else None
            )
            resources_data.append(r_dict)

        video_pct = round(float(pr.video_progress_percent if pr else 0.0), 1)
        lesson_pct = round(float(pr.lesson_progress_percent if pr else 0.0), 1)
        is_completed = bool(pr.is_completed if pr else False)

        # Si la clase NO tiene vídeo pero tiene recursos, calculamos progreso automático por recursos si aún es 0
        has_real_video = bool(les.has_video and les.video_object_key)
        if not has_real_video and len(resources_data) > 0 and lesson_pct == 0:
            lesson_pct = round(
                (lesson_downloaded_count / len(resources_data)) * 100.0, 1
            )
            if lesson_downloaded_count == len(resources_data):
                is_completed = True

        if is_completed:
            lesson_pct = 100.0
            completed_lessons_count += 1

        total_video_percent_sum += video_pct if has_real_video else lesson_pct
        total_lesson_percent_sum += lesson_pct

        lesson_item = les.to_dict(include_resources=False)
        lesson_item.update(
            {
                "global_order": global_idx + 1,
                "module_title": mod.title,
                "scheduled_unlock_at": sched_dt.isoformat(),
                "auto_unlocked": auto_unlocked,
                "is_unlocked": is_unlocked,
                "unlock_source": unlock_source,
                "manual_override_state": ov.force_state if ov else None,
                "progress": {
                    "watched_seconds": round(
                        float(pr.watched_seconds if pr else 0.0), 1
                    ),
                    "max_watched_seconds": round(
                        float(pr.max_watched_seconds if pr else 0.0), 1
                    ),
                    "video_duration_seconds": round(
                        float(
                            (pr.video_duration_seconds if pr else 0.0)
                            or les.video_duration_seconds
                            or 0.0
                        ),
                        1,
                    ),
                    "video_progress_percent": video_pct,
                    "lesson_progress_percent": lesson_pct,
                    "is_completed": is_completed,
                    "completed_at": (
                        pr.completed_at.isoformat()
                        if (pr and pr.completed_at)
                        else None
                    ),
                    "last_accessed_at": (
                        pr.last_accessed_at.isoformat()
                        if (pr and pr.last_accessed_at)
                        else None
                    ),
                },
                "resources": resources_data,
                "resources_count": len(resources_data),
                "resources_downloaded_count": lesson_downloaded_count,
                "booking": (
                    bookings_map[les.id].to_dict() if les.id in bookings_map else None
                ),
            }
        )
        lesson_schedule_by_id[les.id] = lesson_item

    # Agrupar de nuevo por módulos enriquecidos
    modules_payload = []
    for mod in ordered_modules:
        mod_lessons = [
            lesson_schedule_by_id[les.id]
            for les in sorted(mod.lessons, key=lambda l: (l.order_index or 1, l.id))
            if les.id in lesson_schedule_by_id
        ]
        mod_unlocked = any(l["is_unlocked"] for l in mod_lessons) or (
            len(mod_lessons) == 0
        )
        mod_first_unlock = (
            mod_lessons[0]["scheduled_unlock_at"] if mod_lessons else enrolled_at.isoformat()
        )
        mod_completed_count = sum(
            1 for l in mod_lessons if l["progress"]["is_completed"]
        )
        mod_unlocked_count = sum(1 for l in mod_lessons if l["is_unlocked"])
        mod_progress_pct = (
            round(
                sum(l["progress"]["lesson_progress_percent"] for l in mod_lessons)
                / len(mod_lessons),
                1,
            )
            if mod_lessons
            else 0.0
        )

        mod_dict = mod.to_dict(include_lessons=False)
        mod_dict.update(
            {
                "is_unlocked": mod_unlocked,
                "first_scheduled_unlock_at": mod_first_unlock,
                "total_lessons": len(mod_lessons),
                "unlocked_lessons_count": mod_unlocked_count,
                "completed_lessons_count": mod_completed_count,
                "module_progress_percent": mod_progress_pct,
                "lessons": mod_lessons,
            }
        )
        modules_payload.append(mod_dict)

    overall_progress_pct = (
        round(total_lesson_percent_sum / total_lessons, 1)
        if total_lessons > 0
        else 0.0
    )

    weekdays_names = [
        WEEKDAY_NAMES_ES.get(d, "Lunes") for d in course.get_weekdays_list()
    ]
    if course.drip_mode == "immediate":
        drip_summary = "Acceso completo inmediato desde el día 1"
    elif course.drip_mode == "interval_days":
        drip_summary = f"Clase 1 el día de alta y siguiente clase cada {course.drip_interval_days} días"
    else:
        drip_summary = (
            f"Clase 1 el día de alta y siguiente clase cada {', '.join(weekdays_names)}"
        )

    return {
        "course": {
            "id": course.id,
            "title": course.title,
            "slug": course.slug,
            "subtitle": course.subtitle or "",
            "description": course.description or "",
            "is_active": bool(course.is_active),
            "drip_mode": course.drip_mode,
            "drip_weekdays": course.drip_weekdays,
            "drip_weekdays_list": course.get_weekdays_list(),
            "drip_interval_days": int(course.drip_interval_days or 7),
            "drip_summary": drip_summary,
        },
        "enrollment": enrollment.to_dict() if enrollment else None,
        "summary": {
            "total_modules": len(modules_payload),
            "total_lessons": total_lessons,
            "unlocked_lessons_count": unlocked_lessons_count,
            "completed_lessons_count": completed_lessons_count,
            "overall_progress_percent": overall_progress_pct,
            "total_resources_count": total_resources_count,
            "downloaded_resources_count": downloaded_resources_count,
            "next_lesson_to_unlock": next_lesson_to_unlock,
        },
        "modules": modules_payload,
    }
