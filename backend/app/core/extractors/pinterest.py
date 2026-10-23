import json

from .common import InfoExtractor
from .utils import (
    determine_ext,
    float_or_none,
    int_or_none,
    str_or_none,
    strip_or_none,
    traverse_obj,
    unified_timestamp,
    url_or_none,
)


class PinterestBaseIE(InfoExtractor):
    _VALID_URL_BASE = r'''(?x)
        https?://(?:[^/]+\.)?pinterest\.(?:
            com|fr|de|ch|jp|cl|ca|it|co\.uk|nz|ru|com\.au|at|pt|co\.kr|es|com\.mx|
            dk|ph|th|com\.uy|co|nl|info|kr|ie|vn|com\.vn|ec|mx|in|pe|co\.at|hu|
            co\.in|co\.nz|id|com\.ec|com\.py|tw|be|uk|com\.bo|com\.pe)'''

    def _call_api(self, resource, video_id, options):
        return self._download_json(
            f'https://www.pinterest.com/resource/{resource}Resource/get/',
            video_id, f'Download {resource} JSON metadata',
            query={'data': json.dumps({'options': options})},
            headers={'X-Pinterest-PWS-Handler': 'www/[username].js'})['resource_response']

    def _extract_video(self, data, extract_formats=True):
        video_id = data['id']
        thumbnails = []
        images = data.get('images')
        if isinstance(images, dict):
            for thumbnail in images.values():
                if not isinstance(thumbnail, dict):
                    continue
                thumbnail_url = url_or_none(thumbnail.get('url'))
                if not thumbnail_url:
                    continue
                thumbnails.append({
                    'url': thumbnail_url,
                    'width': int_or_none(thumbnail.get('width')),
                    'height': int_or_none(thumbnail.get('height')),
                })

        info = {
            'title': strip_or_none(traverse_obj(data, 'title', 'grid_title', default='')),
            'description': traverse_obj(data, 'seo_description', 'description'),
            'timestamp': unified_timestamp(data.get('created_at')),
            'thumbnails': thumbnails,
            'uploader': traverse_obj(data, ('closeup_attribution', 'full_name')),
            'uploader_id': str_or_none(traverse_obj(data, ('closeup_attribution', 'id'))),
            'repost_count': int_or_none(data.get('repin_count')),
            'comment_count': int_or_none(data.get('comment_count')),
            'categories': traverse_obj(data, ('pin_join', 'visual_annotation'), expected_type=list),
            'tags': traverse_obj(data, 'hashtags', expected_type=list),
        }

        urls = []
        formats = []
        duration = None
        domain = data.get('domain', '')
        if domain.lower() != 'uploaded by user' and traverse_obj(data, ('embed', 'src')):
            if not info['title']:
                info['title'] = None
            return {
                '_type': 'url_transparent',
                'url': data['embed']['src'],
                **info,
            }

        elif extract_formats:
            video_list = traverse_obj(
                data, ('videos', 'video_list'),
                ('story_pin_data', 'pages', ..., 'blocks', ..., 'video', 'video_list'),
                expected_type=dict, get_all=False, default={})
            for format_id, format_dict in video_list.items():
                if not isinstance(format_dict, dict):
                    continue
                format_url = url_or_none(format_dict.get('url'))
                if not format_url or format_url in urls:
                    continue
                urls.append(format_url)
                duration = float_or_none(format_dict.get('duration'), scale=1000)
                ext = determine_ext(format_url)
                if 'hls' in format_id.lower() or ext == 'm3u8':
                    formats.extend(self._extract_m3u8_formats(
                        format_url, video_id, 'mp4', entry_protocol='m3u8_native',
                        m3u8_id=format_id, fatal=False))
                else:
                    formats.append({
                        'url': format_url,
                        'format_id': format_id,
                        'width': int_or_none(format_dict.get('width')),
                        'height': int_or_none(format_dict.get('height')),
                        'duration': duration,
                    })

        return {
            'id': video_id,
            'formats': formats,
            'duration': duration,
            'webpage_url': f'https://www.pinterest.com/pin/{video_id}/',
            'extractor_key': PinterestIE.ie_key(),
            'extractor': PinterestIE.IE_NAME,
            **info,
        }


