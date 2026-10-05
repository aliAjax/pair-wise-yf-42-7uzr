import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def test_permission_denied(self):
        entity = self.service.create(
            Actor("admin", "admin"), 'animal', {'name': 'A', 'sex': 'male'}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                entity["id"],
                'mark_deceased',
                {'cause': 'illness'},
            )

    def test_version_conflict(self):
        entity = self.service.create(
            Actor("admin", "admin"), 'animal', {'name': 'A', 'sex': 'male'}
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                Actor("admin", "admin"),
                entity["id"],
                'mark_deceased',
                {'cause': 'illness'},
                expected_version=999,
            )

    def test_duplicate_idempotency_key_returns_same_entity(self):
        first = self.service.create(
            Actor("admin", "admin"),
            'animal',
            {'name': 'A', 'sex': 'male'},
            idempotency_key="duplicate-check",
        )
        second = self.service.create(
            Actor("admin", "admin"),
            'animal',
            {'name': 'A', 'sex': 'male'},
            idempotency_key="duplicate-check",
        )
        self.assertEqual(first["id"], second["id"])

    def test_correct_pedigree_requires_registrar_role(self):
        entity = self.service.create(
            Actor("admin", "admin"), 'animal', {'name': 'A', 'sex': 'male'}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("coord", "coordinator"),
                entity["id"],
                'correct_pedigree',
                {'sire_id': None, 'reason': 'archive fix'},
            )

    def test_failed_correction_preserves_records_and_allows_retry(self):
        admin = Actor("admin", "admin")
        registrar = Actor("reg-1", "registrar")
        parent = self.service.create(admin, 'animal', {'name': 'P', 'sex': 'male'})
        child = self.service.create(
            admin, 'animal', {'name': 'C', 'sex': 'female', 'sire_id': parent['id']}
        )
        version_before = child["version"]
        with self.assertRaises(ValidationError):
            self.service.transition(
                registrar, child["id"], 'correct_pedigree',
                {'dam_id': 'ghost-animal', 'reason': 'archive fix'},
            )
        after = self.service.get(child["id"])
        self.assertEqual(after["version"], version_before)
        self.assertNotIn("dam_id", after["data"])
        # The original record is untouched, so a corrected retry succeeds.
        retried = self.service.transition(
            registrar, child["id"], 'correct_pedigree',
            {'dam_id': None, 'reason': 'archive fix'},
        )
        self.assertIsNone(retried["data"]["dam_id"])

    def test_correction_cycle_is_rejected(self):
        admin = Actor("admin", "admin")
        root = self.service.create(admin, 'animal', {'name': 'R', 'sex': 'male'})
        child = self.service.create(
            admin, 'animal', {'name': 'C', 'sex': 'female', 'sire_id': root['id']}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                admin, root["id"], 'correct_pedigree',
                {'sire_id': child["id"], 'reason': 'bad archive row'},
            )
        self.assertNotIn("sire_id", self.service.get(root["id"])["data"])

    def test_correction_version_conflict_keeps_originals(self):
        admin = Actor("admin", "admin")
        sire = self.service.create(admin, 'animal', {'name': 'S', 'sex': 'male'})
        dam = self.service.create(admin, 'animal', {'name': 'D', 'sex': 'female'})
        child = self.service.create(admin, 'animal', {'name': 'C', 'sex': 'female'})
        with self.assertRaises(ConflictError):
            self.service.transition(
                admin, child["id"], 'correct_pedigree',
                {'sire_id': sire["id"], 'dam_id': dam["id"], 'reason': 'fix'},
                expected_version=999,
            )
        after = self.service.get(child["id"])
        self.assertEqual(after["version"], child["version"])
        self.assertNotIn("sire_id", after["data"])

    def test_update_entities_rolls_back_whole_batch(self):
        admin = Actor("admin", "admin")
        first = self.service.create(admin, 'animal', {'name': 'A', 'sex': 'male'})
        second = self.service.create(admin, 'animal', {'name': 'B', 'sex': 'male'})
        updates = [
            {
                "id": first["id"],
                "expected_version": first["version"],
                "status": "active",
                "data": dict(first["data"], note="should not persist"),
            },
            {
                "id": second["id"],
                "expected_version": 999,
                "status": "active",
                "data": second["data"],
            },
        ]
        audits = [{
            "entity_id": first["id"],
            "actor_id": "admin",
            "actor_role": "admin",
            "action": "correct_pedigree",
            "from_status": "active",
            "to_status": "active",
            "detail": {"note": "should not persist"},
        }]
        with self.assertRaises(ConflictError):
            self.repo.update_entities(updates, audits)
        unchanged = self.service.get(first["id"])
        self.assertEqual(unchanged["version"], first["version"])
        self.assertNotIn("note", unchanged["data"])
        audit_actions = [
            entry["action"] for entry in self.service.audit_log(first["id"])
        ]
        self.assertNotIn("correct_pedigree", audit_actions)


if __name__ == "__main__":
    unittest.main()
