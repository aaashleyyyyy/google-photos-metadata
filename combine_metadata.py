import os
import sys
import json
import argparse
import exiftool
from pathlib import Path
import logging
import subprocess
import csv
import shlex

SUPPORTED_EXTENSIONS = ['.jpg', '.jpeg', '.png', 'heic', '.mp4', '.mov']

def find_image_json_pairs(directory):
    pairs = []
    missing_metadata = []
    for file in os.listdir(directory):
        if any(file.lower().endswith(ext) for ext in SUPPORTED_EXTENSIONS):
            image_path = os.path.join(directory, file)
            # Support both .supplemental-metadata.json and .suppl.json
            json_path_1 = image_path + '.supplemental-metadata.json'
            json_path_2 = image_path + '.suppl.json'
            if os.path.exists(json_path_1):
                pairs.append((image_path, json_path_1))
            elif os.path.exists(json_path_2):
                pairs.append((image_path, json_path_2))
            else:
                missing_metadata.append(image_path)
    return pairs, missing_metadata

def flatten_json(y, prefix=''):
    out = {}
    for k, v in y.items():
        if isinstance(v, dict):
            out.update(flatten_json(v, prefix + k + '_'))
        else:
            out[prefix + k] = v
    return out

def map_json_to_exif_xmp(flat_metadata, is_video=False):
    # Prefer photoTakenTime for creation date, fallback to creationTime
    photo_taken_timestamp = flat_metadata.get('photoTakenTime_timestamp')
    creation_timestamp = flat_metadata.get('creationTime_timestamp')
    best_timestamp = photo_taken_timestamp or creation_timestamp
    mapping_img = {
        'title': 'XMP:Title',
        'description': 'XMP:Description',
        'imageViews': 'XMP:ImageViews',
        'geoData_latitude': 'EXIF:GPSLatitude',
        'geoData_longitude': 'EXIF:GPSLongitude',
        'geoData_altitude': 'EXIF:GPSAltitude',
        'url': 'XMP:URL',
        'googlePhotosOrigin_mobileUpload_deviceType': 'XMP:DeviceType',
    }
    mapping_vid = {
        'title': 'XMP:Title',
        'description': 'XMP:Description',
        'url': 'XMP:URL',
    }
    exif_xmp_data = {}
    if is_video:
        for k, v in flat_metadata.items():
            if k in mapping_vid:
                tag = mapping_vid[k]
                exif_xmp_data[tag] = v
        # Set video date tags
        if best_timestamp:
            import datetime
            dt = datetime.datetime.fromtimestamp(int(best_timestamp))
            date_str = dt.strftime('%Y:%m:%d %H:%M:%S')
            exif_xmp_data['XMP:CreateDate'] = date_str
            exif_xmp_data['QuickTime:CreateDate'] = date_str
            exif_xmp_data['QuickTime:ModifyDate'] = date_str
            exif_xmp_data['QuickTime:ContentCreateDate'] = date_str
            exif_xmp_data['QuickTime:ContentModifyDate'] = date_str
    else:
        for k, v in flat_metadata.items():
            if k in mapping_img:
                tag = mapping_img[k]
                exif_xmp_data[tag] = v
        if best_timestamp:
            import datetime
            dt = datetime.datetime.fromtimestamp(int(best_timestamp))
            date_str = dt.strftime('%Y:%m:%d %H:%M:%S')
            exif_xmp_data['EXIF:DateTimeOriginal'] = date_str
            exif_xmp_data['XMP:CreateDate'] = date_str
    return exif_xmp_data


def embed_metadata(image_path, metadata):
    ext = os.path.splitext(image_path)[1].lower()
    is_video = ext in ['.mp4', '.mov']
    exif_xmp_data = map_json_to_exif_xmp(metadata, is_video=is_video)
    args = []
    for tag, value in exif_xmp_data.items():
        args.append(f'-{tag}={value}')
    args.append('-overwrite_original')
    with exiftool.ExifTool() as et:
        et.execute(*args, image_path)

def embed_metadata_ffmpeg(video_path, metadata):
    """
    Embed metadata into a video file using ffmpeg. Writes to a new file with _withmeta before the extension.
    For .mov input, output as .mp4 to avoid codec/container issues.
    """
    # Extract fields
    title = metadata.get('title', '')
    description = metadata.get('description', '')
    # Prefer photoTakenTime, fallback to creationTime
    photo_taken_timestamp = metadata.get('photoTakenTime_timestamp')
    creation_timestamp = metadata.get('creationTime_timestamp')
    best_timestamp = photo_taken_timestamp or creation_timestamp
    creation_time = ''
    if best_timestamp:
        import datetime
        dt = datetime.datetime.fromtimestamp(int(best_timestamp))
        creation_time = dt.strftime('%Y-%m-%dT%H:%M:%S')
    # Build ffmpeg command
    base, ext = os.path.splitext(video_path)
    # If input is .mov, output as .mp4
    if ext.lower() == '.mov':
        output_path = f"{base}_withmeta.mp4"
    else:
        output_path = f"{base}_withmeta{ext}"
    cmd = [
        'ffmpeg', '-y', '-i', video_path,
        '-metadata', f'title={title}',
        '-metadata', f'comment={description}'
    ]
    if creation_time:
        cmd += ['-metadata', f'creation_time={creation_time}']
    cmd += ['-codec', 'copy', output_path]
    logging.info(f"Running ffmpeg to embed metadata: {' '.join(shlex.quote(str(c)) for c in cmd)}")
    subprocess.run(cmd, check=True)
    logging.info(f"Created video with embedded metadata: {output_path}")

