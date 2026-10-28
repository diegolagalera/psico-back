"""
Servicio de agenda de sesiones 1:1 con la psicóloga.

- Lee la disponibilidad real del Google Calendar de la psicóloga (FreeBusy API).
- Cruza esa disponibilidad con el horario laboral configurado por el admin.
- Crea eventos con enlace de Google Meet e invita al alumno.
- Cancela eventos (el alumno recibe la cancelación por email desde Google).
"""

import json
import time
import traceback
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dt_time, timedelta, timezone

try:
    import certifi
    import ssl
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CONTEXT = None

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from app.models import SessionBooking, SystemConfig, db

WEEKDAY_LABELS_ES = [
    "Lunes",
    "Martes",
    "Miércoles",
    "Jueves",
    "Viernes",
    "Sábado",
    "Domingo",
]

MONTH_NAMES_ES = [
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
]

# Horario por defecto: L-V 10:00-14:00 y 16:00-20:00
DEFAULT_WORKING_HOURS = {
    "0": [["10:00", "14:00"], ["16:00", "20:00"]],
    "1": [["10:00", "14:00"], ["16:00", "20:00"]],
    "2": [["10:00", "14:00"], ["16:00", "20:00"]],
    "3": [["10:00", "14:00"], ["16:00", "20:00"]],
    "4": [["10:00", "14:00"], ["16:00", "20:00"]],
    "5": [],
    "6": [],
}

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


# ==========================================================
# CONFIGURACIÓN
# ==========================================================
def get_booking_config(override: dict | None = None) -> dict:
    cfg_obj = SystemConfig.query.first()
    if not cfg_obj:
        cfg_obj = SystemConfig()
        db.session.add(cfg_obj)
        db.session.commit()
    cfg = cfg_obj.to_dict()
    if override:
        for key in (
            "calendar_enabled",
            "calendar_use_gmail_token",
            "calendar_token_json",
            "calendar_id",
            "calendar_timezone",
            "session_duration_minutes",
            "booking_working_hours",
            "booking_min_notice_hours",
            "booking_cancel_limit_hours",
        ):
            if key in override and override[key] is not None:
                cfg[key] = override[key]
    cfg["working_hours"] = parse_working_hours(cfg.get("booking_working_hours"))
    return cfg


def _valid_hhmm(value: str) -> bool:
    try:
        datetime.strptime(value, "%H:%M")
        return True
    except Exception:
        return False


def parse_working_hours(raw) -> dict:
    """Devuelve {"0": [["10:00","14:00"], ...], ..., "6": [...]} saneado."""
    data = raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return json.loads(json.dumps(DEFAULT_WORKING_HOURS))
        try:
            data = json.loads(raw)
        except Exception:
            return json.loads(json.dumps(DEFAULT_WORKING_HOURS))
    if not isinstance(data, dict):
        return json.loads(json.dumps(DEFAULT_WORKING_HOURS))

    result = {}
    for day in range(7):
        ranges = data.get(str(day), data.get(day, [])) or []
        clean = []
        for item in ranges:
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and _valid_hhmm(str(item[0]))
                and _valid_hhmm(str(item[1]))
                and str(item[0]) < str(item[1])
            ):
                clean.append([str(item[0]), str(item[1])])
        result[str(day)] = sorted(clean)
    return result


def get_tz(cfg: dict):
    name = (cfg.get("calendar_timezone") or "Europe/Madrid").strip()
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Europe/Madrid") if name != "Europe/Madrid" else timezone.utc


def ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_naive_utc(dt: datetime) -> datetime:
    return ensure_utc(dt).replace(tzinfo=None)


# ==========================================================
# CONEXIÓN CON GOOGLE CALENDAR
# ==========================================================
def _resolve_token_json(cfg: dict) -> str:
    if cfg.get("calendar_use_gmail_token"):
        return (cfg.get("gmail_token_json") or "").strip()
    return (cfg.get("calendar_token_json") or "").strip()


def extract_oauth_client_info(token_raw: str) -> dict:
    """Extrae client_id, client_secret y refresh_token del JSON (directo o con web/installed)."""
    if not token_raw:
        return {}
    try:
        data = json.loads(token_raw)
        if isinstance(data, dict):
            if "web" in data and isinstance(data["web"], dict):
                merged = dict(data["web"])
                if "refresh_token" in data and "refresh_token" not in merged:
                    merged["refresh_token"] = data["refresh_token"]
                return merged
            elif "installed" in data and isinstance(data["installed"], dict):
                merged = dict(data["installed"])
                if "refresh_token" in data and "refresh_token" not in merged:
                    merged["refresh_token"] = data["refresh_token"]
                return merged
            return data
    except Exception:
        pass
    return {}


