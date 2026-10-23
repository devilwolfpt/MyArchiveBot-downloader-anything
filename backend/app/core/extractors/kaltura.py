import base64
import contextlib
import json
import re
import urllib.parse

from .common import InfoExtractor
from .utils import (
    ExtractorError,
    clean_html,
    format_field,
    int_or_none,
    remove_start,
    smuggle_url,
    traverse_obj,
    unsmuggle_url,
)


class KalturaIE(InfoExtractor):
    _VALID_URL = r'''(?x)
                (?:
                    kaltura:(?P<partner_id>\w+):(?P<id>\w+)(?::(?P<player_type>\w+))?|
                    https?://
                        (?:(?:www|cdnapi(?:sec)?)\.)?kaltura\.com(?::\d+)?/
                        (?:
                            (?:
                                # flash player
                                index\.php/(?:kwidget|extwidget/preview)|
                                # html5 player
                                html5/html5lib/[^/]+/mwEmbedFrame\.php
                            )
                        )(?:/(?P<path>[^?]+))?(?:\?(?P<query>.*))?
                )
                '''
    _SERVICE_URL = 'http://cdnapi.kaltura.com'
    _SERVICE_BASE = '/api_v3/service/multirequest'
    # See https://github.com/kaltura/server/blob/master/plugins/content/caption/base/lib/model/enums/CaptionType.php
    _CAPTION_TYPES = {
        1: 'srt',
        2: 'ttml',
        3: 'vtt',
    }
    _TESTS = [{
        'url': 'kaltura:269692:1_1jc2y3e4',
        'md5': '3adcbdb3dcc02d647539e53f284ba171',
        'info_dict': {
            'id': '1_1jc2y3e4',
            'ext': 'mp4',
            'title': 'Straight from the Heart',
            'upload_date': '20131219',
            'uploader_id': 'mlundberg@wolfgangsvault.com',
            'description': 'The Allman Brothers Band, 12/16/1981',
            'thumbnail': r're:https?://.+/thumbnail/.+',
            'timestamp': int,
        },
        'skip': 'The access to this service is forbidden since the specified partner is blocked',
    }, {
        'url': 'http://www.kaltura.com/index.php/kwidget/cache_st/1300318621/wid/_269692/uiconf_id/3873291/entry_id/1_1jc2y3e4',
        'only_matching': True,
    }, {
        'url': 'https://cdnapisec.kaltura.com/index.php/kwidget/wid/_557781/uiconf_id/22845202/entry_id/1_plr1syf3',
        'only_matching': True,
    }, {
        'url': 'https://cdnapisec.kaltura.com/html5/html5lib/v2.30.2/mwEmbedFrame.php/p/1337/uiconf_id/20540612/entry_id/1_sf5ovm7u?wid=_243342',
        'only_matching': True,
    }, {
        # video with subtitles
        'url': 'kaltura:111032:1_cw786r8q',
        'only_matching': True,
    }, {
        # video with ttml subtitles (no fileExt)
        'url': 'kaltura:1926081:0_l5ye1133',
        'info_dict': {
            'id': '0_l5ye1133',
            'ext': 'mp4',
            'title': 'What Can You Do With Python?',
            'upload_date': '20160221',
            'uploader_id': 'stork',
            'thumbnail': r're:https?://.+/thumbnail/.+',
            'timestamp': int,
            'subtitles': {
                'en': [{
                    'ext': 'ttml',
                }],
            },
        },
        'skip': 'Gone. Maybe https://www.safaribooksonline.com/library/tutorials/introduction-to-python-anon/3469/',
        'params': {'skip_download': True},
    }, {
        'url': 'https://www.kaltura.com/index.php/extwidget/preview/partner_id/1770401/uiconf_id/37307382/entry_id/0_58u8kme7/embed/iframe?&flashvars[streamerType]=auto',
        'only_matching': True,
    }, {
        'url': 'https://www.kaltura.com:443/index.php/extwidget/preview/partner_id/1770401/uiconf_id/37307382/entry_id/0_58u8kme7/embed/iframe?&flashvars[streamerType]=auto',
        'only_matching': True,
    }, {
        # unavailable source format
        'url': 'kaltura:513551:1_66x4rg7o',
        'only_matching': True,
    }, {
        # html5lib URL using kwidget player
        'url': 'https://cdnapisec.kaltura.com/html5/html5lib/v2.46/mwEmbedFrame.php/p/691292/uiconf_id/20499062/entry_id/0_c076mna6?wid=_691292&iframeembed=true&playerId=kaltura_player_1420508608&entry_id=0_c076mna6&flashvars%5BakamaiHD.loadingPolicy%5D=preInitialize&flashvars%5BakamaiHD.asyncInit%5D=true&flashvars%5BstreamerType%5D=hdnetwork',
        'info_dict': {
            'id': '0_c076mna6',
            'ext': 'mp4',
            'title': 'md5:4883e7acbcbf42583a2dddc97dee4855',
            'duration': 3608,
            'uploader_id': 'commons@swinburne.edu.au',
            'timestamp': 1408086874,
            'view_count': int,
            'upload_date': '20140815',
            'thumbnail': r're:https?://cfvod\.kaltura\.com/.+',
        },
    }, {
        # html5lib playlist URL using kwidget player
        'url': 'https://cdnapisec.kaltura.com/html5/html5lib/v2.89/mwEmbedFrame.php/p/2019031/uiconf_id/40436601?wid=1_4j3m32cv&iframeembed=true&playerId=kaltura_player_&flashvars[playlistAPI.kpl0Id]=1_jovey5nu&flashvars[ks]=&&flashvars[imageDefaultDuration]=30&flashvars[localizationCode]=en&flashvars[leadWithHTML5]=true&flashvars[forceMobileHTML5]=true&flashvars[nextPrevBtn.plugin]=true&flashvars[hotspots.plugin]=true&flashvars[sideBarContainer.plugin]=true&flashvars[sideBarContainer.position]=left&flashvars[sideBarContainer.clickToClose]=true&flashvars[chapters.plugin]=true&flashvars[chapters.layout]=vertical&flashvars[chapters.thumbnailRotator]=false&flashvars[streamSelector.plugin]=true&flashvars[EmbedPlayer.SpinnerTarget]=videoHolder&flashvars[dualScreen.plugin]=true&flashvars[playlistAPI.playlistUrl]=https://canvasgatechtest.kaf.kaltura.com/playlist/details/{playlistAPI.kpl0Id}/categoryid/126428551',
        'info_dict': {
            'id': '1_jovey5nu',
            'title': '00-00 Introduction',
        },
        'playlist': [
            {
                'info_dict': {
                    'id': '1_b1y5hlvx',
                    'ext': 'mp4',
                    'title': 'CS7646_00-00 Introductio_Introduction',
                    'duration': 91,
                    'thumbnail': r're:https?://cfvod\.kaltura\.com/.+',
                    'view_count': int,
                    'timestamp': 1533154447,
                    'upload_date': '20180801',
                    'uploader_id': 'djoyner3',
                },
            }, {
                'info_dict': {
                    'id': '1_jfb7mdpn',
                    'ext': 'mp4',
                    'title': 'CS7646_00-00 Introductio_Three parts to the course',
                    'duration': 63,
                    'thumbnail': r're:https?://cfvod\.kaltura\.com/.+',
                    'view_count': int,
                    'timestamp': 1533154489,
                    'upload_date': '20180801',
                    'uploader_id': 'djoyner3',
                },
            }, {
                'info_dict': {
                    'id': '1_8xflxdp7',
                    'ext': 'mp4',
                    'title': 'CS7646_00-00 Introductio_Textbooks',
                    'duration': 37,
                    'thumbnail': r're:https?://cfvod\.kaltura\.com/.+',
                    'view_count': int,
                    'timestamp': 1533154512,
                    'upload_date': '20180801',
                    'uploader_id': 'djoyner3',
                },
            }, {
                'info_dict': {
                    'id': '1_3hqew8kn',
                    'ext': 'mp4',
                    'title': 'CS7646_00-00 Introductio_Prerequisites',
                    'duration': 49,
                    'thumbnail': r're:https?://cfvod\.kaltura\.com/.+',
                    'view_count': int,
                    'timestamp': 1533154536,
                    'upload_date': '20180801',
                    'uploader_id': 'djoyner3',
                },
            },
        ],
    }]
    _WEBPAGE_TESTS = [{
        'url': 'https://www.cornell.edu/VIDEO/nima-arkani-hamed-standard-models-of-particle-physics',
        'info_dict': {
            'id': '1_sgtvehim',
            'ext': 'mp4',
            'title': 'Our "Standard Models" of particle physics and cosmology',
            'duration': 5420,
            'thumbnail': r're:https?://cdnsecakmi\.kaltura\.com/.+',
            'timestamp': 1321158993,
            'upload_date': '20111113',
            'uploader_id': 'kps1',
            'view_count': int,
        },
    }, {
        'url': 'https://www.oreilly.com/ideas/my-cloud-makes-pretty-pictures',
        'info_dict': {
            'id': '0_utuok90b',
            'ext': 'mp4',
            'title': '06_matthew_brender_raj_dutt',
            'duration': 331,
            'thumbnail': r're:https?://cfvod\.kaltura\.com/.+',
            'timestamp': 1466638791,
            'upload_date': '20160622',
            'uploader_id': '',
            'view_count': int,
        },
    }, {
        'url': 'https://fod.infobase.com/p_ViewPlaylist.aspx?AssignmentID=NUN8ZY',
        'info_dict': {
            'id': '0_izeg5utt',
            'ext': 'mp4',
            'title': '35871',
            'duration': 3403,
            'thumbnail': r're:https?://cfvod\.kaltura\.com/.+',
            'timestamp': 1355743100,
            'upload_date': '20121217',
            'uploader_id': 'cplapp@learn360.com',
            'view_count': int,
        },
    }, {
        'url': 'https://www.cns.nyu.edu/~eero/math-tools17/Videos/lecture-05sep2017.html',
        'info_dict': {
            'id': '1_9gzouybz',
            'ext': 'mp4',
            'title': 'lecture-05sep2017',
            'duration': 7219,
            'thumbnail': r're:https?://cfvod\.kaltura\.com/.+',
            'timestamp': 1505340777,
            'upload_date': '20170913',
            'uploader_id': 'eps2',
            'view_count': int,
        },
    }]

    @classmethod
    def _extract_embed_urls(cls, url, webpage):
        # Embed codes: https://knowledge.kaltura.com/embedding-kaltura-media-players-your-site
        finditer = (
            list(re.finditer(
                r'''(?xs)
                    kWidget\.(?:thumb)?[Ee]mbed\(
                    \{.*?
                        (?P<q1>['"])wid(?P=q1)\s*:\s*
                        (?P<q2>['"])_?(?P<partner_id>(?:(?!(?P=q2)).)+)(?P=q2),.*?
                        (?P<q3>['"])entry_?[Ii]d(?P=q3)\s*:\s*
                        (?P<q4>['"])(?P<id>(?:(?!(?P=q4)).)+)(?P=q4)(?:,|\s*\})
                ''', webpage))
            or list(re.finditer(
                r'''(?xs)
                    (?P<q1>["'])
                        (?:https?:)?//cdnapi(?:sec)?\.kaltura\.com(?::\d+)?/(?:(?!(?P=q1)).)*\b(?:p|partner_id)/(?P<partner_id>\d+)(?:(?!(?P=q1)).)*
                    (?P=q1).*?
                    (?:
                        (?:
                            entry_?[Ii]d|
                            (?P<q2>["'])entry_?[Ii]d(?P=q2)
                        )\s*:\s*|
                        \[\s*(?P<q2_1>["'])entry_?[Ii]d(?P=q2_1)\s*\]\s*=\s*
                    )
                    (?P<q3>["'])(?P<id>(?:(?!(?P=q3)).)+)(?P=q3)
                ''', webpage))
            or list(re.finditer(
                r'''(?xs)
                    <(?:iframe[^>]+src|meta[^>]+\bcontent)=(?P<q1>["'])\s*
                      (?:https?:)?//(?:(?:www|cdnapi(?:sec)?)\.)?kaltura\.com/(?:(?!(?P=q1)).)*\b(?:p|partner_id)/(?P<partner_id>\d+)
                      (?:(?!(?P=q1)).)*
                      [?&;]entry_id=(?P<id>(?:(?!(?P=q1))[^&])+)
                      (?:(?!(?P=q1)).)*
                    (?P=q1)
                ''', webpage))
        )
        urls = []
        for mobj in finditer:
            embed_info = mobj.groupdict()
            for k, v in embed_info.items():
                if v:
                    embed_info[k] = v.strip()
            embed_url = 'kaltura:{partner_id}:{id}'.format(**embed_info)
            escaped_pid = re.escape(embed_info['partner_id'])
            service_mobj = re.search(
                rf'<script[^>]+src=(["\'])(?P<id>(?:https?:)?//(?:(?!\1).)+)/p/{escaped_pid}/sp/{escaped_pid}00/embedIframeJs',
                webpage)
            if service_mobj:
                embed_url = smuggle_url(embed_url, {'service_url': service_mobj.group('id')})
            urls.append(embed_url)
        return urls

    def _kaltura_api_call(self, video_id, actions, service_url=None, **kwargs):
        params = actions[0]
        params.update(dict(enumerate(actions[1:], start=1)))

        data = self._download_json(
            (service_url or self._SERVICE_URL) + self._SERVICE_BASE,
            video_id, data=json.dumps(params).encode(),
            headers={
                'Content-Type': 'application/json',
                'Accept-Encoding': 'gzip, deflate, br',
            }, **kwargs)

        for idx, status in enumerate(data):
            if not isinstance(status, dict):
                continue
            if status.get('objectType') == 'KalturaAPIException':
                raise ExtractorError(
                    '{} said: {} ({})'.format(self.IE_NAME, status['message'], idx))

        data[1] = traverse_obj(data, (1, 'objects', 0))

        return data

    def _get_video_info(self, video_id, partner_id, service_url=None, player_type='html5'):
        assert player_type in ('html5', 'kwidget')
        if player_type == 'kwidget':
            return self._get_video_info_kwidget(video_id, partner_id, service_url)

        return self._get_video_info_html5(video_id, partner_id, service_url)

    def _get_video_info_html5(self, video_id, partner_id, service_url=None):
        actions = [
            {
                'apiVersion': '3.3.0',
                'clientTag': 'html5:v3.1.0',
                'format': 1,  # JSON, 2 = XML, 3 = PHP
                'ks': '',
                'partnerId': partner_id,
            },
            {
                'expiry': 86400,
                'service': 'session',
                'action': 'startWidgetSession',
                'widgetId': self._build_widget_id(partner_id),
            },
            # info
            {
                'action': 'list',
                'filter': {'redirectFromEntryId': video_id},
                'service': 'baseentry',
                'ks': '{1:result:ks}',
                'responseProfile': {
                    'type': 1,
                    'fields': 'createdAt,dataUrl,duration,name,plays,thumbnailUrl,userId',
                },
            },
            # flavor_assets
            {
                'action': 'getbyentryid',
                'entryId': video_id,
                'service': 'flavorAsset',
                'ks': '{1:result:ks}',
            },
            # captions
            {
                'action': 'list',
                'filter:entryIdEqual': video_id,
                'service': 'caption_captionasset',
                'ks': '{1:result:ks}',
            },
        ]
        return self._kaltura_api_call(
            video_id, actions, service_url, note='Downloading video info JSON (Kaltura html5 player)')

    def _get_video_info_kwidget(self, video_id, partner_id, service_url=None):
        actions = [
            {
                'service': 'multirequest',
                'apiVersion': '3.1',
                'expiry': 86400,
                'clientTag': 'kwidget:v2.89',
                'format': 1,  # JSON, 2 = XML, 3 = PHP
                'ignoreNull': 1,
                'action': 'null',
            },
            # header
            {
                'expiry': 86400,
                'service': 'session',
                'action': 'startWidgetSession',
                'widgetId': self._build_widget_id(partner_id),
            },
            # (empty)
            {
                'expiry': 86400,
                'service': 'session',
                'action': 'startwidgetsession',
                'widgetId': self._build_widget_id(partner_id),
                'format': 9,
                'apiVersion': '3.1',
                'clientTag': 'kwidget:v2.89',
                'ignoreNull': 1,
                'ks': '{1:result:ks}',
            },
            # info
            {
                'action': 'list',
                'filter': {'redirectFromEntryId': video_id},
                'service': 'baseentry',
                'ks': '{1:result:ks}',
                'responseProfile': {
                    'type': 1,
                    'fields': 'createdAt,dataUrl,duration,name,plays,thumbnailUrl,userId',
                },
            },
            # flavor_assets
            {
                'action': 'getbyentryid',
                'entryId': video_id,
                'service': 'flavorAsset',
                'ks': '{1:result:ks}',
            },
            # captions
            {
                'action': 'list',
                'filter:entryIdEqual': video_id,
                'service': 'caption_captionasset',
                'ks': '{1:result:ks}',
            },
        ]
        # second object (representing the second start widget session) is None
        header, _, _info, flavor_assets, captions = self._kaltura_api_call(
            video_id, actions, service_url, note='Downloading video info JSON (Kaltura kwidget player)')
        info = _info['objects'][0]
        return header, info, flavor_assets, captions

    def _build_widget_id(self, partner_id):
        return partner_id if '_' in partner_id else f'_{partner_id}'

    IFRAME_PACKAGE_DATA_REGEX = r'window\.kalturaIframePackageData\s*='

    def _real_extract(self, url):
        url, smuggled_data = unsmuggle_url(url, {})

        mobj = self._match_valid_url(url)
        partner_id, entry_id, player_type = mobj.group('partner_id', 'id', 'player_type')
        ks, captions = None, None
        if not player_type:
            player_type = 'kwidget' if 'html5lib/v2' in url else 'html5'
        if partner_id and entry_id:
            _, info, flavor_assets, captions = self._get_video_info(entry_id, partner_id, smuggled_data.get('service_url'), player_type=player_type)
        else:
            path, query = mobj.group('path', 'query')
            if not path and not query:
                raise ExtractorError('Invalid URL', expected=True)
            params = {}
            if query:
                params = urllib.parse.parse_qs(query)
            if path:
                splitted_path = path.split('/')
                params.update(dict(zip(splitted_path[::2], [[v] for v in splitted_path[1::2]])))  # noqa: B905
            if 'wid' in params:
                partner_id = remove_start(params['wid'][0], '_')
            elif 'p' in params:
                partner_id = params['p'][0]
            elif 'partner_id' in params:
                partner_id = params['partner_id'][0]
            else:
                raise ExtractorError('Invalid URL', expected=True)
            if 'entry_id' in params:
                entry_id = params['entry_id'][0]
                _, info, flavor_assets, captions = self._get_video_info(entry_id, partner_id, player_type=player_type)
            elif 'uiconf_id' in params and 'flashvars[referenceId]' in params:
                reference_id = params['flashvars[referenceId]'][0]
                webpage = self._download_webpage(url, reference_id)
                entry_data = self._search_json(
                    self.IFRAME_PACKAGE_DATA_REGEX, webpage,
                    'kalturaIframePackageData', reference_id)['entryResult']
                info, flavor_assets = entry_data['meta'], entry_data['contextData']['flavorAssets']
                entry_id = info['id']
                # Unfortunately, data returned in kalturaIframePackageData lacks
                # captions so we will try requesting the complete data using
                # regular approach since we now know the entry_id
                # Even if this fails we already have everything extracted
                # apart from captions and can process at least with this
                with contextlib.suppress(ExtractorError):
                    _, info, flavor_assets, captions = self._get_video_info(
                        entry_id, partner_id, player_type=player_type)
            elif 'uiconf_id' in params and 'flashvars[playlistAPI.kpl0Id]' in params:
                playlist_id = params['flashvars[playlistAPI.kpl0Id]'][0]
                webpage = self._download_webpage(url, playlist_id)
                playlist_data = self._search_json(
                    self.IFRAME_PACKAGE_DATA_REGEX, webpage,
                    'kalturaIframePackageData', playlist_id)['playlistResult']
                return self.playlist_from_matches(
                    traverse_obj(playlist_data, (playlist_id, 'items', ..., 'id')),
                    playlist_id, traverse_obj(playlist_data, (playlist_id, 'name')),
                    ie=KalturaIE, getter=lambda x: f'kaltura:{partner_id}:{x}:{player_type}')
            else:
                raise ExtractorError('Invalid URL', expected=True)
            ks = params.get('flashvars[ks]', [None])[0]

        return self._per_video_extract(smuggled_data, entry_id, info, ks, flavor_assets, captions)

    def _per_video_extract(self, smuggled_data, entry_id, info, ks, flavor_assets, captions):
        source_url = smuggled_data.get('source_url')
        if source_url:
            referrer = base64.b64encode(
                '://'.join(urllib.parse.urlparse(source_url)[:2])
                .encode()).decode('utf-8')
        else:
            referrer = None

        def sign_url(unsigned_url):
            if ks:
                unsigned_url += f'/ks/{ks}'
            if referrer:
                unsigned_url += f'?referrer={referrer}'
            return unsigned_url

        data_url = info['dataUrl']
        if '/flvclipper/' in data_url:
            data_url = re.sub(r'/flvclipper/.*', '/serveFlavor', data_url)

        formats = []
        subtitles = {}
        for f in flavor_assets:
            # Continue if asset is not ready
            if f.get('status') != 2:
                continue
            # Original format that's not available (e.g. kaltura:1926081:0_c03e1b5g)
            # skip for now.
            if f.get('fileExt') == 'chun':
                continue
            # DRM-protected video, cannot be decrypted
            if not self.get_param('allow_unplayable_formats') and f.get('fileExt') == 'wvm':
                continue
            if not f.get('fileExt'):
                # QT indicates QuickTime; some videos have broken fileExt
                if f.get('containerFormat') == 'qt':
                    f['fileExt'] = 'mov'
                else:
                    f['fileExt'] = 'mp4'
            video_url = sign_url(
                '{}/flavorId/{}'.format(data_url, f['id']))
            format_id = '{fileExt}-{bitrate}'.format(**f)
            # Source format may not be available (e.g. kaltura:513551:1_66x4rg7o)
            if f.get('isOriginal') is True and not self._is_valid_url(
                    video_url, entry_id, format_id):
                continue
            # audio-only has no videoCodecId (e.g. kaltura:1926081:0_c03e1b5g
            # -f mp4-56)
            vcodec = 'none' if 'videoCodecId' not in f and f.get(
                'frameRate') == 0 else f.get('videoCodecId')
            formats.append({
                'format_id': format_id,
                'ext': f.get('fileExt'),
                'tbr': int_or_none(f['bitrate']),
                'fps': int_or_none(f.get('frameRate')),
                'filesize_approx': int_or_none(f.get('size'), invscale=1024),
                'container': f.get('containerFormat'),
                'vcodec': vcodec,
                'height': int_or_none(f.get('height')),
                'width': int_or_none(f.get('width')),
                'url': video_url,
            })
        if '/playManifest/' in data_url:
            m3u8_url = sign_url(data_url.replace(
                'format/url', 'format/applehttp'))
            fmts, subs = self._extract_m3u8_formats_and_subtitles(
                m3u8_url, entry_id, 'mp4', 'm3u8_native',
                m3u8_id='hls', fatal=False)
            formats.extend(fmts)
            self._merge_subtitles(subs, target=subtitles)

        if captions:
            for caption in captions.get('objects', []):
                # Continue if caption is not ready
                if caption.get('status') != 2:
                    continue
                if not caption.get('id'):
                    continue
                caption_format = int_or_none(caption.get('format'))
                subtitles.setdefault(caption.get('languageCode') or caption.get('language'), []).append({
                    'url': '{}/api_v3/service/caption_captionasset/action/serve/captionAssetId/{}'.format(self._SERVICE_URL, caption['id']),
                    'ext': caption.get('fileExt') or self._CAPTION_TYPES.get(caption_format) or 'ttml',
                })

        return {
            'id': entry_id,
            'title': info['name'],
            'formats': formats,
            'subtitles': subtitles,
            'description': clean_html(info.get('description')),
            'thumbnail': info.get('thumbnailUrl'),
            'duration': info.get('duration'),
            'timestamp': info.get('createdAt'),
            'uploader_id': format_field(info, 'userId', ignore=('None', None)),
            'view_count': int_or_none(info.get('plays')),
        }

