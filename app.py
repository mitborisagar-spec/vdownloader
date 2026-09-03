from flask import Flask, request, jsonify from flask_cors import CORS

app = Flask(_name_)

CORS(app)

@app.route('/', methods=['POST'])

def download():

data = request.ison

url = data.get('url") if not url:

return jsonify({'status': 'error', 'error': {'code': 'URL missing'}}), 400

ydl_opts = {'format': 'best', 'noplaylist': True}

try

with yt_dlp.YoutubeDL(ydl_opts) as ydl: info = ydl.extract_info(url, download=False)

video_url = info.get('url')

# Fallback 1: some extractors (e.g. Twitter/X) only populate requested_downloads

not video_url:

info.get('requested_downloads')

video_url= rd[0].get('url')

# Fallback 2: pick the last (usually highest quality/combined) format

if not video_url:

formats = info.get('formats')

formats:

deo_url = formats[-1].get('url')

n jsonify({

'status'

: error 'error': {'code': 'Could not extract a direct video URL for this link.'}

}), 500

return jsonify({'status': 'success', 'url': video_url})

except Exception return jsonify({'status': 'error', 'error': {'code': str(e)}}), 500

app.run(host='0.0.0.0', port=5000)
