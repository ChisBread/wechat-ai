"""Contact refresh regressions; no real databases or key discovery required."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_linux_database_recovery import LinuxDatabaseService, UserInfo


class ContactMetadataRefreshTest(unittest.TestCase):
    def setUp(self):
        self.db = LinuxDatabaseService(UserInfo(account="synthetic"), scan_keys=False)
        self.db.load_contacts = Mock()
        self.db.load_chat_rooms = Mock()
        self.clock = patch(
            "wechat_ai_bot.services.core.linux_database_service.time.monotonic",
            return_value=100.0,
        )
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_refresh_throttles_and_force_bypasses_interval(self):
        self.assertEqual(self.db._last_contact_refresh, 0)
        self.db.refresh_contact_metadata()
        self.assertEqual(self.db._last_contact_refresh, 100)

        self.now.return_value = 109.9
        self.db.refresh_contact_metadata()
        self.db.load_contacts.assert_called_once_with()
        self.db.load_chat_rooms.assert_called_once_with()
        self.assertEqual(self.db._last_contact_refresh, 100)

        self.db.refresh_contact_metadata(force=True)
        self.assertEqual(self.db.load_contacts.call_count, 2)
        self.assertEqual(self.db.load_chat_rooms.call_count, 2)
        self.assertEqual(self.db._last_contact_refresh, 109.9)

        self.now.return_value = 119.9
        self.db.refresh_contact_metadata()
        self.assertEqual(self.db.load_contacts.call_count, 3)
        self.assertEqual(self.db.load_chat_rooms.call_count, 3)

    def test_refresh_only_loads_contacts_then_rooms(self):
        calls = Mock()
        calls.attach_mock(self.db.load_contacts, "contacts")
        calls.attach_mock(self.db.load_chat_rooms, "rooms")
        self.db.rescan_keys = Mock()
        self.db.load_message_username_map = Mock()
        self.db._discover_accounts = Mock()
        connection = Mock()
        path = Path("synthetic.db")
        self.db._connections[path] = connection
        self.db._table_sequences[(path, "Msg_synthetic")] = 42
        with self.db._lock:
            self.db.refresh_contact_metadata()
        self.assertEqual([call[0] for call in calls.mock_calls], ["contacts", "rooms"])
        self.db.rescan_keys.assert_not_called()
        self.db.load_message_username_map.assert_not_called()
        self.db._discover_accounts.assert_not_called()
        connection.close.assert_not_called()
        self.assertIs(self.db._connections[path], connection)
        self.assertEqual(self.db._table_sequences, {(path, "Msg_synthetic"): 42})

    def test_ensure_contact_returns_existing_without_reload(self):
        contact = SimpleNamespace(username="synthetic")
        self.db._contact_by_username[contact.username] = contact
        self.assertIs(self.db.ensure_contact(contact.username), contact)
        self.db.load_contacts.assert_not_called()
        self.db.load_chat_rooms.assert_not_called()

    def test_ensure_contact_reloads_once_and_returns_new_contact(self):
        contact = SimpleNamespace(username="synthetic@chatroom")
        self.db.load_contacts.side_effect = lambda: self.db._contact_by_username.update(
            {contact.username: contact}
        )
        self.assertIs(self.db.ensure_contact(contact.username), contact)
        self.assertIs(self.db.ensure_contact(contact.username), contact)
        self.db.load_contacts.assert_called_once_with()
        self.db.load_chat_rooms.assert_called_once_with()

    def test_unknown_contacts_share_throttle_and_retry_after_interval(self):
        self.assertIsNone(self.db.ensure_contact("missing"))
        self.assertIsNone(self.db.ensure_contact("another-missing"))
        self.assertIsNone(self.db.ensure_contact("missing"))
        self.db.load_contacts.assert_called_once_with()
        self.db.load_chat_rooms.assert_called_once_with()
        self.now.return_value = 110
        self.assertIsNone(self.db.ensure_contact("missing"))
        self.assertEqual(self.db.load_contacts.call_count, 2)
        self.assertEqual(self.db.load_chat_rooms.call_count, 2)

    def test_empty_name_does_not_reload(self):
        self.assertIsNone(self.db.ensure_contact(""))
        self.assertIsNone(self.db.ensure_contact(None))
        self.db.load_contacts.assert_not_called()

    def test_periodic_message_map_refresh_also_loads_metadata(self):
        self.db._last_message_map_refresh = 100
        self.db.load_message_username_map = Mock()
        with patch("wechat_ai_bot.services.core.linux_database_service.time.time", return_value=130):
            with self.db._lock:
                self.db._refresh_message_maps_if_needed()
        self.db.load_message_username_map.assert_called_once_with()
        self.db.load_contacts.assert_called_once_with()
        self.db.load_chat_rooms.assert_called_once_with()

    def test_periodic_branch_respects_metadata_throttle(self):
        self.db.refresh_contact_metadata()
        self.db.load_message_username_map = Mock()
        with patch("wechat_ai_bot.services.core.linux_database_service.time.time", return_value=130):
            self.db._refresh_message_maps_if_needed()
        self.db.load_message_username_map.assert_called_once_with()
        self.db.load_contacts.assert_called_once_with()
        self.db.load_chat_rooms.assert_called_once_with()

    def test_no_periodic_refresh_before_message_map_interval(self):
        self.db._last_message_map_refresh = 100
        self.db.load_message_username_map = Mock()
        with patch("wechat_ai_bot.services.core.linux_database_service.time.time", return_value=129):
            self.db._refresh_message_maps_if_needed()
        self.db.load_message_username_map.assert_not_called()
        self.db.load_contacts.assert_not_called()
        self.db.load_chat_rooms.assert_not_called()


if __name__ == "__main__":
    unittest.main()
