import os
import re
import io
import json
import time
import uuid
import shutil
import logging
import tempfile
import traceback
import importlib.metadata
from urllib.parse import urlparse

import requests
import yt_dlp

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS


# =========================================================
# APP
# =========================================================

app = Flask(__name__)

CORS(
    app,
    resources={r"/*": {"origins": "*"}},
    methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"]
)


# =========================================================
# CONFIG
# =========================================================

SECRET_COOKIE = "/etc/secrets/cookies.txt"
TMP_COOKIE = "/tmp/cookies.txt"

POT_PROVIDER = "https://vdownloader-pot.onrender.com"

DOWNLOAD_DIR = "/tmp/vdownloader"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FFMPEG_PATH = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"

TOKENS = {}

MAX_TOKEN_AGE = 20 * 60


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger("vdownloader")


# =========================================================
# HELPERS
# =========================================================

def get_yt_dlp_version():
    try:
        return importlib.metadata.version("yt-dlp")
    except Exception:
        try:
            return getattr(yt_dlp, "__version__", "unknown")
        except Exception:
            return "unknown"


def clean_url(url):
    if not url:
        return ""

    return url.strip()


def detect_platform(url):
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return "Unknown"

    if "instagram.com" in host:
        return "Instagram"

    if "facebook.com" in host or "fb.watch" in host:
        return "Facebook"

    if "youtube.com" in host or "youtu.be" in host:
        return "YouTube"

    if "tiktok.com" in host:
        return "TikTok"

    return "Unknown"


def is_valid_url(url):
    try:
        parsed = urlparse(url)

        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.netloc)
        )

    except Exception:
        return False


# =========================================================
# COOKIE FIX
# =========================================================

def prepare_cookie_file():
    """
    IMPORTANT:
    Never give yt-dlp the Render secret path directly.

    /etc/secrets/cookies.txt is read-only.
    Copy it to /tmp first.
    """

    try:

        if os.path.exists(SECRET_COOKIE):

            try:
                shutil.copyfile(
                    SECRET_COOKIE,
                    TMP_COOKIE
                )

                try:
                    os.chmod(TMP_COOKIE, 0o600)
                except Exception:
                    pass

                logger.info("Cookie file copied to /tmp successfully")

                return TMP_COOKIE

            except Exception as e:

                logger.warning(
                    "Could not copy secret cookie file: %s",
                    str(e)
                )

                return None

        if os.path.exists(TMP_COOKIE):
            return TMP_COOKIE

        logger.warning("No cookie file found")

        return None

    except Exception as e:

        logger.warning(
            "Cookie preparation error: %s",
            str(e)
        )

        return None


COOKIE_PATH = prepare_cookie_file()


# =========================================================
# USER AGENTS / HEADERS
# =========================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


COMMON_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "*/*",
    "Connection": "keep-alive"
}


# =========================================================
# YT-DLP OPTIONS
# =========================================================

def common_ydl_options():

    options = {

        "quiet": True,
        "no_warnings": False,

        "noplaylist": True,

        "socket_timeout": 30,

        "retries": 2,

        "fragment_retries": 2,

        "http_headers": COMMON_HEADERS,

        "ffmpeg_location": FFMPEG_PATH,

        "prefer_ffmpeg": True,

        "merge_output_format": "mp4",

        "nocheckcertificate": True,

        "concurrent_fragment_downloads": 4,

    }

    cookie_path = prepare_cookie_file()

    if cookie_path and os.path.exists(cookie_path):
        options["cookiefile"] = cookie_path

    return options


# =========================================================
# INSTAGRAM SESSION
# =========================================================

def instagram_session():

    session = requests.Session()

    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/"
    })

    cookie_path = prepare_cookie_file()

    if cookie_path and os.path.exists(cookie_path):

        try:

            # Read Netscape cookie file manually.
            # This avoids modifying the original Render secret.

            with open(
                cookie_path,
                "r",
                encoding="utf-8",
                errors="ignore"
            ) as f:

                for line in f:

                    line = line.strip()

                    if not line:
                        continue

                    if line.startswith("#") and not line.startswith("#HttpOnly_"):
                        continue

                    parts = line.split("\t")

                    if len(parts) >= 7:

                        domain = parts[0]

                        if domain.startswith("#HttpOnly_"):
                            domain = domain.replace(
                                "#HttpOnly_",
                                "",
                                1
                            )

                        path = parts[2]
                        secure = parts[3]
                        name = parts[5]
                        value = parts[6]

                        if name:

                            try:

                                session.cookies.set(
                                    name,
                                    value,
                                    domain=domain,
                                    path=path
                                )

                            except Exception:
                                pass

        except Exception as e:

            logger.warning(
                "Could not load Instagram cookies: %s",
                str(e)
            )

    return session