def has_calendar_client_id(cfg: dict) -> bool:
    info = extract_oauth_client_info(_resolve_token_json(cfg))
    return bool(info.get("client_id") and info.get("client_secret"))


def is_calendar_ready(cfg: dict) -> bool:
    if not cfg.get("calendar_enabled"):
        return False
    info = extract_oauth_client_info(_resolve_token_json(cfg))
    return bool(info.get("client_id") and info.get("refresh_token"))


def generate_google_oauth_url(cfg: dict, redirect_uri: str, state: str = "") -> str:
    """Genera la URL oficial de Google OAuth2 para autorizar el acceso a Google Calendar."""
    info = extract_oauth_client_info(_resolve_token_json(cfg))
    client_id = info.get("client_id")
    if not client_id:
        raise ValueError(
            "Falta el 'client_id' de Google. Pega el JSON descargado de Google Cloud Console o guárdalo primero."
        )

    scopes = [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.events",
    ]
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if state:
        params["state"] = state
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def exchange_oauth_code_for_token(cfg: dict, code: str, redirect_uri: str) -> dict:
    """Canjea el código devuelto por Google por refresh_token y access_token."""
    info = extract_oauth_client_info(_resolve_token_json(cfg))
    client_id = info.get("client_id")
    client_secret = info.get("client_secret")
    if not client_id or not client_secret:
        raise ValueError(
            "Faltan client_id o client_secret en la configuración para completar la vinculación."
        )

    token_url = info.get("token_uri") or "https://oauth2.googleapis.com/token"
    payload = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode("utf-8")

    req = urllib.request.Request(
        token_url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, context=SSL_CONTEXT, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        err_body = err.read().decode("utf-8", errors="ignore")
        try:
            err_json = json.loads(err_body)
            err_desc = (
                err_json.get("error_description")
                or err_json.get("error")
                or err_body
            )
        except Exception:
            err_desc = err_body
        raise ValueError(f"Error de Google al autorizar ({err.code}): {err_desc}")
    except Exception as exc:
        raise ValueError(f"Error de conexión con Google OAuth: {exc}")

    refresh_token = data.get("refresh_token") or info.get("refresh_token")
    if not refresh_token:
        raise ValueError(
            "Google no devolvió 'refresh_token'. Asegúrate de pulsar 'Permitir' en la pantalla de Google."
        )

    merged_creds = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "token": data.get("access_token"),
        "token_uri": token_url,
        "scope": data.get("scope"),
        "token_type": data.get("token_type", "Bearer"),
    }
    return merged_creds


