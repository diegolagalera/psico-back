import base64
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
import html as html_lib
import json
import os
import re
import secrets
import smtplib
import threading
import time
import traceback
from typing import Tuple

from flask import Flask, has_request_context, request
from sqlalchemy import text

from app.email_service import get_or_create_system_config
from app.models import MarketingLead, User, db
from app.marketing_models import (
    MarketingAutomationQueue,
    MarketingAutomationStep,
    MarketingCampaign,
    MarketingCampaignRecipient,
    MarketingGroup,
    MarketingGroupMember,
    MarketingTemplate,
)

# 1x1 GIF transparente válido (43 bytes)
TRACKING_GIF_BYTES = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

_scheduler_started = False
_scheduler_lock = threading.Lock()


def generate_tracking_token() -> str:
    return secrets.token_urlsafe(24)


def resolve_api_base_url(stored_url: str | None = None) -> str:
    """
    Determina la URL pública del backend para construir los enlaces del píxel
    de apertura y de baja (unsubscribe).
    """
    if stored_url and stored_url.strip():
        return stored_url.strip().rstrip("/")

    env_url = (os.getenv("PUBLIC_API_URL") or "").strip()
    if env_url:
        return env_url.rstrip("/")

    if has_request_context():
        proto = (
            (request.headers.get("X-Forwarded-Proto") or request.scheme or "https")
            .split(",")[0]
            .strip()
        )
        host = (
            (
                request.headers.get("X-Forwarded-Host")
                or request.host
                or "api.carlaviladotpsicologia.com"
            )
            .split(",")[0]
            .strip()
        )
        if "carlaviladotpsicologia.com" in host:
            proto = "https"
        return f"{proto}://{host}".rstrip("/")

    return "https://api.carlaviladotpsicologia.com"


def extract_title_from_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.IGNORECASE | re.DOTALL)
    if match:
        cleaned = re.sub(r"\s+", " ", match.group(1)).strip()
        return html_lib.unescape(cleaned)[:250]
    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", raw_html, re.IGNORECASE | re.DOTALL)
    if h1_match:
        no_tags = re.sub(r"<[^>]+>", "", h1_match.group(1))
        cleaned = re.sub(r"\s+", " ", no_tags).strip()
        return html_lib.unescape(cleaned)[:250]
    return ""


def render_marketing_html(
    raw_html: str,
    recipient_name: str = "",
    recipient_email: str = "",
) -> str:
    """
    Reemplaza las variables dinámicas ([Nombre], {{nombre}}, {{email}}) en la plantilla HTML.
    Si el usuario no dejó su nombre, adapta el saludo automáticamente para que nunca
    aparezca '[Nombre]' ni quede una coma huérfana como 'Hola, :'.
    """
    content = raw_html or ""
    clean_name = (recipient_name or "").strip()
    if clean_name.lower() in ("alumno/a", "visitante", "usuario", "lead", "sin nombre"):
        clean_name = ""

    if clean_name:
        safe_name = html_lib.escape(clean_name)
        for token in (
            "[Nombre]",
            "[nombre]",
            "[NOMBRE]",
            "{{nombre}}",
            "{{name}}",
            "{{Nombre}}",
            "{nombre}",
        ):
            content = content.replace(token, safe_name)
    else:
        # Adaptar saludos frecuentes con [Nombre] cuando no hay nombre
        greeting_patterns = [
            (r"Hola,\s*<strong>\s*(?:\[Nombre\]|\{\{nombre\}\})\s*</strong>\s*:", "¡Hola!"),
            (r"Hola\s*<strong>\s*(?:\[Nombre\]|\{\{nombre\}\})\s*</strong>\s*:", "¡Hola!"),
            (r"Hola,\s*(?:\[Nombre\]|\{\{nombre\}\})\s*:", "¡Hola!"),
            (r"Hola\s+(?:\[Nombre\]|\{\{nombre\}\})\s*,", "¡Hola!"),
            (r"Hola\s+(?:\[Nombre\]|\{\{nombre\}\})\s*:", "¡Hola!"),
        ]
        for pattern, replacement in greeting_patterns:
            content = re.sub(pattern, replacement, content, flags=re.IGNORECASE)

        for token in (
            "[Nombre]",
            "[nombre]",
            "[NOMBRE]",
            "{{nombre}}",
            "{{name}}",
            "{{Nombre}}",
            "{nombre}",
        ):
            content = content.replace(token, "")

    safe_email = html_lib.escape((recipient_email or "").strip())
    for email_token in ("[Email]", "[email]", "{{email}}", "{email}"):
        content = content.replace(email_token, safe_email)

    return content