# =========================================================
# INSTAGRAM URL HELPERS
# =========================================================

def instagram_shortcode(url):

    patterns = [
        r"/reel/([A-Za-z0-9_-]+)",
        r"/p/([A-Za-z0-9_-]+)",
        r"/tv/([A-Za-z0-9_-]+)"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            url
        )

        if match:
            return match.group(1)

    return None


# =========================================================
# INSTAGRAM MEDIA ID
# =========================================================

def extract_instagram_media_id(data):

    if isinstance(data, dict):

        for key in (
            "pk",
            "id",
            "media_id",
            "mediaId"
        ):

            value = data.get(key)

            if value:

                if isinstance(value, int):
                    return str(value)

                if isinstance(value, str):

                    if value.isdigit():
                        return value

                    # Instagram IDs can sometimes look like:
                    # "123456_123456"

                    if "_" in value:
                        first = value.split("_")[0]

                        if first.isdigit():
                            return first

        for value in data.values():

            result = extract_instagram_media_id(value)

            if result:
                return result

    elif isinstance(data, list):

        for item in data:

            result = extract_instagram_media_id(item)

            if result:
                return result

    return None


def instagram_media_id_from_shortcode(shortcode):

    if not shortcode:
        return None

    url = f"https://www.instagram.com/reel/{shortcode}/"

    options = common_ydl_options()

    options.update({
        "skip_download": True
    })

    try:

        with yt_dlp.YoutubeDL(options) as ydl:

            info = ydl.extract_info(
                url,
                download=False
            )

            if info:

                media_id = (
                    info.get("id")
                    or info.get("display_id")
                )

                if media_id:
                    return str(media_id)

    except Exception as e:

        logger.warning(
            "Could not get Instagram media ID: %s",
            str(e)
        )

    return None


# =========================================================
# RECURSIVE MEDIA URL EXTRACTION
# =========================================================

def recursive_media_urls(obj, result=None):

    if result is None:

        result = {
            "video": set(),
            "audio": set(),
            "dash": set()
        }

    if isinstance(obj, dict):

        for key, value in obj.items():

            key_lower = str(key).lower()

            if isinstance(value, str):

                value_lower = value.lower()

                if value.startswith("http"):

                    if (
                        "audio" in key_lower
                        or "music" in key_lower
                        or "sound" in key_lower
                    ):

                        result["audio"].add(value)

                    elif (
                        "dash" in key_lower
                        or ".mpd" in value_lower
                    ):

                        result["dash"].add(value)

                    elif (
                        "video" in key_lower
                        or value_lower.endswith(".mp4")
                        or ".mp4?" in value_lower
                    ):

                        result["video"].add(value)

            elif isinstance(value, (dict, list)):

                recursive_media_urls(
                    value,
                    result
                )

    elif isinstance(obj, list):

        for item in obj:

            recursive_media_urls(
                item,
                result
            )

    return result


# =========================================================
# INSTAGRAM PAGE EXTRACTION
# =========================================================

