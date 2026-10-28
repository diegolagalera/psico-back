import base64
import json
import smtplib
import traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Tuple

from app.models import SystemConfig


def get_or_create_system_config() -> SystemConfig:
    from app.models import db

    config = SystemConfig.query.first()
    if not config:
        config = SystemConfig(
            smtp_host="smtp.gmail.com",
            smtp_port=587,
            smtp_user="",
            smtp_password="",
            use_gmail_api=False,
            gmail_token_json="",
        )
        db.session.add(config)
        db.session.commit()
    return config


def send_system_email(
    to_email: str,
    subject: str,
    html_body: str,
    override_config: dict | None = None,
) -> Tuple[bool, str]:
    """
    Envía un correo electrónico utilizando la configuración almacenada en `system_config`
    o bien `override_config` (útil al probar la conexión antes de guardar).
    Soporta tanto Gmail API OAuth2 (`use_gmail_api=True`) como SMTP estándar (`smtplib`).
    Retorna `(exito: bool, log_detallado: str)`.
    """
    config_obj = get_or_create_system_config()
    cfg = config_obj.to_dict()
    if override_config:
        cfg.update(override_config)

    use_gmail_api = bool(cfg.get("use_gmail_api"))
    logs = []

    if use_gmail_api:
        logs.append("[INFO] Modo seleccionado: Google Gmail API (OAuth 2.0)")
        token_raw = (cfg.get("gmail_token_json") or "").strip()
        if not token_raw:
            return (
                False,
                "\n".join(
                    logs
                    + [
                        "[ERROR] El campo 'gmail_token_json' está vacío.",
                        "[SOLUCIÓN] Pega el JSON con client_id, client_secret y refresh_token siguiendo la guía paso a paso.",
                    ]
                ),
            )

        try:
            token_data = json.loads(token_raw)
            logs.append("[OK] JSON de credenciales OAuth2 parseado correctamente.")
        except Exception as e:
            return (
                False,
                "\n".join(
                    logs
                    + [
                        f"[ERROR] El contenido de gmail_token_json no es un JSON válido: {str(e)}"
                    ]
                ),
            )

        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build

            # Soporta formato directo o anidado bajo "installed" / "web"
            client_info = token_data
            if "installed" in token_data and isinstance(token_data["installed"], dict):
                client_info = {**token_data["installed"], **token_data}
            elif "web" in token_data and isinstance(token_data["web"], dict):
                client_info = {**token_data["web"], **token_data}

            required_keys = ["client_id", "client_secret", "refresh_token"]
            missing = [k for k in required_keys if not client_info.get(k)]
            if missing:
                return (
                    False,
                    "\n".join(
                        logs
                        + [
                            f"[ERROR] Faltan claves obligatorias en el JSON de Gmail API: {', '.join(missing)}",
                            "[NOTA] Asegúrate de incluir client_id, client_secret y refresh_token.",
                        ]
                    ),
                )

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

            logs.append("[INFO] Refrescando Access Token con Google OAuth2...")
            creds.refresh(Request())
            logs.append("[OK] Access Token obtenido exitosamente.")

            service = build("gmail", "v1", credentials=creds)
            sender = cfg.get("smtp_user") or "me"

            mime_message = MIMEMultipart("alternative")
            mime_message["To"] = to_email
            mime_message["From"] = (
                f"Vuelve a Ti · Psicología <{sender}>"
                if "@" in sender
                else "Vuelve a Ti · Psicología"
            )
            mime_message["Subject"] = subject
            mime_message.attach(MIMEText(html_body, "html", "utf-8"))

            encoded_message = base64.urlsafe_b64encode(mime_message.as_bytes()).decode(
                "utf-8"
            )
            create_message = {"raw": encoded_message}

            logs.append(f"[INFO] Enviando mensaje vía Gmail API a '{to_email}'...")
            sent = (
                service.users()
                .messages()
                .send(userId="me", body=create_message)
                .execute()
            )
            msg_id = sent.get("id", "N/A")
            logs.append(
                f"[ÉXITO] Correo enviado correctamente mediante Gmail API. Message ID: {msg_id}"
            )
            return True, "\n".join(logs)

        except Exception as exc:
            logs.append(f"[ERROR GMAIL API] {str(exc)}")
            logs.append(traceback.format_exc(limit=2))
            return False, "\n".join(logs)

    # Modo SMTP Estándar
    smtp_host = (cfg.get("smtp_host") or "").strip()
    smtp_port = int(cfg.get("smtp_port") or 587)
    smtp_user = (cfg.get("smtp_user") or "").strip()
    smtp_password = cfg.get("smtp_password") or ""

    logs.append(f"[INFO] Modo seleccionado: Servidor SMTP ({smtp_host}:{smtp_port})")

    if not smtp_host or not smtp_user or not smtp_password:
        return (
            False,
            "\n".join(
                logs
                + [
                    "[ERROR] Faltan credenciales SMTP (Host, Usuario o Contraseña están vacíos).",
                    "[SOLUCIÓN] Completa todos los campos SMTP en el panel de configuración.",
                ]
            ),
        )

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Vuelve a Ti · Psicología <{smtp_user}>"
        msg["To"] = to_email
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        if smtp_port == 465:
            logs.append(f"[INFO] Iniciando conexión SMTP_SSL a {smtp_host}:{smtp_port}...")
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=12)
        else:
            logs.append(f"[INFO] Iniciando conexión SMTP a {smtp_host}:{smtp_port}...")
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=12)
            logs.append("[INFO] Ejecutando EHLO y STARTTLS...")
            server.ehlo()
            server.starttls()
            server.ehlo()

        logs.append(f"[INFO] Autenticando usuario '{smtp_user}'...")
        server.login(smtp_user, smtp_password)
        logs.append("[OK] Autenticación SMTP completada.")

        logs.append(f"[INFO] Enviando correo a '{to_email}'...")
        server.sendmail(smtp_user, [to_email], msg.as_string())
        server.quit()
        logs.append("[ÉXITO] Correo entregado satisfactoriamente al servidor SMTP.")
        return True, "\n".join(logs)

    except Exception as exc:
        logs.append(f"[ERROR SMTP] {str(exc)}")
        return False, "\n".join(logs)


