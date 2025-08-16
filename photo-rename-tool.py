#!/usr/bin/env python3
"""
Photo Organizer Script
Organizes photos into a structured folder hierarchy based on date and location.

Structure:
- With location: {year}/{month}-{year}/{month}-{year}-{location}/{year}{month}{day}T{hour}{minute}{second}_{location}_{camera}[discriminator].{ext}
- Without location: {year}/{month}-{year}/{year}{month}{day}T{hour}{minute}{second}_{camera}[discriminator].{ext}
"""

import os
import argparse
import re
import shutil
import ast
import json
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
from filelock import FileLock

from utils.geo_reverse_coder import ReverseGeocoder, GeoData
from utils.logger import Logger, LogLevel
from pymediainfo import MediaInfo


def convert_string_to_tuple(coord_string: str | None) -> tuple[str, str, str] | None:
    """Convert a string representation of a tuple to an actual tuple."""
    if coord_string is None:
        return None
    try:
        x = tuple(map(str, ast.literal_eval(coord_string)))
        if len(x) != 3:
            logger.error(f"Invalid coordinate tuple: {coord_string}")
            return None
        return x
    except (ValueError, SyntaxError):
        logger.error(f"Invalid coordinate string: {coord_string}")
        return None


def get_image_data(image_path: Path) -> dict[str, str | dict[str, str]]:
    """Extract data from file."""
    data: dict[str, str | dict[str, str]] = {}
    mod_time = datetime.fromtimestamp(os.path.getmtime(image_path))
    data["FileModifiedDate"] = mod_time.strftime("%Y:%m:%d %H:%M:%S")

    if image_path.suffix.lower() in image_extensions:
        with Image.open(image_path) as image:
            exif_data = image.getexif()

        if exif_data:

            logger.trace(f"Available EXIF tags:")
            for key, value in exif_data.items():
                tag = TAGS.get(key, key)
                logger.trace(f"{tag}({key}): {value}")

            date_taken: str | None = exif_data.get(0x9003)
            if date_taken:
                data["DateTimeOriginal"] = date_taken.rstrip().rstrip("\x00")
            create_date: str | None = exif_data.get(0x9004)
            if create_date:
                data["CreateDate"] = create_date.rstrip().rstrip("\x00")
            modify_date: str | None = exif_data.get(0x0132)
            if modify_date:
                data["ModifyDate"] = modify_date.rstrip().rstrip("\x00")

            offset_time: str | None = exif_data.get(0x9010)
            if offset_time:
                # time zone for ModifyDate
                data["OffsetTime"] = offset_time.rstrip().rstrip("\x00")
            offset_time_original: str | None = exif_data.get(0x9011)
            if offset_time_original:
                # time zone for DateTimeOriginal
                data["OffsetTimeOriginal"] = offset_time_original.rstrip().rstrip(
                    "\x00"
                )
            offset_time_digitized: str | None = exif_data.get(0x9012)
            if offset_time_digitized:
                # time zone for CreateDate
                data["OffsetTimeDigitized"] = offset_time_digitized.rstrip().rstrip(
                    "\x00"
                )

            unique_camera_model: str | None = exif_data.get(0xC614)
            if unique_camera_model:
                data["UniqueCameraModel"] = unique_camera_model.rstrip().rstrip("\x00")
            localized_camera_model: str | None = exif_data.get(0xC615)
            if localized_camera_model:
                data["LocalizedCameraModel"] = localized_camera_model.rstrip().rstrip(
                    "\x00"
                )
            camera_make: str | None = exif_data.get(0x010F)
            if camera_make:
                data["Make"] = camera_make.rstrip().rstrip("\x00")
            camera_model: str | None = exif_data.get(0x0110)
            if camera_model:
                data["Model"] = camera_model.rstrip().rstrip("\x00")

            gps_data = exif_data.get_ifd(0x8825)
            if gps_data:
                gps_dict: dict[str, str] = {}
                for key, val in gps_data.items():
                    gps_tag = GPSTAGS.get(key, key)
                    gps_dict[str(gps_tag).rstrip().rstrip("\x00")] = (
                        str(val).rstrip().rstrip("\x00")
                    )
                data["GPSInfo"] = gps_dict
        del image
    elif image_path.suffix.lower() in video_extensions:
        media_info = MediaInfo.parse(image_path)
        if (
            media_info
            and media_info.general_tracks
            and len(media_info.general_tracks) > 0
        ):
            logger.trace(json.dumps(media_info.to_data(), indent=2))
            general_track = media_info.general_tracks[0]
            if general_track.performer:
                data["Model"] = general_track.performer
            if general_track.xyz:
                xyz = general_track.xyz.strip("/")
                matches = re.findall(r"[+-]\d+\.\d+", xyz)
                latitude, longitude = map(float, matches)
                gps_dict: dict[str, str] = {}
                gps_dict["GPSLatitude"] = f"{latitude}"
                gps_dict["GPSLongitude"] = f"{longitude}"
                data["GPSInfo"] = gps_dict
            if general_track.tagged_date:
                data["DateTimeOriginal"] = general_track.tagged_date
            if general_track.file_last_modification_date:
                data["ModifyDate"] = general_track.file_last_modification_date
            if general_track.file_creation_date:
                data["CreateDate"] = general_track.file_creation_date
        del media_info

    logger.debug(f"{image_path.name} Data:")
    for tag, value in data.items():
        logger.debug(f"  {tag}: {value}")
    return data


