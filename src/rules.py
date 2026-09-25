from datetime import date, datetime

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _today():
    return date.today().isoformat()


def _parse_date(value, field):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        raise ValidationError("invalid date for %s: %s" % (field, value))


def _validate_dataset(actor, data, lookup):
    if len(data.get("access_policy", "")) < 3:
        raise ValidationError("access_policy is required")


def _validate_application(actor, data, lookup):
    dataset = _find_one(lookup, "dataset", "id", data.get("dataset_id"))
    if not dataset:
        raise ValidationError("dataset does not exist")
    if not data.get("purpose", "").strip():
        raise ValidationError("purpose is required")


def _validate_grant_create(actor, data, lookup):
    application = _find_one(lookup, "application", "id", data.get("application_id"))
    if not application:
        raise ValidationError("application does not exist")
    _ensure_consistent_with_application(application, data)


def _validate_approve(actor, entity, data, lookup):
    approvals = data.get("approvals") or []
    if len(set(approvals)) < 3:
        raise ValidationError("at least three distinct committee approvals are required")
    if data.get("conflict_of_interest"):
        raise PermissionDenied("conflicted reviewer cannot approve access")
    return {"original_expires_at": data.get("expires_at")}


def _validate_reapprove(actor, entity, data, lookup):
    approvals = data.get("approvals") or []
    if len(set(approvals)) < 3:
        raise ValidationError("at least three distinct committee approvals are required")
    if data.get("conflict_of_interest"):
        raise PermissionDenied("conflicted reviewer cannot approve access")
    current_cutoff = (entity.get("data") or {}).get("expires_at")
    new_cutoff = _parse_date(data.get("expires_at"), "expires_at")
    if current_cutoff and new_cutoff <= _parse_date(current_cutoff, "expires_at"):
        raise ValidationError("re-approval must extend the approval cutoff")
    return {"reapproved_by": actor.user_id, "reapproved_at": _today()}


def _validate_grant_activate(actor, entity, data, lookup):
    grant = entity.get("data") or {}
    starts_at = _parse_date(data.get("starts_at"), "starts_at")
    expires_at = _parse_date(data.get("expires_at"), "expires_at")
    if expires_at < starts_at:
        raise ValidationError("grant expiry must be after start")
    application = _find_one(lookup, "application", "id", grant.get("application_id"))
    if not application:
        raise ValidationError("linked application does not exist")
    if application["status"] != "approved":
        raise InvalidTransition(
            "application must be approved before activation (status: %s)"
            % application["status"]
        )
    _ensure_consistent_with_application(application, grant)
    cutoff = (application.get("data") or {}).get("expires_at")
    if cutoff and expires_at > _parse_date(cutoff, "expires_at"):
        raise ValidationError("grant expiry exceeds the approved cutoff")
    for sibling in lookup("grant", "application_id", grant.get("application_id")) or []:
        if sibling["id"] != entity["id"] and sibling["status"] == "active":
            raise ConflictError(
                "application already has an active grant: " + sibling["id"]
            )
    return {"activated_by": actor.user_id}


def _validate_grant_renew(actor, entity, data, lookup):
    grant = entity.get("data") or {}
    today = date.today()
    current_expiry = _parse_date(grant.get("expires_at"), "expires_at")
    if current_expiry < today:
        raise InvalidTransition("grant already expired and cannot be renewed")
    new_expiry = _parse_date(data.get("new_expires_at"), "new_expires_at")
    if new_expiry <= current_expiry:
        raise ValidationError("new expiry must be later than the current expiry")
    application = _find_one(lookup, "application", "id", grant.get("application_id"))
    if not application:
        raise ValidationError("linked application does not exist")
    app_data = application.get("data") or {}
    cutoff = app_data.get("expires_at")
    if cutoff and new_expiry > _parse_date(cutoff, "expires_at"):
        raise ValidationError(
            "renewal exceeds the approved cutoff %s; "
            "the excess requires committee re-approval" % cutoff
        )
    extra = {
        "expires_at": data.get("new_expires_at"),
        "renewed_by": actor.user_id,
        "renewed_at": _today(),
    }
    original_cutoff = app_data.get("original_expires_at")
    if original_cutoff and new_expiry > _parse_date(original_cutoff, "original_expires_at"):
        extra["beyond_original_approval"] = True
    return extra


def _validate_grant_freeze(actor, entity, data, lookup):
    grant = entity.get("data") or {}
    application = _find_one(lookup, "application", "id", grant.get("application_id"))
    if not application or application["status"] != "suspended":
        raise InvalidTransition("grant can only be frozen while its application is suspended")
    return {"frozen_by": actor.user_id, "frozen_at": _today()}