def instagram_page_media_info(url):

    session = instagram_session()

    shortcode = instagram_shortcode(url)

    urls = {
        "video": set(),
        "audio": set(),
        "dash": set()
    }

    pages = []

    candidate_urls = []

    if url:
        candidate_urls.append(url)

    if shortcode:

        candidate_urls.append(
            f"https://www.instagram.com/reel/{shortcode}/"
        )

        candidate_urls.append(
            f"https://www.instagram.com/reel/{shortcode}/embed/"
        )

    # remove duplicates
    seen = set()

    candidate_urls = [
        x for x in candidate_urls
        if not (
            x in seen
            or seen.add(x)
        )
    ]

    for page_url in candidate_urls:

        try:

            logger.info(
                "Instagram page request: %s",
                page_url
            )

            response = session.get(
                page_url,
                timeout=25,
                allow_redirects=True
            )

            pages.append({
                "url": page_url,
                "status": response.status_code
            })

            if response.status_code != 200:
                continue

            html = response.text

            # Direct URL patterns
            video_matches = re.findall(
                r'"video_url"\s*:\s*"([^"]+)"',
                html
            )

            audio_matches = re.findall(
                r'"audio_url"\s*:\s*"([^"]+)"',
                html
            )

            for item in video_matches:

                try:
                    item = bytes(
                        item,
                        "utf-8"
                    ).decode(
                        "unicode_escape"
                    )
                except Exception:
                    pass

                if item.startswith("http"):
                    urls["video"].add(item)

            for item in audio_matches:

                try:
                    item = bytes(
                        item,
                        "utf-8"
                    ).decode(
                        "unicode_escape"
                    )
                except Exception:
                    pass

                if item.startswith("http"):
                    urls["audio"].add(item)

            # JSON scripts
            scripts = re.findall(
                r'<script[^>]*>(.*?)</script>',
                html,
                flags=re.DOTALL | re.IGNORECASE
            )

            for script in scripts:

                text = script.strip()

                if not text:
                    continue

                try:

                    parsed = json.loads(text)

                    found = recursive_media_urls(
                        parsed
                    )

                    urls["video"].update(
                        found["video"]
                    )

                    urls["audio"].update(
                        found["audio"]
                    )

                    urls["dash"].update(
                        found["dash"]
                    )

                except Exception:
                    continue

        except Exception as e:

            logger.warning(
                "Instagram page error: %s",
                str(e)
            )

    return {
        "pages": pages,
        "video": list(urls["video"]),
        "audio": list(urls["audio"]),
        "dash": list(urls["dash"])
    }


# =========================================================
# INSTAGRAM MEDIA INFO API
# =========================================================

def instagram_api_info(media_id):

    if not media_id:
        return None

    session = instagram_session()

    api_url = (
        f"https://www.instagram.com/"
        f"api/v1/media/{media_id}/info/"
    )

    try:

        logger.info(
            "Trying Instagram media-info endpoint"
        )

        response = session.get(
            api_url,
            timeout=20,
            headers={
                **COMMON_HEADERS,
                "Referer": "https://www.instagram.com/"
            }
        )

        if response.status_code == 429:

            logger.warning(
                "Instagram media-info HTTP 429"
            )

            return None

        if response.status_code != 200:

            logger.warning(
                "Instagram media-info HTTP %s",
                response.status_code
            )

            return None

        try:
            return response.json()

        except Exception:
            return None

    except Exception as e:

        logger.warning(
            "Instagram media-info error: %s",
            str(e)
        )

        return None


# =========================================================
# YT-DLP INSTAGRAM INFO
# =========================================================

def instagram_ytdlp_info(url):

    logs = []

    options = common_ydl_options()

    options.update({
        "skip_download": True,
        "extract_flat": False,
    })

    try:

        with yt_dlp.YoutubeDL(options) as ydl:

            info = ydl.extract_info(
                url,
                download=False
            )

            return info, logs

    except Exception as e:

        logs.append(
            f"yt-dlp error: {str(e)}"
        )

        logger.warning(
            "yt-dlp Instagram error: %s",
            str(e)
        )

        return None, logs


# =========================================================
# SELECT INSTAGRAM AUDIO
# =========================================================

def find_audio_from_formats(info):

    if not info:
        return None

    formats = info.get("formats") or []

    audio_formats = []

    for fmt in formats:

        acodec = fmt.get("acodec")

        if (
            acodec
            and acodec != "none"
            and fmt.get("url")
        ):

            audio_formats.append(fmt)

    if not audio_formats:
        return None

    # Prefer formats with audio bitrate
    audio_formats.sort(
        key=lambda x: (
            x.get("abr") or 0,
            x.get("tbr") or 0
        ),
        reverse=True
    )

    return audio_formats[0]


# =========================================================
# FIND BEST VIDEO
# =========================================================

def find_best_video(info):

    if not info:
        return None

    formats = info.get("formats") or []

    video_formats = []

    for fmt in formats:

        vcodec = fmt.get("vcodec")

        if (
            vcodec
            and vcodec != "none"
            and fmt.get("url")
        ):

            video_formats.append(fmt)

    if not video_formats:
        return None

    video_formats.sort(
        key=lambda x: (
            x.get("height") or 0,
            x.get("width") or 0,
            x.get("tbr") or 0
        ),
        reverse=True
    )

    return video_formats[0]


