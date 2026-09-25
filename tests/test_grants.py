import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class GrantFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.committee = Actor("committee-1", "committee")

    def tearDown(self):
        self.tmp.cleanup()

    def _approved_application(self, expires_at="2099-01-01", purpose="variant analysis"):
        dataset = self.service.create(
            self.admin, "dataset", {"name": "D", "access_policy": "controlled"}
        )
        application = self.service.create(
            self.admin,
            "application",
            {
                "dataset_id": dataset["id"],
                "applicant_id": "researcher-1",
                "purpose": purpose,
            },
        )
        self.service.transition(self.admin, application["id"], "submit", {})
        self.service.transition(
            self.admin, application["id"], "review", {"committee_id": "c1"}
        )
        self.service.transition(
            self.committee,
            application["id"],
            "approve",
            {
                "approvals": ["r1", "r2", "r3"],
                "terms": "noncommercial",
                "expires_at": expires_at,
            },
        )
        return dataset, self.service.get(application["id"])

    def _create_grant(self, application, **overrides):
        data = {
            "application_id": application["id"],
            "dataset_id": application["data"]["dataset_id"],
            "recipient": application["data"]["applicant_id"],
            "purpose": application["data"]["purpose"],
        }
        data.update(overrides)
        return self.service.create(self.admin, "grant", data)

    def _activate(self, grant, starts_at="2026-01-01", expires_at="2027-01-01"):
        return self.service.transition(
            self.admin,
            grant["id"],
            "activate",
            {"starts_at": starts_at, "expires_at": expires_at},
        )

    def test_grant_requires_existing_application(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "grant",
                {
                    "application_id": "missing",
                    "dataset_id": "d",
                    "recipient": "r",
                    "purpose": "p",
                },
            )

    def test_activate_requires_approved_application(self):
        dataset = self.service.create(
            self.admin, "dataset", {"name": "D", "access_policy": "controlled"}
        )
        application = self.service.create(
            self.admin,
            "application",
            {
                "dataset_id": dataset["id"],
                "applicant_id": "researcher-1",
                "purpose": "variant analysis",
            },
        )
        grant = self._create_grant(application)
        with self.assertRaises(InvalidTransition):
            self._activate(grant)

    def test_activate_checks_recipient_dataset_and_purpose(self):
        _, application = self._approved_application()
        other_dataset = self.service.create(
            self.admin, "dataset", {"name": "D2", "access_policy": "controlled"}
        )
        for overrides in (
            {"recipient": "someone-else"},
            {"dataset_id": other_dataset["id"]},
            {"purpose": "other purpose"},
        ):
            grant = self._create_grant(application, **overrides)
            with self.assertRaises(ValidationError):
                self._activate(grant)

    def test_activate_rejects_expiry_beyond_approval(self):
        _, application = self._approved_application(expires_at="2027-06-30")
        grant = self._create_grant(application)
        with self.assertRaises(ValidationError):
            self._activate(grant, expires_at="2028-01-01")

    def test_only_one_live_grant_per_application(self):
        _, application = self._approved_application()
        first = self._activate(self._create_grant(application))
        self.assertEqual(first["status"], "active")
        second = self._create_grant(application)
        with self.assertRaises(ConflictError):
            self._activate(second)

    def test_renew_within_approval_window(self):
        _, application = self._approved_application()
        grant = self._activate(self._create_grant(application))
        renewed = self.service.transition(
            self.admin, grant["id"], "renew", {"expires_at": "2028-01-01"}
        )
        self.assertEqual(renewed["status"], "active")
        self.assertEqual(renewed["data"]["expires_at"], "2028-01-01")
        record = renewed["data"]["renewals"][-1]
        self.assertEqual(record["status"], "approved")
        self.assertEqual(record["from"], "2027-01-01")
        self.assertEqual(record["to"], "2028-01-01")

    def test_renew_beyond_approval_requires_committee(self):
        _, application = self._approved_application(expires_at="2027-06-30")
        grant = self._activate(self._create_grant(application))
        applicant = Actor("researcher-1", "applicant")
        pending = self.service.transition(
            applicant, grant["id"], "renew", {"expires_at": "2028-01-01"}
        )
        self.assertEqual(pending["status"], "renewal_pending")
        # 委员会批准前保留原期限
        self.assertEqual(pending["data"]["expires_at"], "2027-01-01")
        self.assertEqual(pending["data"]["renewals"][-1]["status"], "pending")
        with self.assertRaises(PermissionDenied):
            self.service.transition(applicant, grant["id"], "approve_renewal", {})
        approved = self.service.transition(
            self.committee, grant["id"], "approve_renewal", {}
        )
        self.assertEqual(approved["status"], "active")
        self.assertEqual(approved["data"]["expires_at"], "2028-01-01")
        record = approved["data"]["renewals"][-1]
        self.assertEqual(record["status"], "approved")
        self.assertEqual(record["decided_by"], "committee-1")

    def test_reject_renewal_keeps_original_expiry(self):
        _, application = self._approved_application(expires_at="2027-06-30")
        grant = self._activate(self._create_grant(application))
        self.service.transition(
            self.admin, grant["id"], "renew", {"expires_at": "2028-01-01"}
        )
        rejected = self.service.transition(
            self.committee, grant["id"], "reject_renewal", {"reason": "out of scope"}
        )
        self.assertEqual(rejected["status"], "active")
        self.assertEqual(rejected["data"]["expires_at"], "2027-01-01")
        record = rejected["data"]["renewals"][-1]
        self.assertEqual(record["status"], "rejected")
        self.assertEqual(record["reason"], "out of scope")

    def test_renew_after_expiry_is_rejected(self):
        _, application = self._approved_application()
        grant = self._activate(
            self._create_grant(application),
            starts_at="2020-01-01",
            expires_at="2021-01-01",
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin, grant["id"], "renew", {"expires_at": "2022-01-01"}
            )

    def test_renew_blocked_while_application_suspended(self):
        _, application = self._approved_application()
        grant = self._activate(self._create_grant(application))
        self.service.transition(
            self.committee, application["id"], "suspend", {"reason": "audit"}
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin, grant["id"], "renew", {"expires_at": "2028-01-01"}
            )

    def test_suspend_freezes_and_resume_restores_only_live_grants(self):
        _, application = self._approved_application()
        stale = self._activate(
            self._create_grant(application),
            starts_at="2020-01-01",
            expires_at="2021-01-01",
        )
        self.service.transition(
            self.committee, application["id"], "suspend", {"reason": "audit"}
        )
        self.assertEqual(self.service.get(stale["id"])["status"], "frozen")
        self.service.transition(self.committee, application["id"], "resume", {})
        # 已过期的凭证不放回，直接标记过期
        self.assertEqual(self.service.get(stale["id"])["status"], "expired")

        live = self._activate(
            self._create_grant(application), expires_at="2099-01-01"
        )
        self.service.transition(
            self.committee, application["id"], "suspend", {"reason": "audit again"}
        )
        self.assertEqual(self.service.get(live["id"])["status"], "frozen")
        self.service.transition(self.committee, application["id"], "resume", {})
        live = self.service.get(live["id"])
        self.assertEqual(live["status"], "active")
        events = [entry["event"] for entry in live["data"]["freezes"]]
        self.assertEqual(events, ["frozen", "unfrozen"])

    def test_unfreeze_expired_grant_is_rejected(self):
        _, application = self._approved_application()
        grant = self._activate(
            self._create_grant(application),
            starts_at="2020-01-01",
            expires_at="2021-01-01",
        )
        self.service.transition(
            self.admin, grant["id"], "freeze", {"reason": "manual"}
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.admin, grant["id"], "unfreeze", {})


if __name__ == "__main__":
    unittest.main()
