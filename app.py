import os
import re
import json
import glob
import shutil
import tempfile
import subprocess
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp


# ============================================================
# APP
# ============================================================

app = Flask(__name__)

CORS(
    app,
    resources={r"/*": {"origins": "*"}},
    methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"],
)


# ============================================================
# PATHS
# ============================================================

DENO_DIR = "/opt/render/project/.deno/bin"

if os.path.isdir(DENO_DIR):
    os.environ["PATH"] = DENO_DIR + os.pathsep + os.environ.get("PATH", "")

COOKIE_PATHS = [
    "/etc/secrets/cookies.txt",
    "/tmp/cookies.txt",
]

COOKIE_PATH = None

for p in COOKIE_PATHS:
    if os.path.exists(p):
        COOKIE_PATH = p
        break


# ============================================================
# CONSTANTS
# ============================================================

POT_PROVIDER = "https://vdownloader-pot.onrender.com"

DOWNLOAD_TIMEOUT = 180

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

INSTAGRAM_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.instagram.com/",
}

DOWNLOAD_DIR = "/tmp/vdownloader"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ============================================================
# LOG CAPTURE
# ============================================================

class LogCapture:
    def __init__(self):
        self.logs = []

    def debug(self, msg):
        self.logs.append(str(msg))

    def warning(self, msg):
        self.logs.append("WARNING: " + str(msg))

    def error(self, msg):
        self.logs.append("ERROR: " + str(msg))


# ============================================================
# UTILITIES
# ============================================================

def clean_url(url):
    if not url:
        return ""

    url = url.strip()

    if url.startswith("<") and url.endswith(">"):
        url = url[1:-1]

    return url


def is_instagram(url):
    host = urlparse(url).netloc.lower()
    return "instagram.com" in host or "instagr.am" in host


def is_youtube(url):
    host = urlparse(url).netloc.lower()
    return (
        "youtube.com" in host
        or "youtu.be" in host
        or "youtube-nocookie.com" in host
    )


def is_facebook(url):
    host = urlparse(url).netloc.lower()
    return (
        "facebook.com" in host
        or "fb.watch" in host
        or "m.facebook.com" in host
    )


def ffmpeg_path():
    possible = [
        shutil.which("ffmpeg"),
        "/usr/bin/ffmpeg",
        "/opt/render/project/src/.venv/bin/ffmpeg",
    ]

    for p in possible:
        if p and os.path.exists(p):
            return p

    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass

    return None


FFMPEG = ffmpeg_path()


def run_ffmpeg(args, timeout=180):
    if not FFMPEG:
        raise RuntimeError("FFmpeg not found")

    cmd = [FFMPEG] + args

    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def copy_cookie_file():
    global COOKIE_PATH

    if COOKIE_PATH and os.path.exists(COOKIE_PATH):
        return COOKIE_PATH

    for src in COOKIE_PATHS:
        if os.path.exists(src):
            try:
                shutil.copyfile(src, "/tmp/cookies.txt")
                COOKIE_PATH = "/tmp/cookies.txt"
                return COOKIE_PATH
            except Exception:
                pass

    return None


def find_downloaded_file(folder):
    files = []

    for root, dirs, names in os.walk(folder):
        for name in names:
            path = os.path.join(root, name)

            if os.path.isfile(path):
                if not name.endswith((".part", ".ytdl", ".temp")):
                    files.append(path)

    if not files:
        return None

    files.sort(
        key=lambda x: os.path.getsize(x),
        reverse=True,
    )

    return files[0]


def safe_filename(name):
    name = name or "vdownloader"

    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()

    return name[:150] or "vdownloader"


# ============================================================
# COOKIE / YTDLP OPTIONS
# ============================================================

def common_ydl_options(log=None):
    opts = {
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": USER_AGENT,
        },
        "logger": log,
    }

    cookie = copy_cookie_file()

    if cookie:
        opts["cookiefile"] = cookie

    return opts


def instagram_options(log=None, cookies=True):
    opts = common_ydl_options(log)

    opts.update({
        "http_headers": INSTAGRAM_HEADERS.copy(),
        "extractor_args": {
            "instagram": {
                "webpage_skip": [],
            }
        },
    })

    if not cookies:
        opts.pop("cookiefile", None)

    return opts


def youtube_options(log=None):
    opts = common_ydl_options(log)

    opts.update({
        "format": (
            "bv*+ba/b"
        ),
        "merge_output_format": "mp4",
        "extractor_args": {
            "youtube": {
                "player_client": [
                    "web_safari",
                    "web"
                ],
                "po_token": [
                    "web+"
                    + POT_PROVIDER
                ],
            }
        },
    })

    return opts