# =========================================================
# DIRECT URL DOWNLOAD
# =========================================================

def download_direct_file(
    media_url,
    filename,
    referer=None
):

    if not media_url:
        raise Exception("Media URL missing")

    output_path = os.path.join(
        DOWNLOAD_DIR,
        filename
    )

    headers = dict(COMMON_HEADERS)

    if referer:
        headers["Referer"] = referer

    response = requests.get(
        media_url,
        headers=headers,
        timeout=60,
        stream=True
    )

    response.raise_for_status()

    with open(
        output_path,
        "wb"
    ) as f:

        for chunk in response.iter_content(
            chunk_size=1024 * 1024
        ):

            if chunk:
                f.write(chunk)

    return output_path


# =========================================================
# FFMPEG MERGE
# =========================================================

def merge_video_audio(
    video_path,
    audio_path,
    output_path
):

    import subprocess

    command = [
        FFMPEG_PATH,

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

        output_path
    ]

    process = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if process.returncode != 0:

        raise Exception(
            process.stderr[-4000:]
        )

    return output_path


# =========================================================
# INSTAGRAM DOWNLOAD
# =========================================================

def download_instagram(url):

    timestamp = int(time.time())

    work_dir = os.path.join(
        DOWNLOAD_DIR,
        f"ig_{uuid.uuid4().hex}"
    )

    os.makedirs(
        work_dir,
        exist_ok=True
    )

    try:

        # -------------------------------------------------
        # 1. yt-dlp
        # -------------------------------------------------

        info, logs = instagram_ytdlp_info(
            url
        )

        video_fmt = find_best_video(
            info
        )

        audio_fmt = find_audio_from_formats(
            info
        )

        # -------------------------------------------------
        # 2. If yt-dlp has BOTH video and audio
        # -------------------------------------------------

        if video_fmt and audio_fmt:

            video_url = video_fmt.get("url")
            audio_url = audio_fmt.get("url")

            if video_url and audio_url:

                video_path = download_direct_file(
                    video_url,
                    f"video_{timestamp}.mp4",
                    "https://www.instagram.com/"
                )

                audio_path = download_direct_file(
                    audio_url,
                    f"audio_{timestamp}.m4a",
                    "https://www.instagram.com/"
                )

                output_path = os.path.join(
                    work_dir,
                    f"instagram_{timestamp}.mp4"
                )

                merge_video_audio(
                    video_path,
                    audio_path,
                    output_path
                )

                return output_path

        # -------------------------------------------------
        # 3. Page metadata fallback
        # -------------------------------------------------

        page_info = instagram_page_media_info(
            url
        )

        page_video = (
            page_info.get("video") or []
        )

        page_audio = (
            page_info.get("audio") or []
        )

        # -------------------------------------------------
        # 4. Page video + audio
        # -------------------------------------------------

        if page_video and page_audio:

            video_path = download_direct_file(
                page_video[0],
                f"page_video_{timestamp}.mp4",
                "https://www.instagram.com/"
            )

            audio_path = download_direct_file(
                page_audio[0],
                f"page_audio_{timestamp}.m4a",
                "https://www.instagram.com/"
            )

            output_path = os.path.join(
                work_dir,
                f"instagram_{timestamp}.mp4"
            )

            merge_video_audio(
                video_path,
                audio_path,
                output_path
            )

            return output_path

        # -------------------------------------------------
        # 5. Media info API — one best effort request
        # -------------------------------------------------

        shortcode = instagram_shortcode(
            url
        )

        media_id = instagram_media_id_from_shortcode(
            url
        )

        if media_id:

            api_data = instagram_api_info(
                media_id
            )

            if api_data:

                api_media = recursive_media_urls(
                    api_data
                )

                api_video = list(
                    api_media["video"]
                )

                api_audio = list(
                    api_media["audio"]
                )

                if api_video and api_audio:

                    video_path = download_direct_file(
                        api_video[0],
                        f"api_video_{timestamp}.mp4",
                        "https://www.instagram.com/"
                    )

                    audio_path = download_direct_file(
                        api_audio[0],
                        f"api_audio_{timestamp}.m4a",
                        "https://www.instagram.com/"
                    )

                    output_path = os.path.join(
                        work_dir,
                        f"instagram_{timestamp}.mp4"
                    )

                    merge_video_audio(
                        video_path,
                        audio_path,
                        output_path
                    )

                    return output_path

        # -------------------------------------------------
        # 6. Last fallback:
        #    yt-dlp normal download
        # -------------------------------------------------

        options = common_ydl_options()

        options.update({

            "outtmpl": os.path.join(
                work_dir,
                "final.%(ext)s"
            ),

            "format": "bv*+ba/b",

            "merge_output_format": "mp4",

            "postprocessors": [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4"
                }
            ]
        })

        try:

            with yt_dlp.YoutubeDL(options) as ydl:

                ydl.download([url])

            candidates = []

            for name in os.listdir(
                work_dir
            ):

                full_path = os.path.join(
                    work_dir,
                    name
                )

                if os.path.isfile(full_path):

                    if name.lower().endswith(
                        (".mp4", ".mkv", ".webm")
                    ):

                        candidates.append(
                            full_path
                        )

            if candidates:

                # Check whether resulting file has audio.
                for candidate in candidates:

                    probe = get_media_stream_info(
                        candidate
                    )

                    if probe.get("audio"):
                        return candidate

                # Don't return a silent file.
                raise Exception(
                    "Instagram video was downloaded "
                    "without an audio stream."
                )

        except Exception as e:

            logger.warning(
                "Instagram final yt-dlp download failed: %s",
                str(e)
            )

        raise Exception(
            "Instagram did not expose an audio stream "
            "for this Reel. The video stream is available, "
            "but audio could not be retrieved."
        )

    except Exception:

        shutil.rmtree(
            work_dir,
            ignore_errors=True
        )

        raise


