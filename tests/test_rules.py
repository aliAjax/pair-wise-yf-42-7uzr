import unittest

from src.rules import Pedigree, inbreeding_coefficient
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


def _animal(animal_id, sire_id=None, dam_id=None):
    return {"id": animal_id, "data": {"sire_id": sire_id, "dam_id": dam_id}}


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        self.assertEqual(inbreeding_coefficient({"id": "a"}, {"id": "a"}), 0.5)
        self.assertEqual(inbreeding_coefficient({"id": "a", "sire_id": "b"}, {"id": "b"}), 0.25)
        self.assertEqual(inbreeding_coefficient({"id": "a", "sire_id": "x"}, {"id": "b", "sire_id": "y"}), 0.0)
        with self.assertRaises(ValidationError):
            self.rules.validate_create(self.admin, "animals", {"name": "A", "sex": "other"})

    def test_pedigree_kinship_and_inbreeding(self):
        animals = [
            _animal("x"),
            _animal("y"),
            _animal("c1", "x", "y"),
            _animal("c2", "x", "y"),
            _animal("grandchild", "c1", "c2"),
        ]
        pedigree = Pedigree(animals)
        self.assertEqual(pedigree.kinship("x", "y"), 0.0)
        self.assertEqual(pedigree.kinship("x", "c1"), 0.25)
        self.assertEqual(pedigree.kinship("c1", "c2"), 0.25)
        self.assertEqual(pedigree.inbreeding("c1"), 0.0)
        self.assertEqual(pedigree.inbreeding("grandchild"), 0.25)

    def test_pedigree_half_sibs_and_unknown_parents(self):
        animals = [
            _animal("x"),
            _animal("h1", "x", None),
            _animal("h2", "x", None),
        ]
        pedigree = Pedigree(animals)
        self.assertEqual(pedigree.kinship("h1", "h2"), 0.125)
        self.assertEqual(pedigree.kinship("h1", "ghost"), 0.0)
        self.assertEqual(pedigree.inbreeding("h1"), 0.0)

    def test_pedigree_descendants_and_cycle_detection(self):
        animals = [
            _animal("root"),
            _animal("child", "root", None),
            _animal("grand", "child", None),
            _animal("other"),
        ]
        pedigree = Pedigree(animals)
        self.assertEqual(pedigree.descendants("root"), ["root", "child", "grand"])
        cyclic = Pedigree([_animal("a", "b", None), _animal("b", "a", None)])
        with self.assertRaises(ValidationError):
            cyclic.depth("a")


if __name__ == "__main__":
    unittest.main()