def facebook_options(log=None):
    opts = common_ydl_options(log)

    opts.update({
        "format": (
            "best[vcodec!=none][acodec!=none]"
            "/best"
        ),
        "merge_output_format": "mp4",
    })

    return opts


# ============================================================
# AUDIO CHECK
# ============================================================

def has_audio(path):
    if not path or not os.path.exists(path):
        return False

    if not FFMPEG:
        return False

    try:
        result = run_ffmpeg([
            "-hide_banner",
            "-i",
            path,
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-"
        ], timeout=60)

        text = (result.stdout or "") + "\n" + (result.stderr or "")

        return (
            result.returncode == 0
            and "Audio:" in text
        )

    except Exception:
        return False


# ============================================================
# VIDEO STREAM CHECK
# ============================================================

def has_video(path):
    if not path or not os.path.exists(path):
        return False

    if not FFMPEG:
        return False

    try:
        result = run_ffmpeg([
            "-hide_banner",
            "-i",
            path,
            "-map",
            "0:v:0",
            "-f",
            "null",
            "-"
        ], timeout=60)

        text = (result.stdout or "") + "\n" + (result.stderr or "")

        return (
            result.returncode == 0
            and "Video:" in text
        )

    except Exception:
        return False


# ============================================================
# INSTAGRAM REEL ID
# ============================================================

def instagram_shortcode(url):
    patterns = [
        r"/reel/([A-Za-z0-9_-]+)",
        r"/reels/([A-Za-z0-9_-]+)",
        r"/p/([A-Za-z0-9_-]+)",
        r"/tv/([A-Za-z0-9_-]+)",
    ]

    for pattern in patterns:
        m = re.search(pattern, url)

        if m:
            return m.group(1)

    return None


def shortcode_to_media_id(shortcode):
    """
    Instagram shortcode -> numeric media id.

    Uses the same base64url concept used by Instagram/yt-dlp.
    """

    if not shortcode:
        return None

    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

    value = 0

    try:
        for char in shortcode:
            value = (value << 6) + alphabet.index(char)

        return str(value)

    except Exception:
        return None


# ============================================================
# INSTAGRAM HTTP SESSION
# ============================================================

