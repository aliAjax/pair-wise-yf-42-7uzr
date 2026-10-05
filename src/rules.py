from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


INBREEDING_THRESHOLD = 0.125


def _validate_animal(actor, data, lookup):
    if data.get("sex") not in ("male", "female", "unknown"):
        raise ValidationError("sex must be male, female or unknown")


def inbreeding_coefficient(sire, dam):
    if not sire or not dam:
        return 1.0
    sire_id = sire.get("id")
    dam_id = dam.get("id")
    if sire_id is None or dam_id is None:
        return 0.0
    if sire_id == dam_id:
        return 0.5
    if sire.get("sire_id") == dam_id or dam.get("sire_id") == sire_id:
        return 0.25
    return 0.0


def _validate_pairing(actor, entity, data, lookup):
    sire = _find_one(lookup, "animal", "id", data.get("sire_id"))
    dam = _find_one(lookup, "animal", "id", data.get("dam_id"))
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active")
    if inbreeding_coefficient(_inbreeding_data(sire), _inbreeding_data(dam)) > INBREEDING_THRESHOLD:
        raise ValidationError("pairing exceeds inbreeding threshold")
    return {"approved_by": actor.user_id}


def _clean_parent(value):
    if value is None or value == "":
        return None
    return value


def _correct_pedigree(actor, entity, data, lookup):
    sire_id = _clean_parent(data.get("sire_id"))
    dam_id = _clean_parent(data.get("dam_id"))
    if sire_id is None and dam_id is None:
        raise ValidationError("at least one of sire_id or dam_id is required")
    animal_id = entity["id"]
    if sire_id is not None:
        if sire_id == animal_id:
            raise ValidationError("animal cannot be its own sire")
        sire = _find_one(lookup, "animal", "id", sire_id)
        if not sire:
            raise ValidationError("sire %s not found" % sire_id)
        if sire["data"].get("sex") not in ("male", "unknown"):
            raise ValidationError("sire %s must be male" % sire_id)
    if dam_id is not None:
        if dam_id == animal_id:
            raise ValidationError("animal cannot be its own dam")
        dam = _find_one(lookup, "animal", "id", dam_id)
        if not dam:
            raise ValidationError("dam %s not found" % dam_id)
        if dam["data"].get("sex") not in ("female", "unknown"):
            raise ValidationError("dam %s must be female" % dam_id)
    patch = {"pedigree_corrected_by": actor.user_id}
    if sire_id is not None:
        patch["sire_id"] = sire_id
    if dam_id is not None:
        patch["dam_id"] = dam_id
    return "active", patch


def _recheck_pairing(actor, entity, data, lookup):
    pdata = entity["data"]
    sire = _find_one(lookup, "animal", "id", pdata.get("sire_id"))
    dam = _find_one(lookup, "animal", "id", pdata.get("dam_id"))
    if not sire or not dam:
        return entity["status"], {
            "recheck_error": "cannot compute inbreeding coefficient: sire or dam not found",
        }
    coeff = inbreeding_coefficient(_inbreeding_data(sire), _inbreeding_data(dam))
    patch = {"recheck_error": None, "last_coefficient": coeff}
    if coeff > INBREEDING_THRESHOLD and entity["status"] == "approved":
        patch["reverted_from"] = "approved"
        patch["reverted_coefficient"] = coeff
        return "proposed", patch
    return entity["status"], patch


def _reconcile_transfer(actor, entity, data, lookup):
    external_ref = data.get("external_ref") or entity["data"].get("external_ref")
    external_ref = _clean_parent(external_ref)
    if external_ref is None:
        raise ValidationError("external_ref is required to reconcile a transfer")
    animals = lookup("animal", "external_id", external_ref) or []
    if not animals:
        animals = lookup("animal", "id", external_ref) or []
    if animals:
        animal = animals[0]
        return "planned", {
            "animal_id": animal["id"],
            "external_ref": external_ref,
            "reconciled": True,
            "suspended_reason": None,
        }
    return "suspended", {
        "external_ref": external_ref,
        "reconciled": False,
        "suspended_reason": (
            "no registered animal matches external reference %s" % external_ref
        ),
    }


CUSTOM_CREATE = {'animal': _validate_animal}
CUSTOM_TRANSITIONS = {
    ('pairing', 'approve'): _validate_pairing,
    ('pairing', 'recheck'): _recheck_pairing,
    ('animal', 'correct_pedigree'): _correct_pedigree,
    ('transfer', 'reconcile'): _reconcile_transfer,
}