def _validate_grant_unfreeze(actor, entity, data, lookup):
    grant = entity.get("data") or {}
    application = _find_one(lookup, "application", "id", grant.get("application_id"))
    if not application or application["status"] != "approved":
        raise InvalidTransition("grant can only be unfrozen after its application resumes")
    if not valid_grant_window(grant.get("expires_at"), _today()):
        raise InvalidTransition("grant already expired and cannot be unfrozen")
    return {"unfrozen_by": actor.user_id, "unfrozen_at": _today()}


def _ensure_consistent_with_application(application, grant_data):
    app_data = application.get("data") or {}
    if grant_data.get("recipient") != app_data.get("applicant_id"):
        raise ValidationError("grant recipient must match the application applicant")
    if grant_data.get("dataset_id") != app_data.get("dataset_id"):
        raise ValidationError("grant dataset must match the application dataset")
    purpose = str(grant_data.get("purpose") or "").strip()
    if purpose != str(app_data.get("purpose") or "").strip():
        raise ValidationError("grant purpose must match the application purpose")


def valid_grant_window(expires_at, as_of):
    return str(expires_at) >= str(as_of)


CUSTOM_CREATE = {
    "dataset": _validate_dataset,
    "application": _validate_application,
    "grant": _validate_grant_create,
}
CUSTOM_TRANSITIONS = {
    ("application", "approve"): _validate_approve,
    ("application", "reapprove"): _validate_reapprove,
    ("grant", "activate"): _validate_grant_activate,
    ("grant", "renew"): _validate_grant_renew,
    ("grant", "freeze"): _validate_grant_freeze,
    ("grant", "unfreeze"): _validate_grant_unfreeze,
}


class RuleEngine:
    ALIASES = {"datasets": "dataset", "applications": "application", "grants": "grant"}
    INITIAL_STATUS = {"dataset": "registered", "application": "draft", "grant": "issued"}
    TRANSITIONS = {
        "dataset": {
            "restrict": (("registered",), "restricted"),
            "publish": (("restricted",), "published"),
        },
        "application": {
            "submit": (("draft",), "submitted"),
            "review": (("submitted",), "under_review"),
            "approve": (("under_review",), "approved"),
            "reapprove": (("approved",), "approved"),
            "reject": (("under_review",), "rejected"),
            "withdraw": (("submitted", "under_review"), "withdrawn"),
            "suspend": (("approved",), "suspended"),
            "resume": (("suspended",), "approved"),
        },
        "grant": {
            "activate": (("issued",), "active"),
            "renew": (("active",), "active"),
            "freeze": (("active",), "frozen"),
            "unfreeze": (("frozen",), "active"),
            "revoke": (("active",), "revoked"),
            "expire": (("active", "frozen"), "expired"),
        },
    }
    CREATE_REQUIRED = {
        "dataset": ("name", "access_policy"),
        "application": ("dataset_id", "applicant_id", "purpose"),
        "grant": ("application_id", "dataset_id", "recipient", "purpose"),
    }
    ACTION_REQUIRED = {
        ("dataset", "restrict"): ("reason",),
        ("application", "review"): ("committee_id",),
        ("application", "approve"): ("approvals", "terms", "expires_at"),
        ("application", "reapprove"): ("approvals", "expires_at"),
        ("application", "reject"): ("reason",),
        ("application", "withdraw"): ("reason",),
        ("application", "suspend"): ("reason",),
        ("grant", "activate"): ("starts_at", "expires_at"),
        ("grant", "renew"): ("new_expires_at",),
        ("grant", "revoke"): ("reason",),
        ("grant", "expire"): ("expired_at",),
    }
    CREATE_ROLES = {
        "dataset": ("admin", "committee"),
        "application": ("admin", "applicant"),
        "grant": ("admin", "committee"),
    }
    ROLE_ACTIONS = {
        "restrict": ("admin", "committee"),
        "publish": ("admin", "committee"),
        "submit": ("admin", "applicant"),
        "review": ("admin", "committee"),
        "approve": ("admin", "committee"),
        "reapprove": ("admin", "committee"),
        "reject": ("admin", "committee"),
        "withdraw": ("admin", "applicant"),
        "suspend": ("admin", "committee"),
        "resume": ("admin", "committee"),
        "activate": ("admin", "committee"),
        "renew": ("admin", "committee"),
        "freeze": ("admin", "committee"),
        "unfreeze": ("admin", "committee"),
        "revoke": ("admin", "committee"),
        "expire": ("admin", "committee"),
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