class PinterestIE(PinterestBaseIE):
    _VALID_URL = rf'{PinterestBaseIE._VALID_URL_BASE}/pin/(?:[\w-]+--)?(?P<id>\d+)'
    _TESTS = [{
        # formats found in data['videos']
        'url': 'https://www.pinterest.com/pin/664281013778109217/',
        'md5': '6550c2af85d6d9f3fe3b88954d1577fc',
        'info_dict': {
            'id': '664281013778109217',
            'ext': 'mp4',
            'title': 'Origami',
            'description': 'md5:e29801cab7d741ea8c741bc50c8d00ab',
            'duration': 57.7,
            'timestamp': 1593073622,
            'upload_date': '20200625',
            'repost_count': int,
            'comment_count': int,
            'categories': list,
            'tags': list,
            'thumbnail': r're:^https?://.*\.(?:jpg|png)$',
        },
    }, {
        # formats found in data['story_pin_data']
        'url': 'https://www.pinterest.com/pin/1084663891475263837/',
        'md5': '069ac19919ab9e1e13fa60de46290b03',
        'info_dict': {
            'id': '1084663891475263837',
            'ext': 'mp4',
            'title': 'Gadget, Cool products, Amazon product, technology, Kitchen gadgets',
            'description': 'md5:d0a4b6ae996ff0c6eed83bc869598d13',
            'uploader': 'CoolCrazyGadgets',
            'uploader_id': '1084664028912989237',
            'upload_date': '20211003',
            'timestamp': 1633246654.0,
            'duration': 14.9,
            'comment_count': int,
            'repost_count': int,
            'categories': 'count:9',
            'tags': list,
            'thumbnail': r're:^https?://.*\.(?:jpg|png)$',
        },
    }, {
        # vimeo.com embed
        'url': 'https://www.pinterest.ca/pin/441282463481903715/',
        'info_dict': {
            'id': '111691128',
            'ext': 'mp4',
            'title': 'Tonite Let\'s All Make Love In London (1967)',
            'description': 'md5:8190f37b3926807809ec57ec21aa77b2',
            'uploader': 'Vimeo',
            'uploader_id': '473792960706651251',
            'upload_date': '20180120',
            'timestamp': 1516409040,
            'duration': 3404,
            'comment_count': int,
            'repost_count': int,
            'categories': 'count:9',
            'tags': [],
            'thumbnail': r're:^https?://.*\.(?:jpg|png)$',
            'uploader_url': 'https://vimeo.com/willardandrade',
        },
        'params': {
            'skip_download': 'm3u8',
        },
    }, {
        'url': 'https://co.pinterest.com/pin/824721750502199491/',
        'only_matching': True,
    },
        {
        'url': 'https://pinterest.com/pin/dive-into-serenity-blue-lagoon-pedi-nails-for-a-tranquil-and-refreshing-spa-experience-video-in-2024--2885187256207927',
        'info_dict': {
            'id': '2885187256207927',
            'ext': 'mp4',
            'title': 'Dive into Serenity: Blue Lagoon Pedi Nails for a Tranquil and Refreshing Spa Experience! 💙💅',
            'description': 'md5:5da41c767d2317e42e49b663b0b2150f',
            'uploader': 'Glamour Artistry |Everyday Outfits, Luxury Fashion & Nail Designs',
            'uploader_id': '1142999717836434688',
            'upload_date': '20240702',
            'timestamp': 1719939156,
            'duration': 7.967,
            'comment_count': int,
            'repost_count': int,
            'categories': 'count:9',
            'tags': ['#BlueLagoonPediNails', '#SpaExperience'],
            'thumbnail': r're:^https?://.*\.(?:jpg|png)$',
        },
    }]

    def _real_extract(self, url):
        video_id = self._match_id(url)
        data = self._call_api(
            'Pin', video_id, {
                'field_set_key': 'unauth_react_main_pin',
                'id': video_id,
            })['data']
        return self._extract_video(data)


class PinterestCollectionIE(PinterestBaseIE):
    _VALID_URL = rf'{PinterestBaseIE._VALID_URL_BASE}/(?P<username>[^/]+)/(?P<id>[^/?#&]+)'
    _TESTS = [{
        'url': 'https://www.pinterest.ca/mashal0407/cool-diys/',
        'info_dict': {
            'id': '585890301462791043',
            'title': 'cool diys',
        },
        'playlist_count': 8,
    }, {
        'url': 'https://www.pinterest.ca/fudohub/videos/',
        'info_dict': {
            'id': '682858430939307450',
            'title': 'VIDEOS',
        },
        'playlist_mincount': 365,
        'skip': 'Test with extract_formats=False',
    }]

    @classmethod
    def suitable(cls, url):
        return False if PinterestIE.suitable(url) else super().suitable(url)

    def _real_extract(self, url):
        username, slug = self._match_valid_url(url).groups()
        board = self._call_api(
            'Board', slug, {
                'slug': slug,
                'username': username,
            })['data']
        board_id = board['id']
        options = {
            'board_id': board_id,
            'page_size': 250,
        }
        bookmark = None
        entries = []
        while True:
            if bookmark:
                options['bookmarks'] = [bookmark]
            board_feed = self._call_api('BoardFeed', board_id, options)
            for item in (board_feed.get('data') or []):
                if not isinstance(item, dict) or item.get('type') != 'pin':
                    continue
                video_id = item.get('id')
                if video_id:
                    # Some pins may not be available anonymously via pin URL
                    # video = self._extract_video(item, extract_formats=False)
                    # video.update({
                    #     '_type': 'url_transparent',
                    #     'url': 'https://www.pinterest.com/pin/%s/' % video_id,
                    # })
                    # entries.append(video)
                    entries.append(self._extract_video(item))
            bookmark = board_feed.get('bookmark')
            if not bookmark:
                break
        return self.playlist_result(
            entries, playlist_id=board_id, playlist_title=board.get('name'))

