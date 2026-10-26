import unittest

from mediaorg.identify import from_guess, identify_video, is_sample
from mediaorg.normalize import make_keys, norm, safe_component, smart_title, split_folder_name, UnsafeName



def ident(name, folder=None, single=True):
    return identify_video(name, folder, single)


class TestNormalize(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(norm("Ésker"), "esker")
        self.assertEqual(norm("Ada Corwin's Garden Club"), "ada corwins garden club")
        self.assertEqual(norm("Kestrel - Quarry"), "kestrel quarry")
        self.assertEqual(norm("Anna & Paul"), "anna and paul")
        self.assertEqual(norm("Сад и Тень"), "сад и тень")  # not erased
        self.assertEqual(norm("静かな港"), "静かな港")

    def test_keys_tiers(self):
        self.assertTrue(make_keys("The Surveyor 2").intersects(make_keys("The Surveyor2")))  # compact
        self.assertTrue(make_keys("Edge of Here").intersects(make_keys("The Edge Of Here")))  # article
        self.assertFalse(make_keys("Cou").intersects(make_keys("Cousin Abe")))  # no substring match
        self.assertFalse(make_keys("Sker").intersects(make_keys("Ésker")))

    def test_split_and_names(self):
        self.assertEqual(split_folder_name("Brimstone (1999)"), ("Brimstone", 1999))
        self.assertEqual(split_folder_name("Glass Town"), ("Glass Town", None))
        self.assertEqual(smart_title("milo forgets to close his gate"), "Milo Forgets To Close His Gate")
        self.assertEqual(smart_title("Sentinel II"), "Sentinel II")  # release casing kept
        self.assertEqual(smart_title("don't wake foxes"), "Don't Wake Foxes")
        self.assertEqual(safe_component("Kestrel: Quarry (2016)"), "Kestrel - Quarry (2016)")
        self.assertEqual(safe_component('What? "Now" <x>|y*. '), "What Now xy")
        with self.assertRaises(UnsafeName):
            safe_component("..")
        with self.assertRaises(UnsafeName):
            safe_component("é" * 200)


class TestFromGuess(unittest.TestCase):
    def test_episode(self):
        m = ident("Switchback.2022.S02E04.1080p.WEB.h264-QUOLL[QRSx.xy].mkv").media
        self.assertEqual((m.kind, m.title, m.year, m.season, m.episodes), ("episode", "Switchback", 2022, 2, (4,)))

    def test_website_prefix_and_country(self):
        m = ident("www.Example.org    -    Switchback.2022.S02E03.1080p.WEB.h264-QUOLL").media
        self.assertEqual((m.title, m.season, m.episodes), ("Switchback", 2, (3,)))
        m = ident("the.lamplight.us.s02e04.720p.web.h264-group[Site].mkv").media
        self.assertEqual((m.title, m.country), ("the lamplight", "US"))

    def test_long_title_with_an_article_is_not_truncated(self):
        self.assertEqual(ident("The.Edge.of.Here.S02E01.1080p.WEB.mkv").media.title, "The Edge of Here")
        self.assertEqual(ident("Look.At.Us.S01E01.720p.mkv").media.title, "Look At Us")

    def test_multi_episode_and_boundaries(self):
        self.assertEqual(ident("Show.S01E01E02.1080p.WEB.mkv").media.episodes, (1, 2))
        self.assertEqual(ident("Show.S01E01-E03.1080p.mkv").media.episodes, (1, 2, 3))
        self.assertEqual(ident("Show.S02E100.1080p.mkv").media.episodes, (100,))

    def test_unsupported_episode_forms(self):
        self.assertIn("date-based", ident("Show.2026.10.21.1080p.WEB.mkv").reason)
        self.assertIn("absolute", ident("[Group] Anime Show - 25 [1080p].mkv").reason)
        self.assertIn("season pack", ident("Show.S01.COMPLETE.1080p.WEB.mkv").reason)
        self.assertIn("multi-season", ident("Show.S01-S03.COMPLETE.1080p.WEB.mkv").reason)

    def test_movies(self):
        m = ident("Saltmere.Part.Two.2023.2160p.UHD.BluRay.mkv").media
        self.assertEqual((m.kind, m.title, m.year), ("movie", "Saltmere Part Two", 2023))
        m = ident("Courier.Unbound.Low.Ledger.Part.One.2022.1080p.WEB.mkv").media
        self.assertEqual(m.title, "Courier Unbound Low Ledger Part One")
        self.assertEqual(ident("Restless.Orchard.2025.TELESYNC.V2.x264-QRX.mkv").media.label(), "Restless Orchard (2025)")

    def test_movie_rules(self):
        # 1.0.1: neither a year nor release markers are required; a title-only movie is filed
        # without year and tMM completes it when it scrapes
        m = ident("Some.Movie.1080p.WEB.mkv").media
        self.assertEqual((m.kind, m.title, m.year), ("movie", "Some Movie", None))
        self.assertEqual(ident("Mira & Tom & Tom & Lena (2025).mkv").media.label(), "Mira & Tom & Tom & Lena (2025)")
        self.assertIn("CD1", ident("Movie.Title.2019.1080p.BluRay.CD1.mkv").reason)
        self.assertIn("software", ident("Wave Editor Pro 2022 v9.3").reason)
        self.assertEqual(ident("Restless.Orchard.2025.TELESYNC.V2.x264-QRX.mkv").media.label(), "Restless Orchard (2025)")
        self.assertIn("episode number", from_guess({"type": "movie", "title": "Show S01E05x"}).reason)

    def test_season_must_be_written_as_one(self):
        # guessit reads 'tamarind-2048-surround' as S20E48 and 'Show.105' as S01E05
        self.assertIn("bare number", ident("tamarind-2048-surround.mp4").reason)
        self.assertIn("bare number", ident("Show.105.Title.mkv").reason)
        self.assertEqual(ident("tamarind-2048-surround.mp4", "Tamarind (2025)").media.label(), "Tamarind (2025)")
        for name in ("Show.S01E05.mkv", "Show.1x05.Title.mkv", "Show Season 1 Episode 5.mkv"):
            self.assertEqual(ident(name).media.label(), "Show S01E05", name)

    def test_versioned_episode_is_an_episode(self):
        # guessit alone reads 'Show.S01E05v2' as a movie titled "Show S01E05v2"
        self.assertEqual(ident("Show.S01E05v2.1080p.mkv").media.label(), "Show S01E05")

    def test_sample_detection(self):
        self.assertTrue(is_sample("Movie.Title.2019.1080p.BluRay.Sample.mkv"))
        self.assertTrue(is_sample("sample-movie.title.2019.1080p.mkv"))
        self.assertFalse(is_sample("Samples.Of.Life.2019.1080p.mkv"))


class TestFolderContext(unittest.TestCase):
    def test_file_wins_for_episodes(self):
        r = ident("The Wandering Choir - Corin Vale (2022) - S03E01 - L'Aube Grise (1080p BluRay x265 10bit EAC3 5.1 Gannet).mkv",
                  "The Wandering Choir - Corin Vale (2022) S03 (1080p BluRay x265 10bit EAC3 5.1 Gannet)", single=False)
        self.assertEqual(r.media.label(), "The Wandering Choir - Corin Vale (2022) S03E01")

    def test_title_from_folder(self):
        r = ident("S01E05.mkv", "The.Thicket.2018.S01.1080p.WEB", single=False)
        self.assertEqual(r.media.label(), "The Thicket (2018) S01E05")

    def test_conflicting_titles_are_not_guessed(self):
        r = ident("Switchback.2022.S02E03.1080p.mkv", "The.Thicket.2018.S01.1080p.WEB")
        self.assertIsNone(r.media)
        self.assertIn("differs", r.reason)

    def test_year_completed_from_folder(self):
        r = ident("Uncommon.S01E03.1080p.WEB.mkv", "Uncommon.2022.S01.COMPLETE.1080p.DSNP.WEB.H264-MUFFINS[QRx]",
                  single=False)
        self.assertEqual(r.media.year, 2022)

    def test_movie_folder_precedence(self):
        r = ident("grp-mt2020-1080p.mkv", "Quillon (2002) [WEBRip] [1080p] [QRS.XY]")
        self.assertEqual(r.media.label(), "Quillon (2002)")
        r = ident("Sky.Fable.The.Drifter.and.Fennel.2024.iNTERNAL.1080p.10bit.HDRip.2CH.x265.HEVC-VEX.mkv",
                  "Sky.Fable.The.Drifter.and.Fennel.2024")  # folder lacks markers -> file used
        self.assertEqual(r.media.label(), "Sky Fable The Drifter and Fennel (2024)")

    def test_collection_files_are_named_by_themselves(self):
        r = ident("Kestrel.1977.1080p.BluRay.mkv", "Kestrel.Quadrilogy.1977-1994.1080p.BluRay", single=False)
        self.assertEqual(r.media.label(), "Kestrel (1977)")  # the planner checks they are different movies
        r = ident("Kestrel.Revival.1080p.BluRay.mkv", "Kestrel.Quadrilogy.1977-1994.1080p.BluRay", single=False)
        self.assertIsNone(r.media)  # no year: never filed out of a folder with several videos

    def test_movie_without_year_in_a_folder(self):
        r = ident("The.Ballast.1080p.mkv", "The Ballast")
        self.assertEqual((r.media.title, r.media.year), ("The Ballast", None))
        self.assertEqual(ident("The.Ballast.2025.1080p.mkv", "The Ballast").media.label(), "The Ballast (2025)")
        self.assertEqual(ident("The.Ballast.1080p.mkv", "The Ballast (2025)").media.label(), "The Ballast (2025)")
        self.assertIsNone(ident("Other.Film.1080p.mkv", "The Ballast").media)  # two titles, no year: not guessed
        r = ident("Other.Film.2018.1080p.mkv", "The Ballast")
        self.assertEqual(r.media.label(), "Other Film (2018)")  # the file name with a year wins

    def test_episodes_numbered_inside_season_folders(self):
        pack = "The Depot (US) Complete Series"
        r = identify_video("05 - Harvest.mkv", pack, False, (pack, "Season 2"))
        self.assertEqual((r.media.title, r.media.country, r.media.season, r.media.episodes), ("The Depot", "US", 2, (5,)))
        r = identify_video("05 - Harvest.mkv", "Depot Pack", False, ("Depot Pack", "The Depot Season 2"))
        self.assertEqual((r.media.title, r.media.season, r.media.episodes), ("The Depot", 2, (5,)))
        r = identify_video("05.mkv", "Cistern (2022)", False, ("Cistern (2022)", "S01"))
        self.assertEqual(r.media.label(), "Cistern (2022) S01E05")
        self.assertIsNone(identify_video("05 - Harvest.mkv", pack, False, (pack,)).media)  # no season folder
        r = identify_video("The.Depot.S01E01.mkv", "TV Pack", False, ("TV Pack", "The Depot", "Season 1"))
        self.assertEqual(r.media.label(), "The Depot S01E01")  # show title from the folder inside the pack
        self.assertIsNone(identify_video("The.Depot.S01E01.mkv", "TV Pack", False, ("TV Pack", "Season 1")).media)
        self.assertIsNone(identify_video("05 - Harvest.mkv", pack, False, (pack, "Extras")).media)


class TestUnusualNames(unittest.TestCase):
    """Names with brackets, ampersands, numbers and release tags around the title."""

    def test_unusual_names_are_identified(self):
        expected = {
            "Ivy Larch 3 (2025) (1080p NF WEB-DL x265 Mote).mkv": "Ivy Larch 3 (2025)",
            "Restless.Orchard.2025.TELESYNC.V2.x264-QRX.mkv": "Restless Orchard (2025)",
            "Meander (2011) [1080p]": "Meander (2011)",
            "Mira & Tom & Tom & Lena (2025) Eng 1080p WEBRip x265 DDP 5.1 ESub.mkv": "Mira & Tom & Tom & Lena (2025)",
        }
        for name, label in expected.items():
            self.assertEqual(ident(name).media.label(), label, name)

    def test_software_is_not_media(self):
        self.assertIsNone(ident("Ledger Tools Suite Pro 2018-2021 v1104 Build 31012.40211 (x64)").media)
        self.assertIsNone(ident("Canvas.Lab.2021.v12.0.x64").media)


if __name__ == "__main__":
    unittest.main()
