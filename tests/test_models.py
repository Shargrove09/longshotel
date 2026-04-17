"""Tests for the Pydantic models."""

from longshotel.models import Availability, Hotel


SAMPLE_HOTEL_DATA = {
    "longitude": -117.15988,
    "hqHotel": False,
    "hotelId": 23091,
    "images": {
        "main": {
            "thumbPath": "https://i.travelapi.com/thumb.jpg",
            "altText": "Primary image",
            "imagePath": "https://i.travelapi.com/full.jpg",
        }
    },
    "latitude": 32.713334,
    "name": "AC Hotel by Marriott San Diego",
    "isEcoCertified": False,
    "distance": 0.5649,
    "starRatingDecimal": 4,
    "amenities": [
        {"type": "Breakfast"},
        {"type": "Pool"},
        {"type": "Fitness Center"},
        {"type": "Parking"},
    ],
    "promotions": [],
    "hotelChain": "Marriott",
    "distanceUnits": "Miles",
    "hasPromo": False,
    "type": "EVENT",
    "starRating": 0,
    "avail": {
        "maxMultiBlockReservations": 0,
        "additionalFeesLong": "Resort fee includes wifi.",
        "isServiceFeeIncluded": True,
        "totalAdditionalFees": 12.01,
        "hotelId": 23091,
        "showInclusiveLowestAvgRate": True,
        "additionalFeesMessage": "This hotel includes a resort fee",
        "status": "SOLDOUT",
        "inclusiveLowestAvgRateNumeric": 355.01,
        "hotelGroupMax": 999999,
        "maxOneBlockReservations": 0,
        "groupMax": 2,
        "lowestAvgRateNumeric": 343,
        "roomsBooked": 0,
        "maxAllowed": 2,
    },
}


def test_hotel_parse() -> None:
    hotel = Hotel.model_validate(SAMPLE_HOTEL_DATA)
    assert hotel.hotel_id == 23091
    assert hotel.name == "AC Hotel by Marriott San Diego"
    assert hotel.hotel_chain == "Marriott"
    assert hotel.distance == 0.5649
    assert hotel.star_rating_decimal == 4
    assert len(hotel.amenities) == 4


def test_hotel_soldout_status() -> None:
    hotel = Hotel.model_validate(SAMPLE_HOTEL_DATA)
    assert hotel.status == "SOLDOUT"
    assert hotel.is_available is False


def test_hotel_available_status() -> None:
    data = {**SAMPLE_HOTEL_DATA, "avail": {**SAMPLE_HOTEL_DATA["avail"], "status": "AVAILABLE"}}
    hotel = Hotel.model_validate(data)
    assert hotel.is_available is True


def test_display_rate_inclusive() -> None:
    hotel = Hotel.model_validate(SAMPLE_HOTEL_DATA)
    assert hotel.display_rate == 355.01


def test_display_rate_non_inclusive() -> None:
    avail_data = {**SAMPLE_HOTEL_DATA["avail"], "showInclusiveLowestAvgRate": False}
    data = {**SAMPLE_HOTEL_DATA, "avail": avail_data}
    hotel = Hotel.model_validate(data)
    assert hotel.display_rate == 343


def test_amenity_list() -> None:
    hotel = Hotel.model_validate(SAMPLE_HOTEL_DATA)
    assert hotel.amenity_list == ["Breakfast", "Pool", "Fitness Center", "Parking"]


def test_availability_is_available() -> None:
    avail = Availability.model_validate(SAMPLE_HOTEL_DATA["avail"])
    assert avail.is_available is False

    avail2 = Availability.model_validate({**SAMPLE_HOTEL_DATA["avail"], "status": "OPEN"})
    assert avail2.is_available is True
