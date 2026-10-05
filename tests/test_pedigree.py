import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, inbreeding_coefficient
from src.service import DomainService


class PedigreeCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.registrar = Actor("registrar", "registrar")
        self.coordinator = Actor("coordinator", "coordinator")

    def tearDown(self):
        self.tmp.cleanup()

    def _pair(self, sire, dam, status="approved"):
        pairing = self.service.create(self.admin, "pairing", {"proposed_by": "coordinator"})
        if status == "approved":
            pairing = self.service.transition(
                self.admin,
                pairing["id"],
                "approve",
                {"sire_id": sire["id"], "dam_id": dam["id"], "approvals": ["vet-1"]},
            )
        return pairing

    def test_correction_reverts_over_threshold_pairing(self):
        # Sire S and dam D are unrelated at approval -> coefficient 0.0.
        sire = self.service.create(self.admin, "animal", {"name": "S", "sex": "male"})
        dam = self.service.create(self.admin, "animal", {"name": "D", "sex": "female"})
        pairing = self._pair(sire, dam)
        self.assertEqual(pairing["status"], "approved")

        # Correct the dam's sire to the sire (father-daughter mating -> 0.25).
        dam = self.service.transition(
            self.registrar, dam["id"], "correct_pedigree", {"sire_id": sire["id"]}
        )
        pairing = self.service.get(pairing["id"])
        self.assertEqual(pairing["status"], "proposed")
        self.assertEqual(pairing["data"]["caused_by_ancestor"], dam["id"])
        self.assertEqual(pairing["data"]["reverted_from"], "approved")
        self.assertEqual(pairing["data"]["reverted_coefficient"], 0.25)

    def test_correction_within_threshold_keeps_approved(self):
        sire = self.service.create(self.admin, "animal", {"name": "S", "sex": "male"})
        dam = self.service.create(self.admin, "animal", {"name": "D", "sex": "female"})
        pairing = self._pair(sire, dam)
        self.assertEqual(pairing["status"], "approved")

        unrelated = self.service.create(
            self.admin, "animal", {"name": "U", "sex": "male"}
        )
        sire = self.service.transition(
            self.registrar, sire["id"], "correct_pedigree", {"sire_id": unrelated["id"]}
        )
        pairing = self.service.get(pairing["id"])
        self.assertEqual(pairing["status"], "approved")
        self.assertNotIn("caused_by_ancestor", pairing["data"])

    def test_correction_requires_at_least_one_parent(self):
        animal = self.service.create(self.admin, "animal", {"name": "A", "sex": "male"})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.registrar, animal["id"], "correct_pedigree", {}
            )

    def test_correction_rejects_own_parent(self):
        animal = self.service.create(self.admin, "animal", {"name": "A", "sex": "male"})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.registrar, animal["id"], "correct_pedigree", {"sire_id": animal["id"]}
            )

    def test_correction_rejects_missing_parent(self):
        animal = self.service.create(self.admin, "animal", {"name": "A", "sex": "male"})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.registrar, animal["id"], "correct_pedigree", {"sire_id": "missing"}
            )

    def test_correction_rejects_wrong_seed_parent(self):
        animal = self.service.create(self.admin, "animal", {"name": "A", "sex": "male"})
        dam = self.service.create(self.admin, "animal", {"name": "D", "sex": "female"})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.registrar, animal["id"], "correct_pedigree", {"sire_id": dam["id"]}
            )

    def test_viewer_cannot_correct_pedigree(self):
        animal = self.service.create(self.admin, "animal", {"name": "A", "sex": "male"})
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                animal["id"],
                "correct_pedigree",
                {"sire_id": animal["id"]},
            )

    def test_recheck_recomputes_and_records_coefficient(self):
        sire = self.service.create(self.admin, "animal", {"name": "S", "sex": "male"})
        dam = self.service.create(self.admin, "animal", {"name": "D", "sex": "female"})
        pairing = self._pair(sire, dam)
        self.assertEqual(pairing["status"], "approved")

        pairing = self.service.transition(self.coordinator, pairing["id"], "recheck", {})
        self.assertEqual(pairing["status"], "approved")
        self.assertEqual(pairing["data"]["last_coefficient"], 0.0)

    def test_recheck_reverts_when_over_threshold(self):
        sire = self.service.create(self.admin, "animal", {"name": "S", "sex": "male"})
        dam = self.service.create(self.admin, "animal", {"name": "D", "sex": "female"})
        pairing = self._pair(sire, dam)
        self.assertEqual(pairing["status"], "approved")

        # Introduce the inbreeding relation directly via the dam's sire.
        dam = self.service.transition(
            self.registrar, dam["id"], "correct_pedigree", {"sire_id": sire["id"]}
        )
        pairing = self.service.get(pairing["id"])
        self.assertEqual(pairing["status"], "proposed")

        # Fix the dam's sire back to an unrelated male, then recheck.
        unrelated = self.service.create(
            self.admin, "animal", {"name": "U", "sex": "male"}
        )
        dam = self.service.transition(
            self.registrar, dam["id"], "correct_pedigree", {"sire_id": unrelated["id"]}
        )
        pairing = self.service.transition(self.coordinator, pairing["id"], "recheck", {})
        # Recheck recomputes but only reverts approved pairings; proposed stays proposed.
        self.assertEqual(pairing["status"], "proposed")
        self.assertEqual(pairing["data"]["last_coefficient"], 0.0)

    def test_failed_recheck_preserves_records(self):
        sire = self.service.create(self.admin, "animal", {"name": "S", "sex": "male"})
        dam = self.service.create(self.admin, "animal", {"name": "D", "sex": "female"})
        pairing = self._pair(sire, dam)
        self.assertEqual(pairing["status"], "approved")

        # Simulate a data inconsistency: point the sire at a missing animal.
        self.repo.update_entity(
            pairing["id"],
            pairing["version"],
            "approved",
            {"proposed_by": "coordinator", "sire_id": "missing", "dam_id": dam["id"]},
        )
        pairing = self.service.get(pairing["id"])
        self.assertEqual(pairing["status"], "approved")

        pairing = self.service.transition(self.coordinator, pairing["id"], "recheck", {})
        self.assertEqual(pairing["status"], "approved")
        self.assertIn("recheck_error", pairing["data"])

        # Fix the record and retry.
        self.repo.update_entity(
            pairing["id"],
            pairing["version"],
            "approved",
            {"proposed_by": "coordinator", "sire_id": sire["id"], "dam_id": dam["id"]},
        )
        pairing = self.service.transition(self.coordinator, pairing["id"], "recheck", {})
        self.assertEqual(pairing["status"], "approved")
        self.assertIsNone(pairing["data"]["recheck_error"])


class TransportReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.registrar = Actor("registrar", "registrar")

    def tearDown(self):
        self.tmp.cleanup()

    def test_reconcile_matches_registered_animal(self):
        animal = self.service.create(
            self.admin, "animal", {"name": "EXT-1", "sex": "male", "external_id": "EXT-001"}
        )
        transfer = self.service.create(
            self.admin,
            "transfer",
            {
                "animal_id": animal["id"],
                "from_institution": "Zoo-A",
                "to_institution": "Zoo-B",
                "external_ref": "EXT-001",
            },
        )
        self.assertEqual(transfer["status"], "planned")

        transfer = self.service.transition(
            self.registrar, transfer["id"], "reconcile", {"external_ref": "EXT-001"}
        )
        self.assertEqual(transfer["status"], "planned")
        self.assertTrue(transfer["data"]["reconciled"])
        self.assertEqual(transfer["data"]["animal_id"], animal["id"])
        self.assertIsNone(transfer["data"]["suspended_reason"])

    def test_reconcile_suspends_unmatched_then_retries(self):
        transfer = self.service.create(
            self.admin,
            "transfer",
            {
                "animal_id": "unknown",
                "from_institution": "Zoo-C",
                "to_institution": "Zoo-B",
                "external_ref": "EXT-999",
            },
        )
        transfer = self.service.transition(
            self.registrar, transfer["id"], "reconcile", {"external_ref": "EXT-999"}
        )
        self.assertEqual(transfer["status"], "suspended")
        self.assertFalse(transfer["data"]["reconciled"])
        self.assertIn("no registered animal", transfer["data"]["suspended_reason"])

        # Register the missing animal and retry reconciliation.
        late = self.service.create(
            self.admin, "animal", {"name": "LATE-1", "sex": "female", "external_id": "EXT-999"}
        )
        transfer = self.service.transition(
            self.registrar, transfer["id"], "reconcile", {"external_ref": "EXT-999"}
        )
        self.assertEqual(transfer["status"], "planned")
        self.assertTrue(transfer["data"]["reconciled"])
        self.assertEqual(transfer["data"]["animal_id"], late["id"])

    def test_reconcile_requires_external_ref(self):
        transfer = self.service.create(
            self.admin,
            "transfer",
            {"animal_id": "x", "from_institution": "A", "to_institution": "B"},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(self.registrar, transfer["id"], "reconcile", {})

    def test_viewer_cannot_reconcile(self):
        transfer = self.service.create(
            self.admin,
            "transfer",
            {"animal_id": "x", "from_institution": "A", "to_institution": "B", "external_ref": "E1"},
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"), transfer["id"], "reconcile", {"external_ref": "E1"}
            )


if __name__ == "__main__":
    unittest.main()