def sanitize_filename(filename: str) -> str:
    """Sanitize filename for filesystem compatibility."""
    # Remove or replace problematic characters
    filename = re.sub(r'[<>:"/\\|?*]', "", filename)
    filename = re.sub(r" ", "_", filename)
    return filename


def generate_final_filename(
    extension: str,
    date: datetime,
    location: GeoData | None,
    camera_model: str | None,
) -> Path | None:
    """Generate the final filename for the processed image."""

    year = date.year
    month = date.month
    day = date.day
    hour = date.hour
    minute = date.minute
    second = date.second

    folder = Path(f"{year}")
    folder /= f"{month:02d}-{year}"
    location_str: str | None = None
    if location:
        address = location.address
        if address:
            city = (
                address.get("city")
                or address.get("town")
                or address.get("village")
                or address.get("county")
            )
            country = address.get("country")
            if city and country:
                location_str = f"{city}_{country}"
            else:
                logger.warning(
                    f"Location data incomplete, geotag = {location.display_name if location.display_name else 'None'}"
                )
        if location_str:
            location_str = sanitize_filename(location_str)
            folder /= f"{month:02d}-{year}-{location_str}"

    # Construct the new filename
    new_filename = f"{year}-{month:02d}-{day:02d}_T{hour:02d}-{minute:02d}-{second:02d}"
    if location_str:
        new_filename += f"_{location_str}"
    if camera_model:
        new_filename += f"_{camera_model}"
    new_filename += extension

    return folder / sanitize_filename(new_filename)


def normalize_dict_results(data: dict[str, str] | str | None) -> str | None:
    """Normalize the dictionary results by stripping whitespace and ensuring all values are strings."""
    if isinstance(data, dict):
        return None
    return data


def normalize_datetime(date_str: str) -> datetime | None:
    """Normalize the datetime string to a consistent format."""
    formats = [
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ]
    utc = date_str.find("UTC") != -1
    temp = date_str.replace(" UTC", "")
    for fmt in formats:
        try:
            date = datetime.strptime(temp, fmt)
            if utc:
                date = date.replace(tzinfo=timezone.utc)
                date = date.astimezone()
            return date
        except ValueError:
            continue

    logger.critical(f"Invalid date format: {date_str}")
    return None


def process_file(file_path: Path, disable_api: bool = False) -> bool:
    """Process a single image file."""
    if (
        file_path.suffix.lower() not in image_extensions
        and file_path.suffix.lower() not in video_extensions
    ):
        return False
    lock = FileLock(str(file_path) + ".lock")

    with lock:
        data = get_image_data(file_path)

    date_original = (
        data.get("DateTimeOriginal")
        or data.get("ModifyDate")
        or data.get("FileModifiedDate")
    )
    if not date_original or not isinstance(date_original, str):
        logger.error(f"Invalid date type: {date_original}, {type(date_original)}")
        return False

    date = normalize_datetime(date_original)
    if not date:
        logger.error(f"Failed to normalize date: {date}")
        return False

    if not disable_api:
        location = data.get("GPSInfo")
        if location and isinstance(location, dict):
            lat_ref = location.get("GPSLatitudeRef")
            lon_ref = location.get("GPSLongitudeRef")
            if lat_ref and lon_ref:
                lat = convert_string_to_tuple(location.get("GPSLatitude"))
                lon = convert_string_to_tuple(location.get("GPSLongitude"))
                if lat and lon:
                    location = geo_reverse.get_location_from_gps(
                        lat, lat_ref, lon, lon_ref
                    )
                else:
                    location = None
            else:
                lat = float(location.get("GPSLatitude", 0))
                lon = float(location.get("GPSLongitude", 0))
                if lat and lon:
                    location = geo_reverse.get_location_from_lat_lon(lat, lon)
                else:
                    location = None
        else:
            logger.debug(f"No GPS location data found for {file_path.name}.")
            location = None
    else:
        logger.trace(
            "Reverse geocoding API is disabled. Location will not be determined."
        )
        location = None

    temp = data.get("Model")
    if temp and isinstance(temp, dict):
        logger.error(f"Invalid camera model: {temp}")
        return False
    else:
        camera_model = normalize_dict_results(temp)

    destination_path_filename = generate_final_filename(
        file_path.suffix, date, location, camera_model
    )
    if destination_path_filename is None:
        logger.error(
            f"Error generating destination path filename for {file_path.name}."
        )
        return False

    destination = destination_directory / destination_path_filename

    if dry_run:
        logger.no_header(f"[DRY RUN] {file_path.name} -> {destination_path_filename}")
    else:
        with lock:
            destination_path = destination.parent
            destination_path.mkdir(parents=True, exist_ok=True)
            if move_mode:
                shutil.move(file_path, destination)
            else:
                shutil.copy2(file_path, destination)
            logger.debug(f"[DONE] {file_path.name} -> {destination_path_filename}")

    return True


