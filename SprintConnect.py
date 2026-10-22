# ============================================================
# SprintConnect
# 1. Systeminitialisierung: Import der Bibliotheken
# ============================================================

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import logging
import threading
import unicodedata
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

try:
    import pandas as pd
except ModuleNotFoundError:
    pd = None


def normalize_stop_text(value: str) -> str:
    """Normalize station names for tolerant user input matching."""
    text = str(value).casefold().strip()
    text = text.replace("ue", "u").replace("oe", "o").replace("ae", "a")
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(char for char in normalized if not unicodedata.combining(char))


def format_gtfs_time(seconds_after_midnight: int) -> str:
    """Format GTFS seconds after midnight as HH:MM."""
    hours = seconds_after_midnight // 3600
    minutes = (seconds_after_midnight % 3600) // 60
    return f"{hours:02d}:{minutes:02d}"


# ============================================================
# 2. Konfigurationsparameter
# ============================================================

APP_NAME = "SprintConnect"

# Concrete GTFS ZIP used for reproducible project results.
GTFS_ZIP_URL = (
    "https://data.opentransportdata.swiss/dataset/"
    "3d2c18f9-9ef1-463f-a249-5c67604efd74/resource/"
    "ee177cd0-df61-4bf4-a47f-95fc7ae577e2/download/"
    "gtfs_fp2026_20260422.zip"
)

# Permalink to the currently published GTFS resource. This can be used later if
# the application should always fetch the newest available timetable export.
GTFS_CURRENT_RESOURCE_URL = "https://data.opentransportdata.swiss/en/dataset/timetable-2026-gtfs2020/permalink"

# Local cache paths. The downloaded ZIP is stored here so the application can
# run faster and still work if the internet connection is unavailable.
BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
ICONS_DIR = ASSETS_DIR / "icons"
BUNNY_ICON_PATH = ICONS_DIR / "bunny.png"
KANGAROO_ICON_PATH = ICONS_DIR / "kangaroo.png"
SPRINTCONNECT_ICON_PATH = ICONS_DIR / "sprintconnect.png"
CACHE_DIR = BASE_DIR / "data_cache"
GTFS_ZIP_PATH = CACHE_DIR / "gtfs.zip"
GTFS_EXTRACT_DIR = CACHE_DIR / "gtfs"
GTFS_METADATA_PATH = CACHE_DIR / "gtfs_metadata.json"
GTFS_STOP_TIMES_SCENARIO_CACHE_DIR = CACHE_DIR / "stop_times_by_scenario"

# GTFS files required for the first prototype.
REQUIRED_GTFS_FILES = (
    "stops.txt",
    "stop_times.txt",
    "trips.txt",
    "routes.txt",
    "transfers.txt",
)

# Transfer risk thresholds in seconds.
COMFORTABLE_TRANSFER_BUFFER = 120
CRITICAL_TRANSFER_BUFFER = 0

# Field assumptions for sprinter transfer modelling.
STANDARD_WALKING_SPEED = 1.3
BUNNY_WALKING_SPEED = 1.8
KANGAROO_WALKING_SPEED = 2.2
STANDARD_REACTION_DELAY = 15
BUNNY_REACTION_DELAY = 5
KANGAROO_REACTION_DELAY = 2
STANDARD_ORIENTATION_DELAY = 20
SPRINTER_ORIENTATION_DELAY = 0
DEFAULT_DOOR_CLOSING_BUFFER = 30

# Program defaults.
DEFAULT_MAX_CONNECTIONS = 5
DEFAULT_SEARCH_WINDOW_MINUTES = 120
MAX_TRANSFER_CANDIDATES = 500
CSV_CHUNK_SIZE = 500_000
DEFAULT_ENCODING = "utf-8-sig"
USE_CURRENT_GTFS = False
CACHE_MAX_AGE = timedelta(days=3)
DOWNLOAD_TIMEOUT_SECONDS = 30
HTTP_USER_AGENT = (
    "SprintConnect/0.1 "
    "(public transport journey planner)"
)
LOG_FORMAT = "%(levelname)s: %(message)s"

GTFS_USE_COLUMNS = {
    "stops.txt": ["stop_id", "stop_name", "parent_station", "platform_code"],
    "stop_times.txt": ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
    "trips.txt": ["trip_id", "route_id"],
    "routes.txt": ["route_id", "route_short_name", "route_long_name"],
    "transfers.txt": ["from_stop_id", "to_stop_id", "min_transfer_time"],
}


# ============================================================
# 3. Datenbeschaffung
# ============================================================


class DataDownloadError(RuntimeError):
    """Raised when GTFS data cannot be downloaded and no cache is usable."""


