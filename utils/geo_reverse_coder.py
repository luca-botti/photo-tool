import time
import os
import json
import requests
import math
import atexit
from dataclasses import dataclass
from typing import List
from pydantic import BaseModel, ValidationError
from enum import Enum, auto

from utils.logger import Logger


class CoordinateRef(Enum):
    N = auto()
    E = auto()
    S = auto()
    W = auto()


def from_str_to_coordinate_ref(value: str) -> CoordinateRef | None:
    mapping = {
        "N": CoordinateRef.N,
        "E": CoordinateRef.E,
        "S": CoordinateRef.S,
        "W": CoordinateRef.W,
    }
    return mapping.get(value.upper(), None)


@dataclass
class GeoData(BaseModel):
    place_id: int | None = None
    osm_type: str | None = None
    osm_id: int | None = None
    boundingbox: List[str | None] | None = None
    lat: str | None = None
    lon: str | None = None
    display_name: str | None = None
    category: str | None = None
    type: str | None = None
    importance: float | None = None
    icon: str | None = None
    address: dict[str, str]
    extratags: dict[str, str] | None = None
    namedetails: dict[str, str] | None = None
    place_rank: int | None = None

    class Config:
        validate_by_name = True


class ReverseGeocoder:

    def __init__(
        self,
        resolution: float = 100.0,
        api_delay: float = 2.0,
        logger: Logger = Logger("ReverseGeocoder"),
        user_agent: str = "ReverseGeoCoder/1.0",
        cache_file: str = ".cache/geodata.json",
    ):
        self._cache: dict[str, GeoData] = {}
        self._resolution = resolution
        self._api_delay = api_delay if api_delay >= 1.0 else 2.0
        self._last_api_call: float | None = None
        self._logger = logger
        self._user_agent = (
            user_agent if user_agent and user_agent != "" else "ReverseGeoCoder/1.0"
        )
        self._cache_file = cache_file
        self._load_cache()
        atexit.register(self._save_cache)

    def approximate_location(
        self, latitude: float, longitude: float, accuracy_km2: float
    ) -> tuple[float, float]:
        """
        Approximate a lat/lon coordinate to the requested accuracy (in km²).

        Args:
            latitude (float): Latitude in degrees.
            longitude (float): Longitude in degrees.
            accuracy_km2 (float): Desired accuracy in square kilometers.

        Returns:
            tuple[float, float]: Approximated (latitude, longitude).
        """
        # Side of square that corresponds to the requested accuracy
        side_km = math.sqrt(accuracy_km2)

        # Convert km to degrees
        deg_lat = side_km / 111.0  # 1° latitude ≈ 111 km
        deg_lon = side_km / (111.0 * math.cos(math.radians(latitude)))

        # Round latitude and longitude to nearest grid point
        approx_lat = round(latitude / deg_lat) * deg_lat
        approx_lon = round(longitude / deg_lon) * deg_lon

        return approx_lat, approx_lon

    def _create_key(self, lat: float, lon: float) -> str:
        """Create a cache key for the given latitude and longitude."""
        latitude, longitude = self.approximate_location(lat, lon, self._resolution)
        return f"{latitude},{longitude}"

    def _load_cache(self):
        """Load cache from file."""
        os.makedirs(os.path.dirname(self._cache_file), exist_ok=True)
        if os.path.exists(self._cache_file):
            with open(self._cache_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
                self._cache = {k: GeoData.model_validate(v) for k, v in raw.items()}
        self._cache_dirty = False

    def _save_cache(self):
        """Save cache to file."""
        if self._cache_dirty:
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(
                    {k: v.model_dump(by_alias=True) for k, v in self._cache.items()},
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
            self._cache_dirty = False

    def convert_gps_to_degrees(
        self, gps_coord: tuple[float, float, float], gps_ref: CoordinateRef | str
    ) -> float | None:
        """Convert GPS coordinates from gps format to decimal degrees."""
        if not isinstance(gps_ref, CoordinateRef):
            temp = from_str_to_coordinate_ref(gps_ref)
            if not temp:
                return None
            gps_ref = temp

        degrees = gps_coord[0]
        minutes = gps_coord[1]
        seconds = gps_coord[2]

        decimal = degrees + (minutes / 60.0) + (seconds / 3600.0)

        if gps_ref in [CoordinateRef.S, CoordinateRef.W]:
            decimal = -decimal

        if gps_ref in [CoordinateRef.N, CoordinateRef.S]:
            # latitude
            if not (-90 <= decimal <= 90):
                self._logger.error(f"Invalid latitude: {decimal}")
                return None
        elif gps_ref in [CoordinateRef.E, CoordinateRef.W]:
            # longitude
            if not (-180 <= decimal <= 180):
                self._logger.error(f"Invalid longitude: {decimal}")
                return None

        return decimal

    def get_location_from_gps(
        self,
        lat_gps_coord: tuple[float, float, float],
        lat_gps_ref: str,
        lon_gps_coord: tuple[float, float, float],
        lon_gps_ref: str,
    ) -> GeoData | None:
        lat = self.convert_gps_to_degrees(lat_gps_coord, lat_gps_ref)
        if lat is None:
            self._logger.error(
                f"Invalid GPS coordinates for latitude: {lat_gps_coord}, {lat_gps_ref}"
            )
            return None

        lon = self.convert_gps_to_degrees(lon_gps_coord, lon_gps_ref)
        if lon is None:
            self._logger.error(
                f"Invalid GPS coordinates for longitude: {lon_gps_coord}, {lon_gps_ref}"
            )
            return None

        return self.get_location_from_lat_lon(lat, lon)

    def get_location_from_lat_lon(self, lat: float, lon: float) -> GeoData | None:
        """Get location information from latitude and longitude."""
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            self._logger.error(f"Invalid coordinates: lat={lat}, lon={lon}")
            return None

        # Use reverse geocoding service to get location
        return self._reverse_geocode(lat, lon)

    def _reverse_geocode(self, lat: float, lon: float) -> GeoData | None:
        """Convert coordinates to location name using Nominatim (OpenStreetMap)."""
        coord_key = self._create_key(lat, lon)
        # Check cache first
        if coord_key in self._cache:
            self._logger.debug(
                f"Cache hit for coordinates: [{lat}, {lon}] -> {coord_key}"
            )
            return self._cache[coord_key]

        self._logger.debug(f"Cache miss for coordinates: [{lat}, {lon}] -> {coord_key}")
        # Rate limiting
        if self._last_api_call is not None:
            elapsed = time.time() - self._last_api_call
            if elapsed < self._api_delay:
                self._logger.warning(
                    f"Rate limit exceeded, waiting {self._api_delay - elapsed:.2f} seconds"
                )
                time.sleep(self._api_delay - elapsed)

        response: requests.Response | None = None

        try:
            # Using Nominatim (free, no API key required)
            url = "https://nominatim.openstreetmap.org/reverse"
            params: dict[str, str] = {
                "lat": str(lat),
                "lon": str(lon),
                "format": "jsonv2",
                "addressdetails": "1",
                "accept-language": "en",
            }

            headers = {"User-Agent": self._user_agent}  # Required by Nominatim

            response = requests.get(url, params=params, headers=headers, timeout=10)
            self._last_api_call = time.time()

            if response.status_code == 200:
                self._logger.trace(
                    f"Reverse geocoding successful response for coordinates: [{lat}, {lon}] -> \n{response.json()}"
                )
                self._cache[coord_key] = GeoData.model_validate(response.json())
                self._logger.trace(
                    f"Reverse geocoding successful for coordinates: [{lat}, {lon}] -> \n{self._cache[coord_key]}"
                )
                self._cache_dirty = True
                return self._cache[coord_key]
            else:
                self._logger.error(
                    f"Reverse geocoding failed for coordinates: [{lat}, {lon}], error: {response.status_code}:{response.text}"
                )
        except ValidationError as e:
            self._logger.error(
                f"Response content: \n{response.json() if response and response.status_code == 200 else 'No response'}"
            )
            self._logger.error(f"Validation error during reverse geocoding: {e}")
        except Exception as e:
            self._logger.error(f"Error during reverse geocoding: {e}")
        return None
