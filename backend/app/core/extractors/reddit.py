import json
import urllib.parse

from .common import InfoExtractor
from .utils import (
    ExtractorError,
    float_or_none,
    int_or_none,
    parse_qs,
    traverse_obj,
    truncate_string,
    try_get,
    unescapeHTML,
    update_url_query,
    url_or_none,
)


class RedditIE(InfoExtractor):
    _VALID_URL = r'https?://(?:\w+\.)?reddit(?:media)?\.com/(?P<slug>(?:(?:r|user)/[^/]+/)?comments/(?P<id>[^/?#&]+))'
    _TESTS = [{
        'url': 'https://www.reddit.com/r/videos/comments/6rrwyj/that_small_heart_attack/',
        'info_dict': {
            'id': 'zv89llsvexdz',
            'ext': 'mp4',
            'display_id': '6rrwyj',
            'title': 'That small heart attack.',
            'alt_title': 'That small heart attack.',
            'thumbnail': r're:^https?://.*\.(?:jpg|png)',
            'thumbnails': 'count:4',
            'timestamp': 1501941939,
            'upload_date': '20170805',
            'uploader': 'Antw87',
            'duration': 12,
            'like_count': int,
            'dislike_count': int,
            'comment_count': int,
            'age_limit': 0,
            'channel_id': 'videos',
        },
        'params': {
            'skip_download': True,
        },
    }, {
        # 1080p fallback format
        'url': 'https://www.reddit.com/r/aww/comments/90bu6w/heat_index_was_110_degrees_so_we_offered_him_a/',
        'md5': '8b5902cfda3006bf90faea7adf765a49',
        'info_dict': {
            'id': 'gyh95hiqc0b11',
            'ext': 'mp4',
            'display_id': '90bu6w',
            'title': 'Heat index was 110 degrees so we offered him a cold drink. He went fo...',
            'alt_title': 'Heat index was 110 degrees so we offered him a cold drink. He went for a full body soak instead',
            'thumbnail': r're:^https?://.*\.(?:jpg|png)',
            'thumbnails': 'count:7',
            'timestamp': 1532051078,
            'upload_date': '20180720',
            'uploader': 'FootLoosePickleJuice',
            'duration': 14,
            'like_count': int,
            'dislike_count': int,
            'comment_count': int,
            'age_limit': 0,
            'channel_id': 'aww',
        },
    }, {
        # User post
        'url': 'https://www.reddit.com/user/creepyt0es/comments/nip71r/i_plan_to_make_more_stickers_and_prints_check/',
        'info_dict': {
            'id': 'zasobba6wp071',
            'ext': 'mp4',
            'display_id': 'nip71r',
            'title': 'I plan to make more stickers and prints! Check them out on my Etsy! O...',
            'alt_title': 'I plan to make more stickers and prints! Check them out on my Etsy! Or get them through my Patreon. Links below.',
            'thumbnail': r're:^https?://.*\.(?:jpg|png)',
            'thumbnails': 'count:5',
            'timestamp': 1621709093,
            'upload_date': '20210522',
            'uploader': 'creepyt0es',
            'duration': 6,
            'like_count': int,
            'dislike_count': int,
            'comment_count': int,
            'age_limit': 18,
            'channel_id': 'u_creepyt0es',
        },
        'params': {
            'skip_download': True,
        },
    }, {
        # videos embedded in reddit text post
        'url': 'https://www.reddit.com/r/KamenRider/comments/wzqkxp/finale_kamen_rider_revice_episode_50_family_to/',
        'playlist_count': 2,
        'info_dict': {
            'id': 'wzqkxp',
            'title': '[Finale] Kamen Rider Revice Episode 50 "Family to the End, Until the ...',
            'alt_title': '[Finale] Kamen Rider Revice Episode 50 "Family to the End, Until the Day We Meet Again" Discussion',
            'description': 'md5:5b7deb328062b164b15704c5fd67c335',
            'uploader': 'TheTwelveYearOld',
            'channel_id': 'KamenRider',
            'comment_count': int,
            'like_count': int,
            'dislike_count': int,
            'age_limit': 0,
            'timestamp': 1661676059.0,
            'upload_date': '20220828',
        },
    }, {
        # crossposted reddit-hosted media
        'url': 'https://www.reddit.com/r/dumbfuckers_club/comments/zjjw82/cringe/',
        'md5': '746180895c7b75a9d6b05341f507699a',
        'info_dict': {
            'id': 'a1oneun6pa5a1',
            'ext': 'mp4',
            'display_id': 'zjjw82',
            'title': 'Cringe',
            'alt_title': 'Cringe',
            'uploader': 'Otaku-senpai69420',
            'thumbnail': r're:^https?://.*\.(?:jpg|png)',
            'upload_date': '20221212',
            'timestamp': 1670812309,
            'duration': 16,
            'like_count': int,
            'dislike_count': int,
            'comment_count': int,
            'age_limit': 0,
            'channel_id': 'dumbfuckers_club',
        },
    }, {
        # post link without subreddit
        'url': 'https://www.reddit.com/comments/124pp33',
        'md5': '15eec9d828adcef4468b741a7e45a395',
        'info_dict': {
            'id': 'antsenjc2jqa1',
            'ext': 'mp4',
            'display_id': '124pp33',
            'title': 'Harmless prank of some old friends',
            'alt_title': 'Harmless prank of some old friends',
            'uploader': 'Dudezila',
            'channel_id': 'ContagiousLaughter',
            'duration': 17,
            'upload_date': '20230328',
            'timestamp': 1680012043,
            'thumbnail': r're:^https?://.*\.(?:jpg|png)',
            'age_limit': 0,
            'comment_count': int,
            'dislike_count': int,
            'like_count': int,
        },
    }, {
        # quarantined subreddit post
        'url': 'https://old.reddit.com/r/GenZedong/comments/12fujy3/based_hasan/',
        'md5': '3156ea69e3c1f1b6259683c5abd36e71',
        'info_dict': {
            'id': '8bwtclfggpsa1',
            'ext': 'mp4',
            'display_id': '12fujy3',
            'title': 'Based Hasan?',
            'alt_title': 'Based Hasan?',
            'uploader': 'KingNigelXLII',
            'channel_id': 'GenZedong',
            'duration': 16,
            'upload_date': '20230408',
            'timestamp': 1680979138,
            'age_limit': 0,
            'comment_count': int,
            'dislike_count': int,
            'like_count': int,
        },
        'skip': 'Requires account that has opted-in to the GenZedong subreddit',
    }, {
        # subtitles in HLS manifest
        'url': 'https://www.reddit.com/r/Unexpected/comments/1cl9h0u/the_insurance_claim_will_be_interesting/',
        'info_dict': {
            'id': 'a2mdj5d57qyc1',
            'ext': 'mp4',
            'display_id': '1cl9h0u',
            'title': 'The insurance claim will be interesting',
            'alt_title': 'The insurance claim will be interesting',
            'uploader': 'darrenpauli',
            'channel_id': 'Unexpected',
            'duration': 53,
            'upload_date': '20240506',
            'timestamp': 1714966382,
            'age_limit': 0,
            'comment_count': int,
            'dislike_count': int,
            'like_count': int,
            'subtitles': {'en': 'mincount:1'},
        },
        'params': {
            'skip_download': True,
        },
    }, {
        # subtitles from caption-url
        'url': 'https://www.reddit.com/r/soccer/comments/1cxwzso/tottenham_1_0_newcastle_united_james_maddison_31/',
        'info_dict': {
            'id': 'xbmj4t3igy1d1',
            'ext': 'mp4',
            'display_id': '1cxwzso',
            'title': 'Tottenham [1] - 0 Newcastle United - James Maddison 31\'',
            'alt_title': 'Tottenham [1] - 0 Newcastle United - James Maddison 31\'',
            'uploader': 'Woodstovia',
            'channel_id': 'soccer',
            'duration': 30,
            'upload_date': '20240522',
            'timestamp': 1716373798,
            'age_limit': 0,
            'comment_count': int,
            'dislike_count': int,
            'like_count': int,
            'subtitles': {'en': 'mincount:1'},
        },
        'params': {
            'skip_download': True,
            'writesubtitles': True,
        },
    }, {
        # "gated" subreddit post
        'url': 'https://old.reddit.com/r/ketamine/comments/degtjo/when_the_k_hits/',
        'info_dict': {
            'id': 'gqsbxts133r31',
            'ext': 'mp4',
            'display_id': 'degtjo',
            'title': 'When the K hits',
            'alt_title': 'When the K hits',
            'uploader': '[deleted]',
            'channel_id': 'ketamine',
            'comment_count': int,
            'like_count': int,
            'dislike_count': int,
            'age_limit': 18,
            'duration': 34,
            'thumbnail': r're:https?://.+/.+\.(?:jpg|png)',
            'timestamp': 1570438713.0,
            'upload_date': '20191007',
        },
    }, {
        'url': 'https://www.reddit.com/r/videos/comments/6rrwyj',
        'only_matching': True,
    }, {
        # imgur
        'url': 'https://www.reddit.com/r/MadeMeSmile/comments/6t7wi5/wait_for_it/',
        'only_matching': True,
    }, {
        # imgur @ old reddit
        'url': 'https://old.reddit.com/r/MadeMeSmile/comments/6t7wi5/wait_for_it/',
        'only_matching': True,
    }, {
        # streamable
        'url': 'https://www.reddit.com/r/videos/comments/6t7sg9/comedians_hilarious_joke_about_the_guam_flag/',
        'only_matching': True,
    }, {
        # youtube
        'url': 'https://www.reddit.com/r/videos/comments/6t75wq/southern_man_tries_to_speak_without_an_accent/',
        'only_matching': True,
    }, {
        # reddit video @ nm reddit
        'url': 'https://nm.reddit.com/r/Cricket/comments/8idvby/lousy_cameraman_finds_himself_in_cairns_line_of/',
        'only_matching': True,
    }, {
        'url': 'https://www.redditmedia.com/r/serbia/comments/pu9wbx/ako_vu%C4%8Di%C4%87_izgubi_izbore_ja_%C4%87u_da_crknem/',
        'only_matching': True,
    }]

    @property
    def _is_logged_in(self):
        return bool(self._get_cookies('https://www.reddit.com/').get('reddit_session'))

    def _real_initialize(self):
        if not self._is_logged_in:
            # We need to get a 'loid' cookie to access the API anonymously
            self._request_webpage(
                'https://old.reddit.com/', None,
                'Setting up session via old reddit', 'Session request failed', fatal=False)

        # Set cookie to opt-in to age-restricted subreddits
        self._set_cookie('reddit.com', 'over18', '1')
        # Set cookie to opt-in to "gated" subreddits
        options = traverse_obj(self._get_cookies('https://www.reddit.com/'), (
            '_options', 'value', {urllib.parse.unquote}, {json.loads}, {dict})) or {}
        options['pref_gated_sr_optin'] = True
        self._set_cookie('reddit.com', '_options', urllib.parse.quote(json.dumps(options)))

    def _get_subtitles(self, video_id):
        # Fallback if there were no subtitles provided by DASH or HLS manifests
        caption_url = f'https://v.redd.it/{video_id}/wh_ben_en.vtt'
        if self._is_valid_url(caption_url, video_id, item='subtitles'):
            return {'en': [{'url': caption_url}]}

    def _real_extract(self, url):
        slug, video_id = self._match_valid_url(url).group('slug', 'id')

        # Fallback for if old.reddit session request failed
        if not self._is_logged_in and not self._get_cookies('https://www.reddit.com/').get('loid'):
            self._request_webpage(
                f'https://www.reddit.com/svc/shreddit/{slug}', video_id,
                'Setting up session via shreddit', 'Session request failed',
                fatal=False, impersonate=True,
                query={
                    'seeker-session': 'false',
                    'render-mode': 'partial',
                    'referer': url,
                })

        try:
            data = self._download_json(
                f'https://www.reddit.com/{slug}/.json', video_id, expected_status=403)
        except ExtractorError as e:
            if isinstance(e.cause, json.JSONDecodeError):
                if self._is_logged_in:
                    raise ExtractorError('Your IP address is unable to access the Reddit API', expected=True)
                self.raise_login_required('Account authentication is required')
            raise

        if traverse_obj(data, 'error') == 403:
            reason = data.get('reason')
            if reason == 'quarantined':
                self.raise_login_required('Quarantined subreddit; an account that has opted in is required')
            elif reason == 'private':
                self.raise_login_required('Private subreddit; an account that has been approved is required')
            else:
                raise ExtractorError(f'HTTP Error 403 Forbidden; reason given: {reason}')

        data = data[0]['data']['children'][0]['data']
        video_url = data['url']

        thumbnails = []

        def add_thumbnail(src):
            if not isinstance(src, dict):
                return
            thumbnail_url = url_or_none(src.get('url'))
            if not thumbnail_url:
                return
            thumbnails.append({
                'url': unescapeHTML(thumbnail_url),
                'width': int_or_none(src.get('width')),
                'height': int_or_none(src.get('height')),
                'http_headers': {'Accept': '*/*'},
            })

        for image in try_get(data, lambda x: x['preview']['images']) or []:
            if not isinstance(image, dict):
                continue
            add_thumbnail(image.get('source'))
            resolutions = image.get('resolutions')
            if isinstance(resolutions, list):
                for resolution in resolutions:
                    add_thumbnail(resolution)

        info = {
            'thumbnails': thumbnails,
            'age_limit': {True: 18, False: 0}.get(data.get('over_18')),
            **traverse_obj(data, {
                'title': ('title', {truncate_string(left=72)}),
                'alt_title': ('title', {str}),
                'description': ('selftext', {str}, filter),
                'timestamp': ('created_utc', {float_or_none}),
                'uploader': ('author', {str}),
                'channel_id': ('subreddit', {str}),
                'like_count': ('ups', {int_or_none}),
                'dislike_count': ('downs', {int_or_none}),
                'comment_count': ('num_comments', {int_or_none}),
            }),
        }

        parsed_url = urllib.parse.urlparse(video_url)

        # Check for embeds in text posts, or else raise to avoid recursing into the same reddit URL
        if 'reddit.com' in parsed_url.netloc and f'/{video_id}/' in parsed_url.path:
            entries = []
            for media in traverse_obj(data, ('media_metadata', ...), expected_type=dict):
                if not media.get('id') or media.get('e') != 'RedditVideo':
                    continue
                formats = []
                if media.get('hlsUrl'):
                    formats.extend(self._extract_m3u8_formats(
                        unescapeHTML(media['hlsUrl']), video_id, 'mp4', m3u8_id='hls', fatal=False))
                if media.get('dashUrl'):
                    formats.extend(self._extract_mpd_formats(
                        unescapeHTML(media['dashUrl']), video_id, mpd_id='dash', fatal=False))
                if formats:
                    entries.append({
                        'id': media['id'],
                        'display_id': video_id,
                        'formats': formats,
                        **info,
                    })
            if entries:
                return self.playlist_result(entries, video_id, **info)
            self.raise_no_formats('No media found', expected=True, video_id=video_id)
            return {**info, 'id': video_id}

        # Check if media is hosted on reddit:
        reddit_video = traverse_obj(data, (
            (None, ('crosspost_parent_list', ...)), ('secure_media', 'media'), 'reddit_video'), get_all=False)
        if reddit_video:
            playlist_urls = [
                try_get(reddit_video, lambda x: unescapeHTML(x[y]))
                for y in ('dash_url', 'hls_url')
            ]

            # Update video_id
            display_id = video_id
            video_id = self._search_regex(
                r'https?://v\.redd\.it/(?P<id>[^/?#&]+)', reddit_video['fallback_url'],
                'video_id', default=display_id)

            dash_playlist_url = playlist_urls[0] or f'https://v.redd.it/{video_id}/DASHPlaylist.mpd'
            hls_playlist_url = playlist_urls[1] or f'https://v.redd.it/{video_id}/HLSPlaylist.m3u8'
            qs = traverse_obj(parse_qs(hls_playlist_url), {
                'f': ('f', 0, {lambda x: ','.join([x, 'subsAll']) if x else 'hd,subsAll'}),
            })
            hls_playlist_url = update_url_query(hls_playlist_url, qs)

            formats = [{
                'url': unescapeHTML(reddit_video['fallback_url']),
                'height': int_or_none(reddit_video.get('height')),
                'width': int_or_none(reddit_video.get('width')),
                'tbr': int_or_none(reddit_video.get('bitrate_kbps')),
                'acodec': 'none',
                'vcodec': 'h264',
                'ext': 'mp4',
                'format_id': 'fallback',
                'format_note': 'DASH video, mp4_dash',
            }]
            hls_fmts, subtitles = self._extract_m3u8_formats_and_subtitles(
                hls_playlist_url, display_id, 'mp4', m3u8_id='hls', fatal=False)
            formats.extend(hls_fmts)
            dash_fmts, dash_subs = self._extract_mpd_formats_and_subtitles(
                dash_playlist_url, display_id, mpd_id='dash', fatal=False)
            formats.extend(dash_fmts)
            self._merge_subtitles(dash_subs, target=subtitles)

            return {
                **info,
                'id': video_id,
                'display_id': display_id,
                'formats': formats,
                'subtitles': subtitles or self.extract_subtitles(video_id),
                'duration': int_or_none(reddit_video.get('duration')),
            }

        if parsed_url.netloc == 'v.redd.it':
            self.raise_no_formats('This video is processing', expected=True, video_id=video_id)
            return {
                **info,
                'id': parsed_url.path.split('/')[1],
                'display_id': video_id,
            }

        # Not hosted on reddit, must continue extraction
        return {
            **info,
            'display_id': video_id,
            '_type': 'url_transparent',
            'url': video_url,
        }

