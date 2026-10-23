"""Testes do parser DASH (MPD)."""
from __future__ import annotations

import pytest

from app.core.exceptions import ManifestParseError
from app.core.manifest.dash import DashParser

MPD_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT20S" minBufferTime="PT2S">
  <Period id="0" duration="PT20S">
    <AdaptationSet mimeType="video/mp4" contentType="video" segmentAlignment="true">
      <Representation id="v0" bandwidth="1200000" width="1280" height="720"
                      codecs="avc1.4d401f" frameRate="25">
        <SegmentTemplate timescale="1000" duration="4000"
                         initialization="init-$RepresentationID$.mp4"
                         media="seg-$RepresentationID$-$Number$.m4s"
                         startNumber="1"/>
      </Representation>
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4" contentType="audio">
      <Representation id="a0" bandwidth="128000" codecs="mp4a.40.2"
                      audioSamplingRate="48000">
        <SegmentTemplate timescale="1000" duration="4000"
                         initialization="init-$RepresentationID$.mp4"
                         media="seg-$RepresentationID$-$Number$.m4s"
                         startNumber="1"/>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

MPD_TIMELINE = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT9S">
  <BaseURL>https://cdn.example.com/dash/</BaseURL>
  <Period>
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <Representation id="v0" bandwidth="800000" width="640" height="360">
        <SegmentTemplate timescale="1000" initialization="init.mp4"
                         media="seg-$Time$.m4s">
          <SegmentTimeline>
            <S t="0" d="3000" r="2"/>
          </SegmentTimeline>
        </SegmentTemplate>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

MPD_SEGMENT_LIST = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT8S">
  <Period>
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <Representation id="v0" bandwidth="500000" width="426" height="240">
        <SegmentList duration="4" timescale="1">
          <Initialization sourceURL="init.mp4" range="0-999"/>
          <SegmentURL media="s1.m4s" mediaRange="1000-2999"/>
          <SegmentURL media="s2.m4s"/>
        </SegmentList>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""

MPD_DYNAMIC = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic">
  <Period>
    <AdaptationSet contentType="video">
      <Representation id="v0" bandwidth="100000">
        <SegmentBase indexRange="0-999">
          <Initialization range="0-500"/>
        </SegmentBase>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


@pytest.fixture
def parser() -> DashParser:
    return DashParser()


class TestDash:
    def test_parses_representations(self, parser: DashParser) -> None:
        manifest = parser.parse(MPD_TEMPLATE, "https://cdn.example.com/manifest.mpd")
        assert len(manifest.representations) == 2
        assert manifest.duration == 20.0
        assert manifest.is_live is False

        video = manifest.representations[0]
        assert video.id == "v0"
        assert video.height == 720
        assert video.width == 1280
        assert video.bandwidth == 1200000
        assert video.frame_rate == 25.0
        assert video.vcodec == "avc1.4d401f"
        assert video.media_type.value == "video"

        audio = manifest.representations[1]
        assert audio.media_type.value == "audio"
        assert audio.acodec == "mp4a.40.2"
        assert audio.audio_sampling_rate == 48000

    def test_segment_template_number_substitution(self, parser: DashParser) -> None:
        manifest = parser.parse(MPD_TEMPLATE, "https://cdn.example.com/manifest.mpd")
        video = manifest.representations[0]

        assert video.initialization is not None
        assert video.initialization.url == "https://cdn.example.com/init-v0.mp4"

        # duration=4s * 20s de apresentação = 5 segmentos
        assert len(video.segments) == 5
        assert video.segments[0].url == "https://cdn.example.com/seg-v0-1.m4s"
        assert video.segments[4].url == "https://cdn.example.com/seg-v0-5.m4s"
        assert video.segments[0].duration == 4.0

    def test_base_url_resolution(self, parser: DashParser) -> None:
        manifest = parser.parse(MPD_TIMELINE, "https://cdn.example.com/manifest.mpd")
        video = manifest.representations[0]
        assert video.initialization.url == "https://cdn.example.com/dash/init.mp4"
        assert video.segments[0].url == "https://cdn.example.com/dash/seg-0.m4s"

    def test_segment_timeline(self, parser: DashParser) -> None:
        manifest = parser.parse(MPD_TIMELINE, "https://cdn.example.com/manifest.mpd")
        video = manifest.representations[0]
        # <S t="0" d="3000" r="2"/> -> 3 segmentos
        assert len(video.segments) == 3
        assert video.segments[2].url.endswith("seg-6000.m4s")

    def test_segment_list(self, parser: DashParser) -> None:
        manifest = parser.parse(MPD_SEGMENT_LIST, "https://cdn.example.com/manifest.mpd")
        video = manifest.representations[0]
        assert len(video.segments) == 2
        assert video.initialization is not None
        assert video.initialization.byte_range == (0, 1000)
        assert video.segments[0].byte_range == (1000, 2000)

    def test_dynamic_is_live(self, parser: DashParser) -> None:
        manifest = parser.parse(MPD_DYNAMIC, "https://cdn.example.com/manifest.mpd")
        assert manifest.is_live is True

    def test_invalid_xml(self, parser: DashParser) -> None:
        with pytest.raises(ManifestParseError):
            parser.parse("<MPD><unclosed>", "https://x.test/m.mpd")

    def test_not_an_mpd(self, parser: DashParser) -> None:
        with pytest.raises(ManifestParseError):
            parser.parse("<html></html>", "https://x.test/m.mpd")
