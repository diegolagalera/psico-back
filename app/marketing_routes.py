from datetime import datetime, timezone
from functools import wraps
import html
import json
import re
import threading

from flask import Blueprint, Response, current_app, jsonify, request
from flask_jwt_extended import get_jwt, jwt_required

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
from app.marketing_service import (
    TRACKING_GIF_BYTES,
    enqueue_new_lead_automation,
    extract_title_from_html,
    generate_tracking_token,
    html_to_plain_text,
    process_pending_marketing_jobs,
    render_marketing_html,
    resolve_api_base_url,
    resolve_campaign_Audience,
    seed_marketing_defaults,
    send_marketing_email,
)

marketing_bp = Blueprint("marketing", __name__)


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


def _parse_iso_datetime(val: str | None) -> datetime | None:
    if not val or not str(val).strip():
        return None
    raw = str(val).strip()
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        return None


def _trigger_async_worker(app_obj) -> None:
    def _run():
        with app_obj.app_context():
            process_pending_marketing_jobs()

    threading.Thread(target=_run, daemon=True).start()


# ==============================================================================
# ENDPOINTS PÚBLICOS: TRACKING DE APERTURA (PÍXEL 1x1) Y BAJA (UNSUBSCRIBE)
# ==============================================================================


def _record_open_by_token(token: str) -> None:
    clean_token = (token or "").strip()
    if not clean_token:
        return
    now_utc = datetime.now(timezone.utc)
    ua = (request.headers.get("User-Agent") or "")[:300]
    try:
        rec = MarketingCampaignRecipient.query.filter_by(
            tracking_token=clean_token
        ).first()
        if rec:
            is_first_open = rec.opened_at is None
            if is_first_open:
                rec.opened_at = now_utc
            rec.last_opened_at = now_utc
            rec.open_count = int(rec.open_count or 0) + 1
            if ua:
                rec.user_agent = ua

            if is_first_open and rec.campaign_id:
                camp = MarketingCampaign.query.get(rec.campaign_id)
                if camp:
                    camp.opened_count = int(camp.opened_count or 0) + 1
            db.session.commit()
            return

        q_item = MarketingAutomationQueue.query.filter_by(
            tracking_token=clean_token
        ).first()
        if q_item:
            is_first_open = q_item.opened_at is None
            if is_first_open:
                q_item.opened_at = now_utc
            q_item.last_opened_at = now_utc
            q_item.open_count = int(q_item.open_count or 0) + 1
            if ua:
                q_item.user_agent = ua

            if is_first_open and q_item.step_id:
                step = MarketingAutomationStep.query.get(q_item.step_id)
                if step:
                    step.total_opened = int(step.total_opened or 0) + 1
            db.session.commit()
    except Exception:
        db.session.rollback()


@marketing_bp.route("/marketing/track/open/<string:token>.gif", methods=["GET"])
def track_email_open(token: str):
    """
    Registra la apertura de un correo electrónico (tanto de campañas como de la
    secuencia 24h/48h/72h) y devuelve un GIF 1x1 transparente sin caché.
    """
    _record_open_by_token(token)
    resp = Response(TRACKING_GIF_BYTES, mimetype="image/gif")
    resp.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate, proxy-revalidate, max-age=0"
    )
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@marketing_bp.route("/marketing/track/click/<string:token>", methods=["GET"])
def track_email_click(token: str):
    """
    Registra la apertura del correo cuando el destinatario hace clic en cualquier
    enlace del email y lo redirige inmediatamente al destino original.
    Funciona incluso si el cliente de correo bloquea imágenes o si se prueba en localhost.
    """
    from flask import redirect

    _record_open_by_token(token)
    target_url = (request.args.get("u") or "").strip()
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://carlaviladotpsicologia.com"
    return redirect(target_url, code=302)


@marketing_bp.route(
    "/marketing/unsubscribe/<string:token>", methods=["GET", "POST"]
)
def unsubscribe_marketing(token: str):
    """
    Procesa la baja de comunicaciones comerciales con un solo clic (cumpliendo
    RFC 8058 List-Unsubscribe-Post de Gmail/Yahoo y enlace directo en el pie del email).
    """
    clean_token = (token or "").strip()
    email_found = ""
    name_found = ""

    if clean_token:
        rec = MarketingCampaignRecipient.query.filter_by(
            tracking_token=clean_token
        ).first()
        if rec:
            email_found = rec.email
            name_found = rec.name or ""
        else:
            q_item = MarketingAutomationQueue.query.filter_by(
                tracking_token=clean_token
            ).first()
            if q_item:
                email_found = q_item.email
                name_found = q_item.name or ""

    if email_found:
        email_clean = email_found.strip().lower()
        lead = MarketingLead.query.filter_by(email=email_clean).first()
        if lead:
            lead.consent_marketing = False
        else:
            lead = MarketingLead(
                email=email_clean,
                name=name_found,
                consent_marketing=False,
                consent_text="Baja solicitada por el usuario desde enlace de correo.",
                source_channel="unsubscribe_link",
            )
            db.session.add(lead)

        # Cancelar correos automáticos pendientes para este email
        pending_auto = MarketingAutomationQueue.query.filter_by(
            email=email_clean, status="pending"
        ).all()
        for p in pending_auto:
            p.status = "skipped_unsubscribed"
            p.error_message = "El usuario solicitó darse de baja."

        db.session.commit()

    if request.method == "POST":
        return jsonify({"status": "unsubscribed", "email": email_found}), 200

    safe_email = html.escape(email_found or "tu correo")
    html_page = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Suscripción actualizada · Carla Viladot Psicología</title>
