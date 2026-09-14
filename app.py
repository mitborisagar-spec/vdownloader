from flask import Flask, request, jsonify, send_file
import yt_dlp
import os
import shutil
import tempfile
import glob
import imageio_ffmpeg
from flask_cors import CORS
from urllib.parse import quote


# =========================================================
# DENO PATH
# =========================================================

_deno_bin = '/opt/render/project/.deno/bin'

if os.path.isdir(_deno_bin):
    os.environ['PATH'] = (
        _deno_bin
        + os.pathsep
        + os.environ.get('PATH', '')
    )


# =========================================================
# FLASK APP
# =========================================================

app = Flask(__name__)

CORS(
    app,
    resources={
        r"/*": {
            "origins": "*"
        }
    },
    methods=[
        "GET",
        "POST",
        "OPTIONS"
    ],
    allow_headers=[
        "Content-Type",
        "Accept"
    ]
)


# =========================================================
# COOKIE SETTINGS
# =========================================================

SECRET_COOKIES_PATH = '/etc/secrets/cookies.txt'
WRITABLE_COOKIES_PATH = '/tmp/cookies.txt'


def get_writable_cookies_path():

    if os.path.exists(SECRET_COOKIES_PATH):

        try:

            shutil.copyfile(
                SECRET_COOKIES_PATH,
                WRITABLE_COOKIES_PATH
            )

            try:
                os.chmod(
                    WRITABLE_COOKIES_PATH,
                    0o600
                )
            except Exception:
                pass

            return WRITABLE_COOKIES_PATH

        except Exception:
            return None

    if os.path.exists(WRITABLE_COOKIES_PATH):
        return WRITABLE_COOKIES_PATH

    return None


# =========================================================
# PLATFORM CHECKS
# =========================================================

def is_youtube(url):

    url = url.lower()

    return (
        'youtube.com' in url
        or 'youtu.be' in url
    )


def is_instagram(url):

    url = url.lower()

    return (
        'instagram.com' in url
        or 'instagr.am' in url
    )


# =========================================================
# LOG CAPTURE
# =========================================================

class LogCapture:

    def __init__(self):
        self.lines = []

    def debug(self, msg):

        self.lines.append(
            str(msg)
        )

    def warning(self, msg):

        self.lines.append(
            'WARNING: ' + str(msg)
        )

    def error(self, msg):

        self.lines.append(
            'ERROR: ' + str(msg)
        )


# =========================================================
# BUILD YT-DLP OPTIONS
# =========================================================

def build_ydl_options(
    temp_dir,
    log_capture,
    url,
    use_cookies=True,
    format_selector=None
):

    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()

    url_lower = url.lower()

    ydl_opts = {

        'noplaylist': True,

        'format': (
            format_selector
            or 'bv*+ba/b'
        ),

        'merge_output_format': 'mp4',

        'outtmpl': os.path.join(
            temp_dir,
            '%(id)s.%(ext)s'
        ),

        'ffmpeg_location': ffmpeg_path,

        'logger': log_capture,

        'verbose': True,

        'retries': 3,

        'fragment_retries': 3,

        'continuedl': True,

    }


    # =====================================================
    # INSTAGRAM
    # =====================================================

    if is_instagram(url_lower):

        ydl_opts['http_headers'] = {

            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 '
                '(KHTML, like Gecko) '
                'Chrome/151.0.0.0 Safari/537.36'
            ),

            'Accept-Language':
                'en-US,en;q=0.9',

            'Referer':
                'https://www.instagram.com/',
        }


        if use_cookies:

            cookies_path = (
                get_writable_cookies_path()
            )

            if cookies_path:

                ydl_opts['cookiefile'] = (
                    cookies_path
                )


        return ydl_opts


    # =====================================================
    # YOUTUBE
    # =====================================================

    if is_youtube(url_lower):

        cookies_path = (
            get_writable_cookies_path()
        )

        if cookies_path:

            ydl_opts['cookiefile'] = (
                cookies_path
            )


        ydl_opts['extractor_args'] = {

            'youtubepot-bgutilhttp': {

                'base_url': [
                    'https://vdownloader-pot.onrender.com'
                ]

            },

            'youtube': {

                'getpot_bgutil_baseurl': [
                    'https://vdownloader-pot.onrender.com'
                ]

            }

        }


    return ydl_opts