def instagram_session():
    session = requests.Session()

    session.headers.update(INSTAGRAM_HEADERS)

    cookie = copy_cookie_file()

    if cookie:
        try:
            with open(cookie, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()

                    if not line or line.startswith("#"):
                        continue

                    parts = line.split("\t")

                    if len(parts) >= 7:
                        domain = parts[0]
                        name = parts[5]
                        value = parts[6]

                        if "instagram.com" in domain:
                            session.cookies.set(
                                name,
                                value,
                                domain=domain
                            )
        except Exception:
            pass

    return session


# ============================================================
# JSON SEARCH HELPERS
# ============================================================

def walk_json(obj, callback, results=None):
    if results is None:
        results = []

    try:
        if callback(obj):
            results.append(obj)
    except Exception:
        pass

    if isinstance(obj, dict):
        for value in obj.values():
            walk_json(value, callback, results)

    elif isinstance(obj, list):
        for value in obj:
            walk_json(value, callback, results)

    return results


def collect_urls_from_json(obj):
    urls = []

    def cb(value):
        if isinstance(value, str):
            low = value.lower()

            if (
                low.startswith("http://")
                or low.startswith("https://")
            ):
                if (
                    ".mp4" in low
                    or ".m4a" in low
                    or ".aac" in low
                    or "video" in low
                    or "audio" in low
                    or "dash" in low
                ):
                    urls.append(value)

        return False

    walk_json(obj, cb)

    unique = []

    for u in urls:
        if u not in unique:
            unique.append(u)

    return unique


# ============================================================
# INSTAGRAM PAGE HTML
# ============================================================

def extract_json_scripts(html):
    scripts = []

    patterns = [
        r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
        r'<script[^>]*data-sjs[^>]*>(.*?)</script>',
    ]

    for pattern in patterns:
        for match in re.finditer(
            pattern,
            html,
            re.I | re.S
        ):
            raw = match.group(1).strip()

            if not raw:
                continue

            try:
                obj = json.loads(raw)
                scripts.append(obj)
            except Exception:
                continue

    return scripts


def find_media_objects(obj):
    found = []

    def cb(value):
        if not isinstance(value, dict):
            return False

        keys = set(value.keys())

        media_keys = {
            "video_versions",
            "audio_versions",
            "dash_manifest",
            "video_url",
            "audio_url",
            "video_duration",
            "clips_metadata",
            "music_metadata",
            "original_sound_info",
        }

        if keys.intersection(media_keys):
            found.append(value)

        return False

    walk_json(obj, cb)

    return found


def parse_instagram_html(html):
    result = {
        "video_urls": [],
        "audio_urls": [],
        "dash_manifests": [],
        "media_objects": [],
    }

    scripts = extract_json_scripts(html)

    for obj in scripts:
        media_objects = find_media_objects(obj)

        for media in media_objects:
            result["media_objects"].append(media)

            # --------------------------------------------
            # video_versions
            # --------------------------------------------

            vv = media.get("video_versions")

            if isinstance(vv, list):
                for item in vv:
                    if isinstance(item, dict):
                        u = item.get("url")

                        if isinstance(u, str):
                            result["video_urls"].append(u)

            # --------------------------------------------
            # audio_versions
            # --------------------------------------------

            av = media.get("audio_versions")

            if isinstance(av, list):
                for item in av:
                    if isinstance(item, dict):
                        u = item.get("url")

                        if isinstance(u, str):
                            result["audio_urls"].append(u)

            # --------------------------------------------
            # direct URLs
            # --------------------------------------------

            for key in [
                "video_url",
                "audio_url",
            ]:
                u = media.get(key)

                if isinstance(u, str):
                    if key == "video_url":
                        result["video_urls"].append(u)
                    else:
                        result["audio_urls"].append(u)

            # --------------------------------------------
            # DASH
            # --------------------------------------------

            dash = media.get("dash_manifest")

            if isinstance(dash, str):
                result["dash_manifests"].append(dash)

    # Remove duplicates
    for key in [
        "video_urls",
        "audio_urls",
        "dash_manifests",
    ]:
        unique = []

        for item in result[key]:
            if item not in unique:
                unique.append(item)

        result[key] = unique

    return result


# ============================================================
# INSTAGRAM PAGE REQUESTS
# ============================================================

def fetch_instagram_page(url, log=None):
    session = instagram_session()

    urls = [
        url,
        url.split("?")[0],
        url.split("?")[0].rstrip("/") + "/",
        url.split("?")[0].rstrip("/") + "/embed/",
    ]

    responses = []

    for page_url in urls:

        try:
            if log:
                log.debug(
                    "[Instagram] Fetching page: "
                    + page_url
                )

            response = session.get(
                page_url,
                timeout=30,
                allow_redirects=True,
            )

            if response.status_code == 200:

                responses.append({
                    "url": page_url,
                    "status": response.status_code,
                    "html": response.text,
                })

        except Exception as e:
            if log:
                log.warning(
                    "[Instagram] Page request failed: "
                    + str(e)
                )

    return responses


# ============================================================
# INSTAGRAM API INFO
# ============================================================

def instagram_api_info(url, log=None):
    """
    Uses Instagram's media info endpoint only with the
    existing normal session/cookies.

    No authentication bypass is attempted.
    """

    media_id = shortcode_to_media_id(
        instagram_shortcode(url)
    )

    if not media_id:
        return None

    session = instagram_session()

    api_urls = [
        f"https://www.instagram.com/api/v1/media/{media_id}/info/",
    ]

    for api_url in api_urls:

        try:
            if log:
                log.debug(
                    "[Instagram] Trying media info endpoint"
                )

            response = session.get(
                api_url,
                timeout=30,
                headers={
                    **INSTAGRAM_HEADERS,
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )

            if response.status_code != 200:
                if log:
                    log.warning(
                        "[Instagram] Media info HTTP "
                        + str(response.status_code)
                    )
                continue

            try:
                data = response.json()
            except Exception:
                continue

            return data

        except Exception as e:
            if log:
                log.warning(
                    "[Instagram] Media info failed: "
                    + str(e)
                )

    return None


# ============================================================
# EXTRACT MEDIA FROM INSTAGRAM API JSON
# ============================================================

def extract_media_from_api(data):
    result = {
        "video_urls": [],
        "audio_urls": [],
        "dash_manifests": [],
    }

    if not data:
        return result

    objects = []

    if isinstance(data, dict):
        objects.append(data)

        items = data.get("items")

        if isinstance(items, list):
            objects.extend(items)

    elif isinstance(data, list):
        objects.extend(data)

    for root in objects:

        media_objects = find_media_objects(root)

        for media in media_objects:

            vv = media.get("video_versions")

            if isinstance(vv, list):
                for item in vv:
                    if isinstance(item, dict):
                        u = item.get("url")

                        if u:
                            result["video_urls"].append(u)

            av = media.get("audio_versions")

            if isinstance(av, list):
                for item in av:
                    if isinstance(item, dict):
                        u = item.get("url")

                        if u:
                            result["audio_urls"].append(u)

            for key in [
                "video_url",
                "audio_url",
            ]:
                u = media.get(key)

                if isinstance(u, str):

                    if key == "video_url":
                        result["video_urls"].append(u)

                    else:
                        result["audio_urls"].append(u)

            dash = media.get("dash_manifest")

            if isinstance(dash, str):
                result["dash_manifests"].append(dash)

    # Also inspect all strings for obvious media URLs
    all_urls = collect_urls_from_json(data)

    for u in all_urls:

        low = u.lower()

        if (
            ".m4a" in low
            or ".aac" in low
            or "audio" in low
        ):
            result["audio_urls"].append(u)

        elif (
            ".mp4" in low
            or "video" in low
        ):
            result["video_urls"].append(u)

    for key in result:
        unique = []

        for item in result[key]:
            if item not in unique:
                unique.append(item)

        result[key] = unique

    return result


# ============================================================
# DOWNLOAD DIRECT URL
# ============================================================

def download_direct_url(url, folder, filename, headers=None):
    if not url:
        return None

    output = os.path.join(folder, filename)

    request_headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.instagram.com/",
    }

    if headers:
        request_headers.update(headers)

    try:
        with requests.get(
            url,
            headers=request_headers,
            stream=True,
            timeout=(30, 180),
        ) as response:

            response.raise_for_status()

            with open(output, "wb") as f:
                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):
                    if chunk:
                        f.write(chunk)

        if os.path.exists(output) and os.path.getsize(output) > 0:
            return output

    except Exception:
        try:
            if os.path.exists(output):
                os.remove(output)
        except Exception:
            pass

    return None