def html_to_plain_text(html_body: str) -> str:
    """
    Genera automáticamente una versión text/plain limpia a partir del HTML.
    Incluir text/plain junto con text/html en MIMEMultipart('alternative') es
    esencial para evitar penalizaciones de filtros anti-spam (SpamAssassin, Gmail, Outlook).
    """
    if not html_body:
        return ""

    text_out = html_body
    # Eliminar bloques head, style y script
    text_out = re.sub(
        r"<(head|style|script)[^>]*>.*?</\1>",
        "",
        text_out,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Preservar enlaces <a href="url">texto</a> -> texto (url)
    def _replace_link(match):
        href = (match.group(1) or "").strip()
        label = re.sub(r"<[^>]+>", "", match.group(2) or "").strip()
        if not href or href.startswith("mailto:") or href == label:
            return label or href
        return f"{label} ({href})"

    text_out = re.sub(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        _replace_link,
        text_out,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Saltos de línea semánticos
    text_out = re.sub(r"<br\s*/?>", "\n", text_out, flags=re.IGNORECASE)
    text_out = re.sub(
        r"</(p|div|tr|h1|h2|h3|h4|li|blockquote)>",
        "\n\n",
        text_out,
        flags=re.IGNORECASE,
    )
    text_out = re.sub(r"<li[^>]*>", "• ", text_out, flags=re.IGNORECASE)
    # Quitar resto de etiquetas HTML
    text_out = re.sub(r"<[^>]+>", "", text_out)
    text_out = html_lib.unescape(text_out)

    # Limpiar espacios múltiples por línea
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text_out.splitlines()]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def prepare_tracked_email_html(
    rendered_html: str,
    tracking_token: str | None = None,
    api_base_url: str | None = None,
    preheader: str = "",
    include_unsubscribe_footer: bool = False,
) -> Tuple[str, str | None]:
    """
    Inyecta en el HTML:
    1. Preheader sutil (si se indica).
    2. Enlace discreto de baja (solo si include_unsubscribe_footer=True, ya que
       incluir cabeceras/pies de baja masiva hace que Gmail clasifique el correo en 'Promociones').
    3. Píxel transparente 1x1 de seguimiento de apertura (sin opacity:0 para que
       el proxy de imágenes de Gmail lo cargue siempre).
    Devuelve (final_html, unsubscribe_url).
    """
    final_html = rendered_html or ""
    base = resolve_api_base_url(api_base_url)

    unsubscribe_url = None
    tracking_pixel_url = None
    if tracking_token:
        if include_unsubscribe_footer:
            unsubscribe_url = f"{base}/api/marketing/unsubscribe/{tracking_token}"
        tracking_pixel_url = f"{base}/api/marketing/track/open/{tracking_token}.gif"

    # 1. Preheader limpio tras <body> (sin cadenas &zwnj; que activan la pestaña Promociones)
    if preheader and preheader.strip():
        safe_pre = html_lib.escape(preheader.strip())
        preheader_block = (
            f'<div style="display:none;max-height:0px;overflow:hidden;font-size:1px;color:#F4F1EA;">'
            f"{safe_pre}</div>"
        )
        body_match = re.search(r"<body[^>]*>", final_html, flags=re.IGNORECASE)
        if body_match:
            insert_pos = body_match.end()
            final_html = (
                final_html[:insert_pos] + preheader_block + final_html[insert_pos:]
            )
        else:
            final_html = preheader_block + final_html

    # 2. Pie de baja opcional + Píxel 1x1 transparente (sin opacity:0 para evitar bloqueo en Gmail)
    extras_before_body_end = ""
    if (
        include_unsubscribe_footer
        and unsubscribe_url
        and "marketing/unsubscribe/" not in final_html
    ):
        extras_before_body_end += f"""
        <div style="text-align:center;padding:12px 15px 24px 15px;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;font-size:12px;color:#8C8E87;background-color:#F4F1EA;">
          <p style="margin:0;">
            Si prefieres dejar de recibir estos correos, puedes
            <a href="{unsubscribe_url}" style="color:#6B8068;text-decoration:underline;" target="_blank">darte de baja aquí</a>.
          </p>
        </div>
        """

    if tracking_pixel_url:
        extras_before_body_end += (
            f'<img src="{tracking_pixel_url}" width="1" height="1" border="0" alt="" '
            f'style="height:1px !important;width:1px !important;border-width:0 !important;'
            f'margin:0 !important;padding:0 !important;" />'
        )

    if extras_before_body_end:
        if re.search(r"</body>", final_html, flags=re.IGNORECASE):
            final_html = re.sub(
                r"</body>",
                f"{extras_before_body_end}</body>",
                final_html,
                count=1,
                flags=re.IGNORECASE,
            )
        else:
            final_html = final_html + extras_before_body_end

    # 3. Envolver enlaces <a href="http..."> para que si el usuario hace clic (incluso con imágenes bloqueadas o en localhost) también cuente como correo abierto
    if tracking_token:
        import urllib.parse

        def _wrap_href(match):
            prefix = match.group(1)
            original_url = match.group(2)
            suffix = match.group(3)
            if (
                not original_url.startswith(("http://", "https://"))
                or "/api/marketing/" in original_url
            ):
                return match.group(0)
            encoded = urllib.parse.quote(original_url, safe="")
            tracked_href = f"{base}/api/marketing/track/click/{tracking_token}?u={encoded}"
            return f"{prefix}{tracked_href}{suffix}"

        final_html = re.sub(
            r'(<a\s+[^>]*href=["\'])([^"\']+)(["\'])',
            _wrap_href,
            final_html,
            flags=re.IGNORECASE,
        )

    return final_html, unsubscribe_url


def send_marketing_email(
    to_email: str,
    subject: str,
    html_body: str,
    recipient_name: str = "",
    tracking_token: str | None = None,
    api_base_url: str | None = None,
    preheader: str = "",
    is_test: bool = False,
    include_unsubscribe: bool = False,
) -> Tuple[bool, str]:
    """
    Envía un correo con versión text/plain + text/html (MIMEMultipart('alternative'))
    y píxel de seguimiento de apertura.
    Nota: `include_unsubscribe=False` por defecto para evitar que Gmail clasifique
    automáticamente el correo en la pestaña 'Promociones' por llevar cabeceras de envío masivo.
    """
    config_obj = get_or_create_system_config()
    cfg = config_obj.to_dict()

    rendered_html = render_marketing_html(
        raw_html=html_body,
        recipient_name=recipient_name,
        recipient_email=to_email,
    )
    final_html, unsubscribe_url = prepare_tracked_email_html(
        rendered_html=rendered_html,
        tracking_token=tracking_token,
        api_base_url=api_base_url,
        preheader=preheader,
        include_unsubscribe_footer=bool(include_unsubscribe and not is_test),
    )
    plain_text = html_to_plain_text(rendered_html)
    if unsubscribe_url and include_unsubscribe and not is_test:
        plain_text += f"\n\n---\nPara darte de baja: {unsubscribe_url}"

    sender_email = (cfg.get("smtp_user") or "").strip()
    sender_domain = (
        sender_email.split("@")[1]
        if "@" in sender_email
        else "carlaviladotpsicologia.com"
    )
    sender_display = "Carla Viladot · Psicóloga"

    # Construir mensaje MIME cumpliendo estándares de entregabilidad
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = (
        formataddr((sender_display, sender_email))
        if "@" in sender_email
        else sender_display
    )
    clean_rec_name = (recipient_name or "").strip()
    if clean_rec_name and clean_rec_name.lower() not in ("alumno/a", "visitante"):
        msg["To"] = formataddr((clean_rec_name, to_email))
    else:
        msg["To"] = to_email

    if "@" in sender_email:
        msg["Reply-To"] = formataddr((sender_display, sender_email))

    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender_domain)

    if unsubscribe_url and include_unsubscribe:
        unsub_headers = [f"<{unsubscribe_url}>"]
        if "@" in sender_email:
            unsub_headers.append(f"<mailto:{sender_email}?subject=Baja>")
        msg["List-Unsubscribe"] = ", ".join(unsub_headers)
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    # IMPORTANTE Anti-Spam: Primero text/plain, después text/html
    msg.attach(MIMEText(plain_text or subject, "plain", "utf-8"))
    msg.attach(MIMEText(final_html, "html", "utf-8"))

    use_gmail_api = bool(cfg.get("use_gmail_api"))
    if use_gmail_api:
        token_raw = (cfg.get("gmail_token_json") or "").strip()
        if not token_raw:
            return False, "El campo 'gmail_token_json' está vacío en Configuración de Correo."
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build

            token_data = json.loads(token_raw)
            client_info = token_data
            if "installed" in token_data and isinstance(token_data["installed"], dict):
                client_info = {**token_data["installed"], **token_data}
            elif "web" in token_data and isinstance(token_data["web"], dict):
                client_info = {**token_data["web"], **token_data}

            creds = Credentials(
                token=client_info.get("token"),
                refresh_token=client_info.get("refresh_token"),
                token_uri=client_info.get(
                    "token_uri", "https://oauth2.googleapis.com/token"
                ),
                client_id=client_info.get("client_id"),
                client_secret=client_info.get("client_secret"),
                scopes=["https://www.googleapis.com/auth/gmail.send"],
            )
            creds.refresh(Request())
            service = build("gmail", "v1", credentials=creds)

            encoded_message = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
            sent = (
                service.users()
                .messages()
                .send(userId="me", body={"raw": encoded_message})
                .execute()
            )
            return True, f"Enviado vía Gmail API (ID: {sent.get('id', 'OK')})"
        except Exception as exc:
            return False, f"Error Gmail API: {str(exc)}"

    # Modo SMTP estándar
    smtp_host = (cfg.get("smtp_host") or "").strip()
    smtp_port = int(cfg.get("smtp_port") or 587)
    smtp_user = (cfg.get("smtp_user") or "").strip()
    smtp_password = cfg.get("smtp_password") or ""

    if not smtp_host or not smtp_user or not smtp_password:
        return (
            False,
            "Faltan credenciales SMTP (Host, Usuario o Contraseña). Configúralas en Configuración -> Correo.",
        )

    try:
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
            server.ehlo()
            server.starttls()
            server.ehlo()

        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [to_email], msg.as_string())
        server.quit()
        return True, "Enviado correctamente vía SMTP."
    except Exception as exc:
        return False, f"Error SMTP: {str(exc)}"