class DataDownloader:
    """Download GTFS data and reuse a local cache whenever possible."""

    def __init__(
        self,
        fixed_url: str = GTFS_ZIP_URL,
        current_resource_url: str = GTFS_CURRENT_RESOURCE_URL,
        use_current_gtfs: bool = USE_CURRENT_GTFS,
        cache_path: Path = GTFS_ZIP_PATH,
        metadata_path: Path = GTFS_METADATA_PATH,
        cache_max_age: timedelta = CACHE_MAX_AGE,
        timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS,
    ) -> None:
        self.fixed_url = fixed_url
        self.current_resource_url = current_resource_url
        self.use_current_gtfs = use_current_gtfs
        self.cache_path = cache_path
        self.metadata_path = metadata_path
        self.cache_max_age = cache_max_age
        self.timeout_seconds = timeout_seconds

    def get_gtfs_zip(self) -> Path:
        """Return a usable GTFS ZIP path, downloading a newer file if needed."""
        if self._cache_is_fresh():
            logging.info("Using fresh cached GTFS file: %s", self.cache_path)
            return self.cache_path
        if self._cache_exists() and not self.use_current_gtfs:
            logging.info("Using cached fixed GTFS file: %s", self.cache_path)
            return self.cache_path

        try:
            final_url = self._resolve_download_url_with_fallback()

            if self._cache_exists() and self._cached_source_url() == final_url:
                logging.info("GTFS cache is old, but permalink still points to the same file.")
                self._write_metadata(final_url)
                return self.cache_path

            logging.info("Downloading GTFS data from %s", final_url)
            self._download_file(final_url)
            self._write_metadata(final_url)
            return self.cache_path

        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            if self._cache_exists():
                logging.warning("Could not download GTFS data. Using existing cache. Reason: %s", exc)
                return self.cache_path
            raise DataDownloadError("GTFS data could not be downloaded and no cache exists.") from exc

    def _get_download_url(self) -> str:
        if self.use_current_gtfs:
            return self.current_resource_url
        return self.fixed_url

    def _resolve_download_url_with_fallback(self) -> str:
        download_url = self._get_download_url()
        try:
            return self._resolve_final_url(download_url)
        except urllib.error.HTTPError as exc:
            if self.use_current_gtfs and self.fixed_url:
                logging.warning(
                    "Could not resolve GTFS permalink (%s). Trying fixed GTFS ZIP URL.",
                    exc,
                )
                return self._resolve_final_url(self.fixed_url)
            raise

    def _cache_exists(self) -> bool:
        return self.cache_path.exists() and self.cache_path.is_file()

    def _cache_is_fresh(self) -> bool:
        if not self._cache_exists() or not self.metadata_path.exists():
            return False

        metadata = self._read_metadata()
        downloaded_at_text = metadata.get("downloaded_at")
        if not downloaded_at_text:
            return False

        try:
            downloaded_at = datetime.fromisoformat(downloaded_at_text)
        except ValueError:
            return False

        return datetime.now() - downloaded_at <= self.cache_max_age

    def _cached_source_url(self) -> Optional[str]:
        return self._read_metadata().get("source_url")

    def _read_metadata(self) -> dict[str, str]:
        if not self.metadata_path.exists():
            return {}
        try:
            with self.metadata_path.open("r", encoding=DEFAULT_ENCODING) as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_metadata(self, source_url: str) -> None:
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "source_url": source_url,
            "downloaded_at": datetime.now().isoformat(timespec="seconds"),
        }
        with self.metadata_path.open("w", encoding=DEFAULT_ENCODING) as file:
            json.dump(metadata, file, indent=2)

    def _resolve_final_url(self, url: str) -> str:
        request = self._build_request(url, method="HEAD")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.geturl()
        except urllib.error.HTTPError as exc:
            if exc.code not in {403, 405}:
                raise

        request = self._build_request(url, method="GET", headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return response.geturl()

    def _download_file(self, url: str) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        request = self._build_request(url)
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            with self.cache_path.open("wb") as file:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    file.write(chunk)

    @staticmethod
    def _build_request(
        url: str,
        method: str = "GET",
        headers: Optional[dict[str, str]] = None,
    ) -> urllib.request.Request:
        request_headers = {
            "User-Agent": HTTP_USER_AGENT,
            "Accept": "application/zip,application/octet-stream,*/*",
        }
        if headers:
            request_headers.update(headers)
        return urllib.request.Request(url, headers=request_headers, method=method)


# ============================================================
# 4. Datenverarbeitung
# ============================================================


@dataclass
class GtfsTables:
    """Container for the GTFS tables needed by SprintConnect."""

    stops: "pd.DataFrame"
    stop_times: "pd.DataFrame"
    trips: "pd.DataFrame"
    routes: "pd.DataFrame"
    transfers: "pd.DataFrame"


class DataProcessor:
    """Extract selected GTFS files and load them as pandas data frames."""

    def __init__(
        self,
        zip_path: Path = GTFS_ZIP_PATH,
        extract_dir: Path = GTFS_EXTRACT_DIR,
        required_files: tuple[str, ...] = REQUIRED_GTFS_FILES,
        encoding: str = DEFAULT_ENCODING,
    ) -> None:
        self.zip_path = zip_path
        self.extract_dir = extract_dir
        self.required_files = required_files
        self.encoding = encoding

    def load_tables_for_scenario(self, scenario: "Scenario") -> GtfsTables:
        """Load only GTFS rows needed for one origin/destination search."""
        self._ensure_pandas_available()
        self.extract_required_files()

        stops = self._read_gtfs_file("stops.txt")
        origin_stop_ids = self.find_stop_ids(stops, scenario.origin_stop_name)
        destination_stop_ids = self.find_stop_ids(stops, scenario.destination_stop_name)

        if not origin_stop_ids:
            raise ValueError(f"Unknown origin stop: {scenario.origin_stop_name}")
        if not destination_stop_ids:
            raise ValueError(f"Unknown destination stop: {scenario.destination_stop_name}")

        stop_times = self._read_relevant_stop_times(origin_stop_ids, destination_stop_ids)
        trips = self._read_gtfs_file("trips.txt")
        relevant_trip_ids = set(stop_times["trip_id"].drop_duplicates())
        trips = trips[trips["trip_id"].isin(relevant_trip_ids)]

        routes = self._read_gtfs_file("routes.txt")
        relevant_route_ids = set(trips["route_id"].drop_duplicates())
        routes = routes[routes["route_id"].isin(relevant_route_ids)]

        transfers = self._read_gtfs_file("transfers.txt")
        transfers = self._prepare_transfers(transfers)

        stop_times = self._prepare_stop_times(stop_times)

        return GtfsTables(
            stops=stops,
            stop_times=stop_times,
            trips=trips,
            routes=routes,
            transfers=transfers,
        )

    def print_gtfs_summary(self, tables: GtfsTables) -> None:
        """Print row counts and columns for the loaded GTFS tables."""
        print("\nGTFS summary")
        print("============")
        self._print_table_info("stops", tables.stops)
        self._print_table_info("routes", tables.routes)
        self._print_table_info("trips", tables.trips)
        self._print_table_info("stop_times", tables.stop_times)
        self._print_table_info("transfers", tables.transfers)

    def extract_required_files(self) -> None:
        """Extract only the GTFS files needed by the application."""
        if not self.zip_path.exists():
            raise FileNotFoundError(f"GTFS ZIP not found: {self.zip_path}")

        self.extract_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(self.zip_path, "r") as gtfs_zip:
            available_files = set(gtfs_zip.namelist())
            missing_files = [file_name for file_name in self.required_files if file_name not in available_files]
            if missing_files:
                missing_text = ", ".join(missing_files)
                raise FileNotFoundError(f"GTFS ZIP is missing required files: {missing_text}")

            for file_name in self.required_files:
                target_path = self.extract_dir / file_name
                if target_path.exists() and target_path.stat().st_mtime >= self.zip_path.stat().st_mtime:
                    continue
                with gtfs_zip.open(file_name) as source:
                    target_path.write_bytes(source.read())

    def _read_gtfs_file(self, file_name: str) -> "pd.DataFrame":
        file_path = self.extract_dir / file_name
        return pd.read_csv(
            file_path,
            usecols=GTFS_USE_COLUMNS.get(file_name),
            dtype=str,
            keep_default_na=False,
            encoding=self.encoding,
        )

    def _read_relevant_stop_times(
        self,
        origin_stop_ids: list[str],
        destination_stop_ids: list[str],
    ) -> "pd.DataFrame":
        file_path = self.extract_dir / "stop_times.txt"
        scenario_cache_path = self._stop_times_scenario_cache_path(origin_stop_ids, destination_stop_ids)
        if scenario_cache_path.exists() and scenario_cache_path.stat().st_mtime >= file_path.stat().st_mtime:
            return pd.read_pickle(scenario_cache_path)

        target_stop_ids = set(origin_stop_ids + destination_stop_ids)
        relevant_trip_ids: set[str] = set()

        for chunk in pd.read_csv(
            file_path,
            usecols=GTFS_USE_COLUMNS["stop_times.txt"],
            dtype=str,
            keep_default_na=False,
            encoding=self.encoding,
            chunksize=CSV_CHUNK_SIZE,
        ):
            matching_rows = chunk[chunk["stop_id"].isin(target_stop_ids)]
            relevant_trip_ids.update(matching_rows["trip_id"].drop_duplicates())

        if not relevant_trip_ids:
            return pd.DataFrame(columns=GTFS_USE_COLUMNS["stop_times.txt"])

        relevant_chunks = []
        for chunk in pd.read_csv(
            file_path,
            usecols=GTFS_USE_COLUMNS["stop_times.txt"],
            dtype=str,
            keep_default_na=False,
            encoding=self.encoding,
            chunksize=CSV_CHUNK_SIZE,
        ):
            matching_rows = chunk[chunk["trip_id"].isin(relevant_trip_ids)]
            if not matching_rows.empty:
                relevant_chunks.append(matching_rows)

        if not relevant_chunks:
            return pd.DataFrame(columns=GTFS_USE_COLUMNS["stop_times.txt"])

        stop_times = pd.concat(relevant_chunks, ignore_index=True)
        scenario_cache_path.parent.mkdir(parents=True, exist_ok=True)
        stop_times.to_pickle(scenario_cache_path)
        return stop_times

    @staticmethod
    def _stop_times_scenario_cache_path(origin_stop_ids: list[str], destination_stop_ids: list[str]) -> Path:
        cache_source = json.dumps(
            {
                "origin": sorted(origin_stop_ids),
                "destination": sorted(destination_stop_ids),
            },
            sort_keys=True,
        )
        cache_key = hashlib.sha256(cache_source.encode(DEFAULT_ENCODING)).hexdigest()[:16]
        return GTFS_STOP_TIMES_SCENARIO_CACHE_DIR / f"{cache_key}.pkl"

    def _prepare_stop_times(self, stop_times: "pd.DataFrame") -> "pd.DataFrame":
        required_columns = {"trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"}
        self._validate_columns(stop_times, required_columns, "stop_times.txt")

        stop_times = stop_times.copy()
        stop_times["arrival_seconds"] = stop_times["arrival_time"].map(self.gtfs_time_to_seconds)
        stop_times["departure_seconds"] = stop_times["departure_time"].map(self.gtfs_time_to_seconds)
        stop_times["stop_sequence_number"] = stop_times["stop_sequence"].astype(int)
        return stop_times

    def _prepare_transfers(self, transfers: "pd.DataFrame") -> "pd.DataFrame":
        required_columns = {"from_stop_id", "to_stop_id", "min_transfer_time"}
        self._validate_columns(transfers, required_columns, "transfers.txt")

        transfers = transfers.copy()
        transfers["min_transfer_time_seconds"] = (
            pd.to_numeric(transfers["min_transfer_time"], errors="coerce")
            .fillna(0)
            .astype(int)
        )
        return transfers

    def _validate_columns(self, table: "pd.DataFrame", required_columns: set[str], file_name: str) -> None:
        missing_columns = required_columns.difference(table.columns)
        if missing_columns:
            missing_text = ", ".join(sorted(missing_columns))
            raise ValueError(f"{file_name} is missing required columns: {missing_text}")

    def find_stop_ids(self, stops: "pd.DataFrame", stop_name: str) -> list[str]:
        """Find stop IDs by exact normalized name first, then by contained text."""
        normalized_name = normalize_stop_text(stop_name)
        normalized_stop_names = stops["stop_name"].map(normalize_stop_text)
        matching_stops = stops[normalized_stop_names == normalized_name]
        if matching_stops.empty:
            matching_stops = stops[normalized_stop_names.str.contains(normalized_name, regex=False)]
        return matching_stops["stop_id"].drop_duplicates().tolist()

    @staticmethod
    def gtfs_time_to_seconds(value: str) -> int:
        """Convert a GTFS time value such as 25:10:00 to seconds after service day start."""
        hours_text, minutes_text, seconds_text = value.split(":")
        return int(hours_text) * 3600 + int(minutes_text) * 60 + int(seconds_text)

    @staticmethod
    def _ensure_pandas_available() -> None:
        if pd is None:
            raise ImportError(
                "pandas is required for GTFS processing. Install it in the project environment first."
            )

    @staticmethod
    def _print_table_info(table_name: str, table: "pd.DataFrame") -> None:
        columns = ", ".join(table.columns)
        print(f"{table_name}: {len(table)} rows")
        print(f"  columns: {columns}")


# ============================================================
# 5. Routenberechnung
# ============================================================


@dataclass
class RouteLeg:
    """One part of a public transport connection."""

    trip_id: str
    route_id: str
    origin_stop_id: str
    destination_stop_id: str
    departure_seconds: int
    arrival_seconds: int


@dataclass
class Connection:
    """A complete connection from origin to destination."""

    legs: list[RouteLeg]

    @property
    def departure_seconds(self) -> int:
        return self.legs[0].departure_seconds

    @property
    def arrival_seconds(self) -> int:
        return self.legs[-1].arrival_seconds

    @property
    def duration_seconds(self) -> int:
        return self.arrival_seconds - self.departure_seconds

    @property
    def transfer_count(self) -> int:
        return max(0, len(self.legs) - 1)


@dataclass(frozen=True)
class ConnectionRiskSummary:
    """Highest-level risk summary for a complete connection."""

    status: Optional[TransferStatus]
    evaluations: list[TransferEvaluation]

    @property
    def label(self) -> str:
        if self.status is None:
            return "no transfer"
        return self.status.value


class RouteBuilder:
    """Find possible connections in the prepared GTFS tables."""

    def __init__(self, tables: GtfsTables) -> None:
        self.tables = tables

    def find_connections(
        self,
        origin_stop_name: str,
        destination_stop_name: str,
        departure_seconds: int,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
    ) -> list[Connection]:
        """Find direct and one-transfer connections for the prototype version."""
        origin_stop_ids = self._find_stop_ids_by_name(origin_stop_name)
        destination_stop_ids = self._find_stop_ids_by_name(destination_stop_name)

        if not origin_stop_ids:
            raise ValueError(self._unknown_stop_message("origin", origin_stop_name))
        if not destination_stop_ids:
            raise ValueError(self._unknown_stop_message("destination", destination_stop_name))

        direct_connections = self._find_direct_connections(
            origin_stop_ids=origin_stop_ids,
            destination_stop_ids=destination_stop_ids,
            departure_seconds=departure_seconds,
        )
        transfer_connections = self._find_one_transfer_connections(
            origin_stop_ids=origin_stop_ids,
            destination_stop_ids=destination_stop_ids,
            departure_seconds=departure_seconds,
        )

        connections = self._deduplicate_connections(direct_connections + transfer_connections)
        connections.sort(key=lambda connection: (connection.arrival_seconds, connection.duration_seconds, connection.transfer_count))
        return connections[:max_connections]

    def _find_stop_ids_by_name(self, stop_name: str) -> list[str]:
        normalized_name = normalize_stop_text(stop_name)
        stops = self.tables.stops
        normalized_stop_names = stops["stop_name"].map(normalize_stop_text)
        matching_stops = stops[normalized_stop_names == normalized_name]
        if matching_stops.empty:
            matching_stops = stops[normalized_stop_names.str.contains(normalized_name, regex=False)]
        return matching_stops["stop_id"].drop_duplicates().tolist()

    def _unknown_stop_message(self, role: str, stop_name: str) -> str:
        suggestions = self._suggest_stop_names(stop_name)
        message = f"Unknown {role} stop: {stop_name}"
        if suggestions:
            message += ". Did you mean: " + ", ".join(suggestions)
        return message

    def _suggest_stop_names(self, stop_name: str, limit: int = 5) -> list[str]:
        stops = self.tables.stops
        if "stop_name" not in stops.columns:
            return []

        normalized_to_original: dict[str, str] = {}
        for original_name in stops["stop_name"].drop_duplicates():
            normalized_to_original.setdefault(normalize_stop_text(original_name), original_name)

        matches = difflib.get_close_matches(
            normalize_stop_text(stop_name),
            list(normalized_to_original),
            n=limit,
            cutoff=0.72,
        )
        return [normalized_to_original[match] for match in matches]

    def _find_direct_connections(
        self,
        origin_stop_ids: list[str],
        destination_stop_ids: list[str],
        departure_seconds: int,
    ) -> list[Connection]:
        stop_times = self.tables.stop_times
        trips = self.tables.trips[["trip_id", "route_id"]]

        origin_rows = stop_times[
            stop_times["stop_id"].isin(origin_stop_ids)
            & (stop_times["departure_seconds"] >= departure_seconds)
        ]
        destination_rows = stop_times[stop_times["stop_id"].isin(destination_stop_ids)]

        candidates = origin_rows.merge(
            destination_rows,
            on="trip_id",
            suffixes=("_origin", "_destination"),
        )
        candidates = candidates[
            candidates["stop_sequence_number_origin"] < candidates["stop_sequence_number_destination"]
        ]
        candidates = candidates.merge(trips, on="trip_id", how="left")
        candidates = candidates.sort_values(["departure_seconds_origin", "arrival_seconds_destination"])

        connections: list[Connection] = []
        for _, row in candidates.iterrows():
            leg = RouteLeg(
                trip_id=row["trip_id"],
                route_id=row["route_id"],
                origin_stop_id=row["stop_id_origin"],
                destination_stop_id=row["stop_id_destination"],
                departure_seconds=int(row["departure_seconds_origin"]),
                arrival_seconds=int(row["arrival_seconds_destination"]),
            )
            connections.append(Connection(legs=[leg]))

        return connections

    def _find_one_transfer_connections(
        self,
        origin_stop_ids: list[str],
        destination_stop_ids: list[str],
        departure_seconds: int,
    ) -> list[Connection]:
        stop_times = self.tables.stop_times
        trips = self.tables.trips[["trip_id", "route_id"]]
        search_end_seconds = departure_seconds + DEFAULT_SEARCH_WINDOW_MINUTES * 60

        origin_rows = stop_times[
            stop_times["stop_id"].isin(origin_stop_ids)
            & (stop_times["departure_seconds"] >= departure_seconds)
            & (stop_times["departure_seconds"] <= search_end_seconds)
        ]
        destination_rows = stop_times[stop_times["stop_id"].isin(destination_stop_ids)]

        first_leg_candidates = origin_rows.merge(
            stop_times,
            on="trip_id",
            suffixes=("_origin", "_transfer"),
        )
        first_leg_candidates = first_leg_candidates[
            first_leg_candidates["stop_sequence_number_origin"]
            < first_leg_candidates["stop_sequence_number_transfer"]
        ]
        first_leg_candidates = first_leg_candidates[
            ~first_leg_candidates["stop_id_transfer"].isin(destination_stop_ids)
        ]
        first_leg_candidates = first_leg_candidates.sort_values(
            ["departure_seconds_origin", "arrival_seconds_transfer"]
        ).head(MAX_TRANSFER_CANDIDATES)

        if first_leg_candidates.empty:
            return []

        transfer_stop_ids = first_leg_candidates["stop_id_transfer"].drop_duplicates().tolist()
        earliest_transfer_arrival = int(first_leg_candidates["arrival_seconds_transfer"].min())
        latest_second_departure = search_end_seconds + DEFAULT_SEARCH_WINDOW_MINUTES * 60

        transfer_departure_rows = stop_times[
            stop_times["stop_id"].isin(transfer_stop_ids)
            & (stop_times["departure_seconds"] >= earliest_transfer_arrival)
            & (stop_times["departure_seconds"] <= latest_second_departure)
        ]

        second_leg_candidates = transfer_departure_rows.merge(
            destination_rows,
            on="trip_id",
            suffixes=("_transfer", "_destination"),
        )
        second_leg_candidates = second_leg_candidates[
            second_leg_candidates["stop_sequence_number_transfer"]
            < second_leg_candidates["stop_sequence_number_destination"]
        ]
        second_leg_candidates = second_leg_candidates[
            ~second_leg_candidates["stop_id_transfer"].isin(origin_stop_ids)
        ]

        candidates = first_leg_candidates.merge(
            second_leg_candidates,
            on="stop_id_transfer",
            suffixes=("_first", "_second"),
        )
        candidates = candidates[candidates["trip_id_first"] != candidates["trip_id_second"]]
        candidates = candidates[
            candidates["departure_seconds_transfer_second"]
            >= candidates["arrival_seconds_transfer_first"] + CRITICAL_TRANSFER_BUFFER
        ]
        candidates = candidates.merge(
            trips.rename(columns={"trip_id": "trip_id_first", "route_id": "route_id_first"}),
            on="trip_id_first",
            how="left",
        )
        candidates = candidates.merge(
            trips.rename(columns={"trip_id": "trip_id_second", "route_id": "route_id_second"}),
            on="trip_id_second",
            how="left",
        )
        candidates = candidates.sort_values(
            [
                "arrival_seconds_destination",
                "departure_seconds_origin",
                "departure_seconds_transfer_second",
            ]
        )

        connections: list[Connection] = []
        for _, row in candidates.head(max(DEFAULT_MAX_CONNECTIONS * 20, 50)).iterrows():
            first_leg = RouteLeg(
                trip_id=row["trip_id_first"],
                route_id=row["route_id_first"],
                origin_stop_id=row["stop_id_origin"],
                destination_stop_id=row["stop_id_transfer"],
                departure_seconds=int(row["departure_seconds_origin"]),
                arrival_seconds=int(row["arrival_seconds_transfer_first"]),
            )
            second_leg = RouteLeg(
                trip_id=row["trip_id_second"],
                route_id=row["route_id_second"],
                origin_stop_id=row["stop_id_transfer"],
                destination_stop_id=row["stop_id_destination"],
                departure_seconds=int(row["departure_seconds_transfer_second"]),
                arrival_seconds=int(row["arrival_seconds_destination"]),
            )
            connections.append(Connection(legs=[first_leg, second_leg]))

        return connections

    @staticmethod
    def _deduplicate_connections(connections: list[Connection]) -> list[Connection]:
        unique_connections: list[Connection] = []
        seen_keys: set[tuple[tuple[str, str, str, int, int], ...]] = set()

        for connection in connections:
            key = tuple(
                (
                    leg.route_id,
                    leg.departure_seconds,
                    leg.arrival_seconds,
                )
                for leg in connection.legs
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            unique_connections.append(connection)

        return unique_connections


# ============================================================
# 6. Benutzerprofil
# ============================================================


class SprinterMode(Enum):
    """Available user modes for transfer-time evaluation."""

    STANDARD = "standard"
    BUNNY = "bunny"
    KANGAROO = "kangaroo"


class CrowdLevel(Enum):
    """Crowd level during a transfer."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class PositionToDoor(Enum):
    """Passenger position in the vehicle before leaving."""

    NEAR_DOOR = "near_door"
    MIDDLE = "middle"
    FAR_FROM_DOOR = "far_from_door"


class TransferType(Enum):
    """Typical interchange situation observed during field work."""

    SAME_PLATFORM = "same_platform"
    NEIGHBOUR_PLATFORM = "neighbour_platform"
    UNDERPASS = "underpass"


@dataclass(frozen=True)
class TransferTypeEstimate:
    """Approximate standard and sprinter transfer times for one transfer type."""

    standard_seconds: int
    sprinter_seconds: int


TRANSFER_TYPE_ESTIMATES = {
    TransferType.SAME_PLATFORM: TransferTypeEstimate(standard_seconds=120, sprinter_seconds=60),
    TransferType.NEIGHBOUR_PLATFORM: TransferTypeEstimate(standard_seconds=240, sprinter_seconds=150),
    TransferType.UNDERPASS: TransferTypeEstimate(standard_seconds=420, sprinter_seconds=270),
}


@dataclass(frozen=True)
class TransferContext:
    """Situation-specific factors that can make one transfer slower or faster."""

    crowd_level: CrowdLevel = CrowdLevel.MEDIUM
    position_to_door: PositionToDoor = PositionToDoor.NEAR_DOOR
    stairs: bool = False
    transfer_type: TransferType = TransferType.NEIGHBOUR_PLATFORM

    def additional_delay_seconds(self) -> int:
        """Return additional delay caused by crowd, vehicle position, and stairs."""
        crowd_delay = {
            CrowdLevel.LOW: 0,
            CrowdLevel.MEDIUM: 20,
            CrowdLevel.HIGH: 45,
        }[self.crowd_level]
        position_delay = {
            PositionToDoor.NEAR_DOOR: 0,
            PositionToDoor.MIDDLE: 10,
            PositionToDoor.FAR_FROM_DOOR: 20,
        }[self.position_to_door]
        stairs_delay = 20 if self.stairs else 0
        return crowd_delay + position_delay + stairs_delay

    @property
    def field_estimate(self) -> TransferTypeEstimate:
        return TRANSFER_TYPE_ESTIMATES[self.transfer_type]


@dataclass(frozen=True)
class SprinterProfile:
    """User profile describing how aggressively transfer times may be reduced."""

    mode: SprinterMode
    display_name: str
    transfer_time_reduction: float
    walking_speed: float
    reaction_delay: int
    orientation_delay: int
    station_knowledge: bool
    door_closing_buffer: int = DEFAULT_DOOR_CLOSING_BUFFER

    @property
    def transfer_time_factor(self) -> float:
        return 1.0 - self.transfer_time_reduction

    def adjusted_transfer_time(self, default_transfer_seconds: int) -> int:
        """Return the transfer time expected for this profile."""
        return max(0, round(default_transfer_seconds * self.transfer_time_factor))

    def estimated_transfer_time_for_distance(self, walking_distance_meters: float, context: TransferContext) -> int:
        """Estimate required transfer time from distance and situation-specific context."""
        walking_seconds = walking_distance_meters / self.walking_speed
        return round(
            walking_seconds
            + self.reaction_delay
            + self.orientation_delay
            + context.additional_delay_seconds()
            + self.door_closing_buffer
        )

    @staticmethod
    def from_mode(mode: SprinterMode) -> "SprinterProfile":
        profiles = {
            SprinterMode.STANDARD: SprinterProfile(
                mode=SprinterMode.STANDARD,
                display_name="Standard",
                transfer_time_reduction=0.0,
                walking_speed=STANDARD_WALKING_SPEED,
                reaction_delay=STANDARD_REACTION_DELAY,
                orientation_delay=STANDARD_ORIENTATION_DELAY,
                station_knowledge=False,
            ),
            SprinterMode.BUNNY: SprinterProfile(
                mode=SprinterMode.BUNNY,
                display_name="Runner",
                transfer_time_reduction=0.20,
                walking_speed=BUNNY_WALKING_SPEED,
                reaction_delay=BUNNY_REACTION_DELAY,
                orientation_delay=SPRINTER_ORIENTATION_DELAY,
                station_knowledge=True,
            ),
            SprinterMode.KANGAROO: SprinterProfile(
                mode=SprinterMode.KANGAROO,
                display_name="Kangaroo",
                transfer_time_reduction=0.40,
                walking_speed=KANGAROO_WALKING_SPEED,
                reaction_delay=KANGAROO_REACTION_DELAY,
                orientation_delay=SPRINTER_ORIENTATION_DELAY,
                station_knowledge=True,
            ),
        }
        return profiles[mode]

    @staticmethod
    def from_user_input(value: str) -> "SprinterProfile":
        normalized_value = value.casefold().strip()
        aliases = {
            "standard": SprinterMode.STANDARD,
            "normal": SprinterMode.STANDARD,
            "0": SprinterMode.STANDARD,
            "bunny": SprinterMode.BUNNY,
            "zajchik": SprinterMode.BUNNY,
            "zaichik": SprinterMode.BUNNY,
            "runner": SprinterMode.BUNNY,
            "1": SprinterMode.BUNNY,
            "kangaroo": SprinterMode.KANGAROO,
            "kenguru": SprinterMode.KANGAROO,
            "2": SprinterMode.KANGAROO,
        }
        if normalized_value not in aliases:
            allowed_values = ", ".join(sorted(aliases))
            raise ValueError(f"Unknown sprinter mode '{value}'. Allowed values: {allowed_values}")
        return SprinterProfile.from_mode(aliases[normalized_value])


# ============================================================
# 7. Szenariodefinition
# ============================================================


@dataclass(frozen=True)
class Scenario:
    """User request and transfer assumptions for one journey search."""

    origin_stop_name: str
    destination_stop_name: str
    departure_datetime: datetime
    sprinter_profile: SprinterProfile
    transfer_context: TransferContext

    @property
    def departure_seconds(self) -> int:
        """Return seconds after midnight for GTFS stop time comparison."""
        return (
            self.departure_datetime.hour * 3600
            + self.departure_datetime.minute * 60
            + self.departure_datetime.second
        )

    @staticmethod
    def create(
        origin_stop_name: str,
        destination_stop_name: str,
        departure_datetime: Optional[datetime] = None,
        sprinter_profile: Optional[SprinterProfile] = None,
        transfer_context: Optional[TransferContext] = None,
    ) -> "Scenario":
        """Create a scenario with sensible defaults for the prototype."""
        return Scenario(
            origin_stop_name=origin_stop_name.strip(),
            destination_stop_name=destination_stop_name.strip(),
            departure_datetime=departure_datetime or datetime.now(),
            sprinter_profile=sprinter_profile or SprinterProfile.from_mode(SprinterMode.STANDARD),
            transfer_context=transfer_context or TransferContext(),
        )


# ============================================================
# 8. Risikobewertung
# ============================================================


class TransferStatus(Enum):
    """Qualitative feasibility labels for transfers."""

    SAFE = "safe"
    TIGHT = "tight"
    CRITICAL = "critical"


@dataclass(frozen=True)
class TransferEvaluation:
    """Result of evaluating one transfer."""

    available_seconds: int
    sprinter_min_seconds: int
    critical_seconds: int
    status: TransferStatus
    profile: SprinterProfile
    context: TransferContext


class RiskEvaluator:
    """Evaluate whether a transfer is suitable for a selected sprinter profile."""

    def __init__(self, profile: SprinterProfile, context: TransferContext) -> None:
        self.profile = profile
        self.context = context

    def evaluate_transfer(
        self,
        available_seconds: int,
        default_transfer_seconds: Optional[int] = None,
        walking_distance_meters: Optional[float] = None,
    ) -> TransferEvaluation:
        """Classify a transfer as ok, tight, or not feasible."""
        sprinter_min_seconds = self._estimate_sprinter_min_time(
            default_transfer_seconds=default_transfer_seconds,
            walking_distance_meters=walking_distance_meters,
        )
        critical_seconds = self._estimate_critical_time(sprinter_min_seconds)

        if available_seconds >= sprinter_min_seconds + COMFORTABLE_TRANSFER_BUFFER:
            status = TransferStatus.SAFE
        elif available_seconds >= sprinter_min_seconds:
            status = TransferStatus.TIGHT
        else:
            status = TransferStatus.CRITICAL

        return TransferEvaluation(
            available_seconds=available_seconds,
            sprinter_min_seconds=sprinter_min_seconds,
            critical_seconds=critical_seconds,
            status=status,
            profile=self.profile,
            context=self.context,
        )

    def _estimate_sprinter_min_time(
        self,
        default_transfer_seconds: Optional[int],
        walking_distance_meters: Optional[float],
    ) -> int:
        if walking_distance_meters is not None:
            return self.profile.estimated_transfer_time_for_distance(walking_distance_meters, self.context)

        if default_transfer_seconds is not None:
            return self.profile.adjusted_transfer_time(default_transfer_seconds)

        return self.context.field_estimate.sprinter_seconds

    @staticmethod
    def _estimate_critical_time(sprinter_min_seconds: int) -> int:
        return sprinter_min_seconds


# ============================================================
# 9. Routendarstellung
# ============================================================


class SprinterRouteDisplay:
    """Format calculated connections for console output and later GUI reuse."""

    def __init__(self, tables: GtfsTables, risk_evaluator: Optional[RiskEvaluator] = None) -> None:
        self.tables = tables
        self.risk_evaluator = risk_evaluator
        self.stop_names = self._build_stop_name_lookup()
        self.route_names = self._build_route_name_lookup()

    def format_connections(self, connections: list[Connection]) -> str:
        """Return a readable text block with all found connections."""
        if not connections:
            return "No connections found."

        lines = ["Available connections", "====================="]
        for index, connection in enumerate(connections, start=1):
            lines.extend(self._format_connection(index, connection))
            lines.append("")
        return "\n".join(lines).rstrip()

    def print_connections(self, connections: list[Connection]) -> None:
        """Print formatted connections to the console."""
        print(self.format_connections(connections))

    def _format_connection(self, index: int, connection: Connection) -> list[str]:
        duration_minutes = round(connection.duration_seconds / 60)
        risk_summary = self.evaluate_connection_risk(connection)
        lines = [
            (
                f"{index}. {format_gtfs_time(connection.departure_seconds)} -> "
                f"{format_gtfs_time(connection.arrival_seconds)} "
                f"({duration_minutes} min, transfers: {connection.transfer_count}, "
                f"risk: {risk_summary.label})"
            )
        ]
        for leg_index, leg in enumerate(connection.legs, start=1):
            lines.append(self._format_leg(leg_index, leg))
            if leg_index < len(connection.legs) and risk_summary.evaluations:
                evaluation = risk_summary.evaluations[leg_index - 1]
                transfer_minutes = round(evaluation.available_seconds / 60)
                required_minutes = round(evaluation.sprinter_min_seconds / 60)
                lines.append(
                    f"      transfer risk: {evaluation.status.value} "
                    f"({transfer_minutes} min available, {required_minutes} min required)"
                )
        return lines

    def evaluate_connection_risk(self, connection: Connection) -> ConnectionRiskSummary:
        if connection.transfer_count == 0 or self.risk_evaluator is None:
            return ConnectionRiskSummary(status=None, evaluations=[])

        evaluations = []
        for current_leg, next_leg in zip(connection.legs, connection.legs[1:]):
            available_seconds = next_leg.departure_seconds - current_leg.arrival_seconds
            default_transfer_seconds = self._lookup_default_transfer_time(
                from_stop_id=current_leg.destination_stop_id,
                to_stop_id=next_leg.origin_stop_id,
            )
            evaluations.append(
                self.risk_evaluator.evaluate_transfer(
                    available_seconds=available_seconds,
                    default_transfer_seconds=default_transfer_seconds,
                )
            )

        priority = {
            TransferStatus.CRITICAL: 3,
            TransferStatus.TIGHT: 2,
            TransferStatus.SAFE: 1,
        }
        worst_status = max((evaluation.status for evaluation in evaluations), key=lambda status: priority[status])
        return ConnectionRiskSummary(status=worst_status, evaluations=evaluations)

    def _format_leg(self, leg_index: int, leg: RouteLeg) -> str:
        origin_name = self.stop_names.get(leg.origin_stop_id, leg.origin_stop_id)
        destination_name = self.stop_names.get(leg.destination_stop_id, leg.destination_stop_id)
        route_name = self.route_names.get(leg.route_id, leg.route_id)
        return (
            f"   Leg {leg_index}: {route_name} | "
            f"{origin_name} {format_gtfs_time(leg.departure_seconds)} -> "
            f"{destination_name} {format_gtfs_time(leg.arrival_seconds)}"
        )

    def _build_stop_name_lookup(self) -> dict[str, str]:
        if "stop_id" not in self.tables.stops.columns or "stop_name" not in self.tables.stops.columns:
            return {}
        return dict(zip(self.tables.stops["stop_id"], self.tables.stops["stop_name"]))

    def _build_route_name_lookup(self) -> dict[str, str]:
        routes = self.tables.routes
        if "route_id" not in routes.columns:
            return {}

        route_names: dict[str, str] = {}
        for _, row in routes.iterrows():
            route_id = row["route_id"]
            short_name = row.get("route_short_name", "")
            long_name = row.get("route_long_name", "")
            route_names[route_id] = short_name or long_name or route_id
        return route_names

    def _lookup_default_transfer_time(self, from_stop_id: str, to_stop_id: str) -> Optional[int]:
        transfers = self.tables.transfers
        required_columns = {"from_stop_id", "to_stop_id", "min_transfer_time_seconds"}
        if not required_columns.issubset(transfers.columns):
            return None

        matches = transfers[
            (transfers["from_stop_id"] == from_stop_id)
            & (transfers["to_stop_id"] == to_stop_id)
        ]
        if matches.empty:
            return None
        return int(matches.iloc[0]["min_transfer_time_seconds"])


# ============================================================
# 10. Fahrgastinformation
# ============================================================


class PassengerInformation:
    """Simulate an in-vehicle passenger information display."""

    def __init__(self, tables: GtfsTables, scenario: Scenario) -> None:
        self.tables = tables
        self.scenario = scenario
        risk_evaluator = RiskEvaluator(
            profile=scenario.sprinter_profile,
            context=scenario.transfer_context,
        )
        self.route_display = SprinterRouteDisplay(tables, risk_evaluator)

    def format_connection_display(self, connection: Connection) -> str:
        """Return a passenger display text for one selected connection."""
        risk_summary = self.route_display.evaluate_connection_risk(connection)
        transfer_evaluations = list(zip(risk_summary.evaluations, connection.legs[1:]))
        lines = [
            "Passenger information",
            "=====================",
            f"Mode: {self.scenario.sprinter_profile.display_name}",
            f"Departure: {format_gtfs_time(connection.departure_seconds)}",
            f"Arrival:   {format_gtfs_time(connection.arrival_seconds)}",
            "",
        ]

        if transfer_evaluations:
            lines.extend(self._format_transfer_summary(transfer_evaluations))
            lines.append("")
        else:
            lines.append("No transfer required.")
            lines.append("")

        lines.extend([
            "Journey:",
        ])

        for leg_index, leg in enumerate(connection.legs, start=1):
            lines.append(self.route_display._format_leg(leg_index, leg))

            if leg_index < len(connection.legs):
                evaluation, next_leg = transfer_evaluations[leg_index - 1]
                lines.extend(self._format_transfer_information(evaluation, next_leg))

        return "\n".join(lines)

    def print_connection_display(self, connection: Connection) -> None:
        """Print the passenger display simulation to the console."""
        print(self.format_connection_display(connection))

    def _format_transfer_summary(self, evaluations: list[tuple[TransferEvaluation, RouteLeg]]) -> list[str]:
        worst_status = max(
            (evaluation.status for evaluation, _ in evaluations),
            key=lambda status: {
                TransferStatus.CRITICAL: 3,
                TransferStatus.TIGHT: 2,
                TransferStatus.SAFE: 1,
            }[status],
        )
        lines = [f"Transfer status: {worst_status.value}"]
        for index, (evaluation, next_leg) in enumerate(evaluations, start=1):
            route_name = self.route_display.route_names.get(next_leg.route_id, next_leg.route_id)
            transfer_minutes = round(evaluation.available_seconds / 60)
            required_minutes = round(evaluation.sprinter_min_seconds / 60)
            lines.append(
                f"Transfer {index}: {transfer_minutes} min available, "
                f"{required_minutes} min required -> {evaluation.status.value}"
            )
            lines.append(
                f"Next: {route_name} at {format_gtfs_time(next_leg.departure_seconds)}"
            )
        return lines

    def _format_transfer_information(self, evaluation: TransferEvaluation, next_leg: RouteLeg) -> list[str]:
        route_name = self.route_display.route_names.get(next_leg.route_id, next_leg.route_id)
        transfer_minutes = round(evaluation.available_seconds / 60)
        required_minutes = round(evaluation.sprinter_min_seconds / 60)
        return [
            "   Transfer:",
            (
                f"      next connection: {route_name} at "
                f"{format_gtfs_time(next_leg.departure_seconds)}"
            ),
            (
                f"      available: {transfer_minutes} min | "
                f"required: {required_minutes} min | status: {evaluation.status.value}"
            ),
        ]


# ============================================================
# 12. GUI
# ============================================================


class SprintConnectGUI:
    """Small Tkinter GUI for the SprintConnect prototype."""

    def __init__(self, use_current_gtfs: bool = USE_CURRENT_GTFS) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.use_current_gtfs = use_current_gtfs
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("1180x760")
        self.root.minsize(1040, 680)
        self.icons: dict[str, object] = {}
        self._build_variables()
        self._build_layout()

    def run(self) -> int:
        self.root.mainloop()
        return 0

    def _build_variables(self) -> None:
        tk = self.tk
        self.origin_var = tk.StringVar(value="Winterthur")
        self.destination_var = tk.StringVar(value="Zurich HB")
        self.departure_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%dT%H:%M"))
        self.mode_var = tk.StringVar(value=SprinterMode.STANDARD.value)
        self.crowd_var = tk.StringVar(value=CrowdLevel.MEDIUM.value)
        self.position_var = tk.StringVar(value=PositionToDoor.NEAR_DOOR.value)
        self.transfer_type_var = tk.StringVar(value=TransferType.NEIGHBOUR_PLATFORM.value)
        self.stairs_var = tk.BooleanVar(value=False)
        self.show_transfer_conditions_var = tk.BooleanVar(value=False)
        self.max_connections_var = tk.IntVar(value=DEFAULT_MAX_CONNECTIONS)
        self.selected_connection_var = tk.IntVar(value=1)
        self.status_var = tk.StringVar(value="Ready.")
        self.current_tables: Optional[GtfsTables] = None
        self.current_tables_key: Optional[tuple[str, str]] = None
        self.current_scenario: Optional[Scenario] = None
        self.current_connections: list[Connection] = []

    def _build_layout(self) -> None:
        ttk = self.ttk
        root = self.root
        root.columnconfigure(0, weight=0, minsize=430)
        root.columnconfigure(1, weight=1, minsize=650)
        root.rowconfigure(0, weight=1)

        left_frame = ttk.Frame(root, padding=16)
        left_frame.grid(row=0, column=0, sticky="nsew")
        left_frame.rowconfigure(0, weight=1)
        left_frame.columnconfigure(0, weight=1)

        canvas = self.tk.Canvas(left_frame, width=390, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        controls_scrollbar = ttk.Scrollbar(left_frame, orient="vertical", command=canvas.yview)
        controls_scrollbar.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=controls_scrollbar.set)

        controls = ttk.Frame(canvas)
        controls_window = canvas.create_window((0, 0), window=controls, anchor="nw")
        controls.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(controls_window, width=event.width))

        output_frame = ttk.Frame(root, padding=(0, 18, 18, 18))
        output_frame.grid(row=0, column=1, sticky="nsew")
        output_frame.rowconfigure(1, weight=3)
        output_frame.rowconfigure(4, weight=2)
        output_frame.columnconfigure(0, weight=1)

        self._build_header(controls)
        self._build_route_inputs(controls)
        self._build_mode_selector(controls)
        self._build_transfer_inputs(controls)

        self.search_button = ttk.Button(left_frame, text="Find connections", command=self._start_search)
        self.search_button.grid(row=1, column=0, sticky="ew", pady=(18, 8))

        ttk.Label(left_frame, textvariable=self.status_var, wraplength=260).grid(row=2, column=0, sticky="ew")

        ttk.Label(output_frame, text="Results", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, sticky="w")
        self.output_text = self.tk.Text(output_frame, wrap="word", height=16, font=("Consolas", 10))
        self.output_text.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        scrollbar = ttk.Scrollbar(output_frame, orient="vertical", command=self.output_text.yview)
        scrollbar.grid(row=1, column=1, sticky="ns", pady=(10, 0))
        self.output_text.configure(yscrollcommand=scrollbar.set)
        self.output_text.tag_configure("risk_safe", foreground="#0f8a4b", font=("Consolas", 10, "bold"))
        self.output_text.tag_configure("risk_tight", foreground="#b36b00", font=("Consolas", 10, "bold"))
        self.output_text.tag_configure("risk_critical", foreground="#c1121f", font=("Consolas", 10, "bold"))
        self.output_text.tag_configure("risk_none", foreground="#475569", font=("Consolas", 10, "bold"))

        ttk.Label(output_frame, text="In-vehicle passenger display", font=("Segoe UI", 13, "bold")).grid(
            row=2, column=0, sticky="w", pady=(18, 0)
        )
        display_controls = ttk.Frame(output_frame)
        display_controls.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(display_controls, text="Connection").grid(row=0, column=0, sticky="w")
        self.connection_selector = ttk.Spinbox(
            display_controls,
            from_=1,
            to=1,
            textvariable=self.selected_connection_var,
            width=6,
            state="disabled",
        )
        self.connection_selector.grid(row=0, column=1, sticky="w", padx=(8, 12))
        self.show_display_button = ttk.Button(
            display_controls,
            text="Show passenger display",
            command=self._show_selected_passenger_display,
            state="disabled",
        )
        self.show_display_button.grid(row=0, column=2, sticky="w")
        self.passenger_display_text = self.tk.Text(
            output_frame,
            wrap="word",
            height=8,
            font=("Consolas", 11, "bold"),
            background="#111827",
            foreground="#f8fafc",
            insertbackground="#f8fafc",
            borderwidth=0,
            padx=18,
            pady=14,
        )
        self.passenger_display_text.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        passenger_scrollbar = ttk.Scrollbar(
            output_frame,
            orient="vertical",
            command=self.passenger_display_text.yview,
        )
        passenger_scrollbar.grid(row=4, column=1, sticky="ns", pady=(8, 0))
        self.passenger_display_text.configure(yscrollcommand=passenger_scrollbar.set)
        self.passenger_display_text.tag_configure("safe", foreground="#22c55e")
        self.passenger_display_text.tag_configure("tight", foreground="#f59e0b")
        self.passenger_display_text.tag_configure("critical", foreground="#ef4444")
        self.passenger_display_text.tag_configure("muted", foreground="#94a3b8")
        self.passenger_display_text.configure(state="disabled")

    def _build_header(self, parent: object) -> None:
        ttk = self.ttk
        icon = self._load_icon(SPRINTCONNECT_ICON_PATH, "sprintconnect", subsample=18)
        header = ttk.Frame(parent)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_NAME, font=("Segoe UI", 20, "bold")).grid(row=0, column=0, sticky="w")
        if icon is not None:
            ttk.Label(header, image=icon).grid(row=0, column=1, sticky="e", padx=(12, 0))
        ttk.Label(parent, text="Sprinter-friendly journey planning", foreground="#555555").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 18)
        )

    def _build_route_inputs(self, parent: object) -> None:
        ttk = self.ttk
        ttk.Label(parent, text="Origin").grid(row=3, column=0, sticky="w")
        ttk.Entry(parent, textvariable=self.origin_var, width=30).grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Label(parent, text="Destination").grid(row=5, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(parent, textvariable=self.destination_var, width=30).grid(row=6, column=0, columnspan=2, sticky="ew")
        ttk.Label(parent, text="Departure").grid(row=7, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(parent, textvariable=self.departure_var, width=30).grid(row=8, column=0, columnspan=2, sticky="ew")
        ttk.Label(parent, text="Format: YYYY-MM-DDTHH:MM", foreground="#666666").grid(
            row=9, column=0, columnspan=2, sticky="w"
        )

    def _build_mode_selector(self, parent: object) -> None:
        ttk = self.ttk
        ttk.Label(parent, text="Mode", font=("Segoe UI", 11, "bold")).grid(
            row=10, column=0, columnspan=2, sticky="w", pady=(18, 6)
        )
        ttk.Radiobutton(parent, text="Standard", variable=self.mode_var, value=SprinterMode.STANDARD.value).grid(
            row=11, column=0, columnspan=2, sticky="w"
        )

        bunny_icon = self._load_icon(BUNNY_ICON_PATH, "bunny", subsample=12)
        kangaroo_icon = self._load_icon(KANGAROO_ICON_PATH, "kangaroo", subsample=12)
        ttk.Radiobutton(
            parent,
            text="Runner (-20%)",
            image=bunny_icon,
            compound="left",
            variable=self.mode_var,
            value=SprinterMode.BUNNY.value,
        ).grid(row=12, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Radiobutton(
            parent,
            text="Kangaroo (-40%)",
            image=kangaroo_icon,
            compound="left",
            variable=self.mode_var,
            value=SprinterMode.KANGAROO.value,
        ).grid(row=13, column=0, columnspan=2, sticky="w", pady=(4, 0))

    def _build_transfer_inputs(self, parent: object) -> None:
        ttk = self.ttk
        self.transfer_conditions_toggle = ttk.Checkbutton(
            parent,
            text="Adjust transfer conditions after route search",
            variable=self.show_transfer_conditions_var,
            command=self._toggle_transfer_conditions,
            state="disabled",
        )
        self.transfer_conditions_toggle.grid(row=14, column=0, columnspan=2, sticky="w", pady=(18, 6))

        self.transfer_conditions_frame = ttk.LabelFrame(parent, text="Transfer conditions", padding=10)
        self.transfer_conditions_frame.grid(row=15, column=0, columnspan=2, sticky="ew")

        self._add_combo(self.transfer_conditions_frame, "Crowd", self.crowd_var, [item.value for item in CrowdLevel], 0)
        self._add_combo(
            self.transfer_conditions_frame,
            "Position in vehicle",
            self.position_var,
            [item.value for item in PositionToDoor],
            1,
        )
        self._add_combo(
            self.transfer_conditions_frame,
            "Transfer type",
            self.transfer_type_var,
            [item.value for item in TransferType],
            2,
        )
        ttk.Checkbutton(self.transfer_conditions_frame, text="Stairs", variable=self.stairs_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        self._toggle_transfer_conditions()

        ttk.Label(parent, text="Max results").grid(row=16, column=0, sticky="w", pady=(10, 0))
        ttk.Spinbox(parent, from_=1, to=20, textvariable=self.max_connections_var, width=8).grid(
            row=16, column=1, sticky="e", pady=(10, 0)
        )

    def _toggle_transfer_conditions(self) -> None:
        if self.show_transfer_conditions_var.get():
            self.transfer_conditions_frame.grid()
        else:
            self.transfer_conditions_frame.grid_remove()

    def _add_combo(self, parent: object, label: str, variable: object, values: list[str], row: int) -> None:
        ttk = self.ttk
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=16).grid(
            row=row, column=1, sticky="e", pady=(6, 0)
        )

    def _load_icon(self, path: Path, key: str, subsample: int) -> Optional[object]:
        if not path.exists():
            return None
        image = self.tk.PhotoImage(file=str(path))
        image = image.subsample(subsample, subsample)
        self.icons[key] = image
        return image

    def _start_search(self) -> None:
        self.search_button.configure(state="disabled")
        self.show_display_button.configure(state="disabled")
        self.connection_selector.configure(state="disabled")
        self.current_scenario = None
        self.current_connections = []
        self.transfer_conditions_toggle.configure(state="disabled")
        self.status_var.set("Loading GTFS and searching connections...")
        self._set_output("Working. This can take a moment on the first run.\n")
        self._set_passenger_display("Passenger display appears after selecting a connection.")
        worker = threading.Thread(target=self._search_worker, daemon=True)
        worker.start()

    def _search_worker(self) -> None:
        try:
            result = self._run_search()
        except Exception as exc:
            self.root.after(0, self._finish_search, f"Error: {exc}")
            return
        self.root.after(0, self._finish_search, result)

    def _run_search(self) -> str:
        downloader = DataDownloader(use_current_gtfs=self.use_current_gtfs)
        downloader.get_gtfs_zip()
        scenario = self._scenario_from_form()
        tables_key = (
            normalize_stop_text(scenario.origin_stop_name),
            normalize_stop_text(scenario.destination_stop_name),
        )
        if self.current_tables is not None and self.current_tables_key == tables_key:
            tables = self.current_tables
        else:
            tables = DataProcessor().load_tables_for_scenario(scenario)
            self.current_tables = tables
            self.current_tables_key = tables_key
        connections = RouteBuilder(tables).find_connections(
            origin_stop_name=scenario.origin_stop_name,
            destination_stop_name=scenario.destination_stop_name,
            departure_seconds=scenario.departure_seconds,
            max_connections=self.max_connections_var.get(),
        )

        risk_evaluator = RiskEvaluator(scenario.sprinter_profile, scenario.transfer_context)
        route_display = SprinterRouteDisplay(tables, risk_evaluator)
        results_text = route_display.format_connections(connections)
        self.current_scenario = scenario
        self.current_connections = connections
        return results_text

    def _scenario_from_form(self) -> Scenario:
        return Scenario.create(
            origin_stop_name=self.origin_var.get(),
            destination_stop_name=self.destination_var.get(),
            departure_datetime=datetime.fromisoformat(self.departure_var.get()),
            sprinter_profile=SprinterProfile.from_user_input(self.mode_var.get()),
            transfer_context=TransferContext(
                crowd_level=CrowdLevel(self.crowd_var.get()),
                position_to_door=PositionToDoor(self.position_var.get()),
                stairs=self.stairs_var.get(),
                transfer_type=TransferType(self.transfer_type_var.get()),
            ),
        )

    def _finish_search(self, text: str) -> None:
        self._set_output(text)
        self.status_var.set("Ready.")
        self.search_button.configure(state="normal")
        if not text.startswith("Error:") and self.current_connections:
            self.transfer_conditions_toggle.configure(state="normal")
            self.connection_selector.configure(state="normal", to=len(self.current_connections))
            self.selected_connection_var.set(1)
            self.show_display_button.configure(state="normal")
            self._set_passenger_display("Select a connection and click 'Show passenger display'.")
        elif not text.startswith("Error:"):
            self._set_passenger_display("No connection selected.")

    def _set_output(self, text: str) -> None:
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", "end")
        self.output_text.insert("1.0", text)
        self._highlight_risk_labels()
        self.output_text.configure(state="disabled")

    def _highlight_risk_labels(self) -> None:
        labels = {
            "risk: safe": "risk_safe",
            "status: safe": "risk_safe",
            "transfer risk: safe": "risk_safe",
            "risk: tight": "risk_tight",
            "status: tight": "risk_tight",
            "transfer risk: tight": "risk_tight",
            "risk: critical": "risk_critical",
            "status: critical": "risk_critical",
            "transfer risk: critical": "risk_critical",
            "risk: no transfer": "risk_none",
            "No transfer required.": "risk_none",
        }
        self._highlight_text_tags(self.output_text, labels)

    def _set_passenger_display(self, text: str) -> None:
        self.passenger_display_text.configure(state="normal")
        self.passenger_display_text.delete("1.0", "end")
        self.passenger_display_text.insert("1.0", text)
        self._highlight_passenger_display()
        self.passenger_display_text.configure(state="disabled")

    def _highlight_passenger_display(self) -> None:
        labels = {
            "safe": "safe",
            "tight": "tight",
            "critical": "critical",
            "No transfer required.": "muted",
            "No connection selected.": "muted",
        }
        self._highlight_text_tags(self.passenger_display_text, labels)

    @staticmethod
    def _highlight_text_tags(text_widget: object, labels: dict[str, str]) -> None:
        for label, tag in labels.items():
            start = "1.0"
            while True:
                index = text_widget.search(label, start, stopindex="end", nocase=True)
                if not index:
                    break
                end = f"{index}+{len(label)}c"
                text_widget.tag_add(tag, index, end)
                start = end

    def _show_selected_passenger_display(self) -> None:
        if not self.current_tables or not self.current_connections:
            self._set_passenger_display("No connection selected.")
            return

        try:
            index = self.selected_connection_var.get()
        except self.tk.TclError:
            index = 1

        if index < 1 or index > len(self.current_connections):
            self._set_passenger_display(f"Connection number must be between 1 and {len(self.current_connections)}.")
            return

        scenario = self._scenario_from_form()
        self.current_scenario = scenario
        passenger_information = PassengerInformation(self.current_tables, scenario)
        self._set_passenger_display(
            passenger_information.format_connection_display(self.current_connections[index - 1])
        )


# ============================================================
# 11. Programmsteuerung
# ============================================================


class SprintConnectApplication:
    """Coordinate the SprintConnect prototype workflow."""

    def __init__(self) -> None:
        self.args = self._parse_arguments()
        self._configure_logging()

    def run(self) -> int:
        """Run the complete prototype workflow."""
        if self.args.gui or self._should_start_gui_by_default():
            gui = SprintConnectGUI(use_current_gtfs=not self.args.fixed_gtfs)
            return gui.run()

        downloader = DataDownloader(use_current_gtfs=not self.args.fixed_gtfs)
        gtfs_zip_path = downloader.get_gtfs_zip()
        logging.info("GTFS ZIP ready: %s", gtfs_zip_path)

        processor = DataProcessor()
        scenario = self._create_scenario()
        tables = processor.load_tables_for_scenario(scenario)

        if self.args.summary:
            processor.print_gtfs_summary(tables)

        route_builder = RouteBuilder(tables)
        connections = route_builder.find_connections(
            origin_stop_name=scenario.origin_stop_name,
            destination_stop_name=scenario.destination_stop_name,
            departure_seconds=scenario.departure_seconds,
            max_connections=self.args.max_connections,
        )

        risk_evaluator = RiskEvaluator(scenario.sprinter_profile, scenario.transfer_context)
        route_display = SprinterRouteDisplay(tables, risk_evaluator)
        route_display.print_connections(connections)

        if connections:
            selected_connection = self._select_connection(connections)
            passenger_information = PassengerInformation(tables, scenario)
            print()
            passenger_information.print_connection_display(selected_connection)

        return 0

    def _create_scenario(self) -> Scenario:
        origin = self.args.origin or input("Origin stop: ").strip()
        destination = self.args.destination or input("Destination stop: ").strip()
        departure_datetime = self._parse_departure_datetime(self.args.departure)
        sprinter_profile = SprinterProfile.from_user_input(self.args.mode)
        transfer_context = TransferContext(
            crowd_level=CrowdLevel(self.args.crowd),
            position_to_door=PositionToDoor(self.args.position),
            stairs=self.args.stairs,
            transfer_type=TransferType(self.args.transfer_type),
        )
        return Scenario.create(
            origin_stop_name=origin,
            destination_stop_name=destination,
            departure_datetime=departure_datetime,
            sprinter_profile=sprinter_profile,
            transfer_context=transfer_context,
        )

    def _select_connection(self, connections: list[Connection]) -> Connection:
        if self.args.connection_index is not None:
            index = self.args.connection_index
        else:
            value = input("Select connection number for passenger display [1]: ").strip()
            index = int(value) if value else 1

        if index < 1 or index > len(connections):
            raise ValueError(f"Connection number must be between 1 and {len(connections)}.")
        return connections[index - 1]

    @staticmethod
    def _parse_departure_datetime(value: Optional[str]) -> datetime:
        if not value:
            return datetime.now()
        return datetime.fromisoformat(value)

    def _parse_arguments(self) -> argparse.Namespace:
        parser = argparse.ArgumentParser(description=f"{APP_NAME} public transport sprinter planner")
        parser.add_argument("--gui", action="store_true", help="start the graphical user interface")
        parser.add_argument("--console", action="store_true", help="use interactive console prompts when inputs are missing")
        parser.add_argument("--origin", help="origin stop name")
        parser.add_argument("--destination", help="destination stop name")
        parser.add_argument("--departure", help="departure datetime in ISO format, e.g. 2026-04-27T16:40")
        parser.add_argument("--mode", default="standard", help="standard, runner, or kangaroo/kenguru")
        parser.add_argument("--crowd", choices=[item.value for item in CrowdLevel], default=CrowdLevel.MEDIUM.value)
        parser.add_argument("--position", choices=[item.value for item in PositionToDoor], default=PositionToDoor.NEAR_DOOR.value)
        parser.add_argument("--stairs", action="store_true", help="add delay for stairs during transfer")
        parser.add_argument(
            "--transfer-type",
            choices=[item.value for item in TransferType],
            default=TransferType.NEIGHBOUR_PLATFORM.value,
        )
        parser.add_argument("--max-connections", type=int, default=DEFAULT_MAX_CONNECTIONS)
        parser.add_argument("--connection-index", type=int, help="connection number used for passenger display")
        parser.add_argument("--summary", action="store_true", help="print GTFS table summary")
        parser.add_argument("--fixed-gtfs", action="store_true", help="use fixed GTFS ZIP instead of permalink")
        parser.add_argument("--verbose", action="store_true", help="show detailed log output")
        return parser.parse_args()

    def _configure_logging(self) -> None:
        level = logging.DEBUG if self.args.verbose else logging.INFO
        logging.basicConfig(level=level, format=LOG_FORMAT)

    def _should_start_gui_by_default(self) -> bool:
        return not self.args.console and (not self.args.origin or not self.args.destination) and not self.args.summary


if __name__ == "__main__":
    app = SprintConnectApplication()
    raise SystemExit(app.run())
