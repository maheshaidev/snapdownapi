#!/usr/bin/env python3
"""
SnapDown - Snapchat Video Downloader Backend
Modern Python Flask backend using yt-dlp for video extraction
"""

import os
import re
import json
import logging
import tempfile
import subprocess
from datetime import datetime
from urllib.parse import urlparse, unquote
from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
import yt_dlp
import requests
from werkzeug.exceptions import BadRequest

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
CORS(app, origins=['http://localhost:3000', 'https://snapdown.app'])

# Configuration
class Config:
    DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'
    PORT = int(os.getenv('PORT', 5000))
    TEMP_DIR = os.getenv('TEMP_DIR', tempfile.gettempdir())
    MAX_FILESIZE = 100 * 1024 * 1024  # 100MB limit

config = Config()

class SnapchatExtractor:
    """Modern Snapchat video extractor using yt-dlp"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        
        # Check if FFmpeg is available
        self.ffmpeg_available = self._check_ffmpeg()
        if not self.ffmpeg_available:
            logger.warning("FFmpeg not found. HLS to MP4 conversion will not be available.")
        
        # yt-dlp configuration optimized for MP4 extraction
        self.ytdl_opts = {
            'format': 'best[ext=mp4][protocol!=m3u8][protocol!=hls]/best[ext=mp4]/mp4[protocol!=m3u8]/best[protocol!=m3u8][protocol!=hls]/best[height<=720]/best',
            'outtmpl': os.path.join(config.TEMP_DIR, '%(title)s.%(ext)s'),
            'extractaudio': False,
            'audioformat': 'mp3',
            'embed_subs': False,
            'writesubtitles': False,
            'writeautomaticsub': False,
            'ignoreerrors': False,
            'no_warnings': False,
            'quiet': False,
            'verbose': config.DEBUG,
            'extract_flat': False,
            'writethumbnail': False,
            'writeinfojson': False,
            'cookiefile': None,
            'prefer_ffmpeg': True,
            'postprocessors': [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }],
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-us,en;q=0.5',
                'Accept-Encoding': 'gzip,deflate',
                'Accept-Charset': 'ISO-8859-1,utf-8;q=0.7,*;q=0.7',
                'Keep-Alive': '300',
                'Connection': 'keep-alive',
            }
        }

    def is_valid_snapchat_url(self, url):
        """Validate if URL is a Snapchat video URL"""
        snapchat_patterns = [
            r'https?://(www\.)?(snapchat\.com|snap\.chat)/t/[A-Za-z0-9_-]+',
            r'https?://(www\.)?(snapchat\.com|snap\.chat)/s/[A-Za-z0-9_-]+', 
            r'https?://(story\.)?(snapchat\.com)/s/[A-Za-z0-9_-]+',
            r'https?://(story\.)?(snapchat\.com)/p/[A-Za-z0-9_-]+',
            r'https?://(www\.)?(snapchat\.com)/discover/[A-Za-z0-9_-]+',
            r'https?://(www\.)?(snapchat\.com)/spotlight/[A-Za-z0-9_-]+',
            # New Spotlight URL pattern with @username
            r'https?://(www\.)?snapchat\.com/@[A-Za-z0-9_.-]+/spotlight/[A-Za-z0-9_-]+',
            # Additional patterns for Snapchat Spotlight with query parameters
            r'https?://(www\.)?snapchat\.com/spotlight/[A-Za-z0-9_-]+\?',
            # Story URLs with UUIDs that contain hyphens and numeric IDs
            r'https?://story\.snapchat\.com/p/[a-f0-9-]{36}/[0-9]+',
            r'https?://story\.snapchat\.com/s/[A-Za-z0-9_-]+\?',
            # General Snapchat domain patterns to catch various URL formats
            r'https?://(www\.)?snapchat\.com/[A-Za-z0-9_\/@.-]+',
            r'https?://story\.snapchat\.com/[A-Za-z0-9_\/-]+',
            # Additional patterns for different Snapchat URL formats
            r'https?://t\.snapchat\.com/[A-Za-z0-9_-]+',
            r'https?://www\.snapchat\.com/add/[A-Za-z0-9_.-]+',
        ]
        
        return any(re.match(pattern, url) for pattern in snapchat_patterns)
    
    def _check_ffmpeg(self):
        """Check if FFmpeg is available in the system"""
        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False
    
    def _is_story_url(self, url):
        """Check if URL is a Snapchat story URL that might need HLS conversion"""
        story_patterns = [
            r'https?://story\.snapchat\.com/p/',
            r'https?://story\.snapchat\.com/s/',
        ]
        return any(re.search(pattern, url) for pattern in story_patterns)
    
    def _convert_hls_to_mp4(self, hls_url, output_path):
        """Convert HLS stream to MP4 using FFmpeg with low memory usage"""
        if not self.ffmpeg_available:
            raise RuntimeError("FFmpeg is not available for HLS conversion")
        
        try:
            # FFmpeg command optimized for low memory usage
            cmd = [
                'ffmpeg',
                '-i', hls_url,
                '-c', 'copy',  # Copy streams without re-encoding when possible
                '-movflags', '+faststart',  # Optimize for web playback
                '-f', 'mp4',
                '-y',  # Overwrite output file
                output_path
            ]
            
            logger.info(f"Converting HLS to MP4: {hls_url} -> {output_path}")
            
            # Run FFmpeg with timeout and capture output
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
                check=True
            )
            
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                logger.info(f"Successfully converted HLS to MP4: {output_path}")
                return output_path
            else:
                raise RuntimeError("FFmpeg conversion failed - output file is empty or doesn't exist")
                
        except subprocess.TimeoutExpired:
            logger.error("FFmpeg conversion timed out")
            raise RuntimeError("Video conversion timed out")
        except subprocess.CalledProcessError as e:
            logger.error(f"FFmpeg conversion failed: {e.stderr}")
            raise RuntimeError(f"Video conversion failed: {e.stderr}")
        except Exception as e:
            logger.error(f"Unexpected error during HLS conversion: {str(e)}")
            raise

    def extract_video_info(self, url):
        """Extract video information using yt-dlp"""
        try:
            # Clean and validate URL
            if not self.is_valid_snapchat_url(url):
                # Try generic URL patterns that might work - be more lenient
                if not any(domain in url.lower() for domain in ['snapchat.com', 'snap.chat']):
                    raise ValueError("URL must be from Snapchat")
                # Log but continue if it's a Snapchat domain but doesn't match our patterns
                logger.warning(f"URL doesn't match known patterns but is from Snapchat domain: {url}")
            
            logger.info(f"Extracting info from URL: {url}")
            
            # Configure yt-dlp with MP4-focused options for Snapchat
            ytdl_opts_custom = self.ytdl_opts.copy()
            ytdl_opts_custom.update({
                'format': 'best[ext=mp4][height<=1080]/mp4[height<=1080]/best[height<=1080]/best',
                'ignoreerrors': True,  # Continue on errors
                'no_warnings': True,   # Reduce noise
                'extract_flat': False,
                'prefer_ffmpeg': True,
                'postprocessors': [{
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4',
                }],
            })
            
            with yt_dlp.YoutubeDL(ytdl_opts_custom) as ytdl:
                # Extract information without downloading
                info = ytdl.extract_info(url, download=False)
                
                if not info:
                    raise ValueError("Could not extract video information")
                
                # Extract relevant information
                video_info = {
                    'success': True,
                    'mediaUrl': info.get('url') or info.get('webpage_url'),
                    'mediaType': 'video',
                    'title': info.get('title', 'Snapchat Video'),
                    'duration': info.get('duration'),
                    'thumbnail': info.get('thumbnail'),
                    'uploader': info.get('uploader', 'Snapchat User'),
                    'view_count': info.get('view_count'),
                    'upload_date': info.get('upload_date'),
                    'description': info.get('description'),
                    'formats': []
                }
                
                # Extract available formats with different handling for story vs spotlight videos
                if 'formats' in info and info['formats']:
                    is_story = self._is_story_url(url)
                    mp4_formats = []
                    hls_formats = []
                    other_formats = []
                    
                    for fmt in info['formats']:
                        if not fmt.get('url'):
                            continue
                            
                        format_info = {
                            'format_id': fmt.get('format_id', 'unknown'),
                            'url': fmt.get('url'),
                            'ext': fmt.get('ext', 'mp4'),
                            'quality': fmt.get('quality', 0),
                            'filesize': fmt.get('filesize'),
                            'width': fmt.get('width'),
                            'height': fmt.get('height'),
                            'fps': fmt.get('fps'),
                            'vcodec': fmt.get('vcodec'),
                            'acodec': fmt.get('acodec'),
                            'protocol': fmt.get('protocol'),
                        }
                        
                        # Check if this is an HLS format
                        is_hls = (fmt.get('url', '').endswith('.m3u8') or 
                                 fmt.get('protocol') == 'm3u8' or
                                 'hls' in str(fmt.get('protocol', '')).lower() or
                                 fmt.get('ext') == 'm3u8')
                        
                        if is_hls:
                            if is_story and self.ffmpeg_available:
                                # For story videos, include HLS with conversion flag
                                format_info['needs_conversion'] = True
                                format_info['original_ext'] = format_info['ext']
                                format_info['ext'] = 'mp4'  # Will be converted to MP4
                                hls_formats.append(format_info)
                            # Skip HLS for spotlight videos (they usually have direct MP4)
                        elif (fmt.get('ext') == 'mp4' or 
                              'mp4' in str(fmt.get('url', '')) or
                              fmt.get('vcodec', '').startswith('h264')):
                            # Prioritize MP4 formats
                            mp4_formats.append(format_info)
                        else:
                            other_formats.append(format_info)
                    
                    # For story videos: HLS first (will be converted), then MP4, then others
                    # For spotlight videos: MP4 first, then others (no HLS)
                    if is_story:
                        video_info['formats'] = (hls_formats + mp4_formats + other_formats)[:5]
                    else:
                        video_info['formats'] = (mp4_formats + other_formats)[:5]
                
                # Set best MP4 format URL as main mediaUrl
                if video_info['formats']:
                    # Try to find the best MP4 format
                    best_mp4 = next((f for f in video_info['formats'] if f['ext'] == 'mp4'), None)
                    if best_mp4:
                        video_info['mediaUrl'] = best_mp4['url']
                    else:
                        video_info['mediaUrl'] = video_info['formats'][0]['url']
                
                logger.info(f"Successfully extracted info for: {video_info.get('title', 'Unknown')}")
                return video_info
                
        except Exception as e:
            logger.error(f"Error extracting video info: {str(e)}")
            raise

    def download_video(self, url, format_id=None):
        """Download video and return file path, with HLS to MP4 conversion for stories"""
        try:
            is_story = self._is_story_url(url)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            # Configure download options
            download_opts = self.ytdl_opts.copy()
            download_opts['outtmpl'] = os.path.join(config.TEMP_DIR, f'snapchat_video_{timestamp}.%(ext)s')
            
            # Check if we need to handle HLS conversion for story videos
            if is_story and format_id and format_id.endswith('_mp4'):
                # This is a converted HLS format for a story video
                original_format_id = format_id.replace('_mp4', '')
                
                # First extract info to get the HLS URL
                with yt_dlp.YoutubeDL(self.ytdl_opts) as ytdl:
                    info = ytdl.extract_info(url, download=False)
                    if not info or 'formats' not in info:
                        raise ValueError("Could not extract video information")
                    
                    # Find the HLS format
                    hls_format = next((f for f in info['formats'] 
                                     if f.get('format_id') == original_format_id), None)
                    
                    if not hls_format or not hls_format.get('url'):
                        raise ValueError(f"Could not find HLS format {original_format_id}")
                    
                    hls_url = hls_format['url']
                    
                    # Convert HLS to MP4
                    output_path = os.path.join(config.TEMP_DIR, f'snapchat_story_{timestamp}.mp4')
                    converted_path = self._convert_hls_to_mp4(hls_url, output_path)
                    return converted_path
            
            # Regular download for spotlight videos or direct MP4 formats
            if format_id and not format_id.endswith('_mp4'):
                download_opts['format'] = f'{format_id}/best[ext=mp4]/mp4/best'
            else:
                download_opts['format'] = 'best[ext=mp4][height<=1080]/mp4[height<=1080]/best[height<=1080]/best'
            
            # Ensure MP4 conversion for non-story videos
            if not is_story:
                download_opts['postprocessors'] = [{
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4',
                }]
            
            with yt_dlp.YoutubeDL(download_opts) as ytdl:
                # Download the video
                info = ytdl.extract_info(url, download=True)
                
                if not info:
                    raise ValueError("Could not download video")
                
                # Find the downloaded file (should be MP4 after conversion)
                filename = ytdl.prepare_filename(info)
                
                # Check for MP4 file first
                mp4_filename = filename.replace('.%(ext)s', '.mp4')
                if os.path.exists(mp4_filename):
                    return mp4_filename
                
                if os.path.exists(filename):
                    return filename
                
                # Try alternative filename patterns
                for ext in ['mp4', 'webm', 'mkv']:
                    alt_filename = filename.replace('.%(ext)s', f'.{ext}')
                    if os.path.exists(alt_filename):
                        return alt_filename
                
                raise FileNotFoundError("Downloaded file not found")
                
        except Exception as e:
            logger.error(f"Error downloading video: {str(e)}")
            raise

# Initialize extractor
extractor = SnapchatExtractor()

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'SnapDown Backend',
        'version': '1.0.0',
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/test-connection', methods=['GET'])
def test_connection():
    """Test API connection"""
    return jsonify({
        'success': True,
        'message': 'SnapDown backend is running',
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/extract', methods=['POST', 'OPTIONS'])
def extract_video():
    """Extract video information from Snapchat URL"""
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    
    try:
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({
                'success': False,
                'error': 'URL is required'
            }), 400
        
        url = data['url'].strip()
        if not url:
            return jsonify({
                'success': False,
                'error': 'URL cannot be empty'
            }), 400
        
        logger.info(f"Extracting video from URL: {url}")
        
        # Extract video information
        video_info = extractor.extract_video_info(url)
        
        return jsonify(video_info)
        
    except ValueError as e:
        logger.error(f"Validation error: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400
        
    except Exception as e:
        logger.error(f"Extraction error: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Failed to extract video: {str(e)}'
        }), 500

@app.route('/api/download', methods=['GET', 'OPTIONS'])
def download_video():
    """Download video file via proxy or serve converted file"""
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    
    try:
        video_url = request.args.get('url')
        filename = request.args.get('filename', 'snapchat_video.mp4')
        original_url = request.args.get('original_url', '')
        
        if not video_url:
            return jsonify({
                'success': False,
                'error': 'URL parameter is required'
            }), 400
        
        logger.info(f"Downloading video from URL: {video_url}")
        
        # Check if this is an M3U8 URL that needs conversion (story videos)
        if (video_url.endswith('.m3u8') or 'm3u8' in video_url) and original_url:
            # This is an M3U8 stream that needs to be converted to MP4
            is_story = extractor._is_story_url(original_url)
            
            if is_story and extractor.ffmpeg_available:
                logger.info(f"Converting M3U8 to MP4 for story video: {video_url}")
                
                try:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    output_path = os.path.join(config.TEMP_DIR, f'snapchat_story_{timestamp}.mp4')
                    
                    # Convert HLS to MP4
                    converted_path = extractor._convert_hls_to_mp4(video_url, output_path)
                    
                    # Serve the converted file
                    return send_file(
                        converted_path,
                        as_attachment=True,
                        download_name=filename,
                        mimetype='video/mp4'
                    )
                    
                except Exception as e:
                    logger.error(f"M3U8 conversion failed: {str(e)}")
                    return jsonify({
                        'success': False,
                        'error': f'Video conversion failed: {str(e)}'
                    }), 500
            else:
                return jsonify({
                    'success': False,
                    'error': 'M3U8 conversion not available (FFmpeg required)'
                }), 500
        
        # Check if this is a converted file (local file path)
        elif video_url.startswith('/') or (len(video_url) > 3 and video_url[1:3] == ':\\'):
            # This is a local file path from HLS conversion
            if os.path.exists(video_url):
                logger.info(f"Serving converted file: {video_url}")
                return send_file(
                    video_url,
                    as_attachment=True,
                    download_name=filename,
                    mimetype='video/mp4'
                )
            else:
                return jsonify({
                    'success': False,
                    'error': 'Converted file not found'
                }), 404
        
        # Regular proxy download for direct URLs
        response = requests.get(video_url, stream=True, timeout=30)
        response.raise_for_status()
        
        # Check content type
        content_type = response.headers.get('Content-Type', 'video/mp4')
        content_length = response.headers.get('Content-Length')
        
        # Create response with proper headers
        def generate():
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        
        flask_response = Response(
            generate(),
            mimetype=content_type,
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Content-Length': content_length,
                'Cache-Control': 'no-cache',
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                'Access-Control-Allow-Headers': '*',
            }
        )
        
        return flask_response
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Download request error: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Failed to download video: {str(e)}'
        }), 500
        
    except Exception as e:
        logger.error(f"Download error: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Download failed: {str(e)}'
        }), 500

@app.route('/api/formats', methods=['POST', 'OPTIONS'])
def get_available_formats():
    """Get available video formats for a Snapchat URL"""
    if request.method == 'OPTIONS':
        return jsonify({'success': True})
    
    try:
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({
                'success': False,
                'error': 'URL is required'
            }), 400
        
        url = data['url'].strip()
        video_info = extractor.extract_video_info(url)
        
        return jsonify({
            'success': True,
            'formats': video_info.get('formats', []),
            'title': video_info.get('title', 'Snapchat Video')
        })
        
    except Exception as e:
        logger.error(f"Error getting formats: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Failed to get formats: {str(e)}'
        }), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({
        'success': False,
        'error': 'Endpoint not found'
    }), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({
        'success': False,
        'error': 'Internal server error'
    }), 500

if __name__ == '__main__':
    logger.info("Starting SnapDown Backend Server")
    logger.info(f"Debug mode: {config.DEBUG}")
    logger.info(f"Port: {config.PORT}")
    
    app.run(
        host='0.0.0.0',
        port=config.PORT,
        debug=config.DEBUG,
        threaded=True
    )