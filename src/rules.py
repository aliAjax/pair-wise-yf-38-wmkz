from datetime import datetime, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# 处于这些状态的凭证视为“有效凭证”，同一申请同一时间只允许一张
LIVE_GRANT_STATUSES = ("active", "frozen", "renewal_pending")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _validate_dataset(actor, data, lookup):
    if len(data.get("access_policy", "")) < 3:
        raise ValidationError("access_policy is required")


def _validate_application(actor, data, lookup):
    dataset = _find_one(lookup, "dataset", "id", data.get("dataset_id"))
    if not dataset:
        raise ValidationError("dataset does not exist")
    if not data.get("purpose", "").strip():
        raise ValidationError("purpose is required")


def _validate_grant(actor, data, lookup):
    application = _find_one(lookup, "application", "id", data.get("application_id"))
    if not application:
        raise ValidationError("application does not exist")


def _validate_approve(actor, entity, data, lookup):
    approvals = data.get("approvals") or []
    if len(set(approvals)) < 3:
        raise ValidationError("at least three distinct committee approvals are required")
    if data.get("conflict_of_interest"):
        raise PermissionDenied("conflicted reviewer cannot approve access")


def valid_grant_window(expires_at, as_of):
    return str(expires_at) >= str(as_of)


def _validate_grant_activate(actor, entity, data, lookup):
    if data.get("expires_at") < data.get("starts_at"):
        raise ValidationError("grant expiry must be after start")
    application = _find_one(lookup, "application", "id", entity["data"].get("application_id"))
    if not application:
        raise ValidationError("application does not exist")
    if application["status"] != "approved":
        raise InvalidTransition("application must be approved before activation")
    application_data = application["data"]
    grant_data = entity["data"]
    for grant_field, application_field, message in (
        ("recipient", "applicant_id", "recipient does not match application applicant"),
        ("dataset_id", "dataset_id", "dataset does not match application"),
        ("purpose", "purpose", "purpose does not match application"),
    ):
        if grant_data.get(grant_field) != application_data.get(application_field):
            raise ValidationError(message)
    if not valid_grant_window(application_data.get("expires_at", ""), data.get("expires_at")):
        raise ValidationError("grant expiry exceeds application approval deadline")
    for other in lookup("grant", "application_id", application["id"]) or []:
        if other["id"] != entity["id"] and other["status"] in LIVE_GRANT_STATUSES:
            raise ConflictError("application already has a live grant: " + other["id"])
    return {"activated_by": actor.user_id}


def _validate_grant_renew(actor, entity, data, lookup):
    current = entity["data"].get("expires_at")
    if not current or not valid_grant_window(current, _today()):
        raise InvalidTransition("grant already expired; renew is not allowed")
    new_expiry = str(data.get("expires_at"))
    if new_expiry <= str(current):
        raise ValidationError("new expiry must be later than current expiry")
    application = _find_one(lookup, "application", "id", entity["data"].get("application_id"))
    if not application or application["status"] != "approved":
        raise InvalidTransition("application must be approved to renew the grant")
    record = {
        "from": current,
        "to": new_expiry,
        "requested_by": actor.user_id,
        "requested_at": _now(),
    }
    renewals = [dict(item) for item in entity["data"].get("renewals") or []]
    renewals.append(record)
    if valid_grant_window(application["data"].get("expires_at", ""), new_expiry):
        record["status"] = "approved"
        return {"expires_at": new_expiry, "renewals": renewals}, "active"
    # 超出原审批截止的部分挂起，等待委员会重新批准；批准前保留原期限
    record["status"] = "pending"
    return {"expires_at": current, "renewals": renewals}, "renewal_pending"


def _pending_renewal(renewals):
    for record in reversed(renewals):
        if record.get("status") == "pending":
            return record
    return None


def _validate_grant_approve_renewal(actor, entity, data, lookup):
    renewals = [dict(item) for item in entity["data"].get("renewals") or []]
    record = _pending_renewal(renewals)
    if not record:
        raise ValidationError("no pending renewal to approve")
    record["status"] = "approved"
    record["decided_by"] = actor.user_id
    record["decided_at"] = _now()
    return {"expires_at": record["to"], "renewals": renewals}


def _validate_grant_reject_renewal(actor, entity, data, lookup):
    renewals = [dict(item) for item in entity["data"].get("renewals") or []]
    record = _pending_renewal(renewals)
    if not record:
        raise ValidationError("no pending renewal to reject")
    record["status"] = "rejected"
    record["decided_by"] = actor.user_id
    record["decided_at"] = _now()
    record["reason"] = data.get("reason")
    return {"renewals": renewals}


