"""Persistent state: atomic, throttled, and never a reason to stop the add-on."""
import json
import os
import unittest

import support  # (also puts the add-on sources on sys.path)

import hypon_store as hst
from hypon_store import StateStore


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(support.temp_dir())
        self.now = 0.0

    def store(self):
        return StateStore.open(self.directory)

    def clocked(self):
        return StateStore(self.directory, clock=lambda: self.now)

    def write_raw(self, text):
        with open(os.path.join(self.directory, hst.FILE_NAME), "w", encoding="utf-8") as f:
            f.write(text)

    def test_what_is_written_is_read_back(self):
        store = self.store()
        store.data["inverters"]["SN1"] = {"status": "online"}
        store.set_mac("192.168.1.87", "aa:bb:cc:dd:ee:ff")
        self.assertTrue(store.flush(force=True))
        again = self.store()
        self.assertEqual(again.data["inverters"], {"SN1": {"status": "online"}})
        self.assertEqual(again.get_mac("192.168.1.87"), "aa:bb:cc:dd:ee:ff")

    def test_an_empty_store_knows_nothing(self):
        store = self.store()
        self.assertIsNone(store.get_mac("192.168.1.87"))
        self.assertEqual(store.data["inverters"], {})

    def test_nothing_is_written_when_nothing_changed(self):
        store = self.store()
        self.assertFalse(store.flush(force=True))
        self.assertFalse(os.path.exists(os.path.join(self.directory, hst.FILE_NAME)))

    def test_setting_the_same_value_again_is_not_a_change(self):
        store = self.store()
        store.set_mac("192.168.1.87", "aa:bb:cc:dd:ee:ff")
        store.flush(force=True)
        store.set_mac("192.168.1.87", "aa:bb:cc:dd:ee:ff")
        self.assertFalse(store.flush(force=True))

    def test_routine_writes_are_throttled_but_forced_ones_are_not(self):
        store = self.clocked()
        store.mark_dirty()
        self.assertTrue(store.flush())                 # the first one goes through
        store.mark_dirty()
        self.now += hst.FLUSH_EVERY_S - 1
        self.assertFalse(store.flush())                # too soon
        self.assertTrue(store.flush(force=True))       # a change of status cannot wait
        store.mark_dirty()
        self.now += hst.FLUSH_EVERY_S
        self.assertTrue(store.flush())                 # the window is over

    def test_the_write_is_atomic_and_leaves_no_temporary_file(self):
        store = self.store()
        store.mark_dirty()
        store.flush(force=True)
        self.assertEqual(os.listdir(self.directory), [hst.FILE_NAME])

    def test_a_corrupted_file_is_ignored(self):
        self.write_raw("{ not json")
        store = self.store()
        self.assertEqual(store.data["inverters"], {})

    def test_a_file_of_an_unknown_version_is_ignored(self):
        self.write_raw(json.dumps({"version": 999, "inverters": {"SN1": {}}}))
        self.assertEqual(self.store().data["inverters"], {})

    def test_a_file_of_the_wrong_shape_is_ignored(self):
        self.write_raw(json.dumps(["not", "a", "dict"]))
        self.assertEqual(self.store().data["inverters"], {})

    def test_malformed_sections_are_skipped_one_by_one(self):
        self.write_raw(json.dumps({"version": hst.VERSION, "macs": ["x"], "inverters": {"SN1": {"a": 1}}}))
        store = self.store()
        self.assertEqual(store.data["macs"], {})
        self.assertEqual(store.data["inverters"], {"SN1": {"a": 1}})

    def test_without_a_persistent_folder_it_works_in_memory(self):
        for directory in (None, "", os.path.join(self.directory, "missing")):
            with self.subTest(directory=directory):
                store = StateStore.open(directory)
                self.assertIsNone(store.path)
                store.set_mac("192.168.1.87", "aa:bb:cc:dd:ee:ff")
                self.assertFalse(store.flush(force=True))
                self.assertEqual(store.get_mac("192.168.1.87"), "aa:bb:cc:dd:ee:ff")

    def test_an_unwritable_folder_is_not_fatal_and_is_not_retried_every_time(self):
        store = self.clocked()
        store.path = os.path.join(self.directory, "missing", hst.FILE_NAME)   # cannot be created
        store.mark_dirty()
        self.assertFalse(store.flush())
        self.now += 1
        self.assertFalse(store.flush())                # throttled, no write storm
        self.assertTrue(store._dirty)                  # still pending: it will be retried later


if __name__ == "__main__":
    unittest.main()