# =========================================================
# FORMAT DEBUG
# =========================================================

def _print_format_debug(info):

    print('')
    print('===== FORMAT DEBUG =====')

    formats = (
        info.get('formats')
        or []
    )

    for f in formats:

        print(
            'FORMAT: {} | EXT: {} | RES: {} | '
            'VCODEC: {} | ACODEC: {} | '
            'PROTO: {} | TBR: {}'.format(
                f.get('format_id'),
                f.get('ext'),
                f.get('resolution'),
                f.get('vcodec'),
                f.get('acodec'),
                f.get('protocol'),
                f.get('tbr')
            )
        )

    print(
        '===== END FORMAT DEBUG ====='
    )

    print('')


    video_formats = [

        f for f in formats

        if f.get('vcodec')
        and f.get('vcodec') != 'none'

    ]


    audio_formats = [

        f for f in formats

        if f.get('acodec')
        and f.get('acodec') != 'none'

        and (
            not f.get('vcodec')
            or f.get('vcodec') == 'none'
        )

    ]


    muxed_formats = [

        f for f in formats

        if f.get('vcodec')
        and f.get('vcodec') != 'none'

        and f.get('acodec')
        and f.get('acodec') != 'none'

    ]


    print(
        'VIDEO FORMATS:',
        len(video_formats)
    )

    print(
        'AUDIO-ONLY FORMATS:',
        len(audio_formats)
    )

    print(
        'MUXED VIDEO+AUDIO FORMATS:',
        len(muxed_formats)
    )


    if audio_formats:

        best_audio = max(

            audio_formats,

            key=lambda x: (
                x.get('abr') or 0,
                x.get('tbr') or 0
            )

        )

        print(
            'BEST AUDIO:',
            best_audio.get('format_id'),
            '| ACODEC:',
            best_audio.get('acodec'),
            '| ABR:',
            best_audio.get('abr')
        )


    elif muxed_formats:

        best_muxed = max(

            muxed_formats,

            key=lambda x: (
                x.get('height') or 0,
                x.get('tbr') or 0
            )

        )

        print(
            'BEST MUXED:',
            best_muxed.get('format_id'),
            '| ACODEC:',
            best_muxed.get('acodec')
        )


    else:

        print(
            'WARNING: NO AUDIO FORMAT FOUND'
        )


    return bool(
        audio_formats
        or muxed_formats
    )


# =========================================================
# DOWNLOAD ONCE
# =========================================================

def _download_once(
    url,
    temp_dir,
    log_capture,
    use_cookies=True,
    format_selector=None
):

    ydl_opts = build_ydl_options(

        temp_dir,

        log_capture,

        url,

        use_cookies=use_cookies,

        format_selector=format_selector

    )


    with yt_dlp.YoutubeDL(
        ydl_opts
    ) as ydl:

        info = ydl.extract_info(
            url,
            download=False
        )


        if is_instagram(url):

            _print_format_debug(
                info
            )


        info = ydl.process_ie_result(
            info,
            download=True
        )


        return info, info


# =========================================================
# CLEAR TEMP FILES
# =========================================================

def _clear_temp_files(temp_dir):

    for f in glob.glob(
        os.path.join(
            temp_dir,
            '*'
        )
    ):

        try:

            if os.path.isfile(f):

                os.remove(f)

        except Exception:

            pass


# =========================================================
# DOWNLOAD + MERGE
# =========================================================

