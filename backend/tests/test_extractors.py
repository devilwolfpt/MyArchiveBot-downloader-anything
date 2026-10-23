"""Testes do routing de extractores e da selecção de formatos."""
from __future__ import annotations

import json

import httpx
import pytest

from app.core.exceptions import ExtractionError, UnsupportedUrlError
from app.core.extractors import registry
from app.core.extractors.base import BaseExtractor, ExtractContext
from app.core.extractors.brightcove import BrightcoveExtractor
from app.core.extractors.generic import GenericHTMLExtractor
from app.core.extractors.kaltura import KalturaExtractor
from app.core.extractors.youtube import INNERTUBE_CLIENTS, YouTubeExtractor
from tests.conftest import run
from app.core.http.client import HttpClient, is_private_host
from app.core.models import DownloadMode, MediaInfo, MediaType, StreamFormat
from app.services.pipeline import DownloadService


class TestRegistry:
    def test_routes_youtube(self) -> None:
        extractor = registry.resolve("https://www.youtube.com/watch?v=abc123")
        assert extractor.name == "youtube"

    def test_routes_short_youtube(self) -> None:
        assert registry.resolve("https://youtu.be/abc123").name == "youtube"

    def test_routes_vimeo(self) -> None:
        assert registry.resolve("https://vimeo.com/123456").name == "vimeo"

    def test_routes_francetv(self) -> None:
        assert registry.resolve("https://www.francetv.fr/france-3/x/y/123.html").name == "francetv"

    def test_routes_dailymotion(self) -> None:
        assert registry.resolve("https://www.dailymotion.com/video/x123").name == "dailymotion"

    def test_routes_jwplatform_player(self) -> None:
        assert (
            registry.resolve("https://cdn.jwplayer.com/players/nPripu9l-ALJ3XQCI.js").name
            == "jwplatform"
        )

    def test_routes_brightcove_player(self) -> None:
        assert (
            registry.resolve(
                "https://players.brightcove.net/123/player_default/index.html?videoId=abc"
            ).name
            == "brightcove"
        )

    def test_routes_kaltura_embed(self) -> None:
        assert (
            registry.resolve(
                "https://cdnapisec.kaltura.com/index.php/extwidget/preview/"
                "partner_id/111/uiconf_id/2/entry_id/1_abc/embed/iframe"
            ).name
            == "kaltura"
        )

    def test_routes_direct_mp4(self) -> None:
        assert registry.resolve("https://cdn.test/file.mp4").name == "direct"

    def test_routes_direct_m3u8(self) -> None:
        assert registry.resolve("https://cdn.test/master.m3u8").name == "direct"

    def test_falls_back_to_catalog(self) -> None:
        # catalog é agora o catch_all universal (prioridade 500, antes do generic em 1000)
        # para qualquer URL sem extractor nativo dedicado
        assert registry.resolve("https://qualquer-site.test/uma/pagina").name == "catalog"

    def test_get_unknown_extractor(self) -> None:
        assert registry.get("nao-existe") is None

    def test_all_extractors_registered(self) -> None:
        names = {e.name for e in registry.all()}
        assert {
            "youtube", "vimeo", "francetv", "dailymotion", "jwplatform", "brightcove",
            "kaltura", "direct", "generic"
        } <= names


