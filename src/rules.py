from collections import deque
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


class Pedigree:
    """Kinship and inbreeding over a snapshot of animal records.

    Unregistered or unknown parents are treated as founders. Cycles in the
    parent graph are rejected with ValidationError instead of recursing
    forever, so a failed recalculation leaves existing records untouched.
    """

    def __init__(self, animals):
        self._parents = {}
        for animal in animals:
            data = animal.get("data", {})
            self._parents[animal["id"]] = (data.get("sire_id"), data.get("dam_id"))
        self._depths = {}
        self._inbreeding = {}
        self._kinship = {}

    def depth(self, animal_id):
        return self._depth(animal_id, set())

    def _depth(self, animal_id, visiting):
        if animal_id in self._depths:
            return self._depths[animal_id]
        if animal_id in visiting:
            raise ValidationError("pedigree cycle detected at animal " + str(animal_id))
        visiting.add(animal_id)
        depth = 0
        sire, dam = self._parents.get(animal_id, (None, None))
        for parent in (sire, dam):
            if parent in self._parents:
                depth = max(depth, 1 + self._depth(parent, visiting))
        visiting.discard(animal_id)
        self._depths[animal_id] = depth
        return depth

    def kinship(self, first, second):
        if not first or not second:
            return 0.0
        if first not in self._parents or second not in self._parents:
            return 0.0
        if first == second:
            return (1.0 + self.inbreeding(first)) / 2.0
        key = (first, second) if first < second else (second, first)
        if key not in self._kinship:
            stable, expand = first, second
            if self.depth(stable) > self.depth(expand):
                stable, expand = expand, stable
            sire, dam = self._parents.get(expand, (None, None))
            self._kinship[key] = (
                self.kinship(stable, sire) + self.kinship(stable, dam)
            ) / 2.0
        return self._kinship[key]

    def inbreeding(self, animal_id):
        if animal_id not in self._inbreeding:
            sire, dam = self._parents.get(animal_id, (None, None))
            if sire in self._parents and dam in self._parents:
                self._inbreeding[animal_id] = self.kinship(sire, dam)
            else:
                self._inbreeding[animal_id] = 0.0
        return self._inbreeding[animal_id]

    def descendants(self, root):
        """Breadth-first list of root plus every animal descending from it."""
        children = {}
        for animal_id, (sire, dam) in self._parents.items():
            for parent in (sire, dam):
                if parent:
                    children.setdefault(parent, []).append(animal_id)
        ordered = []
        seen = {root}
        queue = deque([root])
        while queue:
            current = queue.popleft()
            ordered.append(current)
            for child in children.get(current, []):
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
        return ordered


def _validate_pairing(actor, entity, data, lookup):
    animals = _find_all(lookup, "animal")
    by_id = {animal["id"]: animal for animal in animals}
    sire = by_id.get(data.get("sire_id"))
    dam = by_id.get(data.get("dam_id"))
    if not sire or not dam:
        raise ValidationError("pairing requires two existing animals")
    if sire["status"] != "active" or dam["status"] != "active":
        raise ValidationError("pairing animals must be active")
    if Pedigree(animals).kinship(sire["id"], dam["id"]) > INBREEDING_THRESHOLD:
        raise ValidationError("pairing exceeds inbreeding threshold")
    return {"approved_by": actor.user_id}


def _validate_pedigree_correction(actor, entity, data, lookup):
    if "sire_id" not in data and "dam_id" not in data:
        raise ValidationError("nothing to correct: provide sire_id or dam_id")
    animals = _find_all(lookup, "animal")
    by_id = {animal["id"]: animal for animal in animals}
    current = entity.get("data", {})
    new_sire = data["sire_id"] if "sire_id" in data else current.get("sire_id")
    new_dam = data["dam_id"] if "dam_id" in data else current.get("dam_id")
    for parent in (new_sire, new_dam):
        if parent is None:
            continue
        if parent == entity["id"]:
            raise ValidationError("animal cannot be its own parent")
        if parent not in by_id:
            raise ValidationError("unknown parent animal: " + str(parent))
    corrected = []
    for animal in animals:
        if animal["id"] == entity["id"]:
            animal = dict(animal)
            merged = dict(animal["data"])
            merged["sire_id"] = new_sire
            merged["dam_id"] = new_dam
            animal["data"] = merged
        corrected.append(animal)
    Pedigree(corrected).depth(entity["id"])
    return {"sire_id": new_sire, "dam_id": new_dam}


