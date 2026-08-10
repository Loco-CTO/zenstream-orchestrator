import os
import unittest
from unittest.mock import patch

from app.client_auth import issue_ticket, read_ticket
from fastapi import HTTPException


class ClientTicketTest(unittest.TestCase):
    def setUp(self):
        self.secret = patch.dict(os.environ, {"SECRET_KEY": "ticket-test-secret"})
        self.secret.start()

    def tearDown(self):
        self.secret.stop()

    def test_ticket_requires_matching_claim(self):
        ticket = issue_ticket(
            "user-1",
            "resource",
            60,
            entity="item-1",
            sessionId="session-1",
        )
        self.assertEqual(
            read_ticket(ticket, "resource", {"entity": "item-1"})["uid"],
            "user-1",
        )
        with self.assertRaises(HTTPException):
            read_ticket(ticket, "resource", {"entity": "item-2"})

    def test_reserved_claims_and_excessive_ttl_are_rejected(self):
        with self.assertRaises(ValueError):
            issue_ticket("user-1", "resource", 60, uid="user-2")
        with self.assertRaises(ValueError):
            issue_ticket("user-1", "socket", 61)

    def test_malformed_ticket_is_rejected(self):
        for value in ("", "no-dot", "a.b.c", "a." + "x" * 5000):
            with self.subTest(value=value[:20]):
                with self.assertRaises(HTTPException):
                    read_ticket(value, "resource")


if __name__ == "__main__":
    unittest.main()