# ============================================================================
# Native BaseExtractor wrapper for MyArchive Downloader
# ============================================================================

"""Extractor para vídeos alojados no Kaltura."""

import logging
import re
from urllib.parse import parse_qs, quote, urlsplit

from ..exceptions import DownloaderError, ExtractionError
from ..models import Container, MediaInfo, MediaType, Protocol, StreamFormat, SubtitleTrack
from .base import BaseExtractor, ExtractContext

log = logging.getLogger(__name__)

KALTURA_PATH_RE = re.compile(
    r"/(?:index\.php/(?:kwidget|extwidget/preview)|html5/html5lib/[^/]+/mwEmbedFrame\.php)"
    r"(?:/(?P<path>[^?#]*))?",
    re.IGNORECASE,
)
CONTAINERS: dict[str, Container] = {
    "mp4": Container.MP4,
    "m4v": Container.MP4,
    "webm": Container.WEBM,
    "mov": Container.MOV,
    "mkv": Container.MKV,
    "flv": Container.FLV,
    "mp3": Container.MP3,
    "m4a": Container.M4A,
    "aac": Container.M4A,
    "ogg": Container.OGG,
    "wav": Container.WAV,
}


class KalturaExtractor(BaseExtractor):
    name = "kaltura"
    priority = 23
    patterns = [
        r"^https?://(?:(?:www|cdnapi(?:sec)?)\.)?kaltura\.com(?::\d+)?/"
        r"(?:index\.php/(?:kwidget|extwidget/preview)|html5/html5lib/[^/]+/mwEmbedFrame\.php)",
    ]

    async def extract(self, url: str, ctx: ExtractContext) -> MediaInfo:
        partner_id, entry_id, session_key = self._parse_url(url)
        if not partner_id or not entry_id:
            raise ExtractionError(
                "Não foi possível identificar o partner ID e entry ID Kaltura",
                details={"url": url},
            )

        api_url = "https://cdnapisec.kaltura.com/api_v3/service/multirequest"
        payload = self._build_multirequest(partner_id, entry_id)
        result = await ctx.http.post_json(
            api_url,
            payload,
            headers={
                **ctx.request_headers(referer=url),
                "Content-Type": "application/json",
            },
        )
        info, flavor_assets, captions = self._unpack_response(result)
        if not isinstance(info, dict):
            raise ExtractionError(
                "A API Kaltura não devolveu metadados do vídeo",
                details={"entry_id": entry_id},
            )

        formats = await self._build_formats(
            info, flavor_assets, session_key, ctx, url, entry_id
        )
        subtitles = self._build_subtitles(captions)
        if not formats:
            raise ExtractionError(
                "Kaltura não devolveu formatos media disponíveis",
                details={"entry_id": entry_id},
            )
        duration = self._number(info.get("duration"))
        return MediaInfo(
            extractor=self.name,
            webpage_url=url,
            original_url=url,
            title=str(info.get("name") or entry_id),
            description=info.get("description"),
            thumbnail=info.get("thumbnailUrl"),
            duration=duration,
            uploader_id=str(info["userId"]) if info.get("userId") else None,
            view_count=self._positive_int(info.get("plays")),
            formats=formats,
            subtitles=subtitles,
        )

    @staticmethod
    def _parse_url(url: str) -> tuple[str | None, str | None, str | None]:
        parts = urlsplit(url)
        path_match = KALTURA_PATH_RE.search(parts.path)
        if not path_match:
            return None, None, None
        params = parse_qs(parts.query, keep_blank_values=True)
        path_values: dict[str, str] = {}
        path = path_match.group("path") or ""
        segments = path.strip("/").split("/") if path else []
        for key, value in zip(segments[::2], segments[1::2]):
            path_values[key] = value

        partner_id = (
            (params.get("wid") or [None])[0]
            or (params.get("partner_id") or [None])[0]
            or (params.get("p") or [None])[0]
            or path_values.get("partner_id")
            or path_values.get("p")
        )
        if partner_id:
            partner_id = partner_id.removeprefix("_")
        entry_id = (params.get("entry_id") or [None])[0] or path_values.get("entry_id")
        session_key = (params.get("flashvars[ks]") or [None])[0]
        return partner_id, entry_id, session_key

    @staticmethod
    def _build_multirequest(partner_id: str, entry_id: str) -> dict[str, object]:
        widget_id = partner_id if "_" in partner_id else f"_{partner_id}"
        actions: list[dict[str, object]] = [
            {
                "apiVersion": "3.3.0",
                "clientTag": "html5:v3.1.0",
                "format": 1,
                "ks": "",
                "partnerId": partner_id,
            },
            {
                "expiry": 86400,
                "service": "session",
                "action": "startWidgetSession",
                "widgetId": widget_id,
            },
            {
                "action": "list",
                "filter": {"redirectFromEntryId": entry_id},
                "service": "baseentry",
                "ks": "{1:result:ks}",
                "responseProfile": {
                    "type": 1,
                    "fields": "createdAt,dataUrl,duration,name,plays,thumbnailUrl,userId,description",
                },
            },
            {
                "action": "getbyentryid",
                "entryId": entry_id,
                "service": "flavorAsset",
                "ks": "{1:result:ks}",
            },
            {
                "action": "list",
                "filter:entryIdEqual": entry_id,
                "service": "caption_captionasset",
                "ks": "{1:result:ks}",
            },
        ]
        request: dict[str, object] = actions[0].copy()
        request.update({str(index): action for index, action in enumerate(actions[1:], 1)})
        return request

    @staticmethod
    def _unpack_response(response: object) -> tuple[dict | None, list, dict | None]:
        if not isinstance(response, list) or len(response) < 4:
            raise ExtractionError("A API Kaltura devolveu uma resposta incompleta")
        for item in response:
            if isinstance(item, dict) and item.get("objectType") == "KalturaAPIException":
                raise ExtractionError(
                    f"Kaltura: {item.get('message') or 'erro na API'}",
                    details={"code": item.get("code")},
                )
        raw_info = response[1]
        if isinstance(raw_info, dict) and isinstance(raw_info.get("objects"), list):
            info = next((obj for obj in raw_info["objects"] if isinstance(obj, dict)), None)
        else:
            info = raw_info if isinstance(raw_info, dict) else None
        raw_assets = response[2]
        assets = raw_assets if isinstance(raw_assets, list) else []
        raw_captions = response[3]
        captions = raw_captions if isinstance(raw_captions, dict) else None
        return info, assets, captions

    async def _build_formats(
        self,
        info: dict,
        assets: list,
        session_key: str | None,
        ctx: ExtractContext,
        referer: str,
        entry_id: str,
    ) -> list[StreamFormat]:
        data_url = info.get("dataUrl")
        if not isinstance(data_url, str) or not data_url:
            return []
        if "/flvclipper/" in data_url:
            data_url = re.sub(r"/flvclipper/.*", "/serveFlavor", data_url)
        data_url = data_url.replace("http://", "https://", 1)
        base_url = f"{data_url}/ks/{quote(session_key, safe='')}" if session_key else data_url
        formats: list[StreamFormat] = []
        saw_drm = False

        for asset in assets:
            if not isinstance(asset, dict) or asset.get("status") != 2:
                continue
            extension = str(asset.get("fileExt") or "").lower()
            if extension == "wvm":
                saw_drm = True
                continue
            if extension == "chun":
                continue
            extension = extension or ("mov" if asset.get("containerFormat") == "qt" else "mp4")
            asset_id = asset.get("id")
            if not asset_id:
                continue
            stream_url = f"{base_url}/flavorId/{quote(str(asset_id), safe='')}"
            is_audio = (
                "videoCodecId" not in asset
                and self._number(asset.get("frameRate")) == 0
            )
            bitrate = self._number(asset.get("bitrate"))
            size_kib = self._number(asset.get("size"))
            formats.append(
                StreamFormat(
                    id=f"kaltura-{extension}-{asset.get('bitrate') or asset_id}",
                    url=stream_url,
                    media_type=MediaType.AUDIO if is_audio else MediaType.VIDEO,
                    container=CONTAINERS.get(extension, Container.UNKNOWN),
                    protocol=Protocol.HTTPS,
                    width=self._positive_int(asset.get("width")),
                    height=self._positive_int(asset.get("height")),
                    fps=self._number(asset.get("frameRate")),
                    tbr=bitrate,
                    filesize_approx=int(size_kib * 1024) if size_kib else None,
                    vcodec="none" if is_audio else (
                        str(asset["videoCodecId"]) if asset.get("videoCodecId") else None
                    ),
                    extractor_data={"asset_id": str(asset_id)},
                )
            )

        if "/playManifest/" in data_url:
            manifest_url = data_url.replace("format/url", "format/applehttp")
            if session_key:
                manifest_url = f"{manifest_url}/ks/{quote(session_key, safe='')}"
            try:
                resolved = await self.formats_from_manifest(
                    manifest_url,
                    ctx,
                    headers=ctx.request_headers(referer=referer),
                )
                formats.extend(resolved)
                if not resolved:
                    formats.append(
                        StreamFormat(
                            id="kaltura-hls",
                            url=manifest_url,
                            media_type=MediaType.VIDEO,
                            container=Container.TS,
                            protocol=Protocol.HLS,
                            manifest_url=manifest_url,
                            manifest_kind="hls",
                        )
                    )
            except DownloaderError as exc:
                log.warning("Não foi possível resolver HLS Kaltura para %s: %s", entry_id, exc)
                formats.append(
                    StreamFormat(
                        id="kaltura-hls",
                        url=manifest_url,
                        media_type=MediaType.VIDEO,
                        container=Container.TS,
                        protocol=Protocol.HLS,
                        manifest_url=manifest_url,
                        manifest_kind="hls",
                    )
                )
        if not formats and saw_drm:
            raise ExtractionError(
                "O vídeo Kaltura só disponibiliza assets protegidos por DRM, que não é suportado",
                details={"entry_id": entry_id},
            )
        return formats

    @staticmethod
    def _build_subtitles(captions: dict | None) -> dict[str, list[SubtitleTrack]]:
        tracks: dict[str, list[SubtitleTrack]] = {}
        objects = captions.get("objects", []) if captions else []
        if not isinstance(objects, list):
            return tracks
        extensions = {1: "srt", 2: "ttml", 3: "vtt"}
        for caption in objects:
            if not isinstance(caption, dict) or caption.get("status") != 2 or not caption.get("id"):
                continue
            language = str(caption.get("languageCode") or caption.get("language") or "und")
            extension = caption.get("fileExt") or extensions.get(
                KalturaExtractor._positive_int(caption.get("format")) or 0,
                "ttml",
            )
            tracks.setdefault(language, []).append(
                SubtitleTrack(
                    language=language,
                    ext=str(extension),
                    url=(
                        "https://cdnapisec.kaltura.com/api_v3/service/"
                        "caption_captionasset/action/serve/captionAssetId/"
                        f"{quote(str(caption['id']), safe='')}"
                    ),
                )
            )
        return tracks

    @staticmethod
    def _number(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        try:
            return float(value)
        except ValueError:
            return None

    @classmethod
    def _positive_int(cls, value: object) -> int | None:
        number = cls._number(value)
        return int(number) if number is not None and number > 0 else None