def seed_marketing_defaults() -> None:
    """
    Precarga automáticamente las 3 plantillas HTML de `backend/plantilla_html/`
    (24h.html, 48h.html, 72h.html) y los 3 pasos de la secuencia de bienvenida
    para nuevos usuarios de recursos gratuitos, si aún no existen en la base de datos.
    """
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    templates_dir = os.path.join(base_dir, "plantilla_html")

    default_specs = [
        {
            "step_order": 1,
            "delay_hours": 24,
            "filename": "24h.html",
            "template_name": "Secuencia 24h · El truco para que la guía te funcione",
            "step_title": "Correo 1 · A las 24 horas del registro",
            "default_subject": "El truco para que la guía te funcione ❤️‍🩹",
            "preheader": "Leer la guía cuando estás tranquilo/a es fácil. El secreto está en qué hacer cuando aparece la urgencia.",
        },
        {
            "step_order": 2,
            "delay_hours": 48,
            "filename": "48h.html",
            "template_name": "Secuencia 48h · Por qué el tiempo NO lo cura todo",
            "step_title": "Correo 2 · A las 48 horas del registro",
            "default_subject": "Por qué el tiempo NO lo cura todo ❤️‍🩹",
            "preheader": "Por qué te sigue doliendo tanto aunque pasen las semanas (y qué necesita tu sistema nervioso).",
        },
        {
            "step_order": 3,
            "delay_hours": 72,
            "filename": "72h.html",
            "template_name": "Secuencia 72h · Cómo ir a la raíz del dolor",
            "step_title": "Correo 3 · A las 72 horas del registro",
            "default_subject": "Cómo ir a la raíz del dolor ❤️‍🩹",
            "preheader": "Que te digan que tienes que pasar página no basta; necesitas un mapa claro para reconstruirte.",
        },
    ]

    existing_steps = MarketingAutomationStep.query.count()
    if existing_steps > 0:
        return

    for spec in default_specs:
        file_path = os.path.join(templates_dir, spec["filename"])
        html_raw = ""
        if os.path.isfile(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    html_raw = f.read()
            except Exception:
                html_raw = ""

        if not html_raw:
            html_raw = (
                f"<html><body><h1>{spec['default_subject']}</h1>"
                f"<p>Hola, <strong>[Nombre]</strong>:</p></body></html>"
            )

        extracted_subj = extract_title_from_html(html_raw) or spec["default_subject"]

        # Crear o reutilizar plantilla en marketing_templates
        tpl = MarketingTemplate.query.filter_by(
            original_filename=spec["filename"]
        ).first()
        if not tpl:
            tpl = MarketingTemplate(
                name=spec["template_name"],
                subject=extracted_subj,
                preheader=spec["preheader"],
                category="automation",
                original_filename=spec["filename"],
                html_content=html_raw,
            )
            db.session.add(tpl)
            db.session.flush()

        step = MarketingAutomationStep(
            step_order=spec["step_order"],
            title=spec["step_title"],
            delay_hours=spec["delay_hours"],
            subject=extracted_subj,
            preheader=spec["preheader"],
            template_id=tpl.id,
            original_filename=spec["filename"],
            html_content=html_raw,
            is_active=True,
        )
        db.session.add(step)

    db.session.commit()


def enqueue_new_lead_automation(
    lead: MarketingLead,
    resource_title: str = "",
    base_time: datetime | None = None,
    api_base_url: str | None = None,
) -> int:
    """
    Programa los correos automáticos (24h, 48h, 72h...) para un nuevo usuario
    que deja su correo al descargar un recurso gratuito.
    Retorna el número de correos encolados.
    """
    if not lead or not lead.email or not lead.consent_marketing:
        return 0

    now_utc = base_time or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    resolved_base = resolve_api_base_url(api_base_url)

    steps = (
        MarketingAutomationStep.query.filter_by(is_active=True)
        .order_by(MarketingAutomationStep.step_order.asc())
        .all()
    )

    enqueued = 0
    for step in steps:
        email_clean = lead.email.strip().lower()
        existing = MarketingAutomationQueue.query.filter_by(
            step_id=step.id, email=email_clean
        ).first()
        if existing:
            continue

        delay_h = max(1, int(step.delay_hours or 24))
        scheduled_for = now_utc + timedelta(hours=delay_h)

        item = MarketingAutomationQueue(
            step_id=step.id,
            lead_id=lead.id,
            email=email_clean,
            name=(lead.name or "").strip(),
            resource_title=resource_title or lead.source_resource_title or "Recurso Gratuito",
            delay_hours=delay_h,
            scheduled_for=scheduled_for,
            status="pending",
            tracking_token=generate_tracking_token(),
            api_base_url=resolved_base,
        )
        db.session.add(item)
        enqueued += 1

    if enqueued > 0:
        db.session.commit()
    return enqueued


def resolve_campaign_Audience(
    target_type: str,
    target_group_ids: list[int] | None = None,
    target_emails: list[dict | str] | None = None,
) -> list[dict]:
    """
    Resuelve la lista única de destinatarios `{email, name}` según la selección del admin:
    - 'all': Todos los leads con consentimiento + todos los alumnos activos
    - 'leads': Todos los leads de recursos gratuitos con consent_marketing=True
    - 'students': Todos los usuarios con rol 'user' activos
    - 'groups': Miembros de los grupos seleccionados
    - 'custom': Correos específicos seleccionados o escritos manualmente
    Respeta automáticamente las bajas (consent_marketing == False).
    """
    unsubscribed_emails = {
        row.email.strip().lower()
        for row in MarketingLead.query.filter_by(consent_marketing=False).all()
        if row.email
    }

    recipients_map: dict[str, str] = {}

    def _add(email_raw: str, name_raw: str = ""):
        e = (email_raw or "").strip().lower()
        if not e or "@" not in e or e in unsubscribed_emails:
            return
        n = (name_raw or "").strip()
        if e not in recipients_map or (not recipients_map[e] and n):
            recipients_map[e] = n

    if target_type in ("all", "leads"):
        leads = MarketingLead.query.filter_by(consent_marketing=True).all()
        for l in leads:
            _add(l.email, l.name)

    if target_type in ("all", "students"):
        students = User.query.filter_by(role="user", is_active=True).all()
        for s in students:
            _add(s.email, s.name)

    if target_type == "groups" and target_group_ids:
        clean_ids = [int(gid) for gid in target_group_ids if str(gid).isdigit()]
        if clean_ids:
            members = MarketingGroupMember.query.filter(
                MarketingGroupMember.group_id.in_(clean_ids)
            ).all()
            for m in members:
                _add(m.email, m.name)

    if target_type == "custom" and target_emails:
        # Buscar nombres conocidos de leads o usuarios por si solo pasaron el string del email
        known_names: dict[str, str] = {}
        for l in MarketingLead.query.all():
            if l.email and l.name:
                known_names[l.email.strip().lower()] = l.name.strip()
        for u in User.query.all():
            if u.email and u.name:
                known_names.setdefault(u.email.strip().lower(), u.name.strip())

        for entry in target_emails:
            if isinstance(entry, dict):
                em = (entry.get("email") or "").strip().lower()
                nm = (entry.get("name") or "").strip() or known_names.get(em, "")
                _add(em, nm)
            elif isinstance(entry, str):
                for part in re.split(r"[,;\s\n]+", entry):
                    em = part.strip().lower()
                    if em and "@" in em:
                        _add(em, known_names.get(em, ""))

    return [{"email": k, "name": v} for k, v in recipients_map.items()]


def process_pending_marketing_jobs() -> dict:
    """
    Procesa de forma segura (con bloqueo en BD para evitar duplicados en múltiples workers):
    1. Correos pendientes en `marketing_automation_queue` cuya hora `scheduled_for` ya llegó.
    2. Campañas en `marketing_campaigns` con estado `scheduled` o `sending` cuya hora `scheduled_at` ya llegó.
    """
    summary = {"automation_sent": 0, "automation_failed": 0, "campaigns_sent": 0, "campaigns_failed": 0}

    # Intentar adquirir lock de MySQL no bloqueante para evitar colisión entre workers de Gunicorn
    lock_acquired = False
    try:
        res = db.session.execute(
            text("SELECT GET_LOCK('vuelveati_marketing_worker_lock', 0)")
        ).scalar()
        if res != 1:
            return summary
        lock_acquired = True
    except Exception:
        # Si no es MySQL o falla GET_LOCK, continuamos apoyándonos en estados transaccionales
        pass

    try:
        now_utc = datetime.now(timezone.utc)
        now_naive = now_utc.replace(tzinfo=None)

        # ------------------------------------------------------------------
        # 1. PROCESAR COLA DE AUTOMATIZACIÓN (24h / 48h / 72h)
        # ------------------------------------------------------------------
        due_queue = (
            MarketingAutomationQueue.query.filter(
                MarketingAutomationQueue.status == "pending",
                MarketingAutomationQueue.scheduled_for <= now_naive,
            )
            .order_by(MarketingAutomationQueue.scheduled_for.asc())
            .limit(25)
            .all()
        )

        for item in due_queue:
            step = MarketingAutomationStep.query.get(item.step_id)
            if not step or not step.is_active:
                continue

            # Verificar si el lead sigue con consent_marketing = True
            lead = MarketingLead.query.filter_by(email=item.email).first()
            if lead and not lead.consent_marketing:
                item.status = "skipped_unsubscribed"
                item.error_message = "El usuario se dio de baja de comunicaciones de marketing."
                db.session.commit()
                continue

            item.status = "processing"
            db.session.commit()

            recipient_name = item.name or (lead.name if lead else "") or ""
            ok, log_msg = send_marketing_email(
                to_email=item.email,
                subject=step.subject,
                html_body=step.html_content,
                recipient_name=recipient_name,
                tracking_token=item.tracking_token,
                api_base_url=item.api_base_url,
                preheader=step.preheader or "",
                is_test=False,
            )

            if ok:
                item.status = "sent"
                item.sent_at = datetime.now(timezone.utc)
                item.error_message = None
                step.total_sent = int(step.total_sent or 0) + 1
                summary["automation_sent"] += 1
            else:
                item.status = "failed"
                item.error_message = log_msg
                summary["automation_failed"] += 1

            db.session.commit()
            time.sleep(0.6)  # Pausa anti-spam entre envíos

        # ------------------------------------------------------------------
        # 2. PROCESAR CAMPAÑAS PROGRAMADAS O EN CURSO
        # ------------------------------------------------------------------
        due_campaigns = (
            MarketingCampaign.query.filter(
                MarketingCampaign.status.in_(["scheduled", "sending"]),
                db.or_(
                    MarketingCampaign.scheduled_at.is_(None),
                    MarketingCampaign.scheduled_at <= now_naive,
                ),
            )
            .order_by(MarketingCampaign.scheduled_at.asc())
            .all()
        )

        for camp in due_campaigns:
            if camp.status == "scheduled":
                camp.status = "sending"
                camp.started_at = datetime.now(timezone.utc)
                db.session.commit()

            pending_recs = (
                MarketingCampaignRecipient.query.filter_by(
                    campaign_id=camp.id, status="pending"
                )
                .order_by(MarketingCampaignRecipient.id.asc())
                .limit(35)
                .all()
            )

            if not pending_recs:
                camp.status = "sent"
                camp.completed_at = datetime.now(timezone.utc)
                db.session.commit()
                continue

            for rec in pending_recs:
                lead = MarketingLead.query.filter_by(email=rec.email).first()
                if lead and not lead.consent_marketing:
                    rec.status = "skipped_unsubscribed"
                    rec.error_message = "Usuario dado de baja."
                    db.session.commit()
                    continue

                rec.status = "processing"
                db.session.commit()

                ok, log_msg = send_marketing_email(
                    to_email=rec.email,
                    subject=camp.subject,
                    html_body=camp.html_content,
                    recipient_name=rec.name or "",
                    tracking_token=rec.tracking_token,
                    api_base_url=camp.api_base_url,
                    preheader=camp.preheader or "",
                    is_test=False,
                )

                if ok:
                    rec.status = "sent"
                    rec.sent_at = datetime.now(timezone.utc)
                    rec.error_message = None
                    camp.sent_count = int(camp.sent_count or 0) + 1
                    summary["campaigns_sent"] += 1
                else:
                    rec.status = "failed"
                    rec.error_message = log_msg
                    camp.failed_count = int(camp.failed_count or 0) + 1
                    summary["campaigns_failed"] += 1

                db.session.commit()
                time.sleep(0.6)  # Pausa anti-spam entre envíos

            # Comprobar si ya no quedan pendientes en la campaña
            still_pending = MarketingCampaignRecipient.query.filter_by(
                campaign_id=camp.id, status="pending"
            ).count()
            if still_pending == 0:
                if int(camp.sent_count or 0) == 0 and int(camp.failed_count or 0) > 0:
                    camp.status = "failed"
                else:
                    camp.status = "sent"
                camp.completed_at = datetime.now(timezone.utc)
                db.session.commit()

    except Exception as exc:
        print(f"[MARKETING WORKER ERROR] {exc}")
        traceback.print_exc()
        db.session.rollback()
    finally:
        if lock_acquired:
            try:
                db.session.execute(
                    text("SELECT RELEASE_LOCK('vuelveati_marketing_worker_lock')")
                )
            except Exception:
                pass

    return summary


def start_marketing_scheduler(app: Flask) -> None:
    """
    Inicia un hilo daemon en segundo plano que revisa cada 30 segundos si hay
    correos programados de campañas o de la secuencia 24h/48h/72h listos para enviar.
    """
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    def _loop():
        time.sleep(5)
        while True:
            try:
                with app.app_context():
                    process_pending_marketing_jobs()
            except Exception as exc:
                print(f"[MARKETING SCHEDULER] {exc}")
            time.sleep(30)

    thread = threading.Thread(
        target=_loop, name="VuelveATiMarketingScheduler", daemon=True
    )
    thread.start()
