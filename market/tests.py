"""Run with:  python manage.py test market"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from market import emailer
from market.models import OutboundEmail

S = OutboundEmail.Status


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", EMAIL_SEND_ASYNC=False, EMAIL_MAX_ATTEMPTS=2)
class EmailQueueTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("amina", "amina@example.com", "a-long-test-password-1")

    def queue(self, **extra):
        data = dict(kind="login_code", to_email="amina@example.com", subject="Code", text="Your code is 123456", user=self.user)
        data.update(extra)
        return emailer.queue_email(**data)

    def test_sends_and_wipes_the_message(self):
        with self.captureOnCommitCallbacks(execute=True):
            queued = self.queue()
        queued.refresh_from_db()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(queued.status, S.SENT)
        self.assertEqual(queued.body_text, "")          # the code is not kept after sending

    def test_failure_is_retried_then_given_up(self):
        queued = self.queue()
        with mock.patch.object(emailer.EmailMultiAlternatives, "send", side_effect=OSError("smtp down")):
            emailer.process_outbox()
            queued.refresh_from_db()
            self.assertEqual((queued.status, queued.attempts), (S.QUEUED, 1))
            self.assertGreater(queued.next_attempt_at, timezone.now())
            OutboundEmail.objects.filter(pk=queued.pk).update(next_attempt_at=timezone.now())
            emailer.process_outbox()
        queued.refresh_from_db()
        self.assertEqual(queued.status, S.FAILED)
        self.assertIn("smtp down", queued.last_error)

    def test_expired_code_is_never_sent(self):
        queued = self.queue(expires_at=timezone.now() - timedelta(minutes=1))
        emailer.process_outbox()
        queued.refresh_from_db()
        self.assertEqual(queued.status, S.EXPIRED)
        self.assertEqual(len(mail.outbox), 0)

    def test_newer_code_replaces_a_waiting_one(self):
        first = self.queue()
        self.queue(text="Your code is 654321")
        first.refresh_from_db()
        self.assertEqual(first.status, S.CANCELLED)

    def test_backoff_grows(self):
        self.assertEqual([emailer.retry_delay(n) for n in (1, 2, 3, 4, 9)], [30, 120, 300, 900, 900])

    def test_password_reset_uses_the_queue(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("password_reset"), {"email": "amina@example.com"})
        self.assertTrue(OutboundEmail.objects.filter(kind="password_reset", to_email="amina@example.com").exists())
