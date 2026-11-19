"""ORM models for fuel stations."""

from django.db import models


class Station(models.Model):
    """A US truck stop with a retail diesel/fuel price and city-centroid coords."""

    opis_id = models.PositiveIntegerField(unique=True, db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=512, blank=True)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2, db_index=True)
    price = models.DecimalField(max_digits=8, decimal_places=4)
    lat = models.FloatField()
    lng = models.FloatField()

    class Meta:
        indexes = [
            models.Index(fields=["lat", "lng"]),
            models.Index(fields=["state", "city"]),
        ]
        ordering = ["opis_id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) @ ${self.price}"