def build_welcome_email_html(name: str, email: str, temp_password: str) -> str:
    return f"""
    <div style="font-family: 'Helvetica Neue', Arial, sans-serif; background-color: #FAF8F5; padding: 36px; color: #2C3E35;">
      <div style="max-width: 580px; margin: 0 auto; background: #FFFFFF; border-radius: 16px; padding: 36px; border: 1px solid #E6E1DA;">
        <p style="text-transform: uppercase; letter-spacing: 2px; font-size: 12px; color: #4A6B5D; font-weight: bold; margin: 0 0 8px 0;">
          PROGRAMA VUELVE A TI · 3 SEMANAS
        </p>
        <h1 style="font-size: 24px; color: #2C3E35; margin: 0 0 16px 0;">
          Bienvenida/o a tu espacio de reconstrucción, {name}
        </h1>
        <p style="font-size: 15px; line-height: 1.6; color: #4A5B53;">
          Tu cuenta de acceso al portal de alumnos ha sido activada. Desde hoy comienzas un camino acompañado, sin juicios y a tu propio ritmo para recuperar tu calma y tu identidad.
        </p>
        <div style="background-color: #FAF8F5; border-left: 4px solid #D9826C; padding: 18px; border-radius: 8px; margin: 24px 0;">
          <p style="margin: 0 0 8px 0; font-size: 14px;"><strong>Tus credenciales de acceso:</strong></p>
          <p style="margin: 4px 0; font-size: 14px;"><strong>Email:</strong> {email}</p>
          <p style="margin: 4px 0; font-size: 14px;"><strong>Contraseña temporal:</strong> <code style="background: #EFECE6; padding: 3px 8px; border-radius: 4px;">{temp_password}</code></p>
        </div>
        <p style="font-size: 14px; line-height: 1.6; color: #4A5B53;">
          Accede desde el botón <strong>"Acceso Alumnos"</strong> en nuestra web principal.
        </p>
        <hr style="border: none; border-top: 1px solid #EFECE6; margin: 28px 0;" />
        <p style="font-size: 12px; color: #7C8B84; margin: 0;">
          Con cariño,<br />
          <strong>Carla Viladot · Psicóloga Especialista en Rupturas Amorosas</strong>
        </p>
      </div>
    </div>
    """


def build_contact_notification_html(
    name: str, email: str, phone: str, message: str
) -> str:
    return f"""
    <div style="font-family: 'Helvetica Neue', Arial, sans-serif; background-color: #FAF8F5; padding: 32px; color: #2C3E35;">
      <div style="max-width: 580px; margin: 0 auto; background: #FFFFFF; border-radius: 16px; padding: 32px; border: 1px solid #E6E1DA;">
        <span style="background: #D9826C; color: #FFFFFF; padding: 4px 10px; border-radius: 99px; font-size: 11px; text-transform: uppercase; letter-spacing: 1px;">
          Nueva Solicitud de Sesión / Contacto
        </span>
        <h2 style="margin: 16px 0 12px 0; color: #2C3E35;">Has recibido un mensaje desde la web "Vuelve a Ti"</h2>
        <p style="font-size: 14px; margin: 6px 0;"><strong>Nombre:</strong> {name}</p>
        <p style="font-size: 14px; margin: 6px 0;"><strong>Email:</strong> <a href="mailto:{email}" style="color: #4A6B5D;">{email}</a></p>
        <p style="font-size: 14px; margin: 6px 0;"><strong>Teléfono:</strong> {phone or 'No especificado'}</p>
        <div style="margin-top: 16px; padding: 16px; background: #FAF8F5; border-radius: 12px; border: 1px solid #EFECE6;">
          <p style="margin: 0 0 6px 0; font-size: 12px; color: #4A6B5D; text-transform: uppercase; font-weight: bold;">Situación actual / Mensaje:</p>
          <p style="margin: 0; font-size: 14px; line-height: 1.6; white-space: pre-wrap;">{message}</p>
        </div>
      </div>
    </div>
    """
