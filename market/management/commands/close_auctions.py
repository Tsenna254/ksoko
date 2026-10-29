from django.core.management.base import BaseCommand
from django.utils import timezone

from market.models import Listing, Order


class Command(BaseCommand):
    help = "Close auctions that have passed their end time. Run every minute from cron."

    def handle(self, *args, **options):
        due = Listing.objects.filter(
            status=Listing.Status.ACTIVE, listing_type=Listing.Type.AUCTION, ends_at__lte=timezone.now()
        )
        count = 0
        for listing in due:
            top = listing.bids.first()  # highest bid first (see Bid.Meta.ordering)
            listing.status = Listing.Status.SOLD if top else Listing.Status.ENDED
            listing.buyer = top.bidder if top else None
            listing.save(update_fields=["status", "buyer", "updated_at"])
            if top:
                Order.objects.create(
                    listing=listing, buyer=top.bidder, seller=listing.seller, title=listing.title,
                    source=Order.Source.AUCTION, status=Order.Status.UNPAID, quantity=1, amount=top.amount, delivery_fee=listing.delivery_fee,
                )
            count += 1
        self.stdout.write(f"Closed {count} auction(s).")
