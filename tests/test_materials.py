"""检查交换资料的结构和记录间引用。"""
import json
import subprocess
import sys
from pathlib import Path
import unittest

from catalog import read_table

ROOT = Path(__file__).resolve().parents[1]
TABLE_KEYS = {'plans': ['scope'], 'visit_history': ['visit'], 'provider_terms': ['provider', 'care'], 'candidate_slots': ['slot'], 'family_consents': ['scope', 'provider', 'recorded'], 'extension_offer': ['offer'], 'trial_windows': ['group'], 'extension_events': ['event'], 'handover_proofs': ['event']}


class MaterialsTest(unittest.TestCase):
    def rows(self, name):
        return list(read_table(name))

    def assert_subset(self, left, reference, right, key):
        self.assertLessEqual({r[reference] for r in self.rows(left)},
                             {r[key] for r in self.rows(right)})

    def test_structure(self):
        for name, columns in TABLE_KEYS.items():
            with self.subTest(name=name):
                rows = self.rows(name)
                self.assertTrue(rows)
                keys = []
                for row in rows:
                    self.assertNotIn(None, row)
                    self.assertNotIn(None, row.values())
                    value = tuple(row[field] for field in columns)
                    self.assertTrue(all(value))
                    keys.append(value)
                self.assertEqual(len(keys), len(set(keys)))

    def test_catalog_reports_input_counts(self):
        result = subprocess.check_output([sys.executable, "catalog.py"], cwd=ROOT, text=True)
        self.assertEqual(json.loads(result), {name: len(self.rows(name)) for name in TABLE_KEYS})

    def test_business_references(self):
        self.assert_subset("visit_history", "scope", "plans", "scope")
        self.assert_subset("family_consents", "scope", "plans", "scope")
        self.assertEqual({row["kind"] for row in self.rows("handover_proofs") if row["scope"] == "X02" and row["provider"] == "C"}, {"ready", "confirmed", "missed"})
        self.assertEqual({row["team"] for row in self.rows("candidate_slots") if row["slot"] in {"T01", "T02"}}, {"Q"})