</head>
<body style="margin:0;padding:40px 16px;background-color:#FAF8F5;font-family:'Helvetica Neue',Arial,sans-serif;color:#2C3E35;text-align:center;">
  <div style="max-width:480px;margin:40px auto;background:#FFFFFF;border-radius:20px;padding:36px 28px;border:1px solid #E6E1DA;box-shadow:0 8px 28px rgba(0,0,0,0.04);">
    <div style="width:48px;height:48px;border-radius:50%;background:#F0F3EF;color:#4A6B5D;display:inline-flex;align-items:center;justify-content:center;font-size:22px;margin-bottom:16px;">✓</div>
    <h1 style="font-family:Georgia,serif;font-size:22px;margin:0 0 12px 0;color:#2C3E35;">Has actualizado tus preferencias</h1>
    <p style="font-size:15px;line-height:1.6;color:#555852;margin:0 0 24px 0;">
      Hemos dado de baja a <strong>{safe_email}</strong> de nuestra lista de envíos de email marketing y secuencias automáticas. No recibirás más correos promocionales.
    </p>
    <a href="https://carlaviladotpsicologia.com" style="display:inline-block;background:#4A6B5D;color:#FFFFFF;text-decoration:none;padding:12px 24px;border-radius:99px;font-size:14px;font-weight:bold;">
      Volver a carlaviladotpsicologia.com
    </a>
  </div>