def download_and_merge(
    url,
    temp_dir
):

    log_capture = LogCapture()


    try:

        # =================================================
        # INSTAGRAM
        # =================================================

        if is_instagram(url):

            attempts = [

                (
                    True,
                    'best'
                ),

                (
                    True,
                    'bv*+ba/b'
                ),

                (
                    False,
                    'best'
                ),

                (
                    False,
                    'bv*+ba/b'
                ),

            ]


            info = None


            for use_cookies, selector in attempts:

                _clear_temp_files(
                    temp_dir
                )


                log_capture.lines.append(

                    'Instagram attempt: '
                    'cookies={} format={}'.format(
                        use_cookies,
                        selector
                    )

                )


                try:

                    info, extracted = (
                        _download_once(

                            url,

                            temp_dir,

                            log_capture,

                            use_cookies=use_cookies,

                            format_selector=selector

                        )
                    )


                    if info is not None:

                        break


                except Exception as attempt_error:

                    log_capture.lines.append(

                        'Instagram attempt failed: '
                        + str(attempt_error)

                    )

                    info = None


            if info is None:

                return (
                    None,
                    None,
                    {
                        'error': (
                            'Instagram could not provide '
                            'a downloadable audio/video '
                            'combination. This Reel may '
                            'expose only video media '
                            'to yt-dlp.'
                        ),

                        'logs':
                            log_capture.lines[-80:]
                    }
                )


        # =================================================
        # YOUTUBE / FACEBOOK / TIKTOK
        # =================================================

        else:

            ydl_opts = build_ydl_options(

                temp_dir,

                log_capture,

                url

            )


            with yt_dlp.YoutubeDL(
                ydl_opts
            ) as ydl:

                info = ydl.extract_info(

                    url,

                    download=True

                )


        # =================================================
        # FILE NAME
        # =================================================

        title = info.get(
            'title',
            'video'
        )


        safe_title = ''.join(

            c for c in title

            if c.isalnum()
            or c in (
                ' ',
                '_',
                '-'
            )

        ).strip()


        filename = (

            safe_title[:80]
            or 'video'

        ) + '.mp4'


        # =================================================
        # FIND MP4
        # =================================================

        mp4_files = glob.glob(

            os.path.join(
                temp_dir,
                '*.mp4'
            )

        )


        if mp4_files:

            output_file = max(

                mp4_files,

                key=os.path.getsize

            )


        else:

            media_files = [

                f

                for f in glob.glob(

                    os.path.join(
                        temp_dir,
                        '*'
                    )

                )

                if os.path.isfile(f)

            ]


            if not media_files:

                return (

                    None,

                    None,

                    {
                        'error':
                            'No downloaded file found.',

                        'ffmpeg':
                            imageio_ffmpeg.get_ffmpeg_exe(),

                        'logs':
                            log_capture.lines[-80:]
                    }

                )


            output_file = max(

                media_files,

                key=os.path.getsize

            )


        # =================================================
        # CHECK FILE
        # =================================================

        if not os.path.exists(
            output_file
        ):

            return (

                None,

                None,

                {
                    'error':
                        'Output file does not exist.',

                    'logs':
                        log_capture.lines[-80:]
                }

            )


        if os.path.getsize(
            output_file
        ) == 0:

            return (

                None,

                None,

                {
                    'error':
                        'Output file is empty.',

                    'logs':
                        log_capture.lines[-80:]
                }

            )


        return (
            output_file,
            filename,
            None
        )


    except Exception as e:

        return (

            None,

            None,

            {
                'exception':
                    str(e),

                'deno_found':
                    shutil.which('deno')
                    is not None,

                'deno_dir_exists':
                    os.path.isdir(
                        _deno_bin
                    ),

                'ffmpeg_path':
                    imageio_ffmpeg.get_ffmpeg_exe(),

                'logs':
                    log_capture.lines[-80:]
            }

        )


# =========================================================
# HOME API
# =========================================================

@app.route(
    '/',
    methods=[
        'POST',
        'OPTIONS'
    ]
)
def download():

    # -----------------------------------------------------
    # CORS PREFLIGHT
    # -----------------------------------------------------

    if request.method == 'OPTIONS':

        return '', 204


    try:

        data = request.get_json(
            silent=True
        ) or {}


        url = data.get(
            'url'
        )


        if not url:

            return jsonify({

                'status':
                    'error',

                'error': {

                    'code':
                        'URL missing'

                }

            }), 400


        url = str(url).strip()


        if not url:

            return jsonify({

                'status':
                    'error',

                'error': {

                    'code':
                        'URL missing'

                }

            }), 400


        # -------------------------------------------------
        # STREAM URL
        # -------------------------------------------------

        proxy_url = (

            request.host_url.rstrip('/')

            + '/stream?url='

            + quote(
                url,
                safe=''
            )

        )


        return jsonify({

            'status':
                'success',

            'url':
                proxy_url

        })


    except Exception as e:

        return jsonify({

            'status':
                'error',

            'error': {

                'code':
                    str(e)

            }

        }), 500


