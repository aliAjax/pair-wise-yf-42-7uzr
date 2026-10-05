import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_workflow(self):
        created = {}
        steps = [{'op': 'create', 'as': 'sire', 'kind': 'animal', 'data': {'name': 'M-1', 'sex': 'male'}}, {'op': 'create', 'as': 'dam', 'kind': 'animal', 'data': {'name': 'F-1', 'sex': 'female'}}, {'op': 'create', 'as': 'pairing', 'kind': 'pairing', 'data': {'proposed_by': 'coordinator'}}, {'op': 'transition', 'target': 'pairing', 'action': 'approve', 'data': {'sire_id': '{sire}', 'dam_id': '{dam}', 'approvals': ['vet-1']}, 'expect': 'approved'}, {'op': 'transition', 'target': 'pairing', 'action': 'complete', 'data': {'offspring_ids': ['offspring-1']}, 'expect': 'completed'}, {'op': 'create', 'as': 'transfer', 'kind': 'transfer', 'data': {'animal_id': '{sire}', 'from_institution': 'Zoo-A', 'to_institution': 'Zoo-B'}}, {'op': 'transition', 'target': 'transfer', 'action': 'authorize', 'data': {'permit_id': 'P-1'}, 'expect': 'authorized'}, {'op': 'transition', 'target': 'transfer', 'action': 'ship', 'data': {'transport_id': 'T-1'}, 'expect': 'in_transit'}, {'op': 'transition', 'target': 'transfer', 'action': 'arrive', 'data': {'arrival_date': '2026-05-01'}, 'expect': 'completed'}]
        for step in steps:
            if step["op"] == "create":
                entity = self.service.create(
                    self.actor,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            else:
                entity = self.service.transition(
                    self.actor,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])

    def test_pedigree_correction_recalculates_descendants_and_returns_pairings(self):
        registrar = Actor("reg-1", "registrar")
        sire_x = self.service.create(self.actor, "animal", {"name": "X", "sex": "male"})
        dam_y = self.service.create(self.actor, "animal", {"name": "Y", "sex": "female"})
        child_1 = self.service.create(
            self.actor, "animal",
            {"name": "C1", "sex": "male", "sire_id": sire_x["id"], "dam_id": dam_y["id"]},
        )
        # C2's parents were recorded incorrectly as unknown in the old archive.
        child_2 = self.service.create(self.actor, "animal", {"name": "C2", "sex": "female"})
        grandchild = self.service.create(
            self.actor, "animal",
            {"name": "D", "sex": "female", "sire_id": child_1["id"], "dam_id": child_2["id"]},
        )
        pairing = self.service.create(self.actor, "pairing", {"proposed_by": "coordinator"})
        self.service.transition(
            self.actor, pairing["id"], "approve",
            {"sire_id": child_1["id"], "dam_id": child_2["id"], "approvals": ["vet-1"]},
        )

        updated = self.service.transition(
            registrar, child_2["id"], "correct_pedigree",
            {"sire_id": sire_x["id"], "dam_id": dam_y["id"], "reason": "archive fix"},
        )
        self.assertEqual(updated["data"]["sire_id"], sire_x["id"])
        self.assertEqual(updated["data"]["inbreeding"], 0.0)
        # The correction cascades along descendants: D is now known to come
        # from a full-sib mating, so its inbreeding coefficient is 0.25.
        descendant = self.service.get(grandchild["id"])
        self.assertEqual(descendant["data"]["inbreeding"], 0.25)
        # The approved pairing now exceeds the threshold and goes back to
        # pending review, naming the ancestor whose record was corrected.
        reverted = self.service.get(pairing["id"])
        self.assertEqual(reverted["status"], "proposed")
        reason = reverted["data"]["review_reason"]
        self.assertEqual(reason["cause"], "pedigree_correction")
        self.assertEqual(reason["corrected_animal_id"], child_2["id"])
        self.assertEqual(reason["correction_reason"], "archive fix")
        self.assertEqual(reason["kinship"], 0.25)
        audit_actions = [
            entry["action"] for entry in self.service.audit_log(pairing["id"])
        ]
        self.assertIn("return_to_review", audit_actions)

    def test_transfer_reconcile_suspends_unmatched_then_retries(self):
        registrar = Actor("reg-1", "registrar")
        transfer = self.service.create(
            registrar, "transfer",
            {
                "animal_id": "EXT-9",
                "external_animal_id": "EXT-9",
                "from_institution": "Zoo-A",
                "to_institution": "Zoo-B",
            },
        )
        # No registered animal carries external id EXT-9 yet: park the record.
        parked = self.service.transition(registrar, transfer["id"], "reconcile", {})
        self.assertEqual(parked["status"], "suspended")
        self.assertIn("EXT-9", parked["data"]["suspend_reason"])
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                registrar, transfer["id"], "authorize", {"permit_id": "P-1"}
            )
        # Once the animal is registered locally, the same action reconciles.
        animal = self.service.create(
            registrar, "animal",
            {"name": "A", "sex": "male", "external_id": "EXT-9"},
        )
        matched = self.service.transition(registrar, transfer["id"], "reconcile", {})
        self.assertEqual(matched["status"], "planned")
        self.assertEqual(matched["data"]["animal_id"], animal["id"])
        authorized = self.service.transition(
            registrar, transfer["id"], "authorize", {"permit_id": "P-1"}
        )
        self.assertEqual(authorized["status"], "authorized")

    def test_transfer_reconcile_accepts_internal_id(self):
        registrar = Actor("reg-1", "registrar")
        animal = self.service.create(registrar, "animal", {"name": "A", "sex": "male"})
        transfer = self.service.create(
            registrar, "transfer",
            {"animal_id": animal["id"], "from_institution": "Zoo-A", "to_institution": "Zoo-B"},
        )
        matched = self.service.transition(registrar, transfer["id"], "reconcile", {})
        self.assertEqual(matched["status"], "planned")
        self.assertEqual(matched["data"]["animal_id"], animal["id"])


if __name__ == "__main__":
    unittest.main()
