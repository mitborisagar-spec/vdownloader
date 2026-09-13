from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp
import requests
import os
import shutil
import tempfile
import uuid
import subprocess
import re
import html
import json
import time


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
# PATHS
# =========================================================

DENO_PATH = "/opt/render/project/.deno/bin"

if os.path.isdir(DENO_PATH):
    os.environ["PATH"] = DENO_PATH + os.pathsep + os.environ.get("PATH", "")


COOKIES_SOURCE = "/etc/secrets/cookies.txt"
COOKIES_TMP = "/tmp/cookies.txt"


# =========================================================
# COPY COOKIES
# =========================================================

def prepare_cookies():
    try:
        if os.path.exists(COOKIES_SOURCE):
            shutil.copyfile(COOKIES_SOURCE, COOKIES_TMP)
            return COOKIES_TMP
    except Exception:
        pass

    return None


# =========================================================
# HELPERS
# =========================================================

def is_youtube(url):
    u = (url or "").lower()

    return (
        "youtube.com/" in u
        or "youtu.be/" in u
        or "youtube-nocookie.com/" in u
    )


def is_instagram(url):
    u = (url or "").lower()

    return (
        "instagram.com/" in u
        or "www.instagram.com/" in u
    )


def is_facebook(url):
    u = (url or "").lower()

    return (
        "facebook.com/" in u
        or "fb.watch/" in u
        or "m.facebook.com/" in u
    )


def safe_filename(value):
    value = value or "video"

    value = re.sub(
        r'[\\/:*?"<>|]+',
        "_",
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    ).strip()

    return value[:150] or "video"


def find_ffmpeg():
    candidates = [
        shutil.which("ffmpeg"),
        "/usr/bin/ffmpeg",
        "/opt/render/project/src/.venv/bin/ffmpeg",
    ]

    for path in candidates:
        if path and os.path.exists(path):
            return path

    return None


FFMPEG = find_ffmpeg()


# =========================================================
# LOG CAPTURE
# =========================================================

class LogCapture:
    def __init__(self):
        self.lines = []

    def debug(self, msg):
        self.lines.append(str(msg))

    def warning(self, msg):
        self.lines.append("WARNING: " + str(msg))

    def error(self, msg):
        self.lines.append("ERROR: " + str(msg))

    def info(self, msg):
        self.lines.append(str(msg))

    def get(self):
        return self.lines[-300:]


# =========================================================
# COMMON YT-DLP OPTIONS
# =========================================================

def build_common_options(logger=None):

    opts = {
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "restrictfilenames": True,
        "nocheckcertificate": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "concurrent_fragment_downloads": 4,
        "logger": logger,
    }

    return opts


# =========================================================
# INSTAGRAM HEADERS
# =========================================================

def instagram_headers():

    return {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 10; K) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/151.0.0.0 Mobile Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,image/avif,"
            "image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.instagram.com/",
        "Connection": "keep-alive",
    }


# =========================================================
# YOUTUBE OPTIONS
# =========================================================

def youtube_options(logger=None):

    opts = build_common_options(logger)

    opts.update({
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    })

    cookies = prepare_cookies()

    if cookies:
        opts["cookiefile"] = cookies

    # YouTube POT provider
    opts["extractor_args"] = {
        "youtube": {
            "player_client": [
                "web",
                "web_safari",
                "mweb",
            ]
        }
    }

    return opts


# =========================================================
# FACEBOOK OPTIONS
# =========================================================

def facebook_options(logger=None):

    opts = build_common_options(logger)

    opts.update({
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
        },
    })

    cookies = prepare_cookies()

    if cookies:
        opts["cookiefile"] = cookies

    return opts


# =========================================================
# INSTAGRAM OPTIONS
# =========================================================

def instagram_options(
    logger=None,
    use_cookies=True,
    fmt="bv*+ba/b"
):

    opts = build_common_options(logger)

    opts.update({
        "format": fmt,
        "merge_output_format": "mp4",
        "http_headers": instagram_headers(),
    })

    if use_cookies:
        cookies = prepare_cookies()

        if cookies:
            opts["cookiefile"] = cookies

    return opts