# =========================================================
# STREAM / DOWNLOAD ENDPOINT
# =========================================================

@app.route(
    '/stream',
    methods=[
        'GET'
    ]
)
def stream():

    original_url = request.args.get(
        'url'
    )


    if not original_url:

        return jsonify({

            'status':
                'error',

            'error': {

                'code':
                    'URL missing'

            }

        }), 400


    # -----------------------------------------------------
    # TEMP DIRECTORY
    # -----------------------------------------------------

    temp_dir = tempfile.mkdtemp(
        prefix='vdownloader_'
    )


    try:

        # -------------------------------------------------
        # DOWNLOAD
        # -------------------------------------------------

        output_file, filename, debug = (
            download_and_merge(

                original_url,

                temp_dir

            )
        )


        # -------------------------------------------------
        # ERROR
        # -------------------------------------------------

        if not output_file:

            shutil.rmtree(

                temp_dir,

                ignore_errors=True

            )


            return jsonify({

                'status':
                    'error',

                'error': {

                    'code': (

                        debug.get(

                            'exception',

                            'Could not download '
                            'and merge the video.'

                        )

                        if debug

                        else

                        'Could not download '
                        'and merge the video.'

                    )

                },

                'debug':
                    debug

            }), 500


        # -------------------------------------------------
        # SEND MP4
        # -------------------------------------------------

        response = send_file(

            output_file,

            mimetype='video/mp4',

            as_attachment=True,

            download_name=filename,

            max_age=0

        )


        # -------------------------------------------------
        # CLEAN TEMP FILE
        # -------------------------------------------------

        @response.call_on_close
        def cleanup():

            shutil.rmtree(

                temp_dir,

                ignore_errors=True

            )


        return response


    except Exception as e:

        shutil.rmtree(

            temp_dir,

            ignore_errors=True

        )


        return jsonify({

            'status':
                'error',

            'error': {

                'code':
                    str(e)

            }

        }), 500


# =========================================================
# INSTAGRAM DEBUG ENDPOINT
# =========================================================

