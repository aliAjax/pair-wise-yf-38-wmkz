from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine, valid_grant_window
from .repository import utcnow


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if entity["kind"] == "application":
            if action == "suspend":
                self._freeze_application_grants(actor, updated, patch.get("reason"))
            elif action == "resume":
                self._restore_application_grants(actor, updated)
        return updated

    def _freeze_application_grants(self, actor, application, reason):
        for grant in self.repository.list_entities(kind="grant", status="active"):
            if grant["data"].get("application_id") != application["id"]:
                continue
            _, patch = self.rules.validate_transition(
                actor, grant, "freeze", {"reason": reason or ""}, self._lookup
            )
            merged = dict(grant["data"])
            merged.update(patch)
            self.repository.update_entity(
                grant["id"], grant["version"], "frozen", merged
            )
            self.audit.record(
                grant["id"], actor, "freeze", "active", "frozen", {"patch": patch}
            )

    def _restore_application_grants(self, actor, application):
        today = utcnow()[:10]
        for grant in self.repository.list_entities(kind="grant", status="frozen"):
            if grant["data"].get("application_id") != application["id"]:
                continue
            if not valid_grant_window(grant["data"].get("expires_at"), today):
                merged = dict(grant["data"])
                merged["expired_at"] = today
                self.repository.update_entity(
                    grant["id"], grant["version"], "expired", merged
                )
                self.audit.record(
                    grant["id"],
                    actor,
                    "expire",
                    "frozen",
                    "expired",
                    {"patch": {"expired_at": today}, "reason": "expired while frozen"},
                )
                continue
            _, patch = self.rules.validate_transition(
                actor, grant, "unfreeze", {}, self._lookup
            )
            merged = dict(grant["data"])
            merged.update(patch)
            self.repository.update_entity(
                grant["id"], grant["version"], "active", merged
            )
            self.audit.record(
                grant["id"], actor, "unfreeze", "frozen", "active", {"patch": patch}
            )


    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
