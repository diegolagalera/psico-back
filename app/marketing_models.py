from datetime import datetime, timezone
import json
from app.models import db, _iso_utc


class MarketingTemplate(db.Model):
    """
    Plantilla HTML subida o editada por el administrador para campañas
    o para la secuencia automática de bienvenida (24h / 48h / 72h).
    """

    __tablename__ = "marketing_templates"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(200), nullable=False)
    subject = db.Column(db.String(255), nullable=False, default="")
    preheader = db.Column(db.String(255), nullable=True, default="")
    category = db.Column(
        db.String(60), nullable=False, default="general"
    )  # 'general' | 'automation' | 'campaign'
    original_filename = db.Column(db.String(255), nullable=True, default="")
    html_content = db.Column(db.Text(length=16777215), nullable=False, default="")
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

    def to_dict(self, include_html: bool = True) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "subject": self.subject or "",
            "preheader": self.preheader or "",
            "category": self.category or "general",
            "original_filename": self.original_filename or "",
            "created_at": _iso_utc(self.created_at),
            "updated_at": _iso_utc(self.updated_at),
        }
        if include_html:
            data["html_content"] = self.html_content or ""
        return data


class MarketingGroup(db.Model):
    """
    Grupo o segmento personalizado de correos electrónicos para envíos de marketing.
    """

    __tablename__ = "marketing_groups"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(180), nullable=False)
    description = db.Column(db.String(350), nullable=True, default="")
    color = db.Column(db.String(30), nullable=True, default="#4A6B5D")
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

    members = db.relationship(
        "MarketingGroupMember",
        backref="group",
        cascade="all, delete-orphan",
        order_by="MarketingGroupMember.created_at.desc()",
    )

    def to_dict(self, include_members: bool = True) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
            "color": self.color or "#4A6B5D",
            "members_count": len(self.members),
            "created_at": _iso_utc(self.created_at),
            "updated_at": _iso_utc(self.updated_at),
        }
        if include_members:
            data["members"] = [m.to_dict() for m in self.members]
        return data


