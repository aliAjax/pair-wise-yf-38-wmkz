import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class GrantLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.committee = Actor("committee-1", "committee")

    def tearDown(self):
        self.tmp.cleanup()

    def _approved_application(self, applicant="APP-1", purpose="variant analysis",
                              cutoff="2099-01-01"):
        dataset = self.service.create(
            self.admin, "dataset", {"name": "Cohort", "access_policy": "controlled"}
        )
        application = self.service.create(
            self.admin,
            "application",
            {"dataset_id": dataset["id"], "applicant_id": applicant, "purpose": purpose},
        )
        self.service.transition(self.admin, application["id"], "submit", {})
        self.service.transition(
            self.admin, application["id"], "review", {"committee_id": "committee-a"}
        )
        application = self.service.transition(
            self.committee,
            application["id"],
            "approve",
            {"approvals": ["r1", "r2", "r3"], "terms": "noncommercial",
             "expires_at": cutoff},
        )
        return dataset, application

    def _grant(self, application, **overrides):
        data = {
            "application_id": overrides.pop("application_id", application["id"]),
            "dataset_id": application["data"]["dataset_id"],
            "recipient": application["data"]["applicant_id"],
            "purpose": application["data"]["purpose"],
        }
        data.update(overrides)
        return self.service.create(self.admin, "grant", data)

    def _active_grant(self, application, starts="2026-09-24", expires="2027-01-01"):
        grant = self._grant(application)
        return self.service.transition(
            self.admin,
            grant["id"],
            "activate",
            {"starts_at": starts, "expires_at": expires},
        )

    def test_activate_requires_approved_application(self):
        dataset = self.service.create(
            self.admin, "dataset", {"name": "Cohort", "access_policy": "controlled"}
        )
        application = self.service.create(
            self.admin,
            "application",
            {"dataset_id": dataset["id"], "applicant_id": "APP-1",
             "purpose": "variant analysis"},
        )
        self.service.transition(self.admin, application["id"], "submit", {})
        grant = self._grant(application)
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin,
                grant["id"],
                "activate",
                {"starts_at": "2026-09-24", "expires_at": "2027-01-01"},
            )

    def test_activate_checks_recipient_dataset_and_purpose(self):
        dataset, application = self._approved_application()
        other_dataset = self.service.create(
            self.admin, "dataset", {"name": "Other", "access_policy": "controlled"}
        )
        with self.assertRaises(ValidationError):
            self._grant(application, recipient="SOMEONE-ELSE")
        with self.assertRaises(ValidationError):
            self._grant(application, dataset_id=other_dataset["id"])
        with self.assertRaises(ValidationError):
            self._grant(application, purpose="unrelated purpose")

    def test_activate_cannot_exceed_approved_cutoff(self):
        _, application = self._approved_application(cutoff="2027-06-30")
        grant = self._grant(application)
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin,
                grant["id"],
                "activate",
                {"starts_at": "2026-09-24", "expires_at": "2027-07-01"},
            )

    def test_only_one_active_grant_per_application(self):
        _, application = self._approved_application()
        self._active_grant(application)
        second = self._grant(application)
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin,
                second["id"],
                "activate",
                {"starts_at": "2026-09-24", "expires_at": "2027-01-01"},
            )

    def test_renew_within_original_cutoff(self):
        _, application = self._approved_application(cutoff="2027-06-30")
        grant = self._active_grant(application, expires="2027-01-01")
        renewed = self.service.transition(
            self.admin, grant["id"], "renew", {"new_expires_at": "2027-06-30"}
        )
        self.assertEqual(renewed["status"], "active")
        self.assertEqual(renewed["data"]["expires_at"], "2027-06-30")

    def test_renew_beyond_original_cutoff_needs_committee_reapproval(self):
        _, application = self._approved_application(cutoff="2027-06-30")
        grant = self._active_grant(application, expires="2027-01-01")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, grant["id"], "renew", {"new_expires_at": "2028-01-01"}
            )
        # 委员会重新批准，把审批截止延长到 2028-06-30
        application = self.service.transition(
            self.committee,
            application["id"],
            "reapprove",
            {"approvals": ["r1", "r2", "r4"], "expires_at": "2028-06-30"},
        )
        self.assertEqual(application["status"], "approved")
        self.assertEqual(application["data"]["original_expires_at"], "2027-06-30")
        renewed = self.service.transition(
            self.admin, grant["id"], "renew", {"new_expires_at": "2028-01-01"}
        )
        self.assertEqual(renewed["data"]["expires_at"], "2028-01-01")
        self.assertTrue(renewed["data"]["beyond_original_approval"])

    def test_expired_grant_cannot_be_renewed(self):
        _, application = self._approved_application()
        grant = self._active_grant(application, expires="2027-01-01")
        self.service.transition(
            self.admin, grant["id"], "expire", {"expired_at": "2027-01-02"}
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin, grant["id"], "renew", {"new_expires_at": "2027-06-01"}
            )

    def test_suspend_freezes_grants_and_resume_restores_unexpired(self):
        _, application = self._approved_application()
        grant = self._active_grant(application, expires="2099-01-01")
        self.service.transition(
            self.committee, application["id"], "suspend", {"reason": "compliance check"}
        )
        frozen = self.service.get(grant["id"])
        self.assertEqual(frozen["status"], "frozen")
        self.assertEqual(frozen["data"]["frozen_by"], "committee-1")
        # 冻结中的凭证不能续期或撤销
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin, grant["id"], "renew", {"new_expires_at": "2099-06-01"}
            )
        self.service.transition(self.committee, application["id"], "resume", {})
        restored = self.service.get(grant["id"])
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["data"]["unfrozen_by"], "committee-1")

    def test_resume_expires_grants_past_their_window(self):
        _, application = self._approved_application()
        grant = self._active_grant(
            application, starts="2026-01-01", expires="2026-06-01"
        )
        self.service.transition(
            self.committee, application["id"], "suspend", {"reason": "audit"}
        )
        self.service.transition(self.committee, application["id"], "resume", {})
        expired = self.service.get(grant["id"])
        self.assertEqual(expired["status"], "expired")

    def test_renewal_and_freeze_records_visible_in_audit(self):
        _, application = self._approved_application(cutoff="2027-06-30")
        grant = self._active_grant(application, expires="2027-01-01")
        self.service.transition(
            self.admin, grant["id"], "renew", {"new_expires_at": "2027-03-01"}
        )
        self.service.transition(
            self.committee, application["id"], "suspend", {"reason": "audit"}
        )
        self.service.transition(self.committee, application["id"], "resume", {})
        actions = [
            entry["action"] for entry in self.service.audit_log(entity_id=grant["id"])
        ]
        self.assertEqual(
            actions, ["create", "activate", "renew", "freeze", "unfreeze"]
        )


if __name__ == "__main__":
    unittest.main()