# ============================================================
# MERGE VIDEO + AUDIO
# ============================================================

def merge_video_audio(video_path, audio_path, output_path):
    if not FFMPEG:
        raise RuntimeError("FFmpeg not available")

    result = run_ffmpeg([
        "-y",
        "-i",
        video_path,
        "-i",
        audio_path,

        "-map",
        "0:v:0",
        "-map",
        "1:a:0",

        "-c:v",
        "copy",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-movflags",
        "+faststart",

        output_path,
    ], timeout=180)

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr[-4000:]
        )

    return output_path


# ============================================================
# DOWNLOAD INSTAGRAM DIRECT MEDIA
# ============================================================

def try_instagram_direct_media(url, log=None):

    if log:
        log.debug(
            "[Instagram] Starting direct media fallback"
        )

    folder = tempfile.mkdtemp(
        prefix="instagram_",
        dir=DOWNLOAD_DIR
    )

    try:
        page_results = fetch_instagram_page(
            url,
            log
        )

        combined = {
            "video_urls": [],
            "audio_urls": [],
            "dash_manifests": [],
        }

        for item in page_results:

            parsed = parse_instagram_html(
                item["html"]
            )

            for key in combined:
                combined[key].extend(
                    parsed[key]
                )

        # ----------------------------------------------------
        # API metadata
        # ----------------------------------------------------

        api_data = instagram_api_info(
            url,
            log
        )

        if api_data:

            api_media = extract_media_from_api(
                api_data
            )

            for key in combined:
                combined[key].extend(
                    api_media[key]
                )

        # ----------------------------------------------------
        # Remove duplicates
        # ----------------------------------------------------

        for key in combined:

            unique = []

            for item in combined[key]:
                if item and item not in unique:
                    unique.append(item)

            combined[key] = unique

        if log:
            log.debug(
                "[Instagram] Direct video URLs: "
                + str(len(combined["video_urls"]))
            )

            log.debug(
                "[Instagram] Direct audio URLs: "
                + str(len(combined["audio_urls"]))
            )

            log.debug(
                "[Instagram] DASH manifests: "
                + str(len(combined["dash_manifests"]))
            )

        # ----------------------------------------------------
        # If we have direct video + audio
        # ----------------------------------------------------

        if (
            combined["video_urls"]
            and combined["audio_urls"]
        ):

            video_url = combined["video_urls"][0]
            audio_url = combined["audio_urls"][0]

            video_path = download_direct_url(
                video_url,
                folder,
                "video.mp4"
            )

            if video_path:

                audio_path = download_direct_url(
                    audio_url,
                    folder,
                    "audio.m4a"
                )

                if audio_path:

                    output = os.path.join(
                        folder,
                        "instagram_final.mp4"
                    )

                    try:
                        merge_video_audio(
                            video_path,
                            audio_path,
                            output
                        )

                        if has_audio(output):
                            if log:
                                log.debug(
                                    "[Instagram] "
                                    "Direct audio merge successful"
                                )

                            return output

                    except Exception as e:
                        if log:
                            log.warning(
                                "[Instagram] "
                                "Direct merge failed: "
                                + str(e)
                            )

        # ----------------------------------------------------
        # Direct video only
        # ----------------------------------------------------

        if combined["video_urls"]:

            video_path = download_direct_url(
                combined["video_urls"][0],
                folder,
                "instagram_direct.mp4"
            )

            if video_path and has_video(video_path):

                if has_audio(video_path):
                    return video_path

        return None

    except Exception as e:

        if log:
            log.warning(
                "[Instagram] Direct fallback error: "
                + str(e)
            )

        return None


