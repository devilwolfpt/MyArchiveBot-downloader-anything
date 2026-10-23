"""Testes do parser HLS."""
from __future__ import annotations

import pytest

from app.core.exceptions import ManifestParseError
from app.core.manifest.hls import HlsParser, parse_attribute_list

MASTER = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",NAME="Português",LANGUAGE="pt",DEFAULT=YES,URI="audio_pt.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1280000,AVERAGE-BANDWIDTH=1000000,CODECS="avc1.4d401f,mp4a.40.2",RESOLUTION=1280x720,FRAME-RATE=25.000,AUDIO="aac"
video_720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2560000,CODECS="avc1.640028,mp4a.40.2",RESOLUTION=1920x1080,FRAME-RATE=50.000,AUDIO="aac"
video_1080.m3u8
#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=100000,CODECS="avc1.4d401f",URI="iframe.m3u8"
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:4
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x000102030405060708090A0B0C0D0E0F
#EXTINF:9.009,
seg0.ts
#EXTINF:9.009,
seg1.ts
#EXT-X-DISCONTINUITY
#EXTINF:3.003,
seg2.ts
#EXT-X-ENDLIST
"""

MEDIA_UNENCRYPTED = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXTINF:6.0,
a.ts
#EXTINF:6.0,
b.ts
#EXT-X-ENDLIST
"""

MEDIA_BYTERANGE = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4.0,
#EXT-X-BYTERANGE:1000@0
media.mp4
#EXTINF:4.0,
#EXT-X-BYTERANGE:2000
media.mp4
#EXT-X-ENDLIST
"""


@pytest.fixture
def parser() -> HlsParser:
    return HlsParser()


class TestAttributeList:
    def test_simple(self) -> None:
        attrs = parse_attribute_list('BANDWIDTH=1280000,CODECS="avc1.4d401f,mp4a.40.2"')
        assert attrs["BANDWIDTH"] == "1280000"
        assert attrs["CODECS"] == "avc1.4d401f,mp4a.40.2"

    def test_value_with_comma(self) -> None:
        attrs = parse_attribute_list('NAME="Video, Audio"')
        assert attrs["NAME"] == "Video, Audio"

    def test_flag(self) -> None:
        attrs = parse_attribute_list("DEFAULT=YES,AUTOSELECT=NO")
        assert attrs["DEFAULT"] == "YES"
        assert attrs["AUTOSELECT"] == "NO"


class TestMaster:
    def test_detects_master(self, parser: HlsParser) -> None:
        assert parser.is_master(MASTER)
        assert not parser.is_master(MEDIA)

    def test_parses_variants(self, parser: HlsParser) -> None:
        master = parser.parse_master(MASTER, "https://cdn.example.com/hls/master.m3u8")
        assert len(master.variants) == 3  # 2 streams + 1 i-frame

        v720 = master.variants[0]
        assert v720.height == 720
        assert v720.width == 1280
        assert v720.bandwidth == 1280000
        assert v720.average_bandwidth == 1000000
        assert v720.frame_rate == 25.0
        assert v720.audio_group == "aac"
        assert v720.uri == "https://cdn.example.com/hls/video_720.m3u8"
        assert v720.vcodec == "avc1.4d401f"
        assert v720.acodec == "mp4a.40.2"

        v1080 = master.variants[1]
        assert v1080.height == 1080
        assert v1080.frame_rate == 50.0

    def test_iframe_variant(self, parser: HlsParser) -> None:
        master = parser.parse_master(MASTER, "https://cdn.example.com/hls/master.m3u8")
        iframe = [v for v in master.variants if v.is_iframe]
        assert len(iframe) == 1
        assert iframe[0].uri.endswith("iframe.m3u8")

    def test_renditions(self, parser: HlsParser) -> None:
        master = parser.parse_master(MASTER, "https://cdn.example.com/hls/master.m3u8")
        assert len(master.renditions) == 1
        rend = master.renditions[0]
        assert rend.type == "AUDIO"
        assert rend.language == "pt"
        assert rend.default is True
        assert rend.uri == "https://cdn.example.com/hls/audio_pt.m3u8"

    def test_relative_url_resolution(self, parser: HlsParser) -> None:
        master = parser.parse_master(MASTER, "https://cdn.example.com/a/b/master.m3u8")
        assert master.variants[0].uri == "https://cdn.example.com/a/b/video_720.m3u8"

    def test_invalid_playlist(self, parser: HlsParser) -> None:
        with pytest.raises(ManifestParseError):
            parser.parse_master("sem header", "https://x.test/m.m3u8")


class TestMedia:
    def test_parses_segments(self, parser: HlsParser) -> None:
        playlist = parser.parse_media(MEDIA, "https://cdn.example.com/hls/media.m3u8")
        assert len(playlist.segments) == 3
        assert playlist.target_duration == 10
        assert playlist.is_live is False
        assert playlist.duration == pytest.approx(21.021)
        assert playlist.segments[0].uri == "https://cdn.example.com/hls/seg0.ts"

    def test_detects_discontinuity(self, parser: HlsParser) -> None:
        playlist = parser.parse_media(MEDIA, "https://cdn.example.com/hls/media.m3u8")
        assert playlist.segments[0].discontinuity is False
        assert playlist.segments[2].discontinuity is True

    def test_parses_key(self, parser: HlsParser) -> None:
        playlist = parser.parse_media(MEDIA, "https://cdn.example.com/hls/media.m3u8")
        key = playlist.segments[0].key
        assert key is not None
        assert key.method == "AES-128"
        assert key.uri == "https://cdn.example.com/hls/key.bin"
        assert key.iv == "0x000102030405060708090A0B0C0D0E0F"
        assert key.is_encrypted is True

    def test_unencrypted_segments_have_no_key(self, parser: HlsParser) -> None:
        playlist = parser.parse_media(MEDIA_UNENCRYPTED, "https://cdn.example.com/hls/media.m3u8")
        assert playlist.segments[0].key is None

    def test_live_playlist_without_endlist(self, parser: HlsParser) -> None:
        live = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\na.ts\n"
        playlist = parser.parse_media(live, "https://cdn.example.com/live.m3u8")
        assert playlist.is_live is True

    def test_byte_ranges(self, parser: HlsParser) -> None:
        playlist = parser.parse_media(MEDIA_BYTERANGE, "https://cdn.example.com/hls/media.m3u8")
        assert playlist.segments[0].byte_range == (0, 1000)
        # Segundo byterange sem offset -> herda o fim do anterior
        assert playlist.segments[1].byte_range == (1000, 2000)
        assert playlist.segments[0].map_uri == "https://cdn.example.com/hls/init.mp4"

    def test_empty_playlist_raises(self, parser: HlsParser) -> None:
        with pytest.raises(ManifestParseError):
            parser.parse_media("#EXTM3U\n#EXT-X-ENDLIST\n", "https://x.test/m.m3u8")