# =========================================================
# MEDIA STREAM CHECK
# =========================================================

def get_media_stream_info(path):

    import subprocess

    result = {
        "video": False,
        "audio": False
    }

    try:

        command = [
            FFMPEG_PATH,
            "-i",
            path
        ]

        process = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        output = process.stderr.lower()

        result["video"] = (
            "video:" in output
        )

        result["audio"] = (
            "audio:" in output
        )

    except Exception:
        pass

    return result


# =========================================================
# GENERAL YT-DLP DOWNLOAD
# =========================================================

def download_general(url):

    work_dir = os.path.join(
        DOWNLOAD_DIR,
        f"dl_{uuid.uuid4().hex}"
    )

    os.makedirs(
        work_dir,
        exist_ok=True
    )

    options = common_ydl_options()

    options.update({

        "outtmpl": os.path.join(
            work_dir,
            "download.%(ext)s"
        ),

        "format": (
            "bv*+ba/"
            "b"
        ),

        "merge_output_format": "mp4",

        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4"
            }
        ]
    })

    try:

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:

            ydl.download([
                url
            ])

        files = []

        for root, dirs, filenames in os.walk(
            work_dir
        ):

            for filename in filenames:

                path = os.path.join(
                    root,
                    filename
                )

                if os.path.isfile(path):

                    files.append(path)

        if not files:

            raise Exception(
                "No downloaded file was produced."
            )

        # Prefer MP4
        mp4_files = [
            x for x in files
            if x.lower().endswith(".mp4")
        ]

        if mp4_files:
            return mp4_files[0]

        return files[0]

    except Exception:

        shutil.rmtree(
            work_dir,
            ignore_errors=True
        )

        raise


# =========================================================
# TOKEN SYSTEM
# =========================================================

def create_token(file_path):

    token = uuid.uuid4().hex

    TOKENS[token] = {
        "path": file_path,
        "created": time.time()
    }

    return token


def cleanup_tokens():

    now = time.time()

    expired = []

    for token, data in list(
        TOKENS.items()
    ):

        if (
            now - data.get("created", now)
            > MAX_TOKEN_AGE
        ):

            expired.append(token)

    for token in expired:

        data = TOKENS.pop(
            token,
            None
        )

        if data:

            path = data.get("path")

            if path:

                try:

                    shutil.rmtree(
                        os.path.dirname(path),
                        ignore_errors=True
                    )

                except Exception:
                    pass


# =========================================================
# ROOT API
# =========================================================