# ============================================================
# YT-DLP INFO
# ============================================================

def extract_info(url, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(
            url,
            download=False
        )


# ============================================================
# INSTAGRAM YT-DLP DOWNLOAD
# ============================================================

def instagram_ytdlp_download(
    url,
    log=None,
    use_cookies=True,
    format_selector="bv*+ba/b",
    attempt_name="instagram",
):

    folder = tempfile.mkdtemp(
        prefix="ytig_",
        dir=DOWNLOAD_DIR
    )

    try:

        opts = instagram_options(
            log=log,
            cookies=use_cookies
        )

        opts.update({
            "format": format_selector,
            "merge_output_format": "mp4",

            "outtmpl": os.path.join(
                folder,
                "%(id)s.%(ext)s"
            ),

            "writethumbnail": False,
            "writesubtitles": False,
            "writeinfojson": False,

            "postprocessors": [
                {
                    "key": "FFmpegVideoRemuxer",
                    "preferedformat": "mp4",
                }
            ],
        })

        if log:
            log.debug(
                "[Instagram] yt-dlp attempt: "
                + attempt_name
            )

        with yt_dlp.YoutubeDL(opts) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )

        path = find_downloaded_file(folder)

        if not path:
            return None, info

        return path, info

    except Exception as e:

        if log:
            log.warning(
                "[Instagram] yt-dlp failed: "
                + str(e)
            )

        return None, None


# ============================================================
# INSTAGRAM DOWNLOAD MASTER
# ============================================================

def download_instagram(url, log=None):

    attempts = [

        # ----------------------------------------------------
        # 1. Cookies + best video/audio
        # ----------------------------------------------------

        (
            True,
            "bv*+ba/b",
            "cookies-best"
        ),

        # ----------------------------------------------------
        # 2. Cookies + force combined when available
        # ----------------------------------------------------

        (
            True,
            "best[vcodec!=none][acodec!=none]/bv*+ba/b",
            "cookies-muxed"
        ),

        # ----------------------------------------------------
        # 3. Public
        # ----------------------------------------------------

        (
            False,
            "bv*+ba/b",
            "public-best"
        ),

        # ----------------------------------------------------
        # 4. Public muxed
        # ----------------------------------------------------

        (
            False,
            "best[vcodec!=none][acodec!=none]/bv*+ba/b",
            "public-muxed"
        ),
    ]

    # ========================================================
    # FIRST: direct Instagram metadata fallback
    #
    # We don't do this first because yt-dlp is the primary
    # extractor and already knows current Instagram behavior.
    # ========================================================

    for use_cookies, fmt, name in attempts:

        path, info = instagram_ytdlp_download(
            url,
            log=log,
            use_cookies=use_cookies,
            format_selector=fmt,
            attempt_name=name
        )

        if not path:
            continue

        if has_audio(path):

            if log:
                log.debug(
                    "[Instagram] Audio found using "
                    + name
                )

            return path, info

        if log:
            log.warning(
                "[Instagram] Video downloaded but "
                "NO audio: "
                + name
            )

        # Silent file is deliberately rejected.

        try:
            os.remove(path)
        except Exception:
            pass

    # ========================================================
    # SECOND: Instagram page/API fallback
    # ========================================================

    if log:
        log.debug(
            "[Instagram] yt-dlp returned no audio. "
            "Trying page/API media fallback."
        )

    direct_path = try_instagram_direct_media(
        url,
        log
    )

    if direct_path and has_audio(direct_path):

        return direct_path, {
            "id": instagram_shortcode(url),
            "title": "Instagram Video",
            "direct_fallback": True,
        }

    # ========================================================
    # NOTHING FOUND
    # ========================================================

    if log:
        log.warning(
            "[Instagram] No audio stream was exposed "
            "by available extraction paths."
        )

    return None, None