# =========================================================
# FORMAT SUMMARY
# =========================================================

def format_summary(info):

    formats = info.get("formats") or []

    audio_examples = []
    video_examples = []
    muxed_examples = []

    audio_only = 0
    video_only = 0
    muxed = 0

    for f in formats:

        acodec = f.get("acodec")
        vcodec = f.get("vcodec")

        item = {
            "id": f.get("format_id"),
            "ext": f.get("ext"),
            "protocol": f.get("protocol"),
            "resolution": f.get("resolution"),
            "vcodec": acodec and f.get("vcodec"),
            "acodec": acodec,
            "abr": f.get("abr"),
            "tbr": f.get("tbr"),
            "format_note": f.get("format_note"),
        }

        has_audio = acodec and acodec != "none"
        has_video = vcodec and vcodec != "none"

        if has_audio and has_video:
            muxed += 1

            if len(muxed_examples) < 5:
                muxed_examples.append(item)

        elif has_audio:
            audio_only += 1

            if len(audio_examples) < 5:
                audio_examples.append(item)

        elif has_video:
            video_only += 1

            if len(video_examples) < 5:
                video_examples.append(item)

    return {
        "total": len(formats),
        "audio_only": audio_only,
        "video_only": video_only,
        "muxed": muxed,
        "audio_examples": audio_examples,
        "video_examples": video_examples,
        "muxed_examples": muxed_examples,
    }


# =========================================================
# AUDIO CHECK
# =========================================================

def has_audio_stream(path):

    if not path or not os.path.exists(path):
        return False

    if not FFMPEG:
        return False

    try:

        result = subprocess.run(
            [
                FFMPEG,
                "-hide_banner",
                "-i",
                path,
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-"
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30
        )

        output = (
            (result.stdout or "") +
            "\n" +
            (result.stderr or "")
        )

        return (
            result.returncode == 0
            and "Audio:" in output
        )

    except Exception:
        return False


# =========================================================
# FIND DOWNLOADED FILE
# =========================================================

def find_downloaded_file(directory):

    if not os.path.exists(directory):
        return None

    files = []

    for name in os.listdir(directory):

        path = os.path.join(directory, name)

        if os.path.isfile(path):
            files.append(path)

    if not files:
        return None

    files.sort(
        key=lambda p: os.path.getmtime(p),
        reverse=True
    )

    return files[0]


# =========================================================
# NORMAL YT-DLP DOWNLOAD
# =========================================================

def download_with_ytdlp(
    url,
    workdir,
    opts,
    logger
):

    try:

        opts = dict(opts)

        opts["outtmpl"] = os.path.join(
            workdir,
            "%(id)s.%(ext)s"
        )

        with yt_dlp.YoutubeDL(opts) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )

            downloaded = find_downloaded_file(workdir)

            return {
                "success": bool(downloaded),
                "path": downloaded,
                "info": info,
            }

    except Exception as e:

        logger.error(
            "yt-dlp download failed: " + str(e)
        )

        return {
            "success": False,
            "path": None,
            "info": None,
            "error": str(e),
        }


# =========================================================
# INSTAGRAM PAGE FALLBACK
#
# This does NOT try to magically create missing audio.
# It checks whether Instagram's page itself exposes a
# directly usable media URL that yt-dlp's format list
# did not expose.
# =========================================================