# ============================================================================
# Native BaseExtractor wrapper for MyArchive Downloader
# ============================================================================

"""Extractor nativo do Pinterest.

Suporta pins com vídeo (pinterest.com/pin/id e pin.it/id).
Extrai streams MP4 progressivos em alta resolução e manifests HLS.
"""

import json
import logging
import re
from typing import ClassVar

from ..exceptions import ExtractionError
from ..models import Container, MediaInfo, MediaType, Protocol, StreamFormat
from .base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

PIN_ID_RE = re.compile(r"pinterest\.[a-z.]+/pin/(?P<id>\d+)")
PIN_SHORT_RE = re.compile(r"pin\.it/(?P<id>[a-zA-Z0-9]+)")


class PinterestExtractor(BaseExtractor):
    """Extractor nativo para vídeos do Pinterest."""

    name: ClassVar[str] = "pinterest"
    priority: ClassVar[int] = 42
    patterns: ClassVar[list[str]] = [
        r"^https?://(?:www\.)?pinterest\.(?:com|pt|es|fr|co\.uk|de|ca)/pin/(?P<id>\d+)",
        r"^https?://pin\.it/(?P<id>[a-zA-Z0-9]+)",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        headers = ctx.request_headers(referer="https://www.pinterest.com/")
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        pin_id = None
        m_short = PIN_SHORT_RE.search(url)
        if m_short:
            try:
                res = await ctx.http.get(url, headers=headers)
                final_url = res.url_final or url
                m = PIN_ID_RE.search(final_url)
                if m:
                    pin_id = m.group("id")
            except Exception:  # noqa: BLE001
                pass

        if not pin_id:
            m = PIN_ID_RE.search(url)
            pin_id = m.group("id") if m else None

        if not pin_id:
            raise ExtractionError(f"Não foi possível identificar o pin em {url}")

        page_url = f"https://www.pinterest.com/pin/{pin_id}/"
        try:
            res = await ctx.http.get(page_url, headers=headers)
            body = res.text
        except Exception as exc:
            raise ExtractionError(f"Falha ao aceder ao Pinterest: {exc}") from exc

        # Extrai o script de dados do Pinterest
        script_match = re.search(r'<script id="__PWS_DATA__"[^>]*>(?P<json>\{.+?\})</script>', body)
        data = {}
        if script_match:
            try:
                data = json.loads(script_match.group("json"))
            except Exception:  # noqa: BLE001
                pass

        video_list = None
        title = f"Pinterest Pin {pin_id}"
        description = None
        thumbnail = None

        # Procura nos nós do JSON
        props = data.get("props", {}).get("initialReduxState", {}).get("pins", {}).get(pin_id, {})
        if props:
            title = props.get("title") or props.get("grid_title") or title
            description = props.get("description")
            thumbnail = (props.get("images", {}).get("orig", {}) or {}).get("url")
            videos = props.get("videos", {}) or {}
            video_list = videos.get("video_list") or {}

        formats: list[StreamFormat] = []

        if video_list and isinstance(video_list, dict):
            for key, val in video_list.items():
                if not isinstance(val, dict):
                    continue
                v_url = val.get("url")
                if not v_url:
                    continue

                if v_url.endswith(".m3u8"):
                    try:
                        hls_fmts = await self.formats_from_manifest(v_url, ctx, headers=headers)
                        formats.extend(hls_fmts)
                    except Exception:  # noqa: BLE001
                        formats.append(
                            StreamFormat(
                                id="pin-hls",
                                url=v_url,
                                media_type=MediaType.VIDEO,
                                container=Container.MP4,
                                protocol=Protocol.HLS,
                                manifest_url=v_url,
                                manifest_kind="hls",
                            )
                        )
                else:
                    formats.append(
                        StreamFormat(
                            id=f"pin-{key.lower()}",
                            url=v_url,
                            media_type=MediaType.VIDEO,
                            container=Container.MP4,
                            protocol=Protocol.HTTPS,
                            width=val.get("width"),
                            height=val.get("height"),
                            duration=float(val["duration"]) / 1000 if val.get("duration") else None,
                        )
                    )

        if not formats:
            # Fallback por regex no HTML
            m_mp4 = re.findall(r'https?://v\.pinimg\.com/videos/[^\s"\'<>]+\.mp4', body)
            for i, mp4_url in enumerate(set(m_mp4)):
                formats.append(
                    StreamFormat(
                        id=f"pin-raw-{i}",
                        url=mp4_url,
                        media_type=MediaType.VIDEO,
                        container=Container.MP4,
                        protocol=Protocol.HTTPS,
                    )
                )

        if not formats:
            raise ExtractionError(
                "Nenhum vídeo encontrado neste Pin do Pinterest",
                details={"pin_id": pin_id},
            )

        return MediaInfo(
            extractor=self.name,
            webpage_url=page_url,
            original_url=url,
            title=title,
            description=description,
            thumbnail=thumbnail,
            formats=formats,
        )