# ============================================================
# GENERAL YT-DLP DOWNLOAD
# ============================================================

def general_download(url, platform, log=None):

    folder = tempfile.mkdtemp(
        prefix="general_",
        dir=DOWNLOAD_DIR
    )

    try:

        if platform == "youtube":

            opts = youtube_options(log)

        elif platform == "facebook":

            opts = facebook_options(log)

        else:

            opts = common_ydl_options(log)

            opts.update({
                "format": "bv*+ba/b",
                "merge_output_format": "mp4",
            })

        opts["outtmpl"] = os.path.join(
            folder,
            "%(title).100s-%(id)s.%(ext)s"
        )

        opts["noplaylist"] = True

        with yt_dlp.YoutubeDL(opts) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )

        path = find_downloaded_file(folder)

        if not path:
            return None, info

        # ----------------------------------------------------
        # Never return silent video when audio should exist
        # ----------------------------------------------------

        if platform in ("youtube", "facebook"):

            if not has_audio(path):

                if log:
                    log.warning(
                        "["
                        + platform
                        + "] Download completed "
                        "without audio."
                    )

        return path, info

    except Exception as e:

        if log:
            log.error(
                "["
                + platform
                + "] Download failed: "
                + str(e)
            )

        return None, None


# ============================================================
# FORMAT SUMMARY
# ============================================================

def format_summary(info):

    formats = []

    if not info:
        return {
            "format_count": 0,
            "audio_only": 0,
            "video_only": 0,
            "muxed": 0,
        }

    for f in info.get("formats") or []:

        if not isinstance(f, dict):
            continue

        acodec = f.get("acodec")
        vcodec = f.get("vcodec")

        has_a = (
            acodec
            and acodec != "none"
        )

        has_v = (
            vcodec
            and vcodec != "none"
        )

        if has_a and has_v:
            kind = "muxed"

        elif has_a:
            kind = "audio"

        elif has_v:
            kind = "video"

        else:
            kind = "unknown"

        formats.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "width": f.get("width"),
            "height": f.get("height"),
            "vcodec": vcodec,
            "acodec": acodec,
            "kind": kind,
        })

    return {
        "format_count": len(formats),
        "audio_only": sum(
            1 for f in formats
            if f["kind"] == "audio"
        ),
        "video_only": sum(
            1 for f in formats
            if f["kind"] == "video"
        ),
        "muxed": sum(
            1 for f in formats
            if f["kind"] == "muxed"
        ),
        "formats": formats,
    }


# ============================================================
# SAVE RESULT FOR STREAM
# ============================================================

def create_download_token_file(path):
    """
    Simple temporary token file.

    The frontend receives /stream?token=...
    """

    import uuid

    token = uuid.uuid4().hex

    token_path = os.path.join(
        DOWNLOAD_DIR,
        token + ".json"
    )

    with open(token_path, "w", encoding="utf-8") as f:
        json.dump({
            "path": path,
            "created": __import__("time").time(),
        }, f)

    return token


def read_token(token):
    if not token:
        return None

    if not re.fullmatch(
        r"[a-fA-F0-9]{32}",
        token
    ):
        return None

    path = os.path.join(
        DOWNLOAD_DIR,
        token + ".json"
    )

    if not os.path.exists(path):
        return None

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        return data

    except Exception:
        return None


# ============================================================
# ROOT POST
# ============================================================

