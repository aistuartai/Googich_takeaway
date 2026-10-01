import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from googich_takeaway.destinations.xmp import build_xmp
from googich_takeaway.takeout.dates import DateSource, OffsetSource, ResolvedDate

DATE = ResolvedDate(
    datetime(2019, 7, 4, 10, 15), timedelta(hours=10), DateSource.EXIF, OffsetSource.GPS
)
NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def description_of(xmp: bytes) -> ET.Element:
    root = ET.fromstring(xmp.decode().split("?>", 1)[1].rsplit("<?xpacket", 1)[0])  # noqa: S314
    found = root.find(".//rdf:Description", NS)
    assert found is not None
    return found


def test_date_with_offset() -> None:
    node = description_of(build_xmp(DATE))
    assert node.get(f"{{{NS['exif']}}}DateTimeOriginal") == "2019-07-04T10:15:00+10:00"


def test_gps_southern_and_western_hemispheres() -> None:
    node = description_of(build_xmp(DATE, gps=(-33.8568, -151.2153)))
    assert node.get(f"{{{NS['exif']}}}GPSLatitude") == "33,51.408000S"
    assert node.get(f"{{{NS['exif']}}}GPSLongitude") == "151,12.918000W"


def test_description_is_escaped() -> None:
    text = 'Fish & chips <at> "the" pier'
    node = description_of(build_xmp(DATE, description=text))
    item = node.find("dc:description/rdf:Alt/rdf:li", NS)
    assert item is not None
    assert item.text == text


def test_no_gps_or_description_by_default() -> None:
    xmp = build_xmp(DATE).decode()
    assert "GPSLatitude" not in xmp
    assert "dc:description>" not in xmp
