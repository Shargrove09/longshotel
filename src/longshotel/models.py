"""Pydantic models that mirror the OnPeak Compass JSON response."""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── Nested models ────────────────────────────────────────────────────────────


class ImageDetail(BaseModel):
    thumb_path: str = Field(alias="thumbPath", default="")
    alt_text: str = Field(alias="altText", default="")
    image_path: str = Field(alias="imagePath", default="")


class HotelImages(BaseModel):
    main: ImageDetail | None = None


class Amenity(BaseModel):
    type: str


class Availability(BaseModel):
    hotel_id: int = Field(alias="hotelId")
    status: str
    lowest_avg_rate_numeric: float = Field(alias="lowestAvgRateNumeric", default=0)
    inclusive_lowest_avg_rate_numeric: float = Field(
        alias="inclusiveLowestAvgRateNumeric", default=0
    )
    total_additional_fees: float = Field(alias="totalAdditionalFees", default=0)
    additional_fees_message: str = Field(alias="additionalFeesMessage", default="")
    additional_fees_long: str = Field(alias="additionalFeesLong", default="")
    is_service_fee_included: bool = Field(alias="isServiceFeeIncluded", default=False)
    show_inclusive_lowest_avg_rate: bool = Field(
        alias="showInclusiveLowestAvgRate", default=False
    )
    rooms_booked: int = Field(alias="roomsBooked", default=0)
    max_allowed: int = Field(alias="maxAllowed", default=0)
    group_max: int = Field(alias="groupMax", default=0)
    hotel_group_max: int = Field(alias="hotelGroupMax", default=0)
    max_one_block_reservations: int = Field(
        alias="maxOneBlockReservations", default=0
    )
    max_multi_block_reservations: int = Field(
        alias="maxMultiBlockReservations", default=0
    )

    @property
    def is_available(self) -> bool:
        return self.status.upper() not in {"SOLDOUT", "SOLD_OUT", "UNAVAILABLE"}

    @property
    def display_rate(self) -> float:
        """Return the best rate to show the user."""
        if self.show_inclusive_lowest_avg_rate:
            return self.inclusive_lowest_avg_rate_numeric
        return self.lowest_avg_rate_numeric


# ── Main hotel model ─────────────────────────────────────────────────────────


class Hotel(BaseModel):
    hotel_id: int = Field(alias="hotelId")
    name: str
    hotel_chain: str = Field(alias="hotelChain", default="")
    latitude: float = 0
    longitude: float = 0
    distance: float = 0
    distance_units: str = Field(alias="distanceUnits", default="Miles")
    star_rating_decimal: float = Field(alias="starRatingDecimal", default=0)
    images: HotelImages = Field(default_factory=HotelImages)
    amenities: list[Amenity] = Field(default_factory=list)
    avail: Availability | None = None
    type: str = ""
    has_promo: bool = Field(alias="hasPromo", default=False)
    promotions: list = Field(default_factory=list)

    @property
    def is_available(self) -> bool:
        return self.avail is not None and self.avail.is_available

    @property
    def display_rate(self) -> float | None:
        if self.avail is None:
            return None
        return self.avail.display_rate

    @property
    def status(self) -> str:
        if self.avail is None:
            return "UNKNOWN"
        return self.avail.status

    @property
    def amenity_list(self) -> list[str]:
        return [a.type for a in self.amenities]