@app.route("/", methods=["POST", "OPTIONS"])
def download():

    if request.method == "OPTIONS":
        return "", 204

    log = LogCapture()

    try:

        body = request.get_json(
            silent=True
        ) or {}

        url = clean_url(
            body.get("url")
        )

        if not url:

            return jsonify({
                "status": "error",
                "message": "URL is required."
            }), 400

        # ----------------------------------------------------
        # Determine platform
        # ----------------------------------------------------

        if is_instagram(url):
            platform = "instagram"

        elif is_youtube(url):
            platform = "youtube"

        elif is_facebook(url):
            platform = "facebook"

        else:
            platform = "other"

        log.debug(
            "[API] Platform: "
            + platform
        )

        # ----------------------------------------------------
        # Instagram
        # ----------------------------------------------------

        if platform == "instagram":

            path, info = download_instagram(
                url,
                log
            )

        # ----------------------------------------------------
        # YouTube
        # ----------------------------------------------------

        elif platform == "youtube":

            path, info = general_download(
                url,
                "youtube",
                log
            )

        # ----------------------------------------------------
        # Facebook
        # ----------------------------------------------------

        elif platform == "facebook":

            path, info = general_download(
                url,
                "facebook",
                log
            )

        # ----------------------------------------------------
        # Other
        # ----------------------------------------------------

        else:

            path, info = general_download(
                url,
                "other",
                log
            )

        # ----------------------------------------------------
        # Failed
        # ----------------------------------------------------

        if not path:

            return jsonify({
                "status": "error",
                "message": (
                    "Unable to download this video. "
                    "Instagram may not be exposing an "
                    "audio stream for this Reel."
                ),
                "platform": platform,
                "logs": log.logs[-30:],
            }), 400

        # ----------------------------------------------------
        # Verify video
        # ----------------------------------------------------

        if not has_video(path):

            return jsonify({
                "status": "error",
                "message": "Downloaded file has no video stream.",
                "logs": log.logs[-30:],
            }), 500

        # ----------------------------------------------------
        # Instagram must contain audio
        # ----------------------------------------------------

        audio_ok = has_audio(path)

        if platform == "instagram" and not audio_ok:

            try:
                os.remove(path)
            except Exception:
                pass

            return jsonify({
                "status": "error",
                "message": (
                    "Instagram did not expose an audio stream "
                    "for this Reel."
                ),
                "platform": platform,
                "audio": False,
                "logs": log.logs[-40:],
            }), 400

        # ----------------------------------------------------
        # Create token
        # ----------------------------------------------------

        token = create_download_token_file(
            path
        )

        filename = safe_filename(
            (info or {}).get("title")
            or platform + "-video"
        ) + ".mp4"

        return jsonify({
            "status": "success",
            "platform": platform,
            "audio": audio_ok,
            "filename": filename,
            "url": (
                "/stream?token="
                + token
            ),
            "logs": log.logs[-20:],
        })

    except Exception as e:

        return jsonify({
            "status": "error",
            "message": str(e),
            "logs": log.logs[-40:],
        }), 500


# ============================================================
# STREAM
# ============================================================

