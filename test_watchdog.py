"""
Tests unitaires des briques du moniteur (sans observateur réseau).

Vérifie les composants purs utilisés par monitor_exports_and_move.py :
normalisation de noms, détection des fichiers temporaires et versionnage.
Ce module est découvert par la suite unittest globale (python -m unittest).
"""
import sys
import unittest
from pathlib import Path

# Ajout de la racine du projet au sys.path
sys.path.append(str(Path(__file__).parent))

from filename_utils import get_base_export_stem, normalize_for_matching, slugify_like_ame
from monitor_exports_and_move import ExportHandler, get_versioned_path


class TestWatchdogHelpers(unittest.TestCase):
    def setUp(self):
        self.base_folder = Path(__file__).parent / "test_sandbox_watchdog"
        self.base_folder.mkdir(exist_ok=True)

    def tearDown(self):
        # Nettoyage du bac à sable
        if self.base_folder.exists():
            for item in self.base_folder.glob("**/*"):
                if item.is_file():
                    try:
                        item.unlink()
                    except Exception:
                        pass
            for item in sorted(self.base_folder.glob("**/*"), reverse=True):
                if item.is_dir():
                    try:
                        item.rmdir()
                    except Exception:
                        pass
            try:
                self.base_folder.rmdir()
            except Exception:
                pass

    def test_normalize_for_matching(self):
        self.assertEqual(normalize_for_matching("Café Projet"), "cafe_projet")
        self.assertEqual(normalize_for_matching("Intro - Final"), "intro_final")
        self.assertEqual(normalize_for_matching("  Espaces  multiples  "), "espaces_multiples")
        self.assertEqual(normalize_for_matching("Luna avoue qu’elle n’a pas pardonné Paga pour sa tromperie_9x16"), "luna_avoue_qu_elle_n_a_pas_pardonne_paga_pour_sa_tromperie_9x16")
        self.assertEqual(normalize_for_matching(None), "")

    def test_slugify_like_ame(self):
        self.assertEqual(slugify_like_ame("Café déjà vu"), "Cafe_deja_vu")
        self.assertEqual(slugify_like_ame("Intro–Final"), "Intro_Final")
        self.assertEqual(slugify_like_ame(""), "")

    def test_get_base_export_stem(self):
        self.assertEqual(get_base_export_stem("MonProjet_export.mp4"), "MonProjet_export")
        self.assertEqual(get_base_export_stem("MonProjet_export_1.mp4"), "MonProjet_export")
        # Ne pas retirer le suffixe numérique d'un ratio d'aspect (9x16)
        self.assertEqual(get_base_export_stem("MonProjet_9x16.mp4"), "MonProjet_9x16")
        # Suffixe d'ordre d'export d'EffectCraft / Premiere
        self.assertEqual(get_base_export_stem("M6 - LMCR - La potée lorraine_9x16_2.mp4"), "M6 - LMCR - La potée lorraine_9x16")

    def test_is_temporary_file(self):
        handler = ExportHandler()
        self.assertTrue(handler._is_temporary_file("123e4567-e89b-12d3-a456-426614174000.mp4"))
        self.assertTrue(handler._is_temporary_file(".tmp_fichier.mp4"))
        self.assertTrue(handler._is_temporary_file("._fichier.mp4"))
        self.assertTrue(handler._is_temporary_file("fichier.part"))
        self.assertTrue(handler._is_temporary_file("fichier.mp4~"))
        self.assertFalse(handler._is_temporary_file("MonExport_final.mp4"))

    def test_get_versioned_path(self):
        target = self.base_folder / "output.mp4"
        self.assertEqual(get_versioned_path(target), target)

        target.touch()
        v1 = get_versioned_path(target)
        self.assertEqual(v1, self.base_folder / "output_v1.mp4")

        v1.touch()
        v2 = get_versioned_path(target)
        self.assertEqual(v2, self.base_folder / "output_v2.mp4")


if __name__ == '__main__':
    unittest.main()