class TestBrightcoveExtractor:
    def test_extracts_playback_api_sources_and_captions(self, settings) -> None:
        url = "https://players.brightcove.net/123/player_default/index.html?videoId=abc123"
        config_url = "https://players.brightcove.net/123/player_default/config.json"
        api_url = (
            "https://edge.api.brightcove.com/playback/v1/accounts/123/videos/abc123"
        )
        responses = {
            config_url: '{"video_cloud":{"policy_key":"public-policy"}}',
            api_url: """
                {
                  "id": "abc123",
                  "name": "Brightcove sample",
                  "description": "Description",
                  "duration": 1250,
                  "poster": "https://cdn.example/poster.jpg",
                  "sources": [{
                    "src": "https://cdn.example/video.mp4",
                    "type": "video/mp4",
                    "width": 1280,
                    "height": 720,
                    "avg_bitrate": 1250000,
                    "codec": "h264"
                  }],
                  "text_tracks": [{
                    "kind": "captions",
                    "src": "https://cdn.example/en.vtt",
                    "srclang": "en",
                    "label": "English"
                  }]
                }
            """,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            body = responses.get(str(request.url))
            return httpx.Response(
                200 if body is not None else 404,
                text=body or "",
                request=request,
            )

        async def scenario():
            async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
                return await BrightcoveExtractor().extract(
                    url, ExtractContext(http, settings, referer="https://publisher.example/")
                )

        info = run(scenario())
        assert info.title == "Brightcove sample"
        assert info.duration == 1.25
        assert info.formats[0].url == "https://cdn.example/video.mp4"
        assert info.formats[0].height == 720
        assert info.formats[0].tbr == 1250
        assert info.subtitles["en"][0].url == "https://cdn.example/en.vtt"

    def test_extracts_brightcove_iframe_from_generic_page(self, settings) -> None:
        page_url = "https://publisher.example/watch"
        player_url = (
            "https://players.brightcove.net/123/player_default/index.html?videoId=abc123"
        )
        responses = {
            page_url: f'<iframe src="{player_url}"></iframe>',
            "https://players.brightcove.net/123/player_default/config.json":
                '{"video_cloud":{"policy_key":"public-policy"}}',
            "https://edge.api.brightcove.com/playback/v1/accounts/123/videos/abc123": """
                {"id":"abc123","name":"Embedded BC","sources":[
                  {"src":"https://cdn.example/video.mp4","type":"video/mp4","height":360}
                ]}
            """,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            body = responses.get(str(request.url))
            return httpx.Response(
                200 if body is not None else 404,
                text=body or "",
                request=request,
            )

        async def scenario():
            async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
                return await GenericHTMLExtractor().extract(
                    page_url, ExtractContext(http, settings, referer=page_url)
                )

        info = run(scenario())
        assert info.extractor == "brightcove"
        assert info.title == "Embedded BC"
        assert info.formats[0].height == 360


class TestKalturaExtractor:
    def test_extracts_multirequest_flavors_and_captions(self, settings) -> None:
        url = (
            "https://cdnapisec.kaltura.com/index.php/extwidget/preview/"
            "partner_id/111/uiconf_id/2/entry_id/1_abc/embed/iframe?wid=_111"
        )
        api_url = "https://cdnapisec.kaltura.com/api_v3/service/multirequest"
        response_data = [
            {"ks": "session-token"},
            {
                "objects": [{
                    "id": "1_abc",
                    "name": "Kaltura sample",
                    "description": "Description",
                    "thumbnailUrl": "https://cdn.example/thumb.jpg",
                    "duration": 90,
                    "dataUrl": "https://cdnapisec.kaltura.com/p/111/sp/11100/serveFlavor",
                    "userId": "uploader",
                    "plays": 12,
                }]
            },
            [{
                "status": 2,
                "id": "asset-720",
                "fileExt": "mp4",
                "bitrate": 1400,
                "frameRate": 30,
                "width": 1280,
                "height": 720,
                "videoCodecId": "h264",
                "size": 1024,
            }],
            {"objects": [{
                "status": 2,
                "id": "caption-1",
                "languageCode": "en",
                "format": 3,
            }]},
        ]
        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json=response_data,
                request=request,
            ) if str(request.url) == api_url else httpx.Response(404, request=request)

        async def scenario():
            async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
                return await KalturaExtractor().extract(
                    url, ExtractContext(http, settings, referer=url)
                )

        info = run(scenario())
        assert info.title == "Kaltura sample"
        assert info.duration == 90
        assert info.formats[0].url.endswith("/flavorId/asset-720")
        assert info.formats[0].height == 720
        assert info.formats[0].filesize_approx == 1024 * 1024
        assert info.subtitles["en"][0].ext == "vtt"
        assert info.subtitles["en"][0].url.endswith("captionAssetId/caption-1")
        assert requests[0]["1"]["widgetId"] == "_111"

    def test_extracts_kaltura_iframe_from_generic_page(self, settings) -> None:
        page_url = "https://publisher.example/watch"
        player_url = (
            "https://cdnapisec.kaltura.com/index.php/extwidget/preview/"
            "partner_id/111/uiconf_id/2/entry_id/1_abc/embed/iframe"
        )
        api_url = "https://cdnapisec.kaltura.com/api_v3/service/multirequest"
        payload = [
            {},
            {"objects": [{
                "id": "1_abc", "name": "Embedded Kaltura", "duration": 10,
                "dataUrl": "https://cdnapisec.kaltura.com/p/111/sp/11100/serveFlavor",
            }]},
            [{"status": 2, "id": "asset", "fileExt": "mp4", "height": 240}],
            {"objects": []},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            request_url = str(request.url)
            if request_url == page_url:
                return httpx.Response(
                    200, text=f'<iframe src="{player_url}"></iframe>', request=request
                )
            if request_url == api_url:
                return httpx.Response(200, json=payload, request=request)
            return httpx.Response(404, request=request)

        async def scenario():
            async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
                return await GenericHTMLExtractor().extract(
                    page_url, ExtractContext(http, settings, referer=page_url)
                )

        info = run(scenario())
        assert info.extractor == "kaltura"
        assert info.title == "Embedded Kaltura"
        assert info.formats[0].height == 240


class TestSSRF:
    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1", "localhost", "10.0.0.1", "192.168.1.1", "172.16.0.1", "169.254.169.254"],
    )
    def test_blocks_private_hosts(self, host: str) -> None:
        assert is_private_host(host) is True

    @pytest.mark.parametrize("host", ["example.com", "8.8.8.8", "cdn.youtube.com"])
    def test_allows_public_hosts(self, host: str) -> None:
        assert is_private_host(host) is False


# --------------------------------------------------------------------------- #
def make_info() -> MediaInfo:
    return MediaInfo(
        extractor="fake",
        webpage_url="https://x.test/v",
        original_url="https://x.test/v",
        title="Teste",
        formats=[
            StreamFormat(
                id="muxed-720",
                url="https://x.test/720.mp4",
                media_type=MediaType.VIDEO,
                height=720,
                vcodec="avc1",
                acodec="mp4a",
                tbr=2000,
            ),
            StreamFormat(
                id="v-only-1080",
                url="https://x.test/1080.mp4",
                media_type=MediaType.VIDEO,
                height=1080,
                vcodec="avc1",
                acodec="none",
                tbr=4000,
            ),
            StreamFormat(
                id="a-only-160",
                url="https://x.test/160.m4a",
                media_type=MediaType.AUDIO,
                acodec="mp4a",
                abr=160,
            ),
        ],
    )


class TestFormatSelection:
    def _service(self, settings) -> DownloadService:
        from app.core.downloader.manager import JobManager
        from app.services.extraction import ExtractionService

        return DownloadService(settings, JobManager(), ExtractionService(settings))

    def test_video_mode_prefers_muxed(self, settings) -> None:
        service = self._service(settings)
        selection = service.select_formats(make_info(), mode=DownloadMode.VIDEO)
        assert selection.primary.id == "muxed-720"
        assert selection.needs_merge is False

    def test_both_mode_forces_merge(self, settings) -> None:
        service = self._service(settings)
        selection = service.select_formats(make_info(), mode=DownloadMode.BOTH)
        assert selection.video.id == "v-only-1080"
        assert selection.audio.id == "a-only-160"
        assert selection.needs_merge is True

    def test_audio_mode(self, settings) -> None:
        service = self._service(settings)
        selection = service.select_formats(make_info(), mode=DownloadMode.AUDIO)
        assert selection.primary.id == "a-only-160"

    def test_explicit_format_id(self, settings) -> None:
        service = self._service(settings)
        selection = service.select_formats(make_info(), format_id="v-only-1080")
        assert selection.primary.id == "v-only-1080"

    def test_max_height_filter(self, settings) -> None:
        service = self._service(settings)
        selection = service.select_formats(make_info(), mode=DownloadMode.BOTH, max_height=720)
        assert selection.video.height == 720

    def test_unknown_format_id_raises(self, settings) -> None:
        from app.core.exceptions import NoFormatFoundError

        service = self._service(settings)
        with pytest.raises(NoFormatFoundError):
            service.select_formats(make_info(), format_id="inexistente")


class TestSanitizeFilename:
    def test_removes_dangerous_chars(self) -> None:
        from app.core.downloader.plan import sanitize_filename

        result = sanitize_filename('Vídeo: "malicioso" / ../../etc/passwd')
        assert "/" not in result
        assert ":" not in result
        assert ".." not in result

    def test_truncates(self) -> None:
        from app.core.downloader.plan import sanitize_filename

        assert len(sanitize_filename("x" * 500, max_length=50)) <= 50

    def test_empty_falls_back(self) -> None:
        from app.core.downloader.plan import sanitize_filename

        assert sanitize_filename("") == "media"


class TestExtractorHeaders:
    def test_request_headers_add_origin_from_referer(self, settings) -> None:
        ctx = ExtractContext(None, settings, referer="https://www.france.tv/france-4/episode")

        headers = ctx.request_headers()

        assert headers["Referer"] == "https://www.france.tv/france-4/episode"
        assert headers["Origin"] == "https://www.france.tv"


class TestExtractorBase:
    def test_catch_all_matcher(self) -> None:
        class Dummy(BaseExtractor):
            name = "dummy"
            catch_all = True

            async def extract(self, url, ctx):
                raise NotImplementedError

        assert Dummy().matches("qualquer.coisa") is True

    def test_pattern_matcher(self) -> None:
        class Dummy(BaseExtractor):
            name = "dummy"
            patterns = [r"example\.test/(\d+)"]

            async def extract(self, url, ctx):
                raise NotImplementedError

        dummy = Dummy()
        assert dummy.matches("https://example.test/42") is True
        assert dummy.matches("https://other.test/42") is False


class TestGenericEmbeddedMedia:
    def test_extracts_escaped_and_protocol_relative_player_urls(self, settings) -> None:
        page_url = "https://publisher.example/watch/episode"
        html = r"""
        <html><head><title>Episode</title></head><body>
        <script>
          jwplayer("player").setup({
            "file": "https:\/\/cdn.example\/video.mp4?token=a\u0026expires=123"
          });
          const source = "//cdn.example/alternate.mp4?key=xyz";
        </script>
        </body></html>
        """

        async def scenario():
            transport = httpx.MockTransport(
                lambda request: httpx.Response(200, text=html, request=request)
            )
            async with HttpClient(settings, transport=transport) as http:
                context = ExtractContext(http, settings, referer=page_url)
                return await GenericHTMLExtractor().extract(page_url, context)

        info = run(scenario())
        format_urls = {item.url for item in info.formats}

        assert "https://cdn.example/video.mp4?token=a&expires=123" in format_urls
        assert "https://cdn.example/alternate.mp4?key=xyz" in format_urls

    def test_extracts_json_player_sources_with_mime_types(self, settings) -> None:
        page_url = "https://publisher.example/watch/episode"
        html = """
        <script type="application/json">
        {
          "sources": [
            {"file": "https://cdn.example/manifest?token=abc", "type": "application/vnd.apple.mpegurl"},
            {"src": "https://cdn.example/movie?token=def", "type": "video/mp4"},
            {"url": "https://cdn.example/sound?token=ghi", "mimeType": "audio/mpeg"},
            {"url": "https://publisher.example/article"}
          ]
        }
        </script>
        """
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            if str(request.url) == page_url:
                body = html
            elif str(request.url) == "https://cdn.example/manifest?token=abc":
                body = "#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXTINF:5,\nsegment.ts\n#EXT-X-ENDLIST\n"
            else:
                body = "media"
            return httpx.Response(200, text=body, request=request)

        async def scenario():
            async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
                context = ExtractContext(http, settings, referer=page_url)
                return await GenericHTMLExtractor().extract(page_url, context)

        info = run(scenario())
        formats_by_url = {item.url: item for item in info.formats}

        assert "https://cdn.example/movie?token=def" in formats_by_url
        assert formats_by_url["https://cdn.example/movie?token=def"].media_type is MediaType.VIDEO
        assert "https://cdn.example/sound?token=ghi" in formats_by_url
        assert formats_by_url["https://cdn.example/sound?token=ghi"].media_type is MediaType.AUDIO
        assert "https://cdn.example/manifest?token=abc" in requests
        assert "https://publisher.example/article" not in formats_by_url

    def test_follows_iframe_when_page_has_no_direct_media(self, settings) -> None:
        page_url = "https://publisher.example/watch/episode"
        player_url = "https://player.example/embed/episode"
        responses = {
            page_url: '<iframe src="https://player.example/embed/episode"></iframe>',
            player_url: '<video><source src="https://cdn.example/video.mp4" type="video/mp4"></video>',
        }
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            return httpx.Response(
                200,
                text=responses[str(request.url)],
                request=request,
            )

        async def scenario():
            async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
                context = ExtractContext(http, settings, referer=page_url)
                return await GenericHTMLExtractor().extract(page_url, context)

        info = run(scenario())

        assert [item.url for item in info.formats] == ["https://cdn.example/video.mp4"]
        assert info.extractor_data["embedded_host"] == "cdn.example"
        assert requests == [page_url, player_url]

    @pytest.mark.parametrize(
        "embed_markup",
        [
            '<iframe src="https://cdn.jwplayer.com/players/Ab12Cd34-player.js"></iframe>',
            '<script src="https://cdn.jwplayer.com/players/Ab12Cd34-player.js"></script>',
            '<div data-video-jw-id="Ab12Cd34"></div>',
        ],
    )
    def test_follows_jwplatform_embed_and_extracts_formats_and_subtitles(
        self, settings, embed_markup: str
    ) -> None:
        page_url = "https://publisher.example/watch/episode"
        player_url = "https://cdn.jwplayer.com/players/Ab12Cd34-player.js"
        api_url = "https://cdn.jwplayer.com/v2/media/Ab12Cd34"
        responses = {
            page_url: embed_markup,
            api_url: """
                {
                  "title": "Episode title",
                  "playlist": [{
                    "title": "JW episode",
                    "description": "Episode description",
                    "image": "https://cdn.example/thumb.jpg",
                    "duration": 91,
                    "sources": [{
                      "file": "https://cdn.example/episode.mp4",
                      "type": "video/mp4",
                      "label": "720p",
                      "height": 720,
                      "width": 1280,
                      "bitrate": 1800000
                    }],
                    "tracks": [{
                      "file": "https://cdn.example/captions-en.vtt",
                      "kind": "captions",
                      "label": "English",
                      "language": "en"
                    }]
                  }]
                }
            """,
        }
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            request_url = str(request.url)
            body = responses.get(request_url, "")
            status = 200 if request_url in responses else 404
            return httpx.Response(status, text=body, request=request)

        async def scenario():
            async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
                context = ExtractContext(http, settings, referer=page_url)
                return await GenericHTMLExtractor().extract(page_url, context)

        info = run(scenario())

        assert requests == [page_url, api_url]
        assert info.title == "JW episode"
        assert info.extractor == "jwplatform"
        assert info.duration == 91
        assert info.formats[0].url == "https://cdn.example/episode.mp4"
        assert info.formats[0].height == 720
        assert info.formats[0].tbr == 1800
        assert info.subtitles["en"][0].url == "https://cdn.example/captions-en.vtt"

    def test_embed_traversal_stops_on_cycles(self, settings) -> None:
        page_url = "https://publisher.example/watch/episode"
        player_url = "https://player.example/embed/episode"
        responses = {
            page_url: '<iframe src="https://player.example/embed/episode"></iframe>',
            player_url: '<iframe src="https://publisher.example/watch/episode"></iframe>',
        }
        requests: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(str(request.url))
            return httpx.Response(
                200,
                text=responses[str(request.url)],
                request=request,
            )

        async def scenario():
            async with HttpClient(settings, transport=httpx.MockTransport(handler)) as http:
                context = ExtractContext(http, settings, referer=page_url)
                return await GenericHTMLExtractor().extract(page_url, context)

        with pytest.raises(ExtractionError, match="Nenhuma media encontrada"):
            run(scenario())

        assert requests == [page_url, player_url]


class TestYouTubeExtractor:
    def test_client_profiles_match_current_youtube_profiles(self) -> None:
        clients = {client.name: client for client in INNERTUBE_CLIENTS}

        assert clients["ANDROID"].version == "21.26.364"
        assert clients["IOS"].version == "21.26.4"
        assert clients["VISIONOS"].version == "1.02"
        assert clients["TVHTML5"].version == "5.20260707"
        assert clients["WEB_EMBEDDED_PLAYER"].version == "2.20260708.00.00"
        assert clients["WEB"].version == "2.20260708.00.00"
        assert clients["MWEB"].version == "2.20260708.05.00"
        assert "ANDROID_VR" not in clients

    def test_prefers_visionos_client_with_visitor_data(self, settings, monkeypatch) -> None:
        extractor = YouTubeExtractor()
        calls: list[str] = []

        async def fake_call(client, video_id, ctx, *, visitor_data=None):
            calls.append(client.name)
            if client.name == "ANDROID":
                return {
                    "responseContext": {"visitorData": "visitor"},
                    "streamingData": {"formats": [{"itag": 18}]},
                }
            assert visitor_data == "visitor"
            return {"streamingData": {"formats": [{"itag": 18}]}}

        monkeypatch.setattr(extractor, "_call_innertube", fake_call)
        result = run(extractor._fetch_player("bSjs1R4M1fo", ExtractContext(None, settings)))

        assert result.client.name == "VISIONOS"
        assert calls == ["ANDROID", "VISIONOS"]
        assert next(c for c in INNERTUBE_CLIENTS if c.name == "VISIONOS").client_name_header == "101"

    def test_visionos_request_sends_visitor_data_in_context_and_headers(self, settings) -> None:
        class FakeHttp:
            payload = None
            headers = None

            async def post_json(self, url, payload, *, headers):
                self.payload = payload
                self.headers = headers
                return {"streamingData": {"formats": [{"itag": 18}]}}

        http = FakeHttp()
        client = next(item for item in INNERTUBE_CLIENTS if item.name == "VISIONOS")
        result = run(
            YouTubeExtractor()._call_innertube(
                client,
                "bSjs1R4M1fo",
                ExtractContext(http, settings),
                visitor_data="visitor",
            )
        )

        assert result["streamingData"]["formats"]
        assert http.payload["context"]["client"]["visitorData"] == "visitor"
        assert http.payload["context"]["client"]["timeZone"] == "UTC"
        assert http.headers["X-Goog-Visitor-Id"] == "visitor"

    def test_falls_back_to_embedded_client_without_streaming_data(self, settings, monkeypatch) -> None:
        extractor = YouTubeExtractor()
        calls: list[str] = []

        async def fake_call(client, video_id, ctx, *, visitor_data=None):
            calls.append(client.name)
            if client.name != "TVHTML5_SIMPLY_EMBEDDED_PLAYER":
                return {"playabilityStatus": {"status": "OK"}}
            return {
                "playabilityStatus": {"status": "OK"},
                "streamingData": {"formats": [{"itag": 18}]},
            }

        monkeypatch.setattr(extractor, "_call_innertube", fake_call)
        result = run(extractor._fetch_player("bSjs1R4M1fo", ExtractContext(None, settings)))

        assert result.client.name == "TVHTML5_SIMPLY_EMBEDDED_PLAYER"
        assert calls[-1] == "TVHTML5_SIMPLY_EMBEDDED_PLAYER"


class TestDedicatedSiteExtractors:
    def test_routes_twitter(self) -> None:
        assert registry.resolve("https://twitter.com/NASA/status/123456789").name == "twitter"
        assert registry.resolve("https://x.com/NASA/status/123456789").name == "twitter"

    def test_routes_tiktok(self) -> None:
        assert registry.resolve("https://www.tiktok.com/@user/video/1234567890123456789").name == "tiktok"
        assert registry.resolve("https://vm.tiktok.com/ZM1234567/").name == "tiktok"

    def test_routes_reddit(self) -> None:
        assert registry.resolve("https://www.reddit.com/r/funny/comments/123456/test/").name == "reddit"
        assert registry.resolve("https://v.redd.it/1234567890").name == "reddit"

    def test_routes_twitch(self) -> None:
        assert registry.resolve("https://www.twitch.tv/videos/1234567890").name == "twitch"
        assert registry.resolve("https://clips.twitch.tv/FrailTameGrasshopper").name == "twitch"

    def test_routes_soundcloud(self) -> None:
        assert registry.resolve("https://soundcloud.com/artist/sample-track").name == "soundcloud"

    def test_routes_instagram(self) -> None:
        assert registry.resolve("https://www.instagram.com/reel/Cxyz1234567/").name == "instagram"

    def test_routes_facebook(self) -> None:
        assert registry.resolve("https://www.facebook.com/watch/?v=123456789").name == "facebook"

    def test_routes_pinterest(self) -> None:
        assert registry.resolve("https://www.pinterest.com/pin/123456789/").name == "pinterest"

    def test_routes_bilibili(self) -> None:
        assert registry.resolve("https://www.bilibili.com/video/BV1xx411c7mD").name == "bilibili"

    def test_catalog_routes_any_of_940_sites(self) -> None:
        # Sites do catálogo de 940 ficheiros usam o catalog
        assert registry.resolve("https://rumble.com/v12345-sample-video.html").name == "catalog"
        assert registry.resolve("https://artist.bandcamp.com/track/my-song").name == "catalog"

    def test_unknown_sites_fall_back_to_catalog(self) -> None:
        # Sites desconhecidos usam o catálogo (catch_all de prioridade 500)
        # que tenta os 940 extractors do yt-dlp nativamente
        assert registry.resolve("https://arbitrary-blog-site.local/page").name == "catalog"

    def test_native_extractors_still_take_precedence(self) -> None:
        assert registry.resolve("https://www.youtube.com/watch?v=abc123").name == "youtube"
        assert registry.resolve("https://vimeo.com/123456").name == "vimeo"
        assert registry.resolve("https://www.dailymotion.com/video/x123").name == "dailymotion"

    def test_twitter_extracts_native_syndication(self, settings) -> None:
        from app.core.extractors.twitter import TwitterExtractor

        fake_payload = {
            "id_str": "123456789",
            "text": "Space exploration update",
            "user": {"name": "NASA", "screen_name": "NASA"},
            "mediaDetails": [
                {
                    "type": "video",
                    "media_url_https": "https://pbs.twimg.com/thumb.jpg",
                    "video_info": {
                        "duration_millis": 45000,
                        "variants": [
                            {
                                "bitrate": 2176000,
                                "content_type": "video/mp4",
                                "url": "https://video.twimg.com/vid_1080.mp4",
                            },
                            {
                                "bitrate": 832000,
                                "content_type": "video/mp4",
                                "url": "https://video.twimg.com/vid_720.mp4",
                            },
                        ],
                    },
                }
            ],
        }

        class FakeHttp:
            async def get_json(self, url, headers=None):
                return fake_payload

        extractor = TwitterExtractor()
        ctx = ExtractContext(FakeHttp(), settings)  # type: ignore[arg-type]
        info = run(extractor.extract("https://twitter.com/NASA/status/123456789", ctx))

        assert info.title == "Space exploration update"
        assert info.uploader == "NASA"
        assert info.duration == 45.0
        assert info.thumbnail == "https://pbs.twimg.com/thumb.jpg"
        assert len(info.formats) == 2
        assert info.formats[0].tbr == 2176.0
        assert info.formats[1].tbr == 832.0