@app.route("/stream", methods=["GET"])
def stream():

    token = request.args.get(
        "token",
        ""
    )

    data = read_token(token)

    if not data:

        return jsonify({
            "status": "error",
            "message": "Invalid or expired download token."
        }), 404

    path = data.get("path")

    if not path or not os.path.exists(path):

        return jsonify({
            "status": "error",
            "message": "File no longer exists."
        }), 404

    filename = os.path.basename(path)

    def generate():

        try:

            with open(
                path,
                "rb"
            ) as f:

                while True:

                    chunk = f.read(
                        1024 * 1024
                    )

                    if not chunk:
                        break

                    yield chunk

        finally:

            # ------------------------------------------------
            # Cleanup downloaded file
            # ------------------------------------------------

            try:
                os.remove(path)
            except Exception:
                pass

            token_path = os.path.join(
                DOWNLOAD_DIR,
                token + ".json"
            )

            try:
                os.remove(token_path)
            except Exception:
                pass

    return Response(
        stream_with_context(
            generate()
        ),
        mimetype="video/mp4",
        headers={
            "Content-Disposition": (
                'attachment; filename="'
                + filename
                + '"'
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ============================================================
# INSTAGRAM DEBUG
# ============================================================

@app.route("/instagram-debug", methods=["GET", "POST"])
def instagram_debug():

    if request.method == "POST":

        body = request.get_json(
            silent=True
        ) or {}

        url = clean_url(
            body.get("url")
        )

    else:

        url = clean_url(
            request.args.get("url")
        )

    if not url:

        return jsonify({
            "status": "error",
            "message": "Instagram URL required."
        }), 400

    if not is_instagram(url):

        return jsonify({
            "status": "error",
            "message": "Not an Instagram URL."
        }), 400

    log = LogCapture()

    # --------------------------------------------------------
    # yt-dlp extraction WITHOUT downloading
    # --------------------------------------------------------

    info = None

    try:

        opts = instagram_options(
            log=log,
            cookies=True
        )

        with yt_dlp.YoutubeDL(opts) as ydl:

            info = ydl.extract_info(
                url,
                download=False
            )

    except Exception as e:

        log.warning(
            "[Instagram] yt-dlp info failed: "
            + str(e)
        )

    # --------------------------------------------------------
    # Page inspection
    # --------------------------------------------------------

    page_results = fetch_instagram_page(
        url,
        log
    )

    page_data = {
        "pages": len(page_results),
        "video_url_count": 0,
        "audio_url_count": 0,
        "dash_manifest_count": 0,
    }

    for item in page_results:

        parsed = parse_instagram_html(
            item["html"]
        )

        page_data["video_url_count"] += len(
            parsed["video_urls"]
        )

        page_data["audio_url_count"] += len(
            parsed["audio_urls"]
        )

        page_data["dash_manifest_count"] += len(
            parsed["dash_manifests"]
        )

    # --------------------------------------------------------
    # API inspection
    # --------------------------------------------------------

    api_data = instagram_api_info(
        url,
        log
    )

    api_summary = {
        "available": bool(api_data),
        "video_url_count": 0,
        "audio_url_count": 0,
        "dash_manifest_count": 0,
    }

    if api_data:

        parsed_api = extract_media_from_api(
            api_data
        )

        api_summary["video_url_count"] = len(
            parsed_api["video_urls"]
        )

        api_summary["audio_url_count"] = len(
            parsed_api["audio_urls"]
        )

        api_summary["dash_manifest_count"] = len(
            parsed_api["dash_manifests"]
        )

    # --------------------------------------------------------
    # yt-dlp summary
    # --------------------------------------------------------

    summary = format_summary(
        info
    )

    # --------------------------------------------------------
    # Diagnosis
    # --------------------------------------------------------

    if (
        summary["audio_only"] > 0
        or summary["muxed"] > 0
        or page_data["audio_url_count"] > 0
        or api_summary["audio_url_count"] > 0
    ):

        diagnosis = (
            "An audio-capable stream is exposed. "
            "The downloader should be able to merge "
            "video and audio."
        )

    else:

        diagnosis = (
            "No audio stream was exposed by yt-dlp, "
            "Instagram page metadata, or the available "
            "media-info session path. If the Reel contains "
            "music in Instagram, Instagram is currently "
            "not exposing that audio stream to this server."
        )

    return jsonify({
        "status": "ok",
        "url": url,

        "instagram_shortcode": (
            instagram_shortcode(url)
        ),

        "instagram_media_id": (
            shortcode_to_media_id(
                instagram_shortcode(url)
            )
        ),

        "yt_dlp": {
            "extractor": (
                info.get("extractor")
                if info else None
            ),
            "id": (
                info.get("id")
                if info else None
            ),
            "title": (
                info.get("title")
                if info else None
            ),
            **summary,
        },

        "page": page_data,

        "api": api_summary,

        "diagnosis": diagnosis,

        "logs": log.logs[-50:],
    })


# ============================================================
# HEALTH
# ============================================================

@app.route("/health", methods=["GET"])
def health():

    deno = shutil.which("deno")

    return jsonify({
        "status": "ok",
        "deno": bool(deno),
        "ffmpeg": bool(FFMPEG),
        "ffmpeg_path": FFMPEG,
        "cookies": bool(copy_cookie_file()),
        "yt_dlp": getattr(
            yt_dlp,
            "__version__",
            "unknown"
        ),
    })


# ============================================================
# CLEANUP OLD FILES
# ============================================================

def cleanup_old_files():

    import time

    now = time.time()

    try:

        for path in glob.glob(
            os.path.join(
                DOWNLOAD_DIR,
                "*"
            )
        ):

            try:

                age = (
                    now
                    - os.path.getmtime(path)
                )

                # 30 minutes
                if age > 1800:

                    if os.path.isdir(path):
                        shutil.rmtree(
                            path,
                            ignore_errors=True
                        )

                    else:
                        os.remove(path)

            except Exception:
                pass

    except Exception:
        pass


# ============================================================
# STARTUP
# ============================================================

@app.before_request
def before_request_cleanup():

    # Lightweight cleanup.
    # Errors intentionally ignored.
    try:
        cleanup_old_files()
    except Exception:
        pass


# ============================================================
# LOCAL RUN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
