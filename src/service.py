from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import INBREEDING_THRESHOLD, Pedigree, RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        kind = self.rules.normalize_kind(kind)
        if field is None:
            return self.repository.list_entities(kind=kind)
        return self.repository.find_entities(kind, field, value)

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
        if (entity["kind"], action) == ("animal", "correct_pedigree"):
            return self._correct_pedigree(actor, entity, dict(data or {}), expected)
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
        return updated

    def _correct_pedigree(self, actor, entity, data, expected):
        """Correct parent records, then recalculate descendants and pairings.

        The parent patch, every descendant inbreeding recalculation and every
        pairing sent back to review are written in a single transaction, so a
        failure keeps the original records and the correction can be retried.
        """
        next_status, patch = self.rules.validate_transition(
            actor, entity, "correct_pedigree", data, self._lookup
        )
        animals = self.repository.list_entities(kind="animal")
        corrected = []
        for animal in animals:
            if animal["id"] == entity["id"]:
                animal = dict(animal)
                merged = dict(animal["data"])
                merged["sire_id"] = patch["sire_id"]
                merged["dam_id"] = patch["dam_id"]
                animal["data"] = merged
            corrected.append(animal)
        pedigree = Pedigree(corrected)
        by_id = {animal["id"]: animal for animal in corrected}
        lineage = pedigree.descendants(entity["id"])
        updates = []
        audits = []
        recalculated = []
        for animal_id in lineage:
            animal = by_id[animal_id]
            coefficient = round(pedigree.inbreeding(animal_id), 6)
            previous = animal["data"].get("inbreeding")
            changed = animal_id == entity["id"]
            if previous is None:
                changed = changed or coefficient != 0.0
            else:
                changed = changed or previous != coefficient
            if not changed:
                continue
            new_data = dict(animal["data"])
            new_data["inbreeding"] = coefficient
            updates.append({
                "id": animal_id,
                "expected_version": expected if animal_id == entity["id"] else animal["version"],
                "status": next_status if animal_id == entity["id"] else animal["status"],
                "data": new_data,
            })
            recalculated.append({"animal_id": animal_id, "inbreeding": coefficient})
        affected = set(lineage)
        returned = []
        for pairing in self.repository.list_entities(kind="pairing"):
            if pairing["status"] != "approved":
                continue
            sire_id = pairing["data"].get("sire_id")
            dam_id = pairing["data"].get("dam_id")
            if sire_id not in affected and dam_id not in affected:
                continue
            kinship = round(pedigree.kinship(sire_id, dam_id), 6)
            if kinship <= INBREEDING_THRESHOLD:
                continue
            via = sire_id if sire_id in affected else dam_id
            review_reason = {
                "cause": "pedigree_correction",
                "corrected_animal_id": entity["id"],
                "correction_reason": data.get("reason"),
                "via_animal_id": via,
                "kinship": kinship,
                "threshold": INBREEDING_THRESHOLD,
            }
            new_data = dict(pairing["data"])
            new_data["review_reason"] = review_reason
            updates.append({
                "id": pairing["id"],
                "expected_version": pairing["version"],
                "status": "proposed",
                "data": new_data,
            })
            audits.append({
                "entity_id": pairing["id"],
                "actor_id": actor.user_id,
                "actor_role": actor.role,
                "action": "return_to_review",
                "from_status": "approved",
                "to_status": "proposed",
                "detail": review_reason,
            })
            returned.append({
                "pairing_id": pairing["id"],
                "via_animal_id": via,
                "kinship": kinship,
            })
        audits.append({
            "entity_id": entity["id"],
            "actor_id": actor.user_id,
            "actor_role": actor.role,
            "action": "correct_pedigree",
            "from_status": entity["status"],
            "to_status": next_status,
            "detail": {
                "patch": {"sire_id": patch["sire_id"], "dam_id": patch["dam_id"]},
                "reason": data.get("reason"),
                "recalculated": recalculated,
                "returned_pairings": returned,
            },
        })
        self.repository.update_entities(updates, audits)
        return self.repository.get_entity(entity["id"])

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
