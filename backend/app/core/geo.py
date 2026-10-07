"""Approximate coordinates for the metros datacentres cluster in.

A site's position is asset data: in production an administrator enters it, or it
comes from the site's street address. This table exists so a site that only has a
city still lands on the world map instead of nowhere - and every coordinate taken
from it is marked `city centroid`, so the map can say it is approximate. The
platform never geocodes over the network: an air-gapped DCIM must still draw its
map.

Keys are lower-case city names. Coordinates are city centres to ~0.01 degree,
which is the right precision for "which metro", and the wrong one for "which
building" - set the real figure with PUT /datacenters/{id}/location.
"""

from __future__ import annotations

METRO_CENTROIDS: dict[str, tuple[float, float]] = {
    # North America
    "ashburn": (39.04, -77.49), "atlanta": (33.75, -84.39), "boston": (42.36, -71.06),
    "chicago": (41.88, -87.63), "columbus": (39.96, -83.00), "dallas": (32.78, -96.80),
    "denver": (39.74, -104.99), "houston": (29.76, -95.37), "las vegas": (36.17, -115.14),
    "los angeles": (34.05, -118.24), "miami": (25.76, -80.19), "montreal": (45.50, -73.57),
    "new york": (40.71, -74.01), "newark": (40.74, -74.17), "phoenix": (33.45, -112.07),
    "portland": (45.52, -122.68), "reston": (38.96, -77.36), "san jose": (37.34, -121.89),
    "santa clara": (37.35, -121.96), "seattle": (47.61, -122.33), "toronto": (43.65, -79.38),
    "vancouver": (49.28, -123.12),
    # South America
    "bogota": (4.71, -74.07), "santiago": (-33.45, -70.67), "sao paulo": (-23.55, -46.63),
    "são paulo": (-23.55, -46.63),
    # Europe
    "amsterdam": (52.37, 4.90), "dublin": (53.35, -6.26), "frankfurt": (50.11, 8.68),
    "london": (51.51, -0.13), "madrid": (40.42, -3.70), "manchester": (53.48, -2.24),
    "marseille": (43.30, 5.37), "milan": (45.46, 9.19), "oslo": (59.91, 10.75),
    "paris": (48.86, 2.35), "stockholm": (59.33, 18.07), "warsaw": (52.23, 21.01),
    "zurich": (47.38, 8.54),
    # Middle East and Africa
    "cape town": (-33.92, 18.42), "dubai": (25.20, 55.27), "johannesburg": (-26.20, 28.05),
    "lagos": (6.52, 3.38), "riyadh": (24.71, 46.68), "tel aviv": (32.09, 34.78),
    # Asia Pacific
    "bangalore": (12.97, 77.59), "bengaluru": (12.97, 77.59), "chennai": (13.08, 80.27),
    "delhi": (28.70, 77.10), "hong kong": (22.32, 114.17), "hyderabad": (17.39, 78.49),
    "jakarta": (-6.21, 106.85), "jaipur": (26.91, 75.79), "kuala lumpur": (3.14, 101.69),
    "melbourne": (-37.81, 144.96), "mumbai": (19.08, 72.88), "new delhi": (28.61, 77.21),
    "noida": (28.54, 77.39), "osaka": (34.69, 135.50), "pune": (18.52, 73.86),
    "seoul": (37.57, 126.98), "shanghai": (31.23, 121.47), "singapore": (1.35, 103.82),
    "sydney": (-33.87, 151.21), "tokyo": (35.68, 139.69),
}


def city_centroid(city: str | None) -> tuple[float, float] | None:
    """(lat, lon) of a known metro, or None. Never a guess."""
    if not city:
        return None
    return METRO_CENTROIDS.get(city.strip().lower())
