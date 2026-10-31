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


class SubcategoryTests(TestCase):
    def setUp(self):
        from market.models import Category
        self.Category = Category
        self.phones = Category.objects.create(name="Phones")

    def test_sub_category_gets_its_own_slug_and_full_name(self):
        sub = self.Category.objects.create(name="Smartphones", parent=self.phones)
        self.assertEqual(sub.slug, "phones-smartphones")
        self.assertEqual(str(sub), "Phones \u203a Smartphones")

    def test_same_sub_category_name_can_sit_under_different_parents(self):
        fashion = self.Category.objects.create(name="Fashion")
        self.Category.objects.create(name="Accessories", parent=self.phones)
        other = self.Category.objects.create(name="Accessories", parent=fashion)
        self.assertNotEqual(other.slug, "phones-accessories")

    def test_only_two_levels_are_allowed(self):
        from django.core.exceptions import ValidationError
        sub = self.Category.objects.create(name="Smartphones", parent=self.phones)
        with self.assertRaises(ValidationError):
            self.Category(name="Budget", parent=sub).full_clean()

    def test_duplicate_names_are_rejected_politely(self):
        from django.core.exceptions import ValidationError
        self.Category.objects.create(name="Smartphones", parent=self.phones)
        with self.assertRaises(ValidationError):
            self.Category(name="smartphones", parent=self.phones).full_clean()

    def test_searching_a_main_category_includes_its_sub_categories(self):
        from decimal import Decimal
        from market.models import Listing
        seller = get_user_model().objects.create_user("seller", "s@example.com", "a-long-test-password-1")
        sub = self.Category.objects.create(name="Smartphones", parent=self.phones)
        Listing.objects.create(seller=seller, category=sub, title="Budget phone", description="A decent phone for everyday use.", price=Decimal("9000"))
        self.assertIn(sub.pk, self.phones.family_ids())
        response = self.client.get(reverse("listing_list") + "?category=phones")
        self.assertContains(response, "Budget phone")


class FilterAndSortTests(TestCase):
    def setUp(self):
        from decimal import Decimal
        from market.models import Category, FilterOption, FilterParameter, Listing, ListingAttribute
        self.D = Decimal
        seller = get_user_model().objects.create_user("seller", "s@example.com", "a-long-test-password-1")
        phones = Category.objects.create(name="Phones")
        shoes = Category.objects.create(name="Shoes")
        self.brand = FilterParameter.objects.create(name="Brand", kind="choice")
        self.brand.categories.add(phones)
        self.samsung = FilterOption.objects.create(parameter=self.brand, value="Samsung")
        self.apple = FilterOption.objects.create(parameter=self.brand, value="Apple")
        self.storage = FilterParameter.objects.create(name="Storage", kind="number", unit="GB")
        self.storage.categories.add(phones)
        make = lambda title, cat, price, **kw: Listing.objects.create(
            seller=seller, category=cat, title=title, description="A listing description long enough.", price=Decimal(price), **kw)
        self.a = make("Galaxy", phones, "30000")
        self.b = make("iPhone", phones, "90000")
        self.c = make("Sneakers", shoes, "5000", condition="used")
        ListingAttribute.objects.create(listing=self.a, parameter=self.brand, option=self.samsung)
        ListingAttribute.objects.create(listing=self.b, parameter=self.brand, option=self.apple)
        ListingAttribute.objects.create(listing=self.a, parameter=self.storage, number=Decimal("128"))
        ListingAttribute.objects.create(listing=self.b, parameter=self.storage, number=Decimal("512"))

    def titles(self, query):
        response = self.client.get(reverse("listing_list") + query)
        self.assertEqual(response.status_code, 200)
        return [l.title for l in response.context["page"].object_list]

    def test_admin_defined_choice_filter(self):
        self.assertEqual(self.titles(f"?category=phones&p{self.brand.pk}={self.samsung.pk}"), ["Galaxy"])

    def test_number_range_filter(self):
        self.assertEqual(self.titles(f"?category=phones&p{self.storage.pk}_min=256"), ["iPhone"])

    def test_built_in_filters(self):
        self.assertEqual(self.titles("?min_price=10000&max_price=50000"), ["Galaxy"])
        self.assertEqual(self.titles("?cond=used"), ["Sneakers"])

    def test_sort_menu_orders_by_price(self):
        self.assertEqual(self.titles("?sort=low"), ["Sneakers", "Galaxy", "iPhone"])
        self.assertEqual(self.titles("?sort=high"), ["iPhone", "Galaxy", "Sneakers"])

    def test_filter_menu_only_offers_parameters_for_the_browsed_category(self):
        phones = self.client.get(reverse("listing_list") + "?category=phones")
        self.assertEqual([s["param"].name for s in phones.context["filter_sections"]], ["Brand", "Storage"])
        shoes = self.client.get(reverse("listing_list") + "?category=shoes")
        self.assertEqual(shoes.context["filter_sections"], [])

    def test_upload_form_shows_fields_for_the_right_category_only(self):
        from market.forms import ListingForm
        form = ListingForm()
        phones_id = str(self.a.category_id)
        shoes_id = str(self.c.category_id)
        self.assertIn(self.brand.pk, form.param_map[phones_id])
        self.assertNotIn(self.brand.pk, form.param_map[shoes_id])

    def test_a_required_parameter_must_be_filled_in(self):
        from market.forms import ListingForm
        self.brand.required = True
        self.brand.save()
        form = ListingForm(data={"title": "Test phone", "category": self.a.category_id})
        form.is_valid()
        self.assertIn(f"param_{self.brand.pk}", form.errors)
