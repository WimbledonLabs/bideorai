#!/usr/bin/env python3.6

import argparse
import json
import logging
import os
import shlex
import string
import subprocess
import sys
import tempfile

from pathlib import Path
from pprint import pprint

from common import Config, VideoInfo

DESIRED_VIDEO_CODEC = 'h264'
DESIRED_AUDIO_CODEC = 'aac'

def print_command(args):
    print(' '.join(shlex.quote(str(arg)) for arg in args))

def stream_language(stream):
    # Grab the subtitle language from the subtitle metadata
    # Use the walrus operator for Python 3.8
    if 'tags' in stream:
        if 'language' in stream['tags']:
            return stream['tags']['language']

    return lang

def packager_attrs_args(attrs):
    return ",".join(f"{k}={v}" for k,v in attrs.items())

parser = argparse.ArgumentParser()
parser.add_argument("video_info_path", help="VideoInfo json path")
parser.add_argument("-n", "--dry-run", action="store_true")

script_dir = Path(__file__).resolve().parent
config_file = script_dir.joinpath("post_process_config.json")
config = Config.from_json(config_file.read_text())

log_location = (
    Path(config.base_log_location) / Path(__file__).with_suffix(".log").parts[-1]
)

print(f"Logging to {log_location}", file=sys.stderr)
log_location.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=log_location, level=logging.DEBUG)

args = parser.parse_args()
video_info = VideoInfo.from_json(Path(args.video_info_path).read_text())

b2_bucket = config.b2_bucket
b2_dir = f"content/{video_info.tvdbid}/S{video_info.season:02}E{video_info.episode:02}"

# Ensure we have all the commands we need
ffprobe = subprocess.check_output('which ffprobe'.split()).decode().strip()
ffmpeg = subprocess.check_output('which ffmpeg'.split()).decode().strip()
packager = subprocess.check_output('which packager'.split()).decode().strip()
b2 = subprocess.check_output('which b2'.split()).decode().strip()

# Get video details with ffprobe
ffprobe_args = [ffprobe] + "-v quiet -print_format json -show_format -show_streams".split() + [video_info.video_path]
print_command(ffprobe_args)
json_result = subprocess.check_output(ffprobe_args)
obj = json.loads(json_result)

streams = {}
for stream in obj['streams']:
    streams.setdefault(stream['codec_type'], []).append(stream)

if not streams['video']:
    print("Could not find any video streams, found codec types:", list(streams))
    sys.exit(1)

if len(streams['video']) > 1:
    print("Found multiple video streams:")
    pprint(streams['video'])
    sys.exit(1)

assert Path(video_info.video_path).suffix == '.mkv'
assert obj['format']['format_long_name'] == "Matroska / WebM"

video_stream = streams['video'][0]
video_needs_transcoding = video_stream['codec_name'] != DESIRED_VIDEO_CODEC

audio_stream = streams['audio'][0]
maybe_audio_language = stream_language(audio_stream)
audio_needs_transcoding = audio_stream['codec_name'] != DESIRED_AUDIO_CODEC

ffmpeg_args = [ffmpeg, '-i', video_info.video_path]

if video_needs_transcoding:
    ffmpeg_args += ['-c:v', DESIRED_VIDEO_CODEC]
else:
    ffmpeg_args += ['-c:v', 'copy']

if audio_needs_transcoding:
    ffmpeg_args += ['-c:a', DESIRED_AUDIO_CODEC]
else:
    ffmpeg_args += ['-c:a', 'copy']


# TODO: grab subs

# TODO: create multiple audio channels?

# TODO: what if the input is an mp4 and doesn't need a codec change?

# Randomize the intermediate file names so that multiple invocations of this
# script can run in parallel. We don't _expect_ them to run in parallel, but
# don't want to debug corrupt/incorrect data in the future based on that
# assumption.
with tempfile.TemporaryDirectory() as d:
    print("Executing in temporary directory", d)
    os.chdir(d)

    intermediate_mp4 = f"converted.mp4"

    # Create an mp4 file, which is the only video container that `packager` supports
    ffmpeg_args += [str(intermediate_mp4)]
    print_command(ffmpeg_args)
    if not args.dry_run:
        subprocess.check_call(ffmpeg_args)

    # Demux the video and audio streams from the intermediate mp4, and create an
    # dash video manifest which references those streams. We set the base_url to
    # reference the location where we're _intending_ to put the demuxed files.

    video_file = "video.mp4"
    video_attrs = {
        "in": intermediate_mp4,
        "stream": "video",
        "output": video_file,
    }

    audio_file = "audio.mp4"
    audio_attrs = {
        "in": intermediate_mp4,
        "stream": "audio",
        "output": audio_file,
    }
    if maybe_audio_language:
        audio_attrs['lang'] = maybe_audio_language

    mpd_input_streams = [
        packager_attrs_args(video_attrs),
        packager_attrs_args(audio_attrs),
    ]

    upload_list = [video_file, audio_file]

    # Also include subtitles, if available
    has_subs = 'subtitle' in streams
    for i, stream in enumerate(streams.get('subtitle', [])):
        output_sub_file = f"subs_{i}.vtt"
        intermediate_sub_file = f"in_subs_{i}.vtt"
        ffmpeg_sub_extraction_args = [ffmpeg, '-i', video_info.video_path, '-map', f'0:s:{i}', intermediate_sub_file]
        print_command(ffmpeg_sub_extraction_args )
        if not args.dry_run:
            subprocess.check_call(ffmpeg_sub_extraction_args)

        sub_attrs = {
            'in': intermediate_sub_file,
            'stream': 'text',
            'output': output_sub_file,
        }

        maybe_sub_language = stream_language(stream)
        if maybe_sub_language:
            sub_attrs['lang'] = maybe_sub_language

        mpd_input_streams.append(packager_attrs_args(sub_attrs))
        upload_list.append(output_sub_file)

    packager_args = [
        packager,
        *mpd_input_streams,
        "--mpd_output",
        Path(video_info.video_path).with_suffix(".mpd"),
        "--base_urls",
        f"https://f000.backblazeb2.com/file/{b2_bucket}/{b2_dir}/",
    ]
    print_command(packager_args)
    if not args.dry_run:
        subprocess.check_call(packager_args)

    for file_name in upload_list:
        b2_args = [
            "b2",
            "upload-file",
            b2_bucket,
            file_name,
            f"{b2_dir}/{file_name}"
        ]
        print_command(b2_args)
        if not args.dry_run:
            subprocess.check_call(b2_args)

    print("Done processing. Cleaning up...")
