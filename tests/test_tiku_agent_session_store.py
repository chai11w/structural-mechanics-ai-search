import unittest
from datetime import timedelta

from tiku_agent.session_store import (
    DEFAULT_SESSION_TTL,
    SESSION_STATE_SCHEMA_VERSION,
    SessionStore,
)


class SessionStoreContractTest(unittest.TestCase):
    def test_contract_is_abstract_until_a_storage_backend_is_chosen(self):
        with self.assertRaises(TypeError):
            SessionStore()

    def test_default_session_lifetime_is_two_hours(self):
        self.assertEqual(DEFAULT_SESSION_TTL, timedelta(hours=2))
        self.assertEqual(SESSION_STATE_SCHEMA_VERSION, 1)


if __name__ == "__main__":
    unittest.main()