class RuleEngine:
    ALIASES = {'animals': 'animal', 'pairings': 'pairing', 'transfers': 'transfer'}
    INITIAL_STATUS = {'animal': 'active', 'pairing': 'proposed', 'transfer': 'planned'}
    TRANSITIONS = {'animal': {'mark_deceased': (('active',), 'deceased'), 'quarantine_animal': (('active',), 'quarantined'), 'release_quarantine': (('quarantined',), 'active'), 'correct_pedigree': (('active',), 'active')}, 'pairing': {'approve': (('proposed',), 'approved'), 'reject': (('proposed',), 'rejected'), 'complete': (('approved',), 'completed'), 'recheck': (('proposed', 'approved'), 'proposed')}, 'transfer': {'authorize': (('planned',), 'authorized'), 'ship': (('authorized',), 'in_transit'), 'arrive': (('in_transit',), 'completed'), 'reconcile': (('planned', 'suspended'), 'planned')}}
    CREATE_REQUIRED = {'animal': ('name', 'sex'), 'pairing': ('proposed_by',), 'transfer': ('animal_id', 'from_institution', 'to_institution')}
    ACTION_REQUIRED = {('animal', 'mark_deceased'): ('cause',), ('animal', 'quarantine_animal'): ('reason',), ('pairing', 'approve'): ('sire_id', 'dam_id', 'approvals'), ('pairing', 'reject'): ('reason',), ('pairing', 'complete'): ('offspring_ids',), ('transfer', 'authorize'): ('permit_id',), ('transfer', 'ship'): ('transport_id',), ('transfer', 'arrive'): ('arrival_date',)}
    CREATE_ROLES = {'animal': ('admin', 'registrar'), 'pairing': ('admin', 'coordinator'), 'transfer': ('admin', 'registrar')}
    ROLE_ACTIONS = {'mark_deceased': ('admin', 'veterinarian'), 'quarantine_animal': ('admin', 'veterinarian'), 'release_quarantine': ('admin', 'veterinarian'), 'approve': ('admin', 'coordinator'), 'reject': ('admin', 'coordinator'), 'complete': ('admin', 'coordinator'), 'recheck': ('admin', 'coordinator'), 'authorize': ('admin', 'registrar'), 'ship': ('admin', 'registrar'), 'arrive': ('admin', 'registrar'), 'correct_pedigree': ('admin', 'registrar'), 'reconcile': ('admin', 'registrar')}

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
        custom_result = custom(actor, entity, data, lookup) if custom else None
        if isinstance(custom_result, tuple):
            next_status, extra = custom_result
        else:
            extra = custom_result or {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    def recalculate_descendant_pairings(self, animal_id, lookup, cause_ancestor_id=None):
        """Walk the descendant lineage of animal_id and recompute the inbreeding
        coefficient of every pairing found along the way.

        Returns a list of result dicts (one per pairing) without mutating any
        records. Each result has: pairing_id, status, coefficient, over_threshold
        and error. When cause_ancestor_id is given it is echoed back so callers
        can attribute a reversion to the ancestor correction that triggered it.
        """
        results = []
        queue = [animal_id]
        seen = set()
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            pairings = []
            pairings += lookup("pairing", "sire_id", current) or []
            pairings += lookup("pairing", "dam_id", current) or []
            for pairing in pairings:
                pdata = pairing["data"]
                sire = _find_one(lookup, "animal", "id", pdata.get("sire_id"))
                dam = _find_one(lookup, "animal", "id", pdata.get("dam_id"))
                if not sire or not dam:
                    results.append({
                        "pairing_id": pairing["id"],
                        "status": pairing["status"],
                        "coefficient": None,
                        "over_threshold": None,
                        "error": "cannot compute inbreeding coefficient: sire or dam not found",
                        "cause_ancestor": cause_ancestor_id,
                    })
                    continue
                coeff = inbreeding_coefficient(_inbreeding_data(sire), _inbreeding_data(dam))
                results.append({
                    "pairing_id": pairing["id"],
                    "status": pairing["status"],
                    "coefficient": coeff,
                    "over_threshold": coeff > INBREEDING_THRESHOLD,
                    "error": None,
                    "cause_ancestor": cause_ancestor_id,
                })
            children = []
            children += lookup("animal", "sire_id", current) or []
            children += lookup("animal", "dam_id", current) or []
            for child in children:
                if child["id"] not in seen:
                    queue.append(child["id"])
        return results


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _inbreeding_data(entity):
    """Build the id-bearing dict that inbreeding_coefficient expects.

    The animal's id lives at the entity level while sire_id/dam_id live in
    the data payload, so merge them before computing the coefficient.
    """
    data = entity["data"]
    return {
        "id": entity["id"],
        "sire_id": data.get("sire_id"),
        "dam_id": data.get("dam_id"),
    }


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