def main():
    global logger, image_extensions, video_extensions, dry_run, move_mode, geo_reverse, destination_directory
    image_extensions = {".jpg", ".jpeg", ".png"}
    video_extensions = {".mp4", ".mkv", ".avi"}
    logger = Logger("PhotoOrganizer", level=LogLevel.INFO)
    geo_reverse = ReverseGeocoder(
        logger=logger, user_agent="PhotoOrganizer/0.1", resolution=4.0
    )

    Image.MAX_IMAGE_PIXELS = (
        None  # disable pixel count limit to avoid errors with large images
    )

    parser = argparse.ArgumentParser(
        description="Organize photos into structured folders"
    )
    parser.add_argument("source", help="Source directory containing photos")
    parser.add_argument(
        "destination", help="Destination directory for organized photos"
    )
    parser.add_argument(
        "--move", action="store_true", help="Move files instead of copying them"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without actually doing it",
    )
    parser.add_argument("--offline", action="store_true", help="Disable online calls")

    args = parser.parse_args()

    # Validate source directory
    source_dir = Path(args.source)
    if not source_dir.exists():
        logger.critical(f"Source directory '{source_dir}' does not exist")

    if not source_dir.is_dir():
        logger.critical(f"Source '{source_dir}' is not a directory")

    destination_directory = Path(args.destination)
    if (
        destination_directory.exists()
        and destination_directory.is_dir()
        and len(os.listdir(destination_directory)) > 0
    ):
        logger.critical(
            f"Destination directory '{destination_directory}' is not empty."
        )

    move_mode = args.move
    dry_run = args.dry_run
    offline_mode = args.offline
    # dry_run = True

    if move_mode:
        # logger.warning("Move mode is enabled. Files will be moved instead of copied.")
        logger.critical("Move mode does not work at the moment.")

    logger.info(f"Starting photo organization...")
    logger.info(f"Source: {source_dir}")
    logger.info(f"Destination: {destination_directory}")
    logger.info(f"Mode: {'Move' if move_mode else 'Copy'}")
    logger.info(f"Dry run: {'Yes' if dry_run else 'No'}")
    logger.info("-" * 50)
    logger.info_no_header("")

    # Find all files
    image_files: list[Path] = []
    for ext in image_extensions:
        image_files.extend(source_dir.rglob(f"*{ext}"))
        image_files.extend(source_dir.rglob(f"*{ext.upper()}"))

    for ext in video_extensions:
        image_files.extend(source_dir.rglob(f"*{ext}"))
        image_files.extend(source_dir.rglob(f"*{ext.upper()}"))

    image_files.sort()

    processed_count = 0
    errors: list[Path] = []

    for file_path in image_files:
        if process_file(file_path, offline_mode):
            processed_count += 1
        else:
            logger.error(f"Error processing {file_path}")
            errors.append(file_path)
        logger.info_progress(
            processed_count + len(errors),
            len(image_files),
            prefix="Processing",
            bar_length=50,
        )
        # if processed_count > 10:
        #     break
    logger.end_progress()

    logger.info_no_header("")
    logger.info("-" * 50)
    logger.info(f"Organization complete!")
    logger.info(f"Processed: {processed_count} files")
    logger.info(f"Errors: {len(errors)} files")
    if errors:
        logger.info(f"Error details:")
        for error in errors:
            logger.info(f" - {error}")


if __name__ == "__main__":
    main()