def create_csv_manifest(directory):
    files = os.listdir(directory)
    # Only use original media files (not _withmeta) for base names
    media_files = [f for f in files if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS and '_withmeta' not in os.path.splitext(f)[0]]
    rows = []
    for media in media_files:
        base_name = os.path.splitext(media)[0]
        ext = os.path.splitext(media)[1].lower()
        manifest = {
            'base_name': base_name,
            'image': '',
            'video': '',
            'video_withmeta': '',
            'metadata': ''
        }
        if ext in ['.jpg', '.jpeg', '.png', '.heic']:
            manifest['image'] = media
        elif ext in ['.mp4', '.mov']:
            manifest['video'] = media
        # Look for metadata file (support both .supplemental-metadata.json and .suppl.json)
        metadata_candidates = [
            f"{media}.supplemental-metadata.json",
            f"{media}.suppl.json"
        ]
        for metadata_file in metadata_candidates:
            if metadata_file in files:
                manifest['metadata'] = metadata_file
                break
        # Look for withmeta.mp4 file (always .mp4)
        withmeta_file = f"{base_name}_withmeta.mp4"
        if withmeta_file in files:
            manifest['video_withmeta'] = withmeta_file
        rows.append(manifest)
    csv_path = os.path.join(directory, 'manifest.csv')
    with open(csv_path, 'w', newline='') as csvfile:
        fieldnames = ['base_name', 'image', 'video', 'video_withmeta', 'metadata']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    logging.info(f"Created CSV manifest: {csv_path}")

def set_creation_date_for_all_images(directory, default_date='2000:01:01 00:00:00'):
    """
    For each image in the directory, set the file system creation date to EXIF DateTimeOriginal if present,
    otherwise set to the provided default_date (format: YYYY:MM:DD HH:MM:SS).
    """
    for file in os.listdir(directory):
        ext = os.path.splitext(file)[1].lower()
        if ext in ['.jpg', '.jpeg', '.png', '.heic']:
            image_path = os.path.join(directory, file)
            # Try to set from DateTimeOriginal
            result = subprocess.run([
                'exiftool',
                '-overwrite_original',
                '-FileCreateDate<DateTimeOriginal',
                image_path
            ], capture_output=True, text=True)
            if '1 image files updated' not in result.stdout:
                # If not updated, set to default date
                subprocess.run([
                    'exiftool',
                    '-overwrite_original',
                    f'-FileCreateDate={default_date}',
                    image_path
                ], check=True)
                logging.info(f"Set default creation date for {file}")
            else:
                logging.info(f"Set creation date from EXIF for {file}")

def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    parser = argparse.ArgumentParser(description='Embed Google Takeout JSON metadata into images.')
    parser.add_argument('directory', type=str, help='Directory containing images and JSON metadata')
    args = parser.parse_args()
    
    directory = args.directory
    if not os.path.isdir(directory):
        logging.error(f"Error: {directory} is not a valid directory.")
        sys.exit(1)

    pairs, missing_metadata = find_image_json_pairs(directory)
    if not pairs:
        logging.warning("No image/JSON pairs found.")
        sys.exit(0)

    if missing_metadata:
        logging.warning("The following image files do not have corresponding metadata:")
        for img in missing_metadata:
            logging.warning(f"No metadata: {os.path.basename(img)}")

    logging.info(f"Found {len(pairs)} image/JSON pairs. Ready to embed metadata.")
    logging.info("Listing all image/JSON pairs:")
    for image_path, json_path in pairs:
        logging.info(f"Image: {os.path.basename(image_path)} <-> JSON: {os.path.basename(json_path)}")
    for image_path, json_path in pairs:
        try:
            logging.info(f"Processing {os.path.basename(image_path)} with metadata {os.path.basename(json_path)}")
            with open(json_path, 'r') as f:
                metadata = json.load(f)
            flat_metadata = flatten_json(metadata)
            ext = os.path.splitext(image_path)[1].lower()
            if ext in ['.mp4', '.mov']:
                embed_metadata_ffmpeg(image_path, flat_metadata)
            else:
                embed_metadata(image_path, flat_metadata)
            logging.info(f"Embedded metadata into {os.path.basename(image_path)}")
            # Set file system dates to match DateTimeOriginal (for images only)
            if ext not in ['.mp4', '.mov']:
                subprocess.run([
                    'exiftool',
                    '-overwrite_original',
                    '-FileCreateDate<DateTimeOriginal',
                    image_path
                ], check=True)
                logging.info(f"Updated file system dates for {os.path.basename(image_path)}")
        except Exception as e:
            logging.error(f"Failed to embed metadata for {image_path}: {e}")
    create_csv_manifest(directory)
    set_creation_date_for_all_images(directory)

if __name__ == '__main__':
    main() 