@app.route(
    "/",
    methods=[
        "POST",
        "OPTIONS"
    ]
)
def download():

    if request.method == "OPTIONS":
        return "", 204

    cleanup_tokens()

    try:

        data = request.get_json(
            silent=True
        ) or {}

        url = clean_url(
            data.get("url")
        )

        if not url:

            return jsonify({
                "status": "error",
                "message": "URL is required."
            }), 400

        if not is_valid_url(url):

            return jsonify({
                "status": "error",
                "message": "Invalid URL."
            }), 400

        platform = detect_platform(
            url
        )

        logger.info(
            "Download request: %s",
            platform
        )

        # Instagram gets special audio handling
        if platform == "Instagram":

            file_path = download_instagram(
                url
            )

        else:

            file_path = download_general(
                url
            )

        if not file_path or not os.path.exists(
            file_path
        ):

            raise Exception(
                "Download file was not created."
            )

        token = create_token(
            file_path
        )

        return jsonify({

            "status": "success",

            "platform": platform,

            "token": token,

            "url": (
                request.host_url.rstrip("/")
                + "/stream/"
                + token
            )

        })

    except Exception as e:

        logger.error(
            "Download error: %s",
            str(e)
        )

        return jsonify({

            "status": "error",

            "message": str(e)

        }), 500


# =========================================================
# STREAM
# =========================================================

@app.route(
    "/stream/<token>",
    methods=["GET"]
)
def stream_file(token):

    cleanup_tokens()

    data = TOKENS.get(
        token
    )

    if not data:

        return jsonify({
            "status": "error",
            "message": "Download link expired or invalid."
        }), 404

    file_path = data.get(
        "path"
    )

    if not file_path or not os.path.exists(
        file_path
    ):

        TOKENS.pop(
            token,
            None
        )

        return jsonify({
            "status": "error",
            "message": "File no longer exists."
        }), 404

    filename = os.path.basename(
        file_path
    )

    if not filename.lower().endswith(
        ".mp4"
    ):

        filename = (
            os.path.splitext(filename)[0]
            + ".mp4"
        )

    def generate():

        try:

            with open(
                file_path,
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

            # Delete after streaming
            try:

                shutil.rmtree(
                    os.path.dirname(file_path),
                    ignore_errors=True
                )

            except Exception:
                pass

            TOKENS.pop(
                token,
                None
            )

    return Response(
        stream_with_context(
            generate()
        ),
        mimetype="video/mp4",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            ),
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff"
        }
    )


# =========================================================
# INSTAGRAM DEBUG
# =========================================================