def _validate_transfer_reconcile(actor, entity, data, lookup):
    animals = _find_all(lookup, "animal")
    external = (
        data.get("external_animal_id")
        or entity["data"].get("external_animal_id")
        or entity["data"].get("animal_id")
    )
    match = None
    for animal in animals:
        if animal["id"] == external or animal["data"].get("external_id") == external:
            match = animal
            break
    if match is None:
        return "suspended", {
            "external_animal_id": external,
            "suspend_reason": "no registered animal for external id: %s" % external,
        }
    return "planned", {
        "animal_id": match["id"],
        "external_animal_id": external,
        "suspend_reason": None,
    }


CUSTOM_CREATE = {'animal': _validate_animal}
CUSTOM_TRANSITIONS = {('pairing', 'approve'): _validate_pairing, ('animal', 'correct_pedigree'): _validate_pedigree_correction, ('transfer', 'reconcile'): _validate_transfer_reconcile}


class RuleEngine:
    ALIASES = {'animals': 'animal', 'pairings': 'pairing', 'transfers': 'transfer'}
    INITIAL_STATUS = {'animal': 'active', 'pairing': 'proposed', 'transfer': 'planned'}
    TRANSITIONS = {'animal': {'mark_deceased': (('active',), 'deceased'), 'quarantine_animal': (('active',), 'quarantined'), 'release_quarantine': (('quarantined',), 'active'), 'correct_pedigree': (('active', 'quarantined', 'deceased'), None)}, 'pairing': {'approve': (('proposed',), 'approved'), 'reject': (('proposed',), 'rejected'), 'complete': (('approved',), 'completed')}, 'transfer': {'authorize': (('planned',), 'authorized'), 'ship': (('authorized',), 'in_transit'), 'arrive': (('in_transit',), 'completed'), 'reconcile': (('planned', 'suspended'), 'planned')}}
    CREATE_REQUIRED = {'animal': ('name', 'sex'), 'pairing': ('proposed_by',), 'transfer': ('animal_id', 'from_institution', 'to_institution')}
    ACTION_REQUIRED = {('animal', 'mark_deceased'): ('cause',), ('animal', 'quarantine_animal'): ('reason',), ('animal', 'correct_pedigree'): ('reason',), ('pairing', 'approve'): ('sire_id', 'dam_id', 'approvals'), ('pairing', 'reject'): ('reason',), ('pairing', 'complete'): ('offspring_ids',), ('transfer', 'authorize'): ('permit_id',), ('transfer', 'ship'): ('transport_id',), ('transfer', 'arrive'): ('arrival_date',)}
    CREATE_ROLES = {'animal': ('admin', 'registrar'), 'pairing': ('admin', 'coordinator'), 'transfer': ('admin', 'registrar')}
    ROLE_ACTIONS = {'mark_deceased': ('admin', 'veterinarian'), 'quarantine_animal': ('admin', 'veterinarian'), 'release_quarantine': ('admin', 'veterinarian'), 'correct_pedigree': ('admin', 'registrar'), 'approve': ('admin', 'coordinator'), 'reject': ('admin', 'coordinator'), 'complete': ('admin', 'coordinator'), 'authorize': ('admin', 'registrar'), 'ship': ('admin', 'registrar'), 'arrive': ('admin', 'registrar'), 'reconcile': ('admin', 'registrar')}

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
        extra = {}
        if custom:
            result = custom(actor, entity, data, lookup)
            if isinstance(result, tuple):
                override, extra = result
                if override is not None:
                    next_status = override
            elif result:
                extra = result
        if next_status is None:
            next_status = entity["status"]
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _find_all(lookup, kind):
    if lookup is None:
        return []
    return lookup(kind, None, None) or []


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