def build_calendar_service(cfg: dict):
    """Construye el cliente de Google Calendar v3 a partir del JSON OAuth2."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    token_raw = _resolve_token_json(cfg)
    if not token_raw:
        raise ValueError(
            "No hay credenciales OAuth2 configuradas para Google Calendar."
        )
    client_info = extract_oauth_client_info(token_raw)

    missing = [
        k for k in ("client_id", "client_secret", "refresh_token") if not client_info.get(k)
    ]
    if missing:
        if (
            "refresh_token" in missing
            and client_info.get("client_id")
            and client_info.get("client_secret")
        ):
            raise ValueError(
                "Falta autorizar tu cuenta con Google: tu JSON contiene Client ID y Client Secret, pero todavía no tiene 'refresh_token'. "
                "Haz clic en el botón 'Vincular mi Google Calendar con 1 Clic' para autorizar el acceso en Google."
            )
        raise ValueError(f"Faltan claves en el JSON OAuth2: {', '.join(missing)}")

    creds = Credentials(
        token=client_info.get("token"),
        refresh_token=client_info.get("refresh_token"),
        token_uri=client_info.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=client_info.get("client_id"),
        client_secret=client_info.get("client_secret"),
    )
    creds.refresh(Request())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_user_calendar_ids(service, primary_id: str = "primary") -> list[dict]:
    """
    Obtiene la lista de calendarios asociados a la cuenta de Google.
    Filtra calendarios automáticos como festivos nacionales o cumpleaños de contactos,
    quedándose con todos los calendarios personales y profesionales (personal, consultas, etc.).
    """
    cals = []
    try:
        cal_list = service.calendarList().list().execute()
        for c in cal_list.get("items", []):
            cid = c.get("id", "")
            if not cid:
                continue
            # Omitir calendarios de festivos públicos o cumpleaños de contactos de terceros
            if "#holiday@" in cid or "#contacts@" in cid:
                continue
            cals.append(
                {
                    "id": cid,
                    "summary": c.get("summary") or cid,
                    "primary": bool(c.get("primary", False)),
                    "access_role": c.get("accessRole", "reader"),
                }
            )
    except Exception as exc:
        print(f"[Calendar] Error listando calendarios: {exc}")

    # Asegurar que al menos el calendario primario o configurado esté en la lista
    found_ids = {c["id"] for c in cals}
    if primary_id and primary_id != "primary" and primary_id not in found_ids:
        cals.insert(
            0,
            {
                "id": primary_id,
                "summary": primary_id,
                "primary": False,
                "access_role": "owner",
            },
        )
    elif not cals:
        cals.append(
            {
                "id": "primary",
                "summary": "Calendario Principal",
                "primary": True,
                "access_role": "owner",
            }
        )

    return cals


def fetch_busy_intervals(
    cfg: dict, start_utc: datetime, end_utc: datetime, service=None
) -> list[tuple[datetime, datetime]]:
    """Intervalos ocupados cruzando TODOS los calendarios de la psicóloga (FreeBusy + Events List)."""
    service = service or build_calendar_service(cfg)
    primary_cal_id = (cfg.get("calendar_id") or "primary").strip() or "primary"

    user_cals = get_user_calendar_ids(service, primary_cal_id)
    cal_ids = [c["id"] for c in user_cals]

    time_min_str = ensure_utc(start_utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max_str = ensure_utc(end_utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    busy: list[tuple[datetime, datetime]] = []

    # 1. FreeBusy API (Consulta rápida de disponibilidad en TODOS los calendarios a la vez)
    try:
        body = {
            "timeMin": time_min_str,
            "timeMax": time_max_str,
            "timeZone": "UTC",
            "items": [{"id": cid} for cid in cal_ids],
        }
        result = service.freebusy().query(body=body).execute()
        calendars = result.get("calendars") or {}
        for cal_key, cal_data in calendars.items():
            errors = cal_data.get("errors") or []
            if errors:
                reasons = ", ".join(e.get("reason", "desconocido") for e in errors)
                print(f"[Google Calendar FreeBusy Error] {cal_key}: {reasons}")
                continue
            for item in cal_data.get("busy", []):
                try:
                    s = ensure_utc(
                        datetime.fromisoformat(item["start"].replace("Z", "+00:00"))
                    )
                    e = ensure_utc(
                        datetime.fromisoformat(item["end"].replace("Z", "+00:00"))
                    )
                    busy.append((s, e))
                except Exception as parse_err:
                    print(f"[Calendar] Error parseando busy item: {parse_err}")
    except Exception as exc:
        print(f"[Calendar FreeBusy Exception]: {exc}")

    # 2. Events List API para cada calendario (detecta eventos recurrentes, citas manuales y eventos de día completo)
    tz = get_tz(cfg)
    for cid in cal_ids:
        try:
            events_result = (
                service.events()
                .list(
                    calendarId=cid,
                    timeMin=time_min_str,
                    timeMax=time_max_str,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            for item in events_result.get("items", []):
                if item.get("transparency") == "transparent":
                    continue
                if item.get("status") == "cancelled":
                    continue

                start_info = item.get("start", {})
                end_info = item.get("end", {})

                if start_info.get("dateTime") and end_info.get("dateTime"):
                    s = ensure_utc(
                        datetime.fromisoformat(
                            start_info["dateTime"].replace("Z", "+00:00")
                        )
                    )
                    e = ensure_utc(
                        datetime.fromisoformat(end_info["dateTime"].replace("Z", "+00:00"))
                    )
                    busy.append((s, e))
                elif start_info.get("date") and end_info.get("date"):
                    d_s = date.fromisoformat(start_info["date"])
                    d_e = date.fromisoformat(end_info["date"])
                    s = datetime.combine(d_s, dt_time(0, 0), tzinfo=tz).astimezone(
                        timezone.utc
                    )
                    e = datetime.combine(d_e, dt_time(0, 0), tzinfo=tz).astimezone(
                        timezone.utc
                    )
                    busy.append((s, e))
        except Exception as exc:
            print(f"[Calendar Events List Exception para {cid}]: {exc}")

    # Desduplicar intervalos coincidentes
    unique_busy: list[tuple[datetime, datetime]] = []
    for s, e in busy:
        if not any(
            abs((s - us).total_seconds()) < 60 and abs((e - ue).total_seconds()) < 60
            for us, ue in unique_busy
        ):
            unique_busy.append((s, e))

    cal_names = ", ".join(f"'{c['summary']}'" for c in user_cals)
    print(
        f"[Calendar Sync] Encontrados {len(unique_busy)} intervalos ocupados consultando {len(cal_ids)} calendarios ({cal_names}) entre {time_min_str} y {time_max_str}."
    )
    return unique_busy


# ==========================================================
# CÁLCULO DE HUECOS DISPONIBLES (SEMANA SIGUIENTE Y NAVEGACIÓN)
# ==========================================================
def get_week_dates(cfg: dict, now_utc: datetime | None = None, week_offset: int = 1) -> list[date]:
    tz = get_tz(cfg)
    now_local = ensure_utc(now_utc or datetime.now(timezone.utc)).astimezone(tz)
    today = now_local.date()
    # Lunes de la semana en curso (offset 0)
    current_monday = today - timedelta(days=today.weekday())
    # Lunes de la semana deseada (offset 1 = próxima semana por defecto)
    target_monday = current_monday + timedelta(days=7 * max(0, int(week_offset)))
    return [target_monday + timedelta(days=i) for i in range(7)]


def get_next_week_dates(cfg: dict, now_utc: datetime | None = None) -> list[date]:
    return get_week_dates(cfg, now_utc=now_utc, week_offset=1)


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def compute_available_slots(
    cfg: dict, now_utc: datetime | None = None, week_offset: int = 1
) -> dict:
    """
    Devuelve los huecos de la semana solicitada (por defecto la siguiente, lunes a domingo)
    dentro del horario laboral configurado, indicando si cada hueco está disponible u ocupado por:
      - eventos ocupados del Google Calendar de la psicóloga,
      - sesiones ya reservadas en la plataforma,
      - huecos que incumplen la antelación mínima (booking_min_notice_hours).
    """
    now_utc = ensure_utc(now_utc or datetime.now(timezone.utc))
    tz = get_tz(cfg)
    duration = timedelta(minutes=max(15, int(cfg.get("session_duration_minutes") or 60)))
    min_notice = timedelta(hours=max(0, int(cfg.get("booking_min_notice_hours") or 0)))
    working_hours = cfg.get("working_hours") or parse_working_hours(
        cfg.get("booking_working_hours")
    )

    days = get_week_dates(cfg, now_utc, week_offset=week_offset)
    week_start_utc = datetime.combine(days[0], dt_time(0, 0), tzinfo=tz).astimezone(
        timezone.utc
    )
    week_end_utc = datetime.combine(
        days[-1] + timedelta(days=1), dt_time(0, 0), tzinfo=tz
    ).astimezone(timezone.utc)

    busy: list[tuple[datetime, datetime]] = []
    calendar_connected = False
    calendar_error = None
    if is_calendar_ready(cfg):
        try:
            busy.extend(fetch_busy_intervals(cfg, week_start_utc, week_end_utc))
            calendar_connected = True
        except Exception as exc:
            calendar_error = str(exc)

    # Sesiones ya reservadas en la plataforma (por si el calendario aún no está sincronizado)
    existing = SessionBooking.query.filter(
        SessionBooking.status == "confirmed",
        SessionBooking.start_at < to_naive_utc(week_end_utc),
        SessionBooking.end_at > to_naive_utc(week_start_utc),
    ).all()
    for bk in existing:
        busy.append((ensure_utc(bk.start_at), ensure_utc(bk.end_at)))

    earliest_start = now_utc + min_notice
    days_payload = []
    total_available_slots = 0

    for day in days:
        slots = []
        for start_str, end_str in working_hours.get(str(day.weekday()), []):
            range_start = datetime.combine(
                day, datetime.strptime(start_str, "%H:%M").time(), tzinfo=tz
            )
            range_end = datetime.combine(
                day, datetime.strptime(end_str, "%H:%M").time(), tzinfo=tz
            )
            cursor = range_start
            while cursor + duration <= range_end:
                s_utc = cursor.astimezone(timezone.utc)
                e_utc = (cursor + duration).astimezone(timezone.utc)

                is_too_soon = s_utc < earliest_start
                is_overlap = any(_overlaps(s_utc, e_utc, b_s, b_e) for b_s, b_e in busy)
                available = (not is_too_soon) and (not is_overlap)

                if available:
                    status = "available"
                    status_label = "Disponible"
                    total_available_slots += 1
                elif is_overlap:
                    status = "occupied"
                    status_label = "Ocupado"
                else:
                    status = "notice_limit"
                    status_label = "No disponible"

                slots.append(
                    {
                        "start_at": s_utc.isoformat(),
                        "end_at": e_utc.isoformat(),
                        "start_iso": s_utc.isoformat(),
                        "end_iso": e_utc.isoformat(),
                        "label": f"{cursor.strftime('%H:%M')} - {(cursor + duration).strftime('%H:%M')}",
                        "available": available,
                        "status": status,
                        "status_label": status_label,
                    }
                )
                cursor += duration

        available_count = sum(1 for s in slots if s["available"])
        formatted_day = f"{WEEKDAY_LABELS_ES[day.weekday()]}, {day.day} de {MONTH_NAMES_ES[day.month - 1].capitalize()}"
        days_payload.append(
            {
                "date": day.isoformat(),
                "weekday": day.weekday(),
                "weekday_name": WEEKDAY_LABELS_ES[day.weekday()],
                "weekday_label": WEEKDAY_LABELS_ES[day.weekday()],
                "day_number": day.strftime("%d"),
                "month_name": MONTH_NAMES_ES[day.month - 1].capitalize(),
                "formatted_date": formatted_day,
                "slots": slots,
                "available_count": available_count,
            }
        )

    week_title = (
        "Esta semana"
        if week_offset == 0
        else ("Próxima semana" if week_offset == 1 else f"Semana del {days[0].day} de {MONTH_NAMES_ES[days[0].month - 1]}")
    )
    range_obj = {
        "week_offset": week_offset,
        "week_title": week_title,
        "week_start": days[0].isoformat(),
        "week_end": days[-1].isoformat(),
        "days_summary": f"{days[0].day} al {days[-1].day} de {MONTH_NAMES_ES[days[-1].month - 1]}",
        "timezone": cfg.get("calendar_timezone") or "Europe/Madrid",
        "has_prev": week_offset > 0,
        "has_next": week_offset < 4,
    }

    return {
        "week_start": days[0].isoformat(),
        "week_end": days[-1].isoformat(),
        "range": range_obj,
        "timezone": cfg.get("calendar_timezone") or "Europe/Madrid",
        "session_duration_minutes": int(duration.total_seconds() // 60),
        "calendar_connected": calendar_connected,
        "calendar_enabled": bool(cfg.get("calendar_enabled")),
        "calendar_error": calendar_error,
        "total_slots": total_available_slots,
        "days": days_payload,
    }


# ==========================================================
# EVENTOS (CREAR / CANCELAR)
# ==========================================================
def create_calendar_event(
    cfg: dict, booking: SessionBooking, student, lesson_title: str
) -> tuple[bool, str, str, str]:
    """Crea el evento con Google Meet e invita al alumno. -> (ok, event_id, meet_link, log)"""
    try:
        service = build_calendar_service(cfg)
        calendar_id = (cfg.get("calendar_id") or "primary").strip() or "primary"
        tz_name = cfg.get("calendar_timezone") or "Europe/Madrid"
        tz = get_tz(cfg)
        start_local = ensure_utc(booking.start_at).astimezone(tz)
        end_local = ensure_utc(booking.end_at).astimezone(tz)
        student_name = student.name or student.email

        body = {
            "summary": f"Sesión Vuelve a Ti · {student_name}",
            "description": (
                f"Sesión individual del programa Vuelve a Ti.\n"
                f"Alumno/a: {student_name} ({student.email})\n"
                f"Clase asociada: {lesson_title}\n\n"
                f"Reservada desde el campus de alumnos."
            ),
            "start": {"dateTime": start_local.isoformat(), "timeZone": tz_name},
            "end": {"dateTime": end_local.isoformat(), "timeZone": tz_name},
            "attendees": [{"email": student.email, "displayName": student_name}],
            "conferenceData": {
                "createRequest": {
                    "requestId": f"vat-{booking.id}-{int(time.time())}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "email", "minutes": 24 * 60},
                    {"method": "popup", "minutes": 30},
                ],
            },
        }
        event = (
            service.events()
            .insert(
                calendarId=calendar_id,
                body=body,
                conferenceDataVersion=1,
                sendUpdates="all",
            )
            .execute()
        )
        meet_link = event.get("hangoutLink") or ""
        if not meet_link:
            for ep in (event.get("conferenceData") or {}).get("entryPoints", []):
                if ep.get("entryPointType") == "video":
                    meet_link = ep.get("uri", "")
                    break
        return True, event.get("id", ""), meet_link, "Evento creado en Google Calendar."
    except Exception as exc:
        return False, "", "", f"Error al crear el evento en Google Calendar: {exc}"


def delete_calendar_event(cfg: dict, event_id: str) -> tuple[bool, str]:
    if not event_id:
        return True, "Sin evento asociado en Google Calendar."
    try:
        service = build_calendar_service(cfg)
        calendar_id = (cfg.get("calendar_id") or "primary").strip() or "primary"
        service.events().delete(
            calendarId=calendar_id, eventId=event_id, sendUpdates="all"
        ).execute()
        return True, "Evento eliminado de Google Calendar."
    except Exception as exc:
        msg = str(exc)
        if "410" in msg or "404" in msg:
            return True, "El evento ya no existía en Google Calendar."
        return False, f"No se pudo eliminar el evento de Google Calendar: {msg}"


# ==========================================================
# DIAGNÓSTICO
# ==========================================================
def test_calendar_connection(override: dict | None = None) -> tuple[bool, str, str]:
    cfg = get_booking_config(override)
    logs = ["=== DIAGNÓSTICO GOOGLE CALENDAR ==="]
    calendar_id = (cfg.get("calendar_id") or "primary").strip() or "primary"
    logs.append(
        f"• Credenciales: {'mismo JSON que Gmail API' if cfg.get('calendar_use_gmail_token') else 'JSON propio de Calendar'}"
    )
    logs.append(f"• Calendario: {calendar_id}")
    logs.append(f"• Zona horaria: {cfg.get('calendar_timezone')}")

    if not _resolve_token_json(cfg):
        logs.append("[ERROR] No hay JSON OAuth2 configurado.")
        return False, "Falta el JSON de credenciales OAuth2 de Google.", "\n".join(logs)

    try:
        logs.append("\n1. Obteniendo Access Token con el refresh_token...")
        service = build_calendar_service(cfg)
        logs.append("   [OK] Token obtenido.")

        logs.append(f"\n2. Leyendo el calendario principal '{calendar_id}'...")
        cal = service.calendars().get(calendarId=calendar_id).execute()
        logs.append(f"   [OK] Calendario principal: {cal.get('summary')} ({cal.get('timeZone')})")

        user_cals = get_user_calendar_ids(service, calendar_id)
        cals_desc = ", ".join(f"'{c['summary']}'" for c in user_cals)
        logs.append(f"   [OK] Sincronización multi-calendario: {len(user_cals)} calendarios incluidos ({cals_desc})")

        logs.append("\n3. Consultando disponibilidad (FreeBusy) cruzada en todos los calendarios...")
        days = get_next_week_dates(cfg)
        tz = get_tz(cfg)
        start_utc = datetime.combine(days[0], dt_time(0, 0), tzinfo=tz).astimezone(timezone.utc)
        end_utc = datetime.combine(days[-1] + timedelta(days=1), dt_time(0, 0), tzinfo=tz).astimezone(timezone.utc)
        busy = fetch_busy_intervals(cfg, start_utc, end_utc, service=service)
        logs.append(f"   [OK] {len(busy)} bloques ocupados detectados entre {days[0]} y {days[-1]}.")

        cfg_check = dict(cfg)
        cfg_check["calendar_enabled"] = True
        slots = compute_available_slots(cfg_check)
        logs.append(
            f"\n✓ CONEXIÓN CORRECTA. Huecos libres de {slots['session_duration_minutes']} min la semana siguiente: {slots['total_slots']}"
        )
        return (
            True,
            f"Conectado a Google Calendar ({cal.get('summary')}). {slots['total_slots']} huecos libres la semana que viene.",
            "\n".join(logs),
        )
    except Exception as exc:
        msg = str(exc)
        logs.append(f"\n[ERROR] {msg}")
        if "insufficient" in msg.lower() or "403" in msg:
            logs.append(
                "[SOLUCIÓN] El refresh_token no tiene permiso de Calendar. Genera uno nuevo en "
                "OAuth Playground incluyendo el scope https://www.googleapis.com/auth/calendar "
                "y habilita 'Google Calendar API' en Google Cloud Console."
            )
        logs.append(traceback.format_exc(limit=1))
        return False, "No se pudo conectar con Google Calendar.", "\n".join(logs)