# ============================================================================
# Native BaseExtractor wrapper for MyArchive Downloader
# ============================================================================

"""Extractor nativo do Reddit.

Suporta links do reddit.com, old.reddit.com e v.redd.it.
Extrai vídeo DASH e HLS hospedados pelo Reddit com áudio e legendas.
"""

import logging
import re
from typing import ClassVar

from ..exceptions import ExtractionError
from ..models import Container, MediaInfo, MediaType, Protocol, StreamFormat
from .base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

POST_RE = re.compile(
    r"^https?://(?:\w+\.)?reddit(?:media)?\.com/(?:(?:r|user)/[^/]+/)?comments/(?P<id>[^/?#&]+)"
)
VREDDIT_RE = re.compile(r"^https?://(?:v\.|www\.)?redd\.it/(?P<id>[a-zA-Z0-9]+)")


class RedditExtractor(BaseExtractor):
    """Extractor nativo para publicações e vídeos do Reddit."""

    name: ClassVar[str] = "reddit"
    priority: ClassVar[int] = 37
    patterns: ClassVar[list[str]] = [
        r"^https?://(?:\w+\.)?reddit(?:media)?\.com/(?:(?:r|user)/[^/]+/)?comments/(?P<id>[^/?#&]+)",
        r"^https?://v\.redd\.it/(?P<id>[a-zA-Z0-9]+)",
        r"^https?://(?:\w+\.)?reddit\.com/video/(?P<id>[a-zA-Z0-9]+)",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        post_id = self._extract_id(url)
        if not post_id:
            raise ExtractionError(f"Não foi possível extrair o ID do Reddit em {url}")

        api_url = f"https://www.reddit.com/comments/{post_id}.json?raw_json=1"
        headers = ctx.request_headers(referer="https://www.reddit.com/")
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        try:
            payload = await ctx.http.get_json(api_url, headers=headers)
        except Exception as exc:
            # Tenta fallback via HTML
            return await self._extract_html_fallback(url, post_id, ctx)

        if not payload or not isinstance(payload, list):
            return await self._extract_html_fallback(url, post_id, ctx)

        try:
            data = payload[0]["data"]["children"][0]["data"]
        except (IndexError, KeyError, TypeError):
            return await self._extract_html_fallback(url, post_id, ctx)

        # Trata crossposts
        crosspost = data.get("crosspost_parent_list")
        if crosspost and isinstance(crosspost, list) and crosspost[0]:
            data = crosspost[0]

        media = data.get("secure_media") or data.get("media") or {}
        reddit_video = media.get("reddit_video") or {}

        formats: list[StreamFormat] = []

        # 1) HLS master
        hls_url = reddit_video.get("hls_url")
        if hls_url:
            try:
                formats.extend(await self.formats_from_manifest(hls_url, ctx, headers=headers))
            except Exception as exc:  # noqa: BLE001
                log.debug("Falha ao resolver HLS Reddit: %s", exc)

        # 2) DASH manifest
        dash_url = reddit_video.get("dash_url")
        if dash_url:
            try:
                formats.extend(await self.formats_from_manifest(dash_url, ctx, headers=headers))
            except Exception as exc:  # noqa: BLE001
                log.debug("Falha ao resolver DASH Reddit: %s", exc)

        # 3) Fallback MP4 progressivo
        fallback_url = reddit_video.get("fallback_url")
        if fallback_url:
            formats.append(
                StreamFormat(
                    id="reddit-fallback",
                    url=fallback_url,
                    media_type=MediaType.VIDEO,
                    container=Container.MP4,
                    protocol=Protocol.HTTPS,
                    width=reddit_video.get("width"),
                    height=reddit_video.get("height"),
                    duration=float(reddit_video["duration"]) if reddit_video.get("duration") else None,
                )
            )

        if not formats:
            # Se for link para ficheiro directo ou outro player
            video_url = data.get("url")
            if video_url and (video_url.endswith(".mp4") or "v.redd.it" in video_url):
                return await self.formats_from_direct_url(video_url, ctx)
            raise ExtractionError(
                "O post do Reddit não contém um vídeo nativo compatível",
                details={"post_id": post_id},
            )

        title = data.get("title") or f"Reddit {post_id}"
        uploader = data.get("author")
        thumbnail = data.get("thumbnail") if (data.get("thumbnail") or "").startswith("http") else None

        return MediaInfo(
            extractor=self.name,
            webpage_url=f"https://www.reddit.com/comments/{post_id}",
            original_url=url,
            title=title,
            description=data.get("selftext"),
            thumbnail=thumbnail,
            duration=float(reddit_video["duration"]) if reddit_video.get("duration") else None,
            uploader=uploader,
            view_count=data.get("score"),
            age_limit=18 if data.get("over_18") else None,
            formats=formats,
        )

    async def _extract_html_fallback(self, url: str, post_id: str, ctx: ExtractContext) -> MediaInfo:
        """Fallback via scraping simples da página do Reddit."""
        headers = ctx.request_headers(referer="https://www.reddit.com/")
        result = await ctx.http.get(url, headers=headers)
        html = result.text

        m3u8_match = re.search(r'https?://v\.redd\.it/[^"/]+/HLSPlaylist\.m3u8[^\s"\'<>]*', html)
        mp4_match = re.search(r'https?://v\.redd\.it/[^"/]+/DASH_[^\s"\'<>]*\.mp4', html)

        formats: list[StreamFormat] = []
        if m3u8_match:
            try:
                formats.extend(await self.formats_from_manifest(m3u8_match.group(0), ctx))
            except Exception:  # noqa: BLE001
                pass
        if mp4_match:
            formats.append(
                StreamFormat(
                    id="reddit-html-mp4",
                    url=mp4_match.group(0),
                    media_type=MediaType.VIDEO,
                    container=Container.MP4,
                )
            )

        if not formats:
            raise ExtractionError("Não foi possível extrair vídeo do Reddit", details={"id": post_id})

        return MediaInfo(
            extractor=self.name,
            webpage_url=url,
            original_url=url,
            title=f"Reddit {post_id}",
            formats=formats,
        )

    @staticmethod
    def _extract_id(url: str) -> str | None:
        m = POST_RE.search(url)
        if m:
            return m.group("id")
        m = VREDDIT_RE.search(url)
        if m:
            return m.group("id")
        return None