</body>
</html>"""
    return Response(html_page, mimetype="text/html")


# ==============================================================================
# ENDPOINTS DE ADMINISTRACIÓN: RESUMEN Y PANEL DE CONTROL
# ==============================================================================


@marketing_bp.route("/admin/marketing/overview", methods=["GET"])
@admin_required
def admin_marketing_overview():
    """
    Devuelve todos los datos iniciales del módulo de Email Marketing:
    - Estadísticas globales (enviados, abiertos, % de apertura, en cola)
    - Plantillas HTML
    - Grupos de contactos
    - Campañas
    - Secuencia automática 24h / 48h / 72h y su cola de envíos
    """
    seed_marketing_defaults()
    # Aprovechar para procesar cualquier envío vencido
    process_pending_marketing_jobs()

    templates = (
        MarketingTemplate.query.order_by(MarketingTemplate.updated_at.desc()).all()
    )
    groups = MarketingGroup.query.order_by(MarketingGroup.created_at.desc()).all()
    campaigns = (
        MarketingCampaign.query.order_by(MarketingCampaign.created_at.desc()).all()
    )
    steps = (
        MarketingAutomationStep.query.order_by(
            MarketingAutomationStep.step_order.asc()
        ).all()
    )
    queue_items = (
        MarketingAutomationQueue.query.order_by(
            MarketingAutomationQueue.scheduled_for.desc()
        )
        .limit(200)
        .all()
    )

    leads_active = MarketingLead.query.filter_by(consent_marketing=True).count()
    leads_total = MarketingLead.query.count()
    students_active = User.query.filter_by(role="user", is_active=True).count()

    camp_sent = sum(int(c.sent_count or 0) for c in campaigns)
    camp_opened = sum(int(c.opened_count or 0) for c in campaigns)
    auto_sent = sum(int(s.total_sent or 0) for s in steps)
    auto_opened = sum(int(s.total_opened or 0) for s in steps)

    total_sent = camp_sent + auto_sent
    total_opened = camp_opened + auto_opened
    global_open_rate = (
        round((total_opened / total_sent) * 100, 1) if total_sent > 0 else 0.0
    )

    pending_auto_count = MarketingAutomationQueue.query.filter_by(
        status="pending"
    ).count()
    scheduled_campaigns_count = MarketingCampaign.query.filter_by(
        status="scheduled"
    ).count()

    return (
        jsonify(
            {
                "stats": {
                    "total_sent": total_sent,
                    "total_opened": total_opened,
                    "global_open_rate": global_open_rate,
                    "campaigns_sent": camp_sent,
                    "campaigns_opened": camp_opened,
                    "automation_sent": auto_sent,
                    "automation_opened": auto_opened,
                    "pending_automation_queue": pending_auto_count,
                    "scheduled_campaigns": scheduled_campaigns_count,
                    "active_leads_count": leads_active,
                    "total_leads_count": leads_total,
                    "active_students_count": students_active,
                },
                "templates": [t.to_dict(include_html=True) for t in templates],
                "groups": [g.to_dict(include_members=True) for g in groups],
                "campaigns": [
                    c.to_dict(include_html=False, include_recipients=False)
                    for c in campaigns
                ],
                "automation_steps": [s.to_dict(include_html=True) for s in steps],
                "automation_queue": [q.to_dict() for q in queue_items],
            }
        ),
        200,
    )


@marketing_bp.route("/admin/marketing/contacts", methods=["GET"])
@admin_required
def admin_marketing_contacts():
    """
    Devuelve la lista combinada de contactos disponibles:
    - Leads captados en Recursos Gratuitos (MarketingLead)
    - Alumnos registrados (User role='user')
    """
    leads = MarketingLead.query.order_by(MarketingLead.created_at.desc()).all()
    students = (
        User.query.filter_by(role="user")
        .order_by(User.created_at.desc())
        .all()
    )

    contacts_by_email: dict[str, dict] = {}
    for l in leads:
        em = (l.email or "").strip().lower()
        if not em:
            continue
        contacts_by_email[em] = {
            "email": em,
            "name": l.name or "",
            "source_type": "lead",
            "source_label": f"Lead · {l.source_resource_title or 'Recurso Gratuito'}",
            "consent_marketing": bool(l.consent_marketing),
            "lead_id": l.id,
            "created_at": l.to_dict().get("created_at"),
        }

    for s in students:
        em = (s.email or "").strip().lower()
        if not em:
            continue
        if em in contacts_by_email:
            contacts_by_email[em]["is_also_student"] = True
            if not contacts_by_email[em]["name"] and s.name:
                contacts_by_email[em]["name"] = s.name
        else:
            contacts_by_email[em] = {
                "email": em,
                "name": s.name or "",
                "source_type": "student",
                "source_label": "Alumno/a del Programa",
                "consent_marketing": True,
                "user_id": s.id,
                "created_at": s.to_dict().get("created_at"),
            }

    return (
        jsonify(
            {
                "contacts": list(contacts_by_email.values()),
                "leads": [l.to_dict() for l in leads],
                "students": [s.to_dict() for s in students],
            }
        ),
        200,
    )


# ==============================================================================
# PLANTILLAS HTML, PREVIEW Y ENVÍO DE TEST
# ==============================================================================


@marketing_bp.route("/admin/marketing/templates", methods=["GET"])
@admin_required
def admin_list_templates():
    seed_marketing_defaults()
    templates = (
        MarketingTemplate.query.order_by(MarketingTemplate.updated_at.desc()).all()
    )
    return jsonify({"templates": [t.to_dict(include_html=True) for t in templates]}), 200


@marketing_bp.route("/admin/marketing/templates", methods=["POST"])
@admin_required
def admin_create_template():
    """
    Crea una nueva plantilla HTML a partir de un archivo `.html` subido
    (multipart/form-data) o bien un payload JSON.
    """
    html_content = ""
    original_filename = ""
    name = ""
    subject = ""
    preheader = ""
    category = "general"

    if request.files and "file" in request.files:
        uploaded = request.files["file"]
        original_filename = uploaded.filename or "plantilla.html"
        raw_bytes = uploaded.read()
        try:
            html_content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            html_content = raw_bytes.decode("latin-1", errors="replace")
        name = (request.form.get("name") or "").strip()
        subject = (request.form.get("subject") or "").strip()
        preheader = (request.form.get("preheader") or "").strip()
        category = (request.form.get("category") or "general").strip()
    else:
        data = request.get_json() or {}
        html_content = (data.get("html_content") or "").strip()
        original_filename = (data.get("original_filename") or "plantilla.html").strip()
        name = (data.get("name") or "").strip()
        subject = (data.get("subject") or "").strip()
        preheader = (data.get("preheader") or "").strip()
        category = (data.get("category") or "general").strip()

    if not html_content:
        return jsonify({"error": "El contenido HTML de la plantilla está vacío."}), 400

    extracted_title = extract_title_from_html(html_content)
    if not subject:
        subject = extracted_title or "Comunicación de Carla Viladot · Vuelve a Ti"
    if not name:
        name = (
            extracted_title
            or original_filename.replace(".html", "").replace("_", " ").capitalize()
            or "Nueva Plantilla HTML"
        )

    tpl = MarketingTemplate(
        name=name,
        subject=subject,
        preheader=preheader,
        category=category,
        original_filename=original_filename,
        html_content=html_content,
    )
    db.session.add(tpl)
    db.session.commit()

    return (
        jsonify(
            {
                "message": "Plantilla HTML guardada correctamente.",
                "template": tpl.to_dict(include_html=True),
            }
        ),
        201,
    )


@marketing_bp.route("/admin/marketing/templates/<int:tpl_id>", methods=["PUT"])
@admin_required
def admin_update_template(tpl_id: int):
    tpl = MarketingTemplate.query.get(tpl_id)
    if not tpl:
        return jsonify({"error": "Plantilla no encontrada."}), 404

    if request.files and "file" in request.files:
        uploaded = request.files["file"]
        tpl.original_filename = uploaded.filename or tpl.original_filename
        raw_bytes = uploaded.read()
        try:
            tpl.html_content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            tpl.html_content = raw_bytes.decode("latin-1", errors="replace")

        if request.form.get("name"):
            tpl.name = request.form.get("name").strip()
        if request.form.get("subject"):
            tpl.subject = request.form.get("subject").strip()
        elif not tpl.subject:
            tpl.subject = extract_title_from_html(tpl.html_content) or tpl.name
        if "preheader" in request.form:
            tpl.preheader = request.form.get("preheader").strip()
        if request.form.get("category"):
            tpl.category = request.form.get("category").strip()
    else:
        data = request.get_json() or {}
        if "name" in data and str(data["name"]).strip():
            tpl.name = str(data["name"]).strip()
        if "subject" in data and str(data["subject"]).strip():
            tpl.subject = str(data["subject"]).strip()
        if "preheader" in data:
            tpl.preheader = str(data.get("preheader") or "").strip()
        if "category" in data:
            tpl.category = str(data.get("category") or "general").strip()
        if "original_filename" in data and data["original_filename"]:
            tpl.original_filename = str(data["original_filename"]).strip()
        if "html_content" in data and str(data["html_content"]).strip():
            tpl.html_content = str(data["html_content"])

    # Si algún paso de automatización está vinculado a esta plantilla, sincronizar su HTML si se desea
    linked_steps = MarketingAutomationStep.query.filter_by(template_id=tpl.id).all()
    for st in linked_steps:
        st.html_content = tpl.html_content
        st.original_filename = tpl.original_filename

    db.session.commit()
    return (
        jsonify(
            {
                "message": "Plantilla actualizada correctamente.",
                "template": tpl.to_dict(include_html=True),
            }
        ),
        200,
    )


@marketing_bp.route("/admin/marketing/templates/<int:tpl_id>", methods=["DELETE"])
@admin_required
def admin_delete_template(tpl_id: int):
    tpl = MarketingTemplate.query.get(tpl_id)
    if not tpl:
        return jsonify({"error": "Plantilla no encontrada."}), 404

    db.session.delete(tpl)
    db.session.commit()
    return jsonify({"message": "Plantilla eliminada correctamente."}), 200


@marketing_bp.route("/admin/marketing/preview", methods=["POST"])
@admin_required
def admin_preview_template():
    """
    Genera la previsualización del HTML con reemplazo de variables ([Nombre], {{email}})
    y devuelve también la versión texto plano anti-spam y el asunto detectado.
    """
    data = request.get_json() or {}
    raw_html = data.get("html_content") or ""
    sample_name = (data.get("sample_name") or "María").strip()
    sample_email = (data.get("sample_email") or "maria@ejemplo.com").strip()

    rendered = render_marketing_html(
        raw_html=raw_html,
        recipient_name=sample_name,
        recipient_email=sample_email,
    )
    plain = html_to_plain_text(rendered)
    extracted_subject = extract_title_from_html(raw_html)

    return (
        jsonify(
            {
                "rendered_html": rendered,
                "plain_text": plain,
                "extracted_subject": extracted_subject,
            }
        ),
        200,
    )


@marketing_bp.route("/admin/marketing/send-test", methods=["POST"])
@admin_required
def admin_send_test_email():
    """
    Envía un correo de prueba real usando la plantilla HTML actual al email
    indicado por el administrador para comprobar cómo se ve en la bandeja de entrada.
    """
    data = request.get_json() or {}
    to_email = (data.get("to_email") or "").strip()
    subject = (data.get("subject") or "Correo de prueba · Vuelve a Ti").strip()
    html_content = data.get("html_content") or ""
    sample_name = (data.get("sample_name") or "Carla").strip()
    preheader = (data.get("preheader") or "").strip()

    if not to_email or "@" not in to_email:
        return jsonify({"error": "Introduce un correo electrónico de destino válido."}), 400
    if not html_content.strip():
        return jsonify({"error": "El contenido HTML del correo está vacío."}), 400

    test_subject = (
        subject if subject.startswith("[TEST]") else f"[TEST] {subject}"
    )
    ok, details = send_marketing_email(
        to_email=to_email,
        subject=test_subject,
        html_body=html_content,
        recipient_name=sample_name,
        tracking_token=None,
        api_base_url=resolve_api_base_url(),
        preheader=preheader,
        is_test=True,
    )

    if not ok:
        return jsonify({"error": details, "success": False}), 400

    return (
        jsonify(
            {
                "success": True,
                "message": f"Correo de prueba enviado correctamente a {to_email}.",
                "details": details,
            }
        ),
        200,
    )


# ==============================================================================
# GRUPOS DE CORREOS
# ==============================================================================


@marketing_bp.route("/admin/marketing/groups", methods=["GET"])
@admin_required
def admin_list_groups():
    groups = MarketingGroup.query.order_by(MarketingGroup.created_at.desc()).all()
    return jsonify({"groups": [g.to_dict(include_members=True) for g in groups]}), 200


@marketing_bp.route("/admin/marketing/groups", methods=["POST"])
@admin_required
def admin_create_group():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()
    color = (data.get("color") or "#4A6B5D").strip()
    members_input = data.get("members") or []

    if not name:
        return jsonify({"error": "El nombre del grupo es obligatorio."}), 400

    grp = MarketingGroup(name=name, description=description, color=color)
    db.session.add(grp)
    db.session.flush()

    added_emails = set()
    for item in members_input:
        if isinstance(item, dict):
            em = (item.get("email") or "").strip().lower()
            nm = (item.get("name") or "").strip()
            src = (item.get("source_type") or "manual").strip()
        else:
            em = str(item or "").strip().lower()
            nm = ""
            src = "manual"

        if em and "@" in em and em not in added_emails:
            added_emails.add(em)
            db.session.add(
                MarketingGroupMember(
                    group_id=grp.id, email=em, name=nm, source_type=src
                )
            )

    db.session.commit()
    return (
        jsonify(
            {
                "message": f"Grupo '{grp.name}' creado con {len(added_emails)} contactos.",
                "group": grp.to_dict(include_members=True),
            }
        ),
        201,
    )


@marketing_bp.route("/admin/marketing/groups/<int:group_id>", methods=["PUT"])
@admin_required
def admin_update_group(group_id: int):
    grp = MarketingGroup.query.get(group_id)
    if not grp:
        return jsonify({"error": "Grupo no encontrado."}), 404

    data = request.get_json() or {}
    if "name" in data and str(data["name"]).strip():
        grp.name = str(data["name"]).strip()
    if "description" in data:
        grp.description = str(data.get("description") or "").strip()
    if "color" in data and data["color"]:
        grp.color = str(data["color"]).strip()

    if "members" in data and isinstance(data["members"], list):
        # Sincronizar lista completa de miembros del grupo
        MarketingGroupMember.query.filter_by(group_id=grp.id).delete()
        added_emails = set()
        for item in data["members"]:
            if isinstance(item, dict):
                em = (item.get("email") or "").strip().lower()
                nm = (item.get("name") or "").strip()
                src = (item.get("source_type") or "manual").strip()
            else:
                em = str(item or "").strip().lower()
                nm = ""
                src = "manual"

            if em and "@" in em and em not in added_emails:
                added_emails.add(em)
                db.session.add(
                    MarketingGroupMember(
                        group_id=grp.id, email=em, name=nm, source_type=src
                    )
                )

    db.session.commit()
    return (
        jsonify(
            {
                "message": "Grupo actualizado correctamente.",
                "group": grp.to_dict(include_members=True),
            }
        ),
        200,
    )


@marketing_bp.route(
    "/admin/marketing/groups/<int:group_id>/members", methods=["POST"]
)
@admin_required
def admin_add_group_members(group_id: int):
    grp = MarketingGroup.query.get(group_id)
    if not grp:
        return jsonify({"error": "Grupo no encontrado."}), 404

    data = request.get_json() or {}
    members_input = data.get("members") or []
    raw_text = (data.get("raw_emails") or "").strip()

    existing_emails = {m.email.lower() for m in grp.members}
    added_count = 0

    if raw_text:
        for line in re.split(r"[\n,;]+", raw_text):
            part = line.strip()
            if not part:
                continue
            # Soporta "Nombre <email@dominio.com>" o solo "email@dominio.com"
            m = re.match(r"^([^<]+)<([^>]+)>$", part)
            if m:
                nm = m.group(1).strip()
                em = m.group(2).strip().lower()
            else:
                nm = ""
                em = part.lower()
            if em and "@" in em and em not in existing_emails:
                existing_emails.add(em)
                db.session.add(
                    MarketingGroupMember(
                        group_id=grp.id, email=em, name=nm, source_type="manual"
                    )
                )
                added_count += 1

    for item in members_input:
        if isinstance(item, dict):
            em = (item.get("email") or "").strip().lower()
            nm = (item.get("name") or "").strip()
            src = (item.get("source_type") or "manual").strip()
        else:
            em = str(item or "").strip().lower()
            nm = ""
            src = "manual"
        if em and "@" in em and em not in existing_emails:
            existing_emails.add(em)
            db.session.add(
                MarketingGroupMember(
                    group_id=grp.id, email=em, name=nm, source_type=src
                )
            )
            added_count += 1

    db.session.commit()
    return (
        jsonify(
            {
                "message": f"Se añadieron {added_count} contactos al grupo.",
                "group": grp.to_dict(include_members=True),
            }
        ),
        200,
    )


@marketing_bp.route(
    "/admin/marketing/groups/<int:group_id>/members/<int:member_id>",
    methods=["DELETE"],
)
@admin_required
def admin_remove_group_member(group_id: int, member_id: int):
    member = MarketingGroupMember.query.filter_by(
        id=member_id, group_id=group_id
    ).first()
    if not member:
        return jsonify({"error": "Miembro no encontrado en el grupo."}), 404

    db.session.delete(member)
    db.session.commit()
    grp = MarketingGroup.query.get(group_id)
    return (
        jsonify(
            {
                "message": "Contacto eliminado del grupo.",
                "group": grp.to_dict(include_members=True) if grp else None,
            }
        ),
        200,
    )


@marketing_bp.route("/admin/marketing/groups/<int:group_id>", methods=["DELETE"])
@admin_required
def admin_delete_group(group_id: int):
    grp = MarketingGroup.query.get(group_id)
    if not grp:
        return jsonify({"error": "Grupo no encontrado."}), 404

    db.session.delete(grp)
    db.session.commit()
    return jsonify({"message": "Grupo eliminado correctamente."}), 200


# ==============================================================================
# CAMPAÑAS PROGRAMADAS E INMEDIATAS + TRACKING DETALLADO POR DESTINATARIO
# ==============================================================================


@marketing_bp.route("/admin/marketing/campaigns", methods=["GET"])
@admin_required
def admin_list_campaigns():
    process_pending_marketing_jobs()
    campaigns = (
        MarketingCampaign.query.order_by(MarketingCampaign.created_at.desc()).all()
    )
    return (
        jsonify(
            {
                "campaigns": [
                    c.to_dict(include_html=False, include_recipients=False)
                    for c in campaigns
                ]
            }
        ),
        200,
    )


@marketing_bp.route("/admin/marketing/campaigns/<int:campaign_id>", methods=["GET"])
@admin_required
def admin_get_campaign_detail(campaign_id: int):
    process_pending_marketing_jobs()
    camp = MarketingCampaign.query.get(campaign_id)
    if not camp:
        return jsonify({"error": "Campaña no encontrada."}), 404

    return (
        jsonify(
            {
                "campaign": camp.to_dict(include_html=True, include_recipients=True)
            }
        ),
        200,
    )


@marketing_bp.route("/admin/marketing/campaigns", methods=["POST"])
@admin_required
def admin_create_campaign():
    """
    Crea una campaña de Email Marketing:
    - Puede enviarse al instante (`send_now: True`)
    - O programarse para un día y hora concretos (`scheduled_at: ISO-8601`)
    - O guardarse como borrador (`status: 'draft'`)
    """
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    subject = (data.get("subject") or "").strip()
    preheader = (data.get("preheader") or "").strip()
    template_id = data.get("template_id")
    html_content = (data.get("html_content") or "").strip()

    if not html_content and template_id:
        tpl = MarketingTemplate.query.get(int(template_id))
        if tpl:
            html_content = tpl.html_content
            if not subject:
                subject = tpl.subject
            if not preheader:
                preheader = tpl.preheader or ""

    if not subject:
        subject = extract_title_from_html(html_content) or "Novedades · Vuelve a Ti"
    if not name:
        name = subject

    if not html_content:
        return (
            jsonify({"error": "Debes seleccionar o subir una plantilla HTML para la campaña."}),
            400,
        )

    target_type = (data.get("target_type") or "leads").strip()
    target_group_ids = data.get("target_group_ids") or []
    target_emails = data.get("target_emails") or []

    audience = resolve_campaign_Audience(
        target_type=target_type,
        target_group_ids=target_group_ids,
        target_emails=target_emails,
    )

    if not audience:
        return (
            jsonify(
                {
                    "error": "No se encontraron destinatarios válidos con consentimiento activo para esta selección."
                }
            ),
            400,
        )

    send_now = bool(data.get("send_now", False))
    scheduled_dt = _parse_iso_datetime(data.get("scheduled_at"))

    now_utc = datetime.now(timezone.utc)
    if send_now:
        status = "scheduled"
        scheduled_dt = now_utc
    elif scheduled_dt:
        status = "scheduled"
    else:
        status = "draft"

    api_base = resolve_api_base_url()

    camp = MarketingCampaign(
        name=name,
        subject=subject,
        preheader=preheader,
        template_id=int(template_id) if str(template_id or "").isdigit() else None,
        html_content=html_content,
        target_type=target_type,
        target_groups_json=json.dumps(target_group_ids, ensure_ascii=False),
        target_emails_json=json.dumps(target_emails, ensure_ascii=False),
        status=status,
        scheduled_at=scheduled_dt,
        total_recipients=len(audience),
        api_base_url=api_base,
    )
    db.session.add(camp)
    db.session.flush()

    for person in audience:
        rec = MarketingCampaignRecipient(
            campaign_id=camp.id,
            email=person["email"],
            name=person.get("name") or "",
            tracking_token=generate_tracking_token(),
            status="pending",
        )
        db.session.add(rec)

    db.session.commit()

    if send_now or (scheduled_dt and scheduled_dt <= now_utc):
        # Si son pocos destinatarios (<= 5), procesamos sincrónicamente para respuesta inmediata;
        # si son más, lanzamos worker asíncrono para no bloquear la petición.
        if len(audience) <= 5:
            process_pending_marketing_jobs()
            db.session.refresh(camp)
        else:
            _trigger_async_worker(current_app._get_current_object())

    msg = (
        f"Campaña enviada/iniciada para {len(audience)} destinatarios."
        if send_now
        else f"Campaña programada correctamente para {len(audience)} destinatarios."
    )
    return (
        jsonify(
            {
                "message": msg,
                "campaign": camp.to_dict(include_html=False, include_recipients=True),
            }
        ),
        201,
    )


@marketing_bp.route(
    "/admin/marketing/campaigns/<int:campaign_id>/send-now", methods=["POST"]
)
@admin_required
def admin_send_campaign_now(campaign_id: int):
    camp = MarketingCampaign.query.get(campaign_id)
    if not camp:
        return jsonify({"error": "Campaña no encontrada."}), 404

    if camp.status == "sent":
        return jsonify({"error": "Esta campaña ya fue enviada."}), 400

    camp.status = "scheduled"
    camp.scheduled_at = datetime.now(timezone.utc)
    camp.api_base_url = resolve_api_base_url(camp.api_base_url)

    # Reactivar destinatarios fallidos o cancelados a pending si aún no se enviaron
    for r in camp.recipients:
        if r.status in ("failed", "cancelled"):
            r.status = "pending"
            r.error_message = None

    db.session.commit()

    if camp.total_recipients <= 5:
        process_pending_marketing_jobs()
        db.session.refresh(camp)
    else:
        _trigger_async_worker(current_app._get_current_object())

    return (
        jsonify(
            {
                "message": "Envío de campaña iniciado.",
                "campaign": camp.to_dict(include_html=False, include_recipients=True),
            }
        ),
        200,
    )


@marketing_bp.route(
    "/admin/marketing/campaigns/<int:campaign_id>/cancel", methods=["POST"]
)
@admin_required
def admin_cancel_campaign(campaign_id: int):
    camp = MarketingCampaign.query.get(campaign_id)
    if not camp:
        return jsonify({"error": "Campaña no encontrada."}), 404

    if camp.status not in ("scheduled", "draft", "sending"):
        return jsonify({"error": "Solo se pueden cancelar campañas programadas o en curso."}), 400

    camp.status = "cancelled"
    db.session.commit()
    return (
        jsonify(
            {
                "message": "Campaña cancelada correctamente.",
                "campaign": camp.to_dict(include_html=False, include_recipients=False),
            }
        ),
        200,
    )


@marketing_bp.route("/admin/marketing/campaigns/<int:campaign_id>", methods=["DELETE"])
@admin_required
def admin_delete_campaign(campaign_id: int):
    camp = MarketingCampaign.query.get(campaign_id)
    if not camp:
        return jsonify({"error": "Campaña no encontrada."}), 404

    db.session.delete(camp)
    db.session.commit()
    return jsonify({"message": "Campaña eliminada correctamente."}), 200


# ==============================================================================
# SECUENCIA AUTOMÁTICA NUEVOS LEADS (24H / 48H / 72H)
# ==============================================================================


@marketing_bp.route("/admin/marketing/automation", methods=["GET"])
@admin_required
def admin_get_automation():
    seed_marketing_defaults()
    process_pending_marketing_jobs()

    steps = (
        MarketingAutomationStep.query.order_by(
            MarketingAutomationStep.step_order.asc()
        ).all()
    )
    queue_items = (
        MarketingAutomationQueue.query.order_by(
            MarketingAutomationQueue.scheduled_for.desc()
        )
        .limit(250)
        .all()
    )
    return (
        jsonify(
            {
                "steps": [s.to_dict(include_html=True) for s in steps],
                "queue": [q.to_dict() for q in queue_items],
            }
        ),
        200,
    )


@marketing_bp.route(
    "/admin/marketing/automation/steps/<int:step_id>", methods=["PUT"]
)
@admin_required
def admin_update_automation_step(step_id: int):
    """
    Permite actualizar cualquiera de los 3 correos de la secuencia (24h, 48h, 72h):
    - Subir un nuevo archivo `.html` directamente o elegir una plantilla existente
    - Cambiar las horas de espera (ej. 24, 48, 72)
    - Cambiar el asunto o preheader
    - Activar o pausar ese correo de la secuencia
    """
    step = MarketingAutomationStep.query.get(step_id)
    if not step:
        return jsonify({"error": "Paso de automatización no encontrado."}), 404

    if request.files and "file" in request.files:
        uploaded = request.files["file"]
        fname = uploaded.filename or f"{step.delay_hours}h.html"
        raw_bytes = uploaded.read()
        try:
            html_str = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            html_str = raw_bytes.decode("latin-1", errors="replace")

        step.html_content = html_str
        step.original_filename = fname
        extracted = extract_title_from_html(html_str)
        if request.form.get("subject"):
            step.subject = request.form.get("subject").strip()
        elif extracted:
            step.subject = extracted

        if request.form.get("title"):
            step.title = request.form.get("title").strip()
        if "preheader" in request.form:
            step.preheader = request.form.get("preheader").strip()
        if request.form.get("delay_hours") and str(request.form.get("delay_hours")).isdigit():
            step.delay_hours = max(1, int(request.form.get("delay_hours")))
        if "is_active" in request.form:
            step.is_active = request.form.get("is_active").lower() in ("true", "1", "yes")

        # Actualizar también la plantilla vinculada si existe
        if step.template_id:
            tpl = MarketingTemplate.query.get(step.template_id)
            if tpl:
                tpl.html_content = html_str
                tpl.original_filename = fname
                tpl.subject = step.subject
    else:
        data = request.get_json() or {}
        if "title" in data and str(data["title"]).strip():
            step.title = str(data["title"]).strip()
        if "subject" in data and str(data["subject"]).strip():
            step.subject = str(data["subject"]).strip()
        if "preheader" in data:
            step.preheader = str(data.get("preheader") or "").strip()
        if "delay_hours" in data and str(data["delay_hours"]).isdigit():
            step.delay_hours = max(1, int(data["delay_hours"]))
        if "is_active" in data:
            step.is_active = bool(data["is_active"])
        if "template_id" in data and data["template_id"]:
            tpl = MarketingTemplate.query.get(int(data["template_id"]))
            if tpl:
                step.template_id = tpl.id
                step.html_content = tpl.html_content
                step.original_filename = tpl.original_filename
                if not data.get("subject"):
                    step.subject = tpl.subject
        if "html_content" in data and str(data["html_content"]).strip():
            step.html_content = str(data["html_content"])
        if "original_filename" in data and data["original_filename"]:
            step.original_filename = str(data["original_filename"]).strip()

    db.session.commit()
    return (
        jsonify(
            {
                "message": f"Configuración de '{step.title}' guardada correctamente.",
                "step": step.to_dict(include_html=True),
            }
        ),
        200,
    )


@marketing_bp.route(
    "/admin/marketing/automation/enqueue-lead", methods=["POST"]
)
@admin_required
def admin_manual_enqueue_lead():
    """
    Permite al admin encolar manualmente en la secuencia 24h/48h/72h a un lead específico
    o a todos los leads existentes que aún no hayan entrado en la secuencia.
    """
    data = request.get_json() or {}
    lead_id = data.get("lead_id")
    enqueue_all = bool(data.get("enqueue_all", False))
    api_base = resolve_api_base_url()

    total_enqueued = 0
    if enqueue_all:
        leads = MarketingLead.query.filter_by(consent_marketing=True).all()
        for l in leads:
            total_enqueued += enqueue_new_lead_automation(
                lead=l, api_base_url=api_base
            )
    elif lead_id:
        lead = MarketingLead.query.get(int(lead_id))
        if not lead:
            return jsonify({"error": "Lead no encontrado."}), 404
        total_enqueued = enqueue_new_lead_automation(
            lead=lead, api_base_url=api_base
        )
    else:
        return jsonify({"error": "Indica un lead_id o enqueue_all=True."}), 400

    queue_items = (
        MarketingAutomationQueue.query.order_by(
            MarketingAutomationQueue.scheduled_for.desc()
        )
        .limit(250)
        .all()
    )
    return (
        jsonify(
            {
                "message": f"Se programaron {total_enqueued} correos en la secuencia automática.",
                "enqueued_count": total_enqueued,
                "queue": [q.to_dict() for q in queue_items],
            }
        ),
        200,
    )


@marketing_bp.route(
    "/admin/marketing/automation/queue/<int:queue_id>/send-now",
    methods=["POST"],
)
@admin_required
def admin_send_automation_queue_item_now(queue_id: int):
    """
    Adelanta y envía inmediatamente un correo de la cola 24h/48h/72h sin esperar
    a que se cumpla el plazo programado.
    """
    item = MarketingAutomationQueue.query.get(queue_id)
    if not item:
        return jsonify({"error": "Elemento de la cola no encontrado."}), 404

    step = MarketingAutomationStep.query.get(item.step_id)
    if not step:
        return jsonify({"error": "Paso de automatización no encontrado."}), 404

    ok, log_msg = send_marketing_email(
        to_email=item.email,
        subject=step.subject,
        html_body=step.html_content,
        recipient_name=item.name or "",
        tracking_token=item.tracking_token,
        api_base_url=resolve_api_base_url(item.api_base_url),
        preheader=step.preheader or "",
        is_test=False,
    )

    if ok:
        item.status = "sent"
        item.sent_at = datetime.now(timezone.utc)
        item.error_message = None
        step.total_sent = int(step.total_sent or 0) + 1
        db.session.commit()
        return (
            jsonify(
                {
                    "message": f"Correo '{step.title}' enviado inmediatamente a {item.email}.",
                    "item": item.to_dict(),
                }
            ),
            200,
        )
    else:
        item.status = "failed"
        item.error_message = log_msg
        db.session.commit()
        return jsonify({"error": log_msg, "item": item.to_dict()}), 400


@marketing_bp.route(
    "/admin/marketing/automation/queue/<int:queue_id>/cancel",
    methods=["POST"],
)
@admin_required
def admin_cancel_automation_queue_item(queue_id: int):
    item = MarketingAutomationQueue.query.get(queue_id)
    if not item:
        return jsonify({"error": "Elemento de la cola no encontrado."}), 404

    if item.status != "pending":
        return jsonify({"error": "Solo se pueden cancelar envíos pendientes."}), 400

    item.status = "cancelled"
    db.session.commit()
    return (
        jsonify(
            {
                "message": "Envío programado cancelado.",
                "item": item.to_dict(),
            }
        ),
        200,
    )


@marketing_bp.route("/admin/marketing/process-queue", methods=["POST"])
@admin_required
def admin_trigger_process_queue():
    summary = process_pending_marketing_jobs()
    return (
        jsonify(
            {
                "message": "Cola de envíos revisada y procesada.",
                "summary": summary,
            }
        ),
        200,
    )