class MarketingGroupMember(db.Model):
    """
    Miembro (correo electrónico) perteneciente a un grupo de marketing.
    """

    __tablename__ = "marketing_group_members"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    group_id = db.Column(
        db.Integer,
        db.ForeignKey("marketing_groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email = db.Column(db.String(180), nullable=False, index=True)
    name = db.Column(db.String(140), nullable=True, default="")
    source_type = db.Column(
        db.String(40), nullable=False, default="manual"
    )  # 'lead' | 'student' | 'manual'
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        db.UniqueConstraint("group_id", "email", name="uq_marketing_group_email"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "group_id": self.group_id,
            "email": self.email,
            "name": self.name or "",
            "source_type": self.source_type or "manual",
            "created_at": _iso_utc(self.created_at),
        }


class MarketingCampaign(db.Model):
    """
    Campaña de Email Marketing (inmediata o programada para fecha y hora).
    """

    __tablename__ = "marketing_campaigns"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(220), nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    preheader = db.Column(db.String(255), nullable=True, default="")
    template_id = db.Column(
        db.Integer,
        db.ForeignKey("marketing_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    html_content = db.Column(db.Text(length=16777215), nullable=False, default="")

    # Selección de audiencia: 'all' | 'leads' | 'students' | 'groups' | 'custom'
    target_type = db.Column(db.String(40), nullable=False, default="leads")
    target_groups_json = db.Column(db.Text, nullable=True, default="[]")
    target_emails_json = db.Column(db.Text, nullable=True, default="[]")

    # Estado: 'draft' | 'scheduled' | 'sending' | 'sent' | 'cancelled' | 'failed'
    status = db.Column(db.String(30), nullable=False, default="draft", index=True)
    scheduled_at = db.Column(db.DateTime, nullable=True, index=True)  # UTC
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    total_recipients = db.Column(db.Integer, nullable=False, default=0)
    sent_count = db.Column(db.Integer, nullable=False, default=0)
    failed_count = db.Column(db.Integer, nullable=False, default=0)
    opened_count = db.Column(db.Integer, nullable=False, default=0)

    api_base_url = db.Column(db.String(255), nullable=True, default="")

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

    recipients = db.relationship(
        "MarketingCampaignRecipient",
        backref="campaign",
        cascade="all, delete-orphan",
        order_by="MarketingCampaignRecipient.id.asc()",
    )

    def to_dict(
        self, include_html: bool = False, include_recipients: bool = False
    ) -> dict:
        try:
            group_ids = (
                json.loads(self.target_groups_json)
                if self.target_groups_json
                else []
            )
        except Exception:
            group_ids = []

        try:
            custom_emails = (
                json.loads(self.target_emails_json)
                if self.target_emails_json
                else []
            )
        except Exception:
            custom_emails = []

        sent = int(self.sent_count or 0)
        opened = int(self.opened_count or 0)
        open_rate = round((opened / sent) * 100, 1) if sent > 0 else 0.0

        data = {
            "id": self.id,
            "name": self.name,
            "subject": self.subject,
            "preheader": self.preheader or "",
            "template_id": self.template_id,
            "target_type": self.target_type or "leads",
            "target_group_ids": group_ids,
            "target_emails": custom_emails,
            "status": self.status or "draft",
            "scheduled_at": _iso_utc(self.scheduled_at),
            "started_at": _iso_utc(self.started_at),
            "completed_at": _iso_utc(self.completed_at),
            "total_recipients": int(self.total_recipients or 0),
            "sent_count": sent,
            "failed_count": int(self.failed_count or 0),
            "opened_count": opened,
            "open_rate": open_rate,
            "created_at": _iso_utc(self.created_at),
            "updated_at": _iso_utc(self.updated_at),
        }
        if include_html:
            data["html_content"] = self.html_content or ""
        if include_recipients:
            data["recipients"] = [r.to_dict() for r in self.recipients]
        return data


class MarketingCampaignRecipient(db.Model):
    """
    Destinatario individual de una campaña con su token de seguimiento de apertura.
    """

    __tablename__ = "marketing_campaign_recipients"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    campaign_id = db.Column(
        db.Integer,
        db.ForeignKey("marketing_campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email = db.Column(db.String(180), nullable=False, index=True)
    name = db.Column(db.String(140), nullable=True, default="")
    tracking_token = db.Column(
        db.String(64), unique=True, nullable=False, index=True
    )
    # 'pending' | 'processing' | 'sent' | 'failed' | 'skipped_unsubscribed'
    status = db.Column(db.String(30), nullable=False, default="pending", index=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=True)
    last_opened_at = db.Column(db.DateTime, nullable=True)
    open_count = db.Column(db.Integer, nullable=False, default=0)
    user_agent = db.Column(db.String(300), nullable=True, default="")
    error_message = db.Column(db.Text, nullable=True)

    __table_args__ = (
        db.UniqueConstraint(
            "campaign_id", "email", name="uq_campaign_recipient_email"
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "campaign_id": self.campaign_id,
            "email": self.email,
            "name": self.name or "",
            "status": self.status,
            "sent_at": _iso_utc(self.sent_at),
            "is_opened": bool(self.opened_at is not None or (self.open_count or 0) > 0),
            "opened_at": _iso_utc(self.opened_at),
            "last_opened_at": _iso_utc(self.last_opened_at),
            "open_count": int(self.open_count or 0),
            "error_message": self.error_message or "",
        }


class MarketingAutomationStep(db.Model):
    """
    Paso de la secuencia automatizada para nuevos usuarios que dejan su correo
    por primera vez al descargar un recurso gratuito (24h, 48h, 72h).
    """

    __tablename__ = "marketing_automation_steps"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    step_order = db.Column(db.Integer, nullable=False, default=1)
    title = db.Column(db.String(200), nullable=False)
    delay_hours = db.Column(db.Integer, nullable=False, default=24)
    subject = db.Column(db.String(255), nullable=False)
    preheader = db.Column(db.String(255), nullable=True, default="")
    template_id = db.Column(
        db.Integer,
        db.ForeignKey("marketing_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    original_filename = db.Column(db.String(255), nullable=True, default="")
    html_content = db.Column(db.Text(length=16777215), nullable=False, default="")
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    total_sent = db.Column(db.Integer, nullable=False, default=0)
    total_opened = db.Column(db.Integer, nullable=False, default=0)

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

    def to_dict(self, include_html: bool = True) -> dict:
        sent = int(self.total_sent or 0)
        opened = int(self.total_opened or 0)
        open_rate = round((opened / sent) * 100, 1) if sent > 0 else 0.0
        data = {
            "id": self.id,
            "step_order": self.step_order,
            "title": self.title,
            "delay_hours": int(self.delay_hours or 24),
            "subject": self.subject,
            "preheader": self.preheader or "",
            "template_id": self.template_id,
            "original_filename": self.original_filename or "",
            "is_active": bool(self.is_active),
            "total_sent": sent,
            "total_opened": opened,
            "open_rate": open_rate,
            "created_at": _iso_utc(self.created_at),
            "updated_at": _iso_utc(self.updated_at),
        }
        if include_html:
            data["html_content"] = self.html_content or ""
        return data


class MarketingAutomationQueue(db.Model):
    """
    Cola de envíos programados de la secuencia de bienvenida (24h, 48h, 72h)
    para cada nuevo lead captado en recursos gratuitos.
    """

    __tablename__ = "marketing_automation_queue"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    step_id = db.Column(
        db.Integer,
        db.ForeignKey("marketing_automation_steps.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    lead_id = db.Column(
        db.Integer,
        db.ForeignKey("marketing_leads.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    email = db.Column(db.String(180), nullable=False, index=True)
    name = db.Column(db.String(140), nullable=True, default="")
    resource_title = db.Column(db.String(220), nullable=True, default="")
    delay_hours = db.Column(db.Integer, nullable=False, default=24)

    scheduled_for = db.Column(db.DateTime, nullable=False, index=True)  # UTC
    # 'pending' | 'processing' | 'sent' | 'failed' | 'cancelled' | 'skipped_unsubscribed'
    status = db.Column(db.String(30), nullable=False, default="pending", index=True)
    tracking_token = db.Column(
        db.String(64), unique=True, nullable=False, index=True
    )

    sent_at = db.Column(db.DateTime, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=True)
    last_opened_at = db.Column(db.DateTime, nullable=True)
    open_count = db.Column(db.Integer, nullable=False, default=0)
    user_agent = db.Column(db.String(300), nullable=True, default="")
    error_message = db.Column(db.Text, nullable=True)
    api_base_url = db.Column(db.String(255), nullable=True, default="")

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    step = db.relationship("MarketingAutomationStep", lazy="joined")

    __table_args__ = (
        db.UniqueConstraint("step_id", "email", name="uq_auto_step_email"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "step_id": self.step_id,
            "step_title": self.step.title if self.step else f"{self.delay_hours}h",
            "step_subject": self.step.subject if self.step else "",
            "lead_id": self.lead_id,
            "email": self.email,
            "name": self.name or "",
            "resource_title": self.resource_title or "",
            "delay_hours": int(self.delay_hours or 24),
            "scheduled_for": _iso_utc(self.scheduled_for),
            "status": self.status,
            "sent_at": _iso_utc(self.sent_at),
            "is_opened": bool(self.opened_at is not None or (self.open_count or 0) > 0),
            "opened_at": _iso_utc(self.opened_at),
            "last_opened_at": _iso_utc(self.last_opened_at),
            "open_count": int(self.open_count or 0),
            "error_message": self.error_message or "",
            "created_at": _iso_utc(self.created_at),
        }