def instagram_page_fallback(
    url,
    workdir,
    logger
):

    session = requests.Session()

    session.headers.update(
        instagram_headers()
    )

    cookies = prepare_cookies()

    if cookies:
        try:
            with open(cookies, "r", encoding="utf-8") as f:
                cookie_text = f.read()

            # Basic Netscape cookie import
            for line in cookie_text.splitlines():

                if (
                    not line
                    or line.startswith("#")
                    or len(line.split("\t")) < 7
                ):
                    continue

                parts = line.split("\t")

                domain = parts[0]
                name = parts[5]
                value = parts[6]

                session.cookies.set(
                    name,
                    value,
                    domain=domain
                )

        except Exception as e:

            logger.warning(
                "Could not import Instagram cookies: "
                + str(e)
            )

    try:

        response = session.get(
            url,
            timeout=30,
            allow_redirects=True
        )

        if response.status_code != 200:

            logger.warning(
                "Instagram page fallback HTTP "
                + str(response.status_code)
            )

            return None

        page = response.text

        # -------------------------------------------------
        # 1. og:video
        # -------------------------------------------------

        og_patterns = [
            r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video["\']',
            r'<meta[^>]+property=["\']og:video:secure_url["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video:secure_url["\']',
        ]

        media_urls = []

        for pattern in og_patterns:

            matches = re.findall(
                pattern,
                page,
                flags=re.I
            )

            for match in matches:

                value = html.unescape(match)

                if value not in media_urls:
                    media_urls.append(value)

        # -------------------------------------------------
        # 2. Search escaped media URLs
        # -------------------------------------------------

        escaped_patterns = [
            r'"video_url"\s*:\s*"([^"]+)"',
            r'"video_versions"\s*:\s*\[(.*?)\]',
        ]

        for pattern in escaped_patterns:

            matches = re.findall(
                pattern,
                page,
                flags=re.I | re.S
            )

            for match in matches:

                if isinstance(match, tuple):
                    continue

                value = match

                value = value.replace(
                    "\\/",
                    "/"
                )

                value = value.replace(
                    "\\u0026",
                    "&"
                )

                value = html.unescape(value)

                if value.startswith("http"):

                    if (
                        ".mp4" in value
                        or "video" in value.lower()
                    ):
                        if value not in media_urls:
                            media_urls.append(value)

        if not media_urls:

            logger.warning(
                "Instagram page fallback found no direct media URL"
            )

            return None

        # -------------------------------------------------
        # Download candidate media
        # -------------------------------------------------

        for index, media_url in enumerate(
            media_urls[:10]
        ):

            try:

                logger.info(
                    "Instagram page fallback candidate "
                    + str(index + 1)
                )

                filename = os.path.join(
                    workdir,
                    "instagram_fallback_"
                    + str(index)
                    + ".mp4"
                )

                r = session.get(
                    media_url,
                    stream=True,
                    timeout=45,
                    allow_redirects=True
                )

                content_type = (
                    r.headers.get(
                        "Content-Type",
                        ""
                    ).lower()
                )

                if r.status_code != 200:
                    continue

                if (
                    "video" not in content_type
                    and "mp4" not in content_type
                ):
                    continue

                with open(
                    filename,
                    "wb"
                ) as output:

                    for chunk in r.iter_content(
                        chunk_size=1024 * 256
                    ):

                        if chunk:
                            output.write(chunk)

                if (
                    os.path.exists(filename)
                    and os.path.getsize(filename) > 10000
                ):

                    # IMPORTANT:
                    # Accept only if actual file has audio.
                    if has_audio_stream(filename):

                        logger.info(
                            "Instagram fallback found "
                            "a media file WITH audio"
                        )

                        return filename

                    logger.info(
                        "Instagram fallback media "
                        "has no audio"
                    )

            except Exception as e:

                logger.warning(
                    "Instagram fallback candidate failed: "
                    + str(e)
                )

        return None

    except Exception as e:

        logger.error(
            "Instagram page fallback failed: "
            + str(e)
        )

        return None


# =========================================================
# INSTAGRAM DOWNLOAD
# =========================================================

def download_instagram(url, workdir, logger):

    attempts = [

        # 1. Cookies + normal muxed/audio
        (
            True,
            "bv*+ba/b",
            "cookies-normal"
        ),

        # 2. Cookies + explicitly prefer muxed
        (
            True,
            "best[vcodec!=none][acodec!=none]/bv*+ba/b",
            "cookies-muxed"
        ),

        # 3. Public normal
        (
            False,
            "bv*+ba/b",
            "public-normal"
        ),

        # 4. Public muxed
        (
            False,
            "best[vcodec!=none][acodec!=none]/bv*+ba/b",
            "public-muxed"
        ),
    ]

    last_summary = None

    for use_cookies, fmt, label in attempts:

        logger.info(
            "Instagram attempt: " + label
        )

        try:

            opts = instagram_options(
                logger=logger,
                use_cookies=use_cookies,
                fmt=fmt
            )

            # First extract without downloading.
            with yt_dlp.YoutubeDL(opts) as ydl:

                info = ydl.extract_info(
                    url,
                    download=False
                )

            summary = format_summary(info)

            last_summary = summary

            logger.info(
                "Instagram formats: "
                + json.dumps(
                    summary,
                    ensure_ascii=False
                )
            )

            # If Instagram actually exposed audio,
            # download normally.
            if (
                summary["muxed"] > 0
                or summary["audio_only"] > 0
            ):

                result = download_with_ytdlp(
                    url,
                    workdir,
                    opts,
                    logger
                )

                if result["success"]:

                    path = result["path"]

                    if has_audio_stream(path):

                        logger.info(
                            "Instagram normal download "
                            "has audio"
                        )

                        return {
                            "success": True,
                            "path": path,
                            "summary": summary,
                        }

                    logger.warning(
                        "Downloaded Instagram file "
                        "still has no audio"
                    )

            else:

                logger.warning(
                    "Instagram exposed no usable audio "
                    "in this attempt"
                )

        except Exception as e:

            logger.error(
                "Instagram attempt "
                + label
                + " failed: "
                + str(e)
            )

    # =====================================================
    # PAGE LEVEL FALLBACK
    # =====================================================

    logger.info(
        "Starting Instagram page-level audio fallback"
    )

    fallback_path = instagram_page_fallback(
        url,
        workdir,
        logger
    )

    if fallback_path:

        return {
            "success": True,
            "path": fallback_path,
            "summary": last_summary,
            "fallback": "page-media"
        }

    # =====================================================
    # FINAL ERROR
    # =====================================================

    return {
        "success": False,
        "path": None,
        "summary": last_summary,
        "error": (
            "Instagram did not expose a usable audio "
            "stream for this Reel. The available "
            "Instagram formats contained video but "
            "no audio stream."
        )
    }


# =========================================================
# GENERAL DOWNLOAD
# =========================================================

def download_video(url, workdir, logger):

    # Instagram gets special handling.
    if is_instagram(url):

        return download_instagram(
            url,
            workdir,
            logger
        )

    # YouTube
    if is_youtube(url):

        logger.info(
            "Using YouTube downloader"
        )

        opts = youtube_options(
            logger
        )

        result = download_with_ytdlp(
            url,
            workdir,
            opts,
            logger
        )

        return result

    # Facebook
    if is_facebook(url):

        logger.info(
            "Using Facebook downloader"
        )

        opts = facebook_options(
            logger
        )

        result = download_with_ytdlp(
            url,
            workdir,
            opts,
            logger
        )

        return result

    # Other supported URLs
    logger.info(
        "Using generic downloader"
    )

    opts = build_common_options(
        logger
    )

    opts.update({
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
    })

    cookies = prepare_cookies()

    if cookies:
        opts["cookiefile"] = cookies

    return download_with_ytdlp(
        url,
        workdir,
        opts,
        logger
    )


# =========================================================
# ROOT API
# =========================================================

@app.route(
    "/",
    methods=["POST", "OPTIONS"]
)
def download():

    if request.method == "OPTIONS":
        return "", 204

    try:

        data = request.get_json(
            silent=True
        ) or {}

        url = (
            data.get("url")
            or data.get("video_url")
            or ""
        ).strip()

        if not url:

            return jsonify({
                "status": "error",
                "error": {
                    "code": "URL_REQUIRED"
                }
            }), 400

        if not (
            url.startswith("http://")
            or url.startswith("https://")
        ):

            return jsonify({
                "status": "error",
                "error": {
                    "code": "INVALID_URL"
                }
            }), 400

        # Keep a server-side request id.
        request_id = uuid.uuid4().hex

        return jsonify({
            "status": "success",
            "url": (
                request.host_url.rstrip("/")
                + "/stream?url="
                + requests.utils.quote(
                    url,
                    safe=""
                )
                + "&id="
                + request_id
            ),
        })

    except Exception as e:

        return jsonify({
            "status": "error",
            "error": {
                "code": str(e)
            }
        }), 500


# =========================================================
# STREAM / ACTUAL DOWNLOAD
# =========================================================

@app.route(
    "/stream",
    methods=["GET"]
)
def stream():

    url = (
        request.args.get("url")
        or ""
    ).strip()

    if not url:

        return jsonify({
            "status": "error",
            "error": {
                "code": "URL_REQUIRED"
            }
        }), 400

    logger = LogCapture()

    workdir = tempfile.mkdtemp(
        prefix="vdownloader_"
    )

    try:

        logger.info(
            "Starting download for: " + url
        )

        result = download_video(
            url,
            workdir,
            logger
        )

        if not result.get("success"):

            debug_data = {
                "logs": logger.get(),
                "format_summary": result.get(
                    "summary"
                ),
            }

            return jsonify({
                "status": "error",
                "error": {
                    "code": result.get(
                        "error",
                        "Download failed"
                    )
                },
                "debug": debug_data
            }), 500

        path = result.get("path")

        if not path or not os.path.exists(path):

            return jsonify({
                "status": "error",
                "error": {
                    "code": "OUTPUT_FILE_NOT_FOUND"
                },
                "debug": {
                    "logs": logger.get()
                }
            }), 500

        # -------------------------------------------------
        # Final audio check
        # -------------------------------------------------

        audio_present = has_audio_stream(
            path
        )

        logger.info(
            "Final audio check: "
            + str(audio_present)
        )

        # -------------------------------------------------
        # If Instagram still has no audio,
        # do NOT return silent file.
        # -------------------------------------------------

        if (
            is_instagram(url)
            and not audio_present
        ):

            return jsonify({
                "status": "error",
                "error": {
                    "code": (
                        "Instagram returned a video "
                        "without an audio stream."
                    )
                },
                "debug": {
                    "logs": logger.get(),
                    "format_summary": result.get(
                        "summary"
                    )
                }
            }), 422

        # -------------------------------------------------
        # Determine filename
        # -------------------------------------------------

        ext = (
            os.path.splitext(path)[1]
            or ".mp4"
        )

        filename = (
            "VDownloader"
            + ext
        )

        # -------------------------------------------------
        # Stream file
        # -------------------------------------------------

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

                try:
                    shutil.rmtree(
                        workdir,
                        ignore_errors=True
                    )
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
                "Content-Type": "video/mp4",
                "Cache-Control": "no-store",
                "X-VDownloader-Audio": (
                    "yes"
                    if audio_present
                    else "no"
                ),
            }
        )

    except Exception as e:

        try:
            shutil.rmtree(
                workdir,
                ignore_errors=True
            )
        except Exception:
            pass

        return jsonify({
            "status": "error",
            "error": {
                "code": str(e)
            },
            "debug": {
                "logs": logger.get()
            }
        }), 500


# =========================================================
# HEALTH
# =========================================================

@app.route(
    "/health",
    methods=["GET"]
)
def health():

    return jsonify({
        "status": "ok",
        "deno": os.path.isdir(
            DENO_PATH
        ),
        "ffmpeg": bool(FFMPEG),
        "ffmpeg_path": FFMPEG,
    })


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                10000
            )
        )
    )
