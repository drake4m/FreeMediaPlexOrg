import unittest

from mediaorg.identify import identify_video
from mediaorg.matching import lib_folder, match_movie, match_series

from .helpers import MOVIES, SERIES

SERIES_F = [lib_folder(n, f"/S/{n}") for n in SERIES]
MOVIES_F = [lib_folder(n, f"/M/{n}") for n in MOVIES]


def media(name, folder=None):
    r = identify_video(name, folder, True)
    assert r.media, (name, r.reason)
    return r.media


class TestSeriesMatching(unittest.TestCase):
    def check(self, release, status, folder=None, extra=()):
        m = match_series(media(release), SERIES_F + [lib_folder(n, f"/S/{n}") for n in extra])
        self.assertEqual(m.status, status, m.describe())
        if folder:
            self.assertEqual(m.folder.name, folder)
        return m

    def test_real_library_matches(self):
        self.check("Far.Signal.Brave.Old.Coasts.S04E01.1080p.WEB.h264-QUOLL[QRSx.xy].mkv", "match",
                   "Far Signal Brave Old Coasts (2021)")
        self.check("The.Edge.of.Here.S02E01.1080p.WEB.mkv", "match", "The Edge Of Here (2022)")
        self.check("tidemark.s02e01.1080p.web.h264-lark[QRSx.xy].mkv", "match", "TideMark (2024)")
        self.check("Switchback.2022.S02E04.1080p.WEB.h264-QUOLL.mkv", "match", "Switchback (2022)")
        self.check("Switchback S02E05 Undercurrent 1080p ATVP WEB-DL DDP5 1 Atmos H 264-BRISK[QRSx.xy].mkv", "match",
                   "Switchback (2022)")
        self.check("The.Wandering.Choir.Corin.Vale.S03E01.1080p.mkv", "match", "The Wandering Choir Corin Vale (2022)")
        self.check("The Wandering Choir - Corin Vale (2022) - S03E02 - x (1080p BluRay x265 Gannet).mkv", "match",
                   "The Wandering Choir Corin Vale (2022)")
        self.check("Ori-Tal.Benno.S01E01.1080p.mkv", "match", "Ori-Tal Benno (2021)")

    def test_year_conflict_reported_not_guessed(self):
        m = self.check("Brimstone.2016.S01E01.1080p.WEB.mkv", "year_conflict")
        self.assertEqual([c.name for c in m.candidates], ["Brimstone (1999)"])
        self.check("Brimstone.S01E02.1080p.WEB.mkv", "match", "Brimstone (1999)")  # no year in release: reuse

    def test_ambiguous(self):
        self.check("Captain.Nobody.S01E01.720p.mkv", "ambiguous", extra=("Captain Nobody (1962)", "Captain Nobody (2004)"))
        self.check("Captain.Nobody.2004.S01E01.720p.mkv", "match", "Captain Nobody (2004)",
                   extra=("Captain Nobody (1962)", "Captain Nobody (2004)"))

    def test_no_substring_matching(self):
        self.check("Pal.S05E01.1080p.WEB.mkv", "new", extra=("Cousin Abe (2016)",))
        self.check("Cistern.S03E02.1080p.mkv", "match", "Cistern (2022)")
        self.check("Yonder.S01E01.1080p.mkv", "match", "Yonder (2018)")

    def test_country_variant(self):
        extra = ("Brazen (US) (2009)", "Brazen (2002)")
        self.check("Brazen.US.S04E08.720p.HDTV.x264-GRAVEL.mkv", "match", "Brazen (US) (2009)", extra)

    def test_undated_provisional_folder(self):
        # folders created without a year by an earlier run, before tMM renamed them
        folders = [lib_folder("Cape Sorrow", "/S/Cape Sorrow")]
        self.assertEqual(match_series(media("Cape.Sorrow.S01E03.1080p.x265-ToRuS.mkv"), folders).status, "match")
        self.assertEqual(match_series(media("Cape.Sorrow.2025.S01E03.1080p.mkv"), folders).status, "match")


class TestMovieMatching(unittest.TestCase):
    def check(self, release, status, folder=None):
        m = match_movie(media(release), MOVIES_F)
        self.assertEqual(m.status, status, m.describe())
        if folder:
            self.assertEqual(m.folder.name, folder)

    def test_real_library_matches(self):
        self.check("Kestrel.Quarry.2016.1080p.BluRay.x264.mkv", "match", "Kestrel - Quarry (2016)")
        self.check("Sentinel.II.2023.1080p.WEB.mkv", "match", "Sentinel II (2023)")
        self.check("The.Surveyor.2.2023.1080p.WEB.mkv", "match", "The Surveyor2 (2023)")
        self.check("Saltmere.Part.Two.2023.2160p.UHD.BluRay.mkv", "match", "Saltmere - Part Two (2023)")
        self.check("Courier.Unbound.Low.Ledger.Part.One.2022.1080p.WEB.mkv", "match",
                   "Courier - Unbound - Low Ledger Part One (2022)")
        self.check("Ada.Corwins.Garden.Club.2019.1080p.WEB.mkv", "match", "Ada Corwin's Garden Club (2019)")
        self.check("Find.Us.At.Dawn.Were.Long.Gone.2024.1080p.WEB.mkv", "match", "Find Us At Dawn - Were Long Gone (2024)")
        self.check("Kestrel3.1988.1080p.BluRay.mkv", "match", "Kestrel3 (1988)")

    def test_remake_gets_new_folder(self):
        self.check("Saltmere.1982.1080p.BluRay.mkv", "new")
        self.check("Warden.1986.2160p.UHD.BluRay.mkv", "match", "Warden (1986)")

    def test_near_year_is_reported(self):
        self.check("Pale.Hours.Like.Ours.2022.1080p.WEB.mkv", "near_year")


if __name__ == "__main__":
    unittest.main()