@app.route(
    "/instagram-debug",
    methods=["GET"]
)
def instagram_debug():

    url = request.args.get(
        "url",
        ""
    ).strip()

    if not url:

        return jsonify({
            "status": "error",
            "message": "Use ?url=INSTAGRAM_URL"
        }), 400

    if detect_platform(url) != "Instagram":

        return jsonify({
            "status": "error",
            "message": "URL is not recognized as Instagram."
        }), 400

    result = {

        "status": "ok",

        "url": url,

        "instagram_shortcode": (
            instagram_shortcode(url)
        ),

        "yt_dlp": {},

        "page": {},

        "api": {},

        "logs": []

    }

    # -----------------------------------------------
    # yt-dlp
    # -----------------------------------------------

    try:

        info, logs = instagram_ytdlp_info(
            url
        )

        result["logs"].extend(
            logs
        )

        if info:

            formats = info.get(
                "formats"
            ) or []

            format_summary = []

            video_count = 0
            audio_count = 0
            muxed_count = 0

            for fmt in formats:

                vcodec = fmt.get(
                    "vcodec"
                )

                acodec = fmt.get(
                    "acodec"
                )

                has_video = (
                    vcodec
                    and vcodec != "none"
                )

                has_audio = (
                    acodec
                    and acodec != "none"
                )

                if has_video:
                    video_count += 1

                if has_audio:
                    audio_count += 1

                if has_video and has_audio:
                    muxed_count += 1

                format_summary.append({

                    "format_id": fmt.get(
                        "format_id"
                    ),

                    "ext": fmt.get(
                        "ext"
                    ),

                    "width": fmt.get(
                        "width"
                    ),

                    "height": fmt.get(
                        "height"
                    ),

                    "vcodec": vcodec,

                    "acodec": acodec,

                    "kind": (
                        "muxed"
                        if has_video and has_audio
                        else "video"
                        if has_video
                        else "audio"
                        if has_audio
                        else "unknown"
                    )

                })

            result["yt_dlp"] = {

                "id": info.get(
                    "id"
                ),

                "title": info.get(
                    "title"
                ),

                "extractor": info.get(
                    "extractor_key"
                ),

                "format_count": len(
                    formats
                ),

                "video_only": video_count,

                "audio_only": audio_count,

                "muxed": muxed_count,

                "formats": format_summary

            }

    except Exception as e:

        result["logs"].append(
            f"yt-dlp debug error: {str(e)}"
        )

    # -----------------------------------------------
    # Page
    # -----------------------------------------------

    try:

        page = instagram_page_media_info(
            url
        )

        result["page"] = {

            "pages": len(
                page.get("pages", [])
            ),

            "video_url_count": len(
                page.get("video", [])
            ),

            "audio_url_count": len(
                page.get("audio", [])
            ),

            "dash_manifest_count": len(
                page.get("dash", [])
            )

        }

    except Exception as e:

        result["logs"].append(
            f"page debug error: {str(e)}"
        )

    # -----------------------------------------------
    # API
    # -----------------------------------------------

    try:

        media_id = instagram_media_id_from_shortcode(
            url
        )

        result["instagram_media_id"] = media_id

        if media_id:

            api_data = instagram_api_info(
                media_id
            )

            if api_data:

                found = recursive_media_urls(
                    api_data
                )

                result["api"] = {

                    "available": True,

                    "video_url_count": len(
                        found["video"]
                    ),

                    "audio_url_count": len(
                        found["audio"]
                    ),

                    "dash_manifest_count": len(
                        found["dash"]
                    )

                }

            else:

                result["api"] = {

                    "available": False,

                    "video_url_count": 0,

                    "audio_url_count": 0,

                    "dash_manifest_count": 0

                }

    except Exception as e:

        result["logs"].append(
            f"API debug error: {str(e)}"
        )

    # -----------------------------------------------
    # Diagnosis
    # -----------------------------------------------

    yt_audio = (
        result.get("yt_dlp", {})
        .get("audio_only", 0)
    )

    yt_muxed = (
        result.get("yt_dlp", {})
        .get("muxed", 0)
    )

    page_audio = (
        result.get("page", {})
        .get("audio_url_count", 0)
    )

    api_audio = (
        result.get("api", {})
        .get("audio_url_count", 0)
    )

    if (
        yt_audio
        or yt_muxed
        or page_audio
        or api_audio
    ):

        result["diagnosis"] = (
            "An audio-capable stream is exposed. "
            "The downloader can attempt to merge it."
        )

    else:

        result["diagnosis"] = (
            "No audio stream was exposed by yt-dlp, "
            "Instagram page metadata, or the available "
            "media-info session path. If the Reel contains "
            "music in Instagram, Instagram is currently "
            "not exposing that audio stream to this server."
        )

    return jsonify(
        result
    )


# =========================================================
# HEALTH
# =========================================================

@app.route(
    "/health",
    methods=["GET"]
)
def health():

    cookie_exists = (
        os.path.exists(
            TMP_COOKIE
        )
        or os.path.exists(
            SECRET_COOKIE
        )
    )

    deno_path = shutil.which(
        "deno"
    )

    ffmpeg_path = shutil.which(
        "ffmpeg"
    )

    return jsonify({

        "status": "ok",

        "cookies": cookie_exists,

        "deno": bool(
            deno_path
        ),

        "ffmpeg": bool(
            ffmpeg_path
        ),

        "ffmpeg_path": (
            ffmpeg_path
            or FFMPEG_PATH
        ),

        "yt_dlp": get_yt_dlp_version()

    })


# =========================================================
# CLEANUP
# =========================================================

@app.route(
    "/cleanup",
    methods=["GET"]
)
def cleanup():

    cleanup_tokens()

    return jsonify({
        "status": "ok"
    })


# =========================================================
# STARTUP
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    logger.info(
        "VDownloader API starting..."
    )

    logger.info(
        "yt-dlp version: %s",
        get_yt_dlp_version()
    )

    logger.info(
        "FFmpeg: %s",
        FFMPEG_PATH
    )

    logger.info(
        "Cookies available: %s",
        bool(prepare_cookie_file())
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