def _validate_grant_freeze(actor, entity, data, lookup):
    events = list(entity["data"].get("freezes") or [])
    events.append(
        {
            "event": "frozen",
            "by": actor.user_id,
            "at": _now(),
            "reason": data.get("reason"),
        }
    )
    return {"freezes": events}


def _validate_grant_unfreeze(actor, entity, data, lookup):
    if not valid_grant_window(entity["data"].get("expires_at", ""), _today()):
        raise InvalidTransition("grant already expired; cannot restore")
    events = list(entity["data"].get("freezes") or [])
    events.append({"event": "unfrozen", "by": actor.user_id, "at": _now()})
    return {"freezes": events}


CUSTOM_CREATE = {
    'dataset': _validate_dataset,
    'application': _validate_application,
    'grant': _validate_grant,
}
CUSTOM_TRANSITIONS = {
    ('application', 'approve'): _validate_approve,
    ('grant', 'activate'): _validate_grant_activate,
    ('grant', 'renew'): _validate_grant_renew,
    ('grant', 'approve_renewal'): _validate_grant_approve_renewal,
    ('grant', 'reject_renewal'): _validate_grant_reject_renewal,
    ('grant', 'freeze'): _validate_grant_freeze,
    ('grant', 'unfreeze'): _validate_grant_unfreeze,
}


class RuleEngine:
    ALIASES = {'datasets': 'dataset', 'applications': 'application', 'grants': 'grant'}
    INITIAL_STATUS = {'dataset': 'registered', 'application': 'draft', 'grant': 'issued'}
    TRANSITIONS = {
        'dataset': {
            'restrict': (('registered',), 'restricted'),
            'publish': (('restricted',), 'published'),
        },
        'application': {
            'submit': (('draft',), 'submitted'),
            'review': (('submitted',), 'under_review'),
            'approve': (('under_review',), 'approved'),
            'reject': (('under_review',), 'rejected'),
            'withdraw': (('submitted', 'under_review'), 'withdrawn'),
            'suspend': (('approved',), 'suspended'),
            'resume': (('suspended',), 'approved'),
        },
        'grant': {
            'activate': (('issued',), 'active'),
            'renew': (('active',), 'active'),
            'approve_renewal': (('renewal_pending',), 'active'),
            'reject_renewal': (('renewal_pending',), 'active'),
            'freeze': (('active',), 'frozen'),
            'unfreeze': (('frozen',), 'active'),
            'revoke': (('active',), 'revoked'),
            'expire': (('active', 'frozen'), 'expired'),
        },
    }
    CREATE_REQUIRED = {
        'dataset': ('name', 'access_policy'),
        'application': ('dataset_id', 'applicant_id', 'purpose'),
        'grant': ('application_id', 'dataset_id', 'recipient', 'purpose'),
    }
    ACTION_REQUIRED = {
        ('dataset', 'restrict'): ('reason',),
        ('application', 'review'): ('committee_id',),
        ('application', 'approve'): ('approvals', 'terms', 'expires_at'),
        ('application', 'reject'): ('reason',),
        ('application', 'withdraw'): ('reason',),
        ('application', 'suspend'): ('reason',),
        ('grant', 'activate'): ('starts_at', 'expires_at'),
        ('grant', 'renew'): ('expires_at',),
        ('grant', 'reject_renewal'): ('reason',),
        ('grant', 'freeze'): ('reason',),
        ('grant', 'revoke'): ('reason',),
        ('grant', 'expire'): ('expired_at',),
    }
    CREATE_ROLES = {
        'dataset': ('admin', 'committee'),
        'application': ('admin', 'applicant'),
        'grant': ('admin', 'committee'),
    }
    ROLE_ACTIONS = {
        'restrict': ('admin', 'committee'),
        'publish': ('admin', 'committee'),
        'submit': ('admin', 'applicant'),
        'review': ('admin', 'committee'),
        'approve': ('admin', 'committee'),
        'reject': ('admin', 'committee'),
        'withdraw': ('admin', 'applicant'),
        'activate': ('admin', 'committee'),
        'revoke': ('admin', 'committee'),
        'expire': ('admin', 'committee'),
        ('application', 'suspend'): ('admin', 'committee'),
        ('application', 'resume'): ('admin', 'committee'),
        ('grant', 'renew'): ('admin', 'committee', 'applicant'),
        ('grant', 'approve_renewal'): ('admin', 'committee'),
        ('grant', 'reject_renewal'): ('admin', 'committee'),
        ('grant', 'freeze'): ('admin', 'committee'),
        ('grant', 'unfreeze'): ('admin', 'committee'),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        if isinstance(extra, tuple):
            # 自定义校验可以覆盖目标状态（如续期超出审批截止时挂起）
            extra, next_status = extra
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
