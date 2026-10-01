"""XMP sidecars sent with each upload.

Immich gives an uploaded XMP sidecar precedence over metadata embedded in the file (confirmed
against Immich 2.7.5), so the sidecar carries the resolved capture date with an explicit offset,
plus GPS and description when known. The media file itself is never modified.
"""

from xml.sax.saxutils import quoteattr

from googich_takeaway.takeout.dates import ResolvedDate


def build_xmp(
    date: ResolvedDate,
    gps: tuple[float, float] | None = None,
    description: str = "",
) -> bytes:
    attributes = {
        "exif:DateTimeOriginal": date.xmp_value(),
        "photoshop:DateCreated": date.xmp_value(),
        "xmp:CreateDate": date.xmp_value(),
    }
    if gps is not None:
        latitude, longitude = gps
        attributes["exif:GPSLatitude"] = _coordinate(latitude, "N", "S")
        attributes["exif:GPSLongitude"] = _coordinate(longitude, "E", "W")
    rendered = "".join(f"\n   {name}={quoteattr(value)}" for name, value in attributes.items())
    body = ""
    if description:
        escaped = description.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        body = (
            "\n   <dc:description><rdf:Alt>"
            f'<rdf:li xml:lang="x-default">{escaped}</rdf:li>'
            "</rdf:Alt></dc:description>\n  "
        )
    return (
        "<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>\n"
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '   xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
        '   xmlns:exif="http://ns.adobe.com/exif/1.0/"\n'
        '   xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"\n'
        '   xmlns:xmp="http://ns.adobe.com/xap/1.0/"'
        f"{rendered}>{body}</rdf:Description>\n"
        " </rdf:RDF>\n"
        "</x:xmpmeta>\n"
        "<?xpacket end='w'?>"
    ).encode()


def _coordinate(value: float, positive: str, negative: str) -> str:
    """XMP GPS format: ``DDD,MM.mmmmmmK``."""
    absolute = abs(value)
    degrees = int(absolute)
    minutes = (absolute - degrees) * 60
    return f"{degrees},{minutes:.6f}{positive if value >= 0 else negative}"