@app.route(
    '/instagram-debug',
    methods=[
        'GET'
    ]
)
def instagram_debug():

    url = request.args.get(
        'url'
    )


    if not url:

        return jsonify({

            'status':
                'error',

            'error': {

                'code':
                    'URL missing'

            }

        }), 400


    url = str(url).strip()


    log_capture = LogCapture()


    result = {

        'status':
            'ok',

        'url':
            url,

        'yt_dlp': {

            'video_only':
                0,

            'audio_only':
                0,

            'muxed':
                0,

            'format_count':
                0,

            'formats':
                []

        },

        'page': {

            'video_url_count':
                0,

            'audio_url_count':
                0,

            'dash_manifest_count':
                0

        },

        'api': {

            'video_url_count':
                0,

            'audio_url_count':
                0,

            'dash_manifest_count':
                0

        },

        'diagnosis':
            '',

        'logs':
            []

    }


    # -----------------------------------------------------
    # CHECK INSTAGRAM
    # -----------------------------------------------------

    if not is_instagram(url):

        result['status'] = 'error'

        result['diagnosis'] = (
            'This URL is not recognized '
            'as an Instagram URL.'
        )

        return jsonify(
            result
        ), 400


    temp_dir = None


    try:

        # =================================================
        # YT-DLP DEBUG
        # =================================================

        temp_dir = tempfile.mkdtemp(
            prefix='instagram_debug_'
        )


        ydl_opts = build_ydl_options(

            temp_dir,

            log_capture,

            url,

            use_cookies=True,

            format_selector='best'

        )


        ydl_opts['skip_download'] = True


        with yt_dlp.YoutubeDL(
            ydl_opts
        ) as ydl:

            info = ydl.extract_info(

                url,

                download=False

            )


        formats = (
            info.get('formats')
            or []
        )


        # -------------------------------------------------
        # VIDEO ONLY
        # -------------------------------------------------

        video_formats = [

            f

            for f in formats

            if f.get('vcodec')

            and f.get('vcodec') != 'none'

            and (

                not f.get('acodec')

                or f.get('acodec') == 'none'

            )

        ]


        # -------------------------------------------------
        # AUDIO ONLY
        # -------------------------------------------------

        audio_formats = [

            f

            for f in formats

            if f.get('acodec')

            and f.get('acodec') != 'none'

            and (

                not f.get('vcodec')

                or f.get('vcodec') == 'none'

            )

        ]


        # -------------------------------------------------
        # MUXED
        # -------------------------------------------------

        muxed_formats = [

            f

            for f in formats

            if f.get('vcodec')

            and f.get('vcodec') != 'none'

            and f.get('acodec')

            and f.get('acodec') != 'none'

        ]


        result['yt_dlp']['video_only'] = (
            len(video_formats)
        )


        result['yt_dlp']['audio_only'] = (
            len(audio_formats)
        )


        result['yt_dlp']['muxed'] = (
            len(muxed_formats)
        )


        result['yt_dlp']['format_count'] = (
            len(formats)
        )


        # -------------------------------------------------
        # FORMAT DETAILS
        # -------------------------------------------------

        for f in formats:

            result['yt_dlp']['formats'].append({

                'format_id':
                    f.get('format_id'),

                'ext':
                    f.get('ext'),

                'width':
                    f.get('width'),

                'height':
                    f.get('height'),

                'vcodec':
                    f.get('vcodec'),

                'acodec':
                    f.get('acodec'),

                'protocol':
                    f.get('protocol'),

                'abr':
                    f.get('abr'),

                'tbr':
                    f.get('tbr')

            })


        # =================================================
        # DIAGNOSIS
        # =================================================

        if (
            result['yt_dlp']['audio_only']
            > 0
        ):

            result['diagnosis'] = (

                'Audio-only formats are available '
                'from yt-dlp. Instagram audio '
                'should be downloadable.'

            )


        elif (
            result['yt_dlp']['muxed']
            > 0
        ):

            result['diagnosis'] = (

                'A muxed video+audio format '
                'is available from yt-dlp.'

            )


        elif (
            result['yt_dlp']['video_only']
            > 0
        ):

            result['diagnosis'] = (

                'Instagram exposed video-only '
                'formats to yt-dlp, but no '
                'separate audio-only or muxed '
                'audio stream was exposed.'

            )


        else:

            result['diagnosis'] = (

                'yt-dlp did not expose usable '
                'Instagram video/audio formats.'

            )


        result['logs'] = (
            log_capture.lines[-80:]
        )


        return jsonify(
            result
        )


    except Exception as e:

        result['status'] = 'error'


        result['diagnosis'] = (

            'Instagram debug extraction failed.'

        )


        result['error'] = str(e)


        result['logs'] = (
            log_capture.lines[-80:]
        )


        return jsonify(
            result
        ), 500


    finally:

        if temp_dir:

            shutil.rmtree(

                temp_dir,

                ignore_errors=True

            )


# =========================================================
# HEALTH CHECK
# =========================================================

@app.route(
    '/health',
    methods=[
        'GET'
    ]
)
def health():

    try:

        ffmpeg_path = (
            imageio_ffmpeg.get_ffmpeg_exe()
        )


        ffmpeg_exists = (
            os.path.exists(
                ffmpeg_path
            )
        )


        cookies_exists = (
            os.path.exists(
                SECRET_COOKIES_PATH
            )
        )


        return jsonify({

            'status':
                'ok',

            'ffmpeg':
                ffmpeg_exists,

            'ffmpeg_path':
                ffmpeg_path,

            'deno':
                (
                    shutil.which('deno')
                    is not None
                ),

            'cookies':
                cookies_exists

        })


    except Exception as e:

        return jsonify({

            'status':
                'error',

            'error':
                str(e)

        }), 500


# =========================================================
# RUN
# =========================================================

if __name__ == '__main__':

    app.run(

        host='0.0.0.0',

        port=5000

    )
