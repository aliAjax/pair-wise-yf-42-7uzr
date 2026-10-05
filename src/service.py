from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .repository import utcnow
from .rules import RuleEngine


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
        if action == "correct_pedigree":
            self._apply_descendant_recalculation(
                entity_id, actor, cause_ancestor_id=entity_id
            )
        return updated

    def _apply_descendant_recalculation(self, animal_id, actor, cause_ancestor_id):
        """Recompute coefficients for pairings along the corrected animal's
        lineage and revert any that now exceed the inbreeding threshold.

        Pairings that cannot be recomputed keep their original status and record
        a recheck_error so the operation can be retried later without losing data.
        """
        results = self.rules.recalculate_descendant_pairings(
            animal_id, self._lookup, cause_ancestor_id=cause_ancestor_id
        )
        for result in results:
            pairing = self.repository.get_entity(result["pairing_id"])
            if not pairing:
                continue
            pdata = dict(pairing["data"])
            if result["error"]:
                pdata["recheck_error"] = result["error"]
                self.repository.update_entity(
                    pairing["id"], pairing["version"], pairing["status"], pdata
                )
                self.audit.record(
                    pairing["id"],
                    actor,
                    "recheck_failed",
                    pairing["status"],
                    pairing["status"],
                    {
                        "error": result["error"],
                        "cause_ancestor": cause_ancestor_id,
                    },
                )
                continue
            if result["over_threshold"] and pairing["status"] == "approved":
                pdata["reverted_from"] = "approved"
                pdata["caused_by_ancestor"] = cause_ancestor_id
                pdata["reverted_coefficient"] = result["coefficient"]
                pdata["recheck_error"] = None
                self.repository.update_entity(
                    pairing["id"], pairing["version"], "proposed", pdata
                )
                self.audit.record(
                    pairing["id"],
                    actor,
                    "revert_to_proposed",
                    "approved",
                    "proposed",
                    {
                        "cause_ancestor": cause_ancestor_id,
                        "coefficient": result["coefficient"],
                    },
                )
            elif pdata.get("recheck_error"):
                pdata["recheck_error"] = None
                self.repository.update_entity(
                    pairing["id"], pairing["version"], pairing["status"], pdata
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
