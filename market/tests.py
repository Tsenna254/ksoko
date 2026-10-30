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


class PayOnDeliveryTests(TestCase):
    def setUp(self):
        from decimal import Decimal
        from market import cod
        from market.models import Category, Listing
        self.cod, self.D = cod, Decimal
        User = get_user_model()
        self.seller = User.objects.create_user("seller", "seller@example.com", "a-long-test-password-1")
        self.buyer = User.objects.create_user("buyer", "buyer@example.com", "a-long-test-password-1")
        self.staff = User.objects.create_user("boss", "boss@example.com", "a-long-test-password-1", is_staff=True)
        self.cat = Category.objects.create(name="Phones")
        self.item = Listing.objects.create(
            seller=self.seller, category=self.cat, title="Phone", description="A good phone, barely used." * 2,
            price=Decimal("10000"), quantity=3, allow_cod=True)

    def pay_fee(self, listing):
        from market.models import Payment
        fee = self.cod.sync_cod_fee(listing)
        payment = Payment.objects.create(user=listing.seller, purpose="listing_fee", method="mpesa", amount=fee.amount)
        fee.payment = payment
        fee.save()
        payment.mark_paid(f"QGH7ABC{payment.pk}")
        payment.confirm(self.staff)

    def fresh(self, listing):
        from market.models import Listing
        return Listing.objects.get(pk=listing.pk)

    def test_fee_is_ten_percent_of_price_times_quantity(self):
        fee = self.cod.sync_cod_fee(self.item)
        self.assertEqual(fee.amount, self.D("3000"))             # 10% of 10,000 x 3
        self.assertFalse(self.fresh(self.item).cod_active)       # not on until the fee is confirmed

    def test_pay_on_delivery_switches_on_after_the_fee_is_confirmed(self):
        self.pay_fee(self.item)
        self.assertTrue(self.fresh(self.item).cod_active)

    def test_raising_the_price_needs_a_top_up_fee(self):
        self.pay_fee(self.item)
        self.item.price = self.D("12000")
        self.item.save()
        top_up = self.cod.sync_cod_fee(self.item)
        self.assertEqual(top_up.amount, self.D("600"))           # 10% of the extra 6,000
        self.assertFalse(self.fresh(self.item).cod_active)

    def test_cod_checkout_splits_by_seller_and_delivery_completes_after_staff_confirm(self):
        from market.models import CartItem, Category, Listing, Order, Payment
        from market.payments import create_payments
        other = get_user_model().objects.create_user("seller2", "s2@example.com", "a-long-test-password-1")
        second = Listing.objects.create(seller=other, category=self.cat, title="Case", description="A sturdy phone case." * 3,
                                        price=self.D("1000"), quantity=1, allow_cod=True)
        self.pay_fee(self.item)
        self.pay_fee(second)
        CartItem.objects.create(user=self.buyer, listing=self.fresh(self.item), quantity=1)
        CartItem.objects.create(user=self.buyer, listing=self.fresh(second), quantity=1)
        payments = create_payments(self.buyer, "cod", "", "254712345678", "Westlands, Nairobi, house 5")
        self.assertEqual(len(payments), 2)                       # one payment per seller
        self.assertTrue(all(p.status == Payment.Status.COD_PENDING for p in payments))
        self.assertEqual(self.fresh(self.item).quantity, 2)      # stock reserved
        payment = payments[0]
        self.assertTrue(payment.mark_cod_delivered())            # seller: delivered, cash collected
        self.assertTrue(payment.confirm(self.staff))             # staff confirm
        self.assertEqual(payment.orders.first().status, Order.Status.COMPLETED)

    def test_repeated_failed_deliveries_switch_pay_on_delivery_off(self):
        from market.cart import cart_summary
        from market.models import CartItem, Payment
        from market.payments import cod_blocker
        self.pay_fee(self.item)
        for _ in range(2):
            Payment.objects.create(user=self.buyer, method="cod", status="rejected", amount=self.D("500"))
        CartItem.objects.create(user=self.buyer, listing=self.fresh(self.item), quantity=1)
        self.assertIn("switched off", cod_blocker(self.buyer, cart_summary(self.buyer)))
