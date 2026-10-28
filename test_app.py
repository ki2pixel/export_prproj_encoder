"""Tests unitaires de la version Linux / prproj2ffmpeg.

Exécution :
    .venv/bin/python -m unittest discover -v
    .venv/bin/python -m unittest test_app.TestAMEWatchfolder.test_path_safety

Les fixtures réelles (projets Premiere .prproj) sont dans `fixtures/`.
"""
import gzip
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

# Ajout de la racine du projet au sys.path
sys.path.append(str(Path(__file__).parent))

import submit_to_watchfolder as stw
from config import (
    CLEAN_OUTPUT_GUARD_SECONDS,
    PRPROJ2FFMPEG_PYTHON,
    STATUS_ABORTED,
    STATUS_DONE,
    STATUS_DONE_WITH_WARNINGS,
    STATUS_ENCODING,
    STATUS_FAILED,
    STATUS_MOVED,
    STATUS_SUBMITTED,
    STATUS_SUPERSEDED,
    is_matchable_status,
)
from filename_utils import (
    extract_job_id,
    get_base_export_stem,
    is_assist_stem,
    sanitize_for_filesystem,
    strip_assist_suffix,
    strip_job_id,
)
from monitor_exports_and_move import _is_same_filesystem, get_versioned_path, move_to_final_verified
from prproj_engine import (
    EncodeSettings,
    build_command,
    parse_bitrate,
    probe_ffmpeg,
    read_project_info,
    summarize_line,
)
from submit_to_watchfolder import build_expected_export_stem, is_safe_path, select_assist_priority

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_PRPROJ = FIXTURES / "PP - LRDP - Yann Stotz_9x16.prproj"
PRPROJ2FFMPEG_SCRIPT = Path(__file__).parent / "tools" / "prproj-unpacker" / "prproj2ffmpeg.py"


def load_prproj2ffmpeg():
    """Charge le script vendorisé comme module : réservé aux tests (l'application, elle,
    l'exécute en sous-processus et ne l'importe jamais)."""
    spec = importlib.util.spec_from_file_location("prproj2ffmpeg_vendorise", PRPROJ2FFMPEG_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestAMEWatchfolder(unittest.TestCase):
    def setUp(self):
        self.base_folder = Path(__file__).parent / "test_sandbox"
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

    # --- Sécurité et nommage ---

    def test_path_safety(self):
        safe_sub = self.base_folder / "project1" / "projets"
        safe_sub.mkdir(parents=True, exist_ok=True)
        safe_file = safe_sub / "test.prproj"
        safe_file.touch()

        unsafe_traverse = self.base_folder / ".." / "some_file.txt"

        self.assertTrue(is_safe_path(safe_sub, self.base_folder))
        self.assertTrue(is_safe_path(safe_file, self.base_folder))
        self.assertFalse(is_safe_path(unsafe_traverse, self.base_folder))

    def test_filename_versioning(self):
        target_file = self.base_folder / "output.mp4"

        # Le fichier n'existe pas encore
        v0 = get_versioned_path(target_file)
        self.assertEqual(v0, target_file)

        target_file.touch()
        v1 = get_versioned_path(target_file)
        self.assertEqual(v1, self.base_folder / "output_v1.mp4")

        v1.touch()
        v2 = get_versioned_path(target_file)
        self.assertEqual(v2, self.base_folder / "output_v2.mp4")

    def test_strip_job_id(self):
        self.assertEqual(strip_job_id("Mon Projet_9x16_20261013_120000_abcdef"), "Mon Projet_9x16")
        self.assertEqual(strip_job_id("Mon Projet_9x16"), "Mon Projet_9x16")
        self.assertEqual(strip_job_id(""), "")

    def test_extract_job_id(self):
        test_filenames = [
            "intro_20260714_160822_abc123.prproj",
            "project_name_20260714_160822_abc123_1.mp4",
            "intro_20260714_160822_abc123.prproj.tmp",
        ]

        for filename in test_filenames:
            self.assertEqual(extract_job_id(filename), "20260714_160822_abc123")
        self.assertIsNone(extract_job_id("aucun-identifiant.prproj"))

    def test_scan_non_recursif(self):
        projets_dir = self.base_folder / "project1" / "projets"
        auto_save_dir = projets_dir / "Adobe Premiere Pro Auto-Save"
        auto_save_dir.mkdir(parents=True, exist_ok=True)
        autre_dir = projets_dir / "_artefacts"
        autre_dir.mkdir(parents=True, exist_ok=True)

        final_prproj = projets_dir / "a.prproj"
        final_prproj.touch()
        artifact_prproj = auto_save_dir / "b.prproj"
        artifact_prproj.touch()
        (autre_dir / "c.prproj").touch()
        (projets_dir / "notes.txt").touch()

        sandbox_log = self.base_folder / "test_log.csv"
        with patch("submit_to_watchfolder.SUBMISSION_LOG_FILE", sandbox_log), \
             patch("submit_to_watchfolder.SUBMISSION_TEXT_LOG_FILE", self.base_folder / "test_log.txt"):
            found = stw.find_projects_to_process(self.base_folder, "project", False)

        self.assertIn(final_prproj, found)
        self.assertNotIn(artifact_prproj, found)
        self.assertEqual(len(found), 1)

    def test_scan_priorite_assist(self):
        projets_dir = self.base_folder / "project1" / "projets"
        projets_dir.mkdir(parents=True, exist_ok=True)

        normal = projets_dir / "X_9x16.prproj"
        assist = projets_dir / "X_9x16_assist.prproj"
        normal_only = projets_dir / "Y_9x16.prproj"
        orphan = projets_dir / "Z_9x16_assist.prproj"
        for path in (normal, assist, normal_only, orphan):
            path.touch()

        sandbox_log = self.base_folder / "test_log.csv"
        with patch("submit_to_watchfolder.SUBMISSION_LOG_FILE", sandbox_log), \
             patch("submit_to_watchfolder.SUBMISSION_TEXT_LOG_FILE", self.base_folder / "test_log.txt"):
            found = stw.find_projects_to_process(self.base_folder, "project", False)

        self.assertIn(assist, found)
        self.assertNotIn(normal, found)
        self.assertIn(normal_only, found)
        self.assertIn(orphan, found)
        self.assertEqual(len(found), 3)

    def test_sanitize_for_filesystem(self):
        self.assertEqual(sanitize_for_filesystem('La potée lorraine_9x16'), 'La potée lorraine_9x16')
        self.assertEqual(sanitize_for_filesystem('Titre: sous-titre ?'), 'Titre_ sous-titre _')
        self.assertEqual(sanitize_for_filesystem('Séquence <A|B>C*D"E'), 'Séquence _A_B_C_D_E')
        self.assertEqual(sanitize_for_filesystem('Nom avec points...   '), 'Nom avec points')
        self.assertEqual(sanitize_for_filesystem(''), '')

    def test_get_base_export_stem(self):
        self.assertEqual(get_base_export_stem("M6 - LMCR - La potée lorraine_9x16_2.mp4"),
                         "M6 - LMCR - La potée lorraine_9x16")
        # Un ratio d'image n'est jamais considéré comme un suffixe de version
        self.assertEqual(get_base_export_stem("MonProjet_9x16.mp4"), "MonProjet_9x16")
        self.assertEqual(get_base_export_stem("MonProjet_16x9.mp4"), "MonProjet_16x9")
        self.assertEqual(get_base_export_stem("M6 avec accent_é_3.xml"), "M6 avec accent_é")

    def test_expected_export_stem(self):
        # Le nom final dérive du nom du fichier .prproj (fixture réelle), sans lire son contenu
        self.assertEqual(build_expected_export_stem(FIXTURE_PRPROJ), "PP - LRDP - Yann Stotz_9x16")

        # Les suffixes de version sont préservés (variantes distinctes du même projet)
        self.assertEqual(build_expected_export_stem(self.base_folder / "M6 - AVDP - Titre_9x16_2.prproj"),
                         "M6 - AVDP - Titre_9x16_2")

        # Préfixe numérique et mention « copy N » retirés
        self.assertEqual(build_expected_export_stem(self.base_folder / "156 Mon Projet copy 2.prproj"),
                         "Mon Projet")

        # Suffixe de job_id retiré (adoption d'un projet déposé à la main)
        self.assertEqual(
            build_expected_export_stem(self.base_folder / "M6 - LMCR - La potée lorraine_9x16_20261013_120000_abcdef.prproj"),
            "M6 - LMCR - La potée lorraine_9x16")

    def test_assist_suffix_helpers(self):
        self.assertTrue(is_assist_stem("Projet_9x16_assist"))
        self.assertTrue(is_assist_stem("Projet_9x16_ASSIST"))
        self.assertFalse(is_assist_stem("Projet_9x16"))
        self.assertFalse(is_assist_stem("Projet_assist_v2"))

        self.assertEqual(strip_assist_suffix("Projet_9x16_assist"), "Projet_9x16")
        self.assertEqual(strip_assist_suffix("Projet_9x16_ASSIST"), "Projet_9x16")
        self.assertEqual(strip_assist_suffix("Projet_9x16"), "Projet_9x16")
        self.assertEqual(strip_assist_suffix(""), "")

    def test_expected_export_stem_assist(self):
        # La variante _assist produit toujours le nom final de la version classique
        self.assertEqual(
            build_expected_export_stem(
                self.base_folder / "Ce tueur a dirigé l’enquête pour retrouver son beau-fils mort _9x16_assist.prproj"),
            "Ce tueur a dirigé l’enquête pour retrouver son beau-fils mort _9x16")

        # Associations avec le préfixe numérique, « copy N » et le job_id d'adoption
        self.assertEqual(
            build_expected_export_stem(self.base_folder / "156 Mon Projet copy 2_assist.prproj"),
            "Mon Projet")
        self.assertEqual(
            build_expected_export_stem(self.base_folder / "Projet_assist_20261013_120000_abcdef.prproj"),
            "Projet")

        # Un « _assist » qui n'est pas le suffixe terminal reste inchangé
        self.assertEqual(
            build_expected_export_stem(self.base_folder / "Projet_assist_v2.prproj"),
            "Projet_assist_v2")

    def test_select_assist_priority(self):
        normal = Path("/projets/X_9x16.prproj")
        assist = Path("/projets/X_9x16_assist.prproj")
        other = Path("/projets/Y_9x16.prproj")
        orphan = Path("/projets/Z_9x16_assist.prproj")

        selected, superseded = select_assist_priority([normal, assist, other, orphan])

        self.assertIn(assist, selected)
        self.assertNotIn(normal, selected)
        self.assertIn(other, selected)
        self.assertIn(orphan, selected)
        self.assertEqual(superseded, [(normal, assist)])

    def test_supersede_pending_normal_jobs(self):
        from db_helper import add_submission, get_submission_by_job_id, init_db

        db_path = self.base_folder / "jobs.db"
        init_db(db_path)
        projets_dir = self.base_folder / "project1" / "projets"
        projets_dir.mkdir(parents=True, exist_ok=True)

        normal = projets_dir / "X_9x16.prproj"
        assist = projets_dir / "X_9x16_assist.prproj"
        encoding_normal = projets_dir / "Y_9x16.prproj"
        encoding_assist = projets_dir / "Y_9x16_assist.prproj"
        unrelated = projets_dir / "Z_9x16.prproj"
        for path in (normal, assist, encoding_normal, encoding_assist, unrelated):
            path.touch()

        job_normal = "20261013_120000_aaaaaa"
        job_encoding = "20261013_120000_bbbbbb"
        job_unrelated = "20261013_120000_cccccc"
        add_submission(db_path, "2026-10-13 12:00:00", str(normal),
                       f"X_9x16_{job_normal}.prproj", "X_9x16", job_normal,
                       status=STATUS_SUBMITTED)
        add_submission(db_path, "2026-10-13 12:00:01", str(encoding_normal),
                       f"Y_9x16_{job_encoding}.prproj", "Y_9x16", job_encoding,
                       status=STATUS_ENCODING)
        add_submission(db_path, "2026-10-13 12:00:02", str(unrelated),
                       f"Z_9x16_{job_unrelated}.prproj", "Z_9x16", job_unrelated,
                       status=STATUS_SUBMITTED)

        with patch("submit_to_watchfolder.JOBS_DB_FILE", db_path), \
             patch("submit_to_watchfolder.WATCH_FOLDER_OUTPUT", self.base_folder / "WATCH_OUT"), \
             patch("submit_to_watchfolder.SUBMISSION_TEXT_LOG_FILE", self.base_folder / "test_log.txt"):
            stw.supersede_pending_normal_jobs(assist)
            stw.supersede_pending_normal_jobs(encoding_assist)

        # La normale encore en file est remplacée ; l'encodage en cours et le projet sans
        # rapport ne sont jamais modifiés.
        self.assertEqual(get_submission_by_job_id(db_path, job_normal)['Status'], STATUS_SUPERSEDED)
        self.assertEqual(get_submission_by_job_id(db_path, job_encoding)['Status'], STATUS_ENCODING)
        self.assertEqual(get_submission_by_job_id(db_path, job_unrelated)['Status'], STATUS_SUBMITTED)

    def test_update_submission_status_if_current(self):
        from db_helper import (
            add_submission,
            get_submission_by_job_id,
            init_db,
            update_submission_status_if_current,
        )

        db_path = self.base_folder / "jobs.db"
        init_db(db_path)
        job_id = "20261013_120000_dddddd"
        add_submission(db_path, "2026-10-13 12:00:00", "/x.prproj", f"x_{job_id}.prproj",
                       "x", job_id, status=STATUS_SUBMITTED)

        # Transition refusée si le statut attendu ne correspond plus
        self.assertFalse(update_submission_status_if_current(
            db_path, job_id, STATUS_ENCODING, STATUS_SUPERSEDED))
        self.assertEqual(get_submission_by_job_id(db_path, job_id)['Status'], STATUS_SUBMITTED)

        # Transition appliquée si le job est toujours dans l'état attendu
        self.assertTrue(update_submission_status_if_current(
            db_path, job_id, STATUS_SUBMITTED, STATUS_SUPERSEDED))
        self.assertEqual(get_submission_by_job_id(db_path, job_id)['Status'], STATUS_SUPERSEDED)

    # --- Contrat prproj2ffmpeg ---

    def test_build_command(self):
        settings = EncodeSettings(bitrate="10M", encoder="auto")
        command = build_command(
            Path('/tmp/Projet "spécial" – potée.prproj'),
            Path("/tmp/sortie potée.mp4"),
            settings,
            media_bases=[self.base_folder],
            script_path=Path("/opt/tools/prproj2ffmpeg.py"),
        )
        self.assertEqual(command[:3], [PRPROJ2FFMPEG_PYTHON, "/opt/tools/prproj2ffmpeg.py",
                                       '/tmp/Projet "spécial" – potée.prproj'])
        self.assertEqual(command[3:7], ["--out", "/tmp/sortie potée.mp4", "--bitrate", "10M"])
        self.assertNotIn("--encoder", command)
        self.assertEqual(command[7:9], ["--base", str(self.base_folder)])

        # Encodeur forcé et bases inexistantes ignorées
        forced = build_command(Path("a.prproj"), Path("b.mp4"),
                               EncodeSettings(bitrate="8M", encoder="libx264"),
                               media_bases=[Path("/inexistant-xyz")],
                               script_path=Path("/s.py"))
        self.assertEqual(forced[forced.index("--encoder") + 1], "libx264")
        self.assertEqual(forced[forced.index("--bitrate") + 1], "8M")
        self.assertNotIn("--base", forced)

        # Script introuvable : erreur explicite
        with patch("prproj_engine.resolve_prproj2ffmpeg_script", return_value=None):
            with self.assertRaises(FileNotFoundError):
                build_command(Path("a.prproj"), Path("b.mp4"), settings)

    def test_parse_bitrate(self):
        self.assertEqual(parse_bitrate("10M"), 10_000_000)
        self.assertEqual(parse_bitrate("8000k"), 8_000_000)
        self.assertEqual(parse_bitrate("1200000"), 1_200_000)
        self.assertEqual(parse_bitrate("n'importe quoi"), 0)

    def test_summarize_line(self):
        # Statistiques ffmpeg : journalisées, jamais remontées à l'UI
        self.assertIsNone(summarize_line("frame=  120 fps= 25 q=23.0 size=    1024kB time=00:00:04.80 bitrate=1747.6kbits/s speed=1.02x"))
        self.assertIsNone(summarize_line("video:1234kB audio:56kB subtitle:0kB other streams:0kB global headers:0kB muxing overhead: 1.1%"))
        # Bruit technique (bannière, flux, configuration) : masqué
        self.assertIsNone(summarize_line("ffmpeg version 6.1.1 Copyright (c) 2000-2023 the FFmpeg developers"))
        self.assertIsNone(summarize_line("  Stream #0:0(und): Video: h264 (High) (avc1 / 0x31637661), yuv420p, 1080x1920"))
        self.assertIsNone(summarize_line("   "))

        # Lignes signifiantes
        self.assertIn("✓ Export terminé", summarize_line("done: /tmp/sortie.mp4"))
        self.assertIn("ERREUR", summarize_line("error: ffmpeg failed with exit code 1"))
        self.assertIn("média introuvable", summarize_line("error: media not found for F:\\Rushes\\clip.mp4 (use --base)"))
        self.assertIn("ERREUR: prproj2ffmpeg", summarize_line("error: no sequence found in project"))
        self.assertIn("cache", summarize_line("[cache] 8-bit alpha intermediate for 'logo.mov'"))
        self.assertIn("conversion alpha", summarize_line("[cache] conversion alpha 8 bits de 'logo.mov' (0-61.5 s)…"))
        self.assertIn("recadrage alpha", summarize_line("[bbox] recadrage alpha de 'm6+.png': 172x90+824+292"))
        self.assertIn("analyse alpha", summarize_line("[bbox] analyse alpha de 'logo.mov' (0-61.5 s)…"))
        self.assertIn("repli libx264", summarize_line(
            "[encoder] h264_nvenc indisponible (GPU/session NVENC), repli libx264"))
        self.assertIn("commande ffmpeg", summarize_line("ffmpeg -y -hide_banner -i a.prproj -filter_complex [0:v]trim=…"))
        self.assertIn("ERREUR: ffmpeg", summarize_line("/usr/bin/ffmpeg: No such file or directory"))

    # --- Recadrage alpha des images fixes ---

    def test_bbox_scan_exact(self):
        """La boîte alpha exacte doit englober les pixels isolés et les contours fins
        (cropdetect, lui, rogne selon la moyenne des lignes de bord)."""
        engine = load_prproj2ffmpeg()
        w, h = 8, 4

        def plane(rows):
            return bytes(value for row in rows for value in row)

        # Transparent partout (alpha <= 2) : aucune boîte
        self.assertIsNone(engine._bbox_scan(plane([[0] * w] * h), w, h))
        self.assertIsNone(engine._bbox_scan(plane([[2] * w] * h), w, h))

        # Un seul pixel opaque au seuil + 1 : boîte de 1x1 (aucun seuil de moyenne)
        rows = [[0] * w for _ in range(h)]
        rows[1][2] = 3
        self.assertEqual(engine._bbox_scan(plane(rows), w, h), (2, 1, 1, 1))

        # Contour fin (1 px de haut) : la boîte doit couvrir toute la largeur balayée
        rows = [[0] * w for _ in range(h)]
        for x in range(1, 7):
            rows[0][x] = 255
            rows[3][x] = 255
        self.assertEqual(engine._bbox_scan(plane(rows), w, h), (1, 0, 6, 4))

        # Plan incomplet (lecture ffmpeg tronquée) : aucune boîte plutôt qu'une fausse
        self.assertIsNone(engine._bbox_scan(b"\x00\x00\x00", w, h))

    def test_pad_box_marge_et_bornage(self):
        engine = load_prproj2ffmpeg()
        # Marge de 2 px, dimensions alignées sur 2, bord gauche borné à 0
        self.assertEqual(engine._pad_box((2, 1, 1, 1), 8, 4), [0, 0, 4, 4])
        # Une boîte qui couvre déjà toute l'image ne sert à rien
        self.assertIsNone(engine._pad_box((0, 0, 8, 4), 8, 4))

    # --- Fenêtre visible des overlays masqués ---

    @staticmethod
    def _overlay_item(engine, scale, position, anchor, mask=None):
        item = engine.TrackItem()
        item.start, item.end = 0.0, 10.0
        item.scale_w = item.scale_h = scale
        item.anchor = anchor
        item.position = types.SimpleNamespace(kind='point', static=position, kfs=[])
        item.masks = [mask] if mask is not None else []
        return item

    @staticmethod
    def _rect_mask(engine, x, y, w, h, feather=0.0, opacity=100.0, inverted=False):
        mask = engine.Mask()
        mask.shape = 'rect'
        mask.bbox = (x / 1920.0, y / 1080.0, (x + w) / 1920.0, (y + h) / 1080.0)
        mask.feather = feather
        mask.opacity = opacity
        mask.inverted = inverted
        mask.expansion = 0.0
        return mask

    @staticmethod
    def _clip_item(engine, start, end, in_s=0.0, out_s=10.0,
                   path=r"F:\rush\clip.mp4", still=False):
        item = engine.TrackItem()
        item.start, item.end = start, end
        item.clip = engine.Clip()
        item.clip.source_path = path
        item.clip.in_sec = in_s
        item.clip.out_sec = out_s
        item.clip.still = still
        return item

    def test_visible_overlay_window_masque_rectangulaire(self):
        """Cas réel GOAL : zoom 252 % masqué -> crop 444x267 au lieu de 4838x2722.

        Le rectangle plein n'est pas élargi de 1 px ; out_h impair impose le repli RGBA.
        """
        engine = load_prproj2ffmpeg()
        mask = self._rect_mask(engine, 740, 53, 444, 267)
        item = self._overlay_item(
            engine, 252.0,
            (0.49845234111503317, 0.16592929437756537),
            (0.501200614952957, 0.17319108802551955), mask)
        window = engine.visible_overlay_window(item, 1920, 1080, 1080, 1920)
        self.assertEqual(window, (740, 53, 444, 267, -22, -19, 1119, 673))
        # La surface rendue est bien inférieure au scale plein cadre 4838x2722.
        self.assertLess(window[6] * window[7], (4838 * 2722) // 10)

    def test_visible_overlay_window_replis(self):
        engine = load_prproj2ffmpeg()
        mask = self._rect_mask(engine, 740, 53, 444, 267)

        # Sans masque : le chemin composition est conservé.
        item = self._overlay_item(engine, 252.0, (0.5, 0.5), (0.5, 0.5))
        self.assertIsNone(engine.visible_overlay_window(item, 1920, 1080, 1080, 1920))

        # Position animée : composition.
        item = self._overlay_item(engine, 252.0, (0.5, 0.5), (0.5, 0.5), mask)
        item.position = types.SimpleNamespace(
            kind='point', static=(0.5, 0.5),
            kfs=[types.SimpleNamespace(t=1.0, x=0.5, y=0.5, ix=0, iy=0, ox=0, oy=0)])
        self.assertIsNone(engine.visible_overlay_window(item, 1920, 1080, 1080, 1920))

        # Rotation : composition.
        item = self._overlay_item(engine, 252.0, (0.5, 0.5), (0.5, 0.5), mask)
        item.rotation = types.SimpleNamespace(kfs=[], static=12.0)
        self.assertIsNone(engine.visible_overlay_window(item, 1920, 1080, 1080, 1920))

        # Masque inversé : composition (zone opaque = complément).
        item = self._overlay_item(engine, 252.0, (0.5, 0.5), (0.5, 0.5),
                                  self._rect_mask(engine, 740, 53, 444, 267, inverted=True))
        self.assertIsNone(engine.visible_overlay_window(item, 1920, 1080, 1080, 1920))

        # Flou : le crop au ras du masque couperait sa diffusion.
        item = self._overlay_item(engine, 252.0, (0.5, 0.5), (0.5, 0.5), mask)
        item.blur = engine.BlurFx()
        item.blur.amount = 10.0
        self.assertIsNone(engine.visible_overlay_window(item, 1920, 1080, 1080, 1920))

        # Masque avec feather (non simple) : cx/cy/cw/ch alignés sur 2.
        feather_mask = self._rect_mask(engine, 101, 55, 201, 101, feather=4.0)
        item = self._overlay_item(engine, 252.0, (0.5, 0.5), (0.5, 0.5), feather_mask)
        window = engine.visible_overlay_window(item, 1920, 1080, 1080, 1920)
        self.assertIsNotNone(window)
        for value in window[:4]:
            self.assertEqual(value & 1, 0, window)

    def test_simple_opaque_rect_mask(self):
        engine = load_prproj2ffmpeg()
        mask = self._rect_mask(engine, 0, 0, 10, 10)
        self.assertTrue(engine._is_simple_opaque_rect_mask(
            self._overlay_item(engine, 100.0, (0.5, 0.5), (0.5, 0.5), mask)))
        self.assertFalse(engine._is_simple_opaque_rect_mask(
            self._overlay_item(engine, 100.0, (0.5, 0.5), (0.5, 0.5),
                               self._rect_mask(engine, 0, 0, 10, 10, feather=3.0))))
        self.assertFalse(engine._is_simple_opaque_rect_mask(
            self._overlay_item(engine, 100.0, (0.5, 0.5), (0.5, 0.5),
                               self._rect_mask(engine, 0, 0, 10, 10, opacity=50.0))))
        self.assertFalse(engine._is_simple_opaque_rect_mask(
            self._overlay_item(engine, 100.0, (0.5, 0.5), (0.5, 0.5))))

    def test_mask_union_bbox_drawbox_et_feather(self):
        engine = load_prproj2ffmpeg()
        item = self._overlay_item(engine, 100.0, (0.5, 0.5), (0.5, 0.5),
                                  self._rect_mask(engine, 740, 53, 444, 267))
        self.assertEqual(engine._mask_union_bbox_px(item, 1920, 1080),
                         (740.0, 53.0, 1184.0, 320.0))
        # Le feather élargit la fenêtre pour ne pas couper le bord flou.
        item = self._overlay_item(engine, 100.0, (0.5, 0.5), (0.5, 0.5),
                                  self._rect_mask(engine, 100, 100, 200, 100, feather=4.0))
        self.assertEqual(engine._mask_union_bbox_px(item, 1920, 1080),
                         (92.0, 92.0, 308.0, 208.0))

    def test_crop_window_fits_rejette_hors_cadre(self):
        """La piste de base reste en composition quand le cadrage sort du cadre."""
        engine = load_prproj2ffmpeg()
        item = engine.TrackItem()
        item.start, item.end = 0.0, 10.0
        item.scale_w = item.scale_h = 180.0
        item.anchor = (0.443333333, 0.0)
        item.position = types.SimpleNamespace(
            kind='point', static=(0.49845, 60.805151 / 1920), kfs=[])
        self.assertFalse(engine.crop_window_fits(item, 1080, 1920, 1920, 1080))

    # --- Plage utilisée / cache alpha (phases 1 et 2) ---

    def test_alpha_cap_limite_la_plage_utilisee(self):
        engine = load_prproj2ffmpeg()
        source = self.base_folder / "media.mov"
        source.write_bytes(b"x" * 8)
        engine._alpha_limits.clear()
        engine._duration_cache.clear()
        key = os.path.abspath(str(source))
        with patch.object(engine, "probe_duration", return_value=300.0):
            engine._alpha_limits[key] = 61.44
            self.assertEqual(engine._alpha_cap(str(source)), 61.5)
            # Fin utilisée au-delà de la durée : tout le fichier, sans -t
            engine._alpha_limits[key] = 299.95
            self.assertIsNone(engine._alpha_cap(str(source)))
        self.assertIsNone(engine._alpha_cap(str(self.base_folder / "absent.mov")))

    def test_note_alpha_end_prend_le_maximum(self):
        engine = load_prproj2ffmpeg()
        engine._alpha_limits.clear()
        with patch.object(engine, "resolve_media", return_value="/tmp/media.mov"):
            engine._note_alpha_end("source", 10.0, [])
            engine._note_alpha_end("source", 25.0, [])
            engine._note_alpha_end("source", 15.0, [])
        self.assertEqual(engine._alpha_limits[os.path.abspath("/tmp/media.mov")], 25.0)

    def test_alpha_intermediate_limite_la_conversion(self):
        engine = load_prproj2ffmpeg()
        cache_root = self.base_folder / "cache"
        cache_root.mkdir()
        source = self.base_folder / "source.mov"
        source.write_bytes(b"prores4444" * 4096)
        engine.CACHE_ROOT = str(cache_root)
        engine._alpha_cache.clear()
        engine._alpha_limits.clear()
        engine._duration_cache.clear()
        engine._probe_cache.clear()
        engine._content_key_cache.clear()
        engine._alpha_limits[os.path.abspath(str(source))] = 61.44

        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            if cmd[0] == 'ffmpeg':
                Path(cmd[-1]).write_bytes(b"qtrle")
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

        with patch.dict(os.environ, {"PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX": "15"}), \
             patch.object(engine, "probe_video", return_value=("prores", "yuva444p12le", 1080, 1920)), \
             patch.object(engine, "probe_duration", return_value=300.0), \
             patch.object(engine.subprocess, "run", side_effect=fake_run):
            effective = engine.alpha_intermediate(str(source))

        self.assertTrue(effective.startswith(str(cache_root)))
        self.assertTrue(Path(effective).exists())
        self.assertEqual(len(commands), 1)
        self.assertIn("-t", commands[0])
        self.assertEqual(commands[0][commands[0].index("-t") + 1], "61.500000")
        self.assertEqual(commands[0][commands[0].index("-c:v") + 1], "qtrle")

    def test_alpha_bbox_limite_et_met_en_cache_negatif(self):
        engine = load_prproj2ffmpeg()
        cache_root = self.base_folder / "cache"
        cache_root.mkdir()
        source = self.base_folder / "source.mov"
        source.write_bytes(b"alpha" * 4096)
        engine.CACHE_ROOT = str(cache_root)
        engine._bbox_cache.clear()
        engine._alpha_limits.clear()
        engine._duration_cache.clear()
        engine._probe_cache.clear()
        engine._content_key_cache.clear()
        engine._alpha_limits[os.path.abspath(str(source))] = 61.44

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

        with patch.dict(os.environ, {"PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX": "15"}), \
             patch.object(engine, "probe_video", return_value=("prores", "yuva444p12le", 100, 100)), \
             patch.object(engine, "probe_duration", return_value=300.0), \
             patch.object(engine.subprocess, "run", side_effect=fake_run):
            self.assertIsNone(engine.alpha_bbox(str(source)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][calls[0].index("-t") + 1], "61.500000")

        # Liste vide = cache négatif valide : le second passage ne relance pas ffmpeg.
        engine._bbox_cache.clear()
        with patch.object(engine, "probe_video", return_value=("prores", "yuva444p12le", 100, 100)), \
             patch.object(engine, "probe_duration", return_value=300.0), \
             patch.object(engine.subprocess, "run", side_effect=AssertionError("scan relancé")):
            self.assertEqual(engine.alpha_bbox(str(source)), [])

    def test_cache_pleine_duree_reutilise_pour_une_plage_bornee(self):
        engine = load_prproj2ffmpeg()
        cache_root = self.base_folder / "cache"
        cache_root.mkdir()
        source = self.base_folder / "source.mov"
        source.write_bytes(b"prores4444" * 1024)
        engine.CACHE_ROOT = str(cache_root)
        engine._alpha_cache.clear()
        engine._bbox_cache.clear()
        engine._alpha_limits.clear()
        engine._duration_cache.clear()
        engine._probe_cache.clear()
        engine._content_key_cache.clear()
        engine._alpha_limits[os.path.abspath(str(source))] = 61.44

        # Ancien cache pleine durée (clé chemin + mtime), comme sur la machine réelle.
        full_mov = cache_root / (engine._cache_key(str(source), "qtrle8") + ".mov")
        full_mov.write_bytes(b"full")
        bbox_file = cache_root / (engine._cache_key(str(full_mov), "bbox2") + ".bbox.json")
        bbox_file.write_text("[1, 2, 3, 4]")

        with patch.object(engine, "probe_video", return_value=("prores", "yuva444p12le", 100, 100)), \
             patch.object(engine, "probe_duration", return_value=300.0), \
             patch.object(engine.subprocess, "run", side_effect=AssertionError("aucun ffmpeg attendu")):
            effective = engine.alpha_intermediate(str(source))
            self.assertEqual(effective, str(full_mov))
            self.assertEqual(engine.alpha_bbox(effective), [1, 2, 3, 4])

    def test_alpha_reutilise_intermediaire_plus_long(self):
        engine = load_prproj2ffmpeg()
        cache_root = self.base_folder / "cache"
        cache_root.mkdir()
        source = self.base_folder / "source.mov"
        source.write_bytes(b"prores4444" * 1024)
        engine.CACHE_ROOT = str(cache_root)
        engine._alpha_cache.clear()
        engine._alpha_limits.clear()
        engine._duration_cache.clear()
        engine._probe_cache.clear()
        engine._content_key_cache.clear()
        engine._alpha_limits[os.path.abspath(str(source))] = 91.44

        longer = cache_root / "long.mov"
        longer.write_bytes(b"qtrle")
        meta = {
            "file": longer.name,
            "source": os.path.abspath(str(source)),
            "content": engine._content_signature(str(source)),
            "cap": 102.0,
            "duration": 102.0,
        }
        (cache_root / (longer.name + ".qtrle.json")).write_text(json.dumps(meta))

        with patch.dict(os.environ, {"PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX": "15"}), \
             patch.object(engine, "probe_video", return_value=("prores", "yuva444p12le", 100, 100)), \
             patch.object(engine, "probe_duration", return_value=120.0), \
             patch.object(engine.subprocess, "run",
                          side_effect=AssertionError("aucune conversion attendue")):
            effective = engine.alpha_intermediate(str(source))

        self.assertEqual(effective, str(longer))
        self.assertEqual(engine._alpha_limits[os.path.abspath(str(longer))], 91.5)

    def test_alpha_convertit_pleine_duree_si_queue_courte(self):
        engine = load_prproj2ffmpeg()
        cache_root = self.base_folder / "cache"
        cache_root.mkdir()
        source = self.base_folder / "source.mov"
        source.write_bytes(b"prores4444" * 1024)
        engine.CACHE_ROOT = str(cache_root)
        engine._alpha_cache.clear()
        engine._alpha_limits.clear()
        engine._duration_cache.clear()
        engine._probe_cache.clear()
        engine._content_key_cache.clear()
        engine._alpha_limits[os.path.abspath(str(source))] = 91.44

        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            if cmd[0] == 'ffmpeg':
                Path(cmd[-1]).write_bytes(b"qtrle")
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

        with patch.dict(os.environ, {"PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX": "15"}), \
             patch.object(engine, "probe_video", return_value=("prores", "yuva444p12le", 100, 100)), \
             patch.object(engine, "probe_duration", return_value=100.0), \
             patch.object(engine.subprocess, "run", side_effect=fake_run):
            effective = engine.alpha_intermediate(str(source))
            self.assertEqual(len(commands), 1)
            self.assertNotIn("-t", commands[0])
            full_mov = Path(effective)
            self.assertTrue(full_mov.exists())
            meta = json.loads(Path(str(full_mov) + ".qtrle.json").read_text())
            self.assertIsNone(meta["cap"])
            self.assertEqual(meta["duration"], 100.0)

            # Une plage plus longue réutilise l'intermédiaire pleine durée.
            engine._alpha_cache.clear()
            engine._alpha_limits[os.path.abspath(str(source))] = 99.44
            effective2 = engine.alpha_intermediate(str(source))
            self.assertEqual(effective2, effective)
            self.assertEqual(len(commands), 1)

        self.assertEqual(engine._alpha_limits[os.path.abspath(str(full_mov))], 99.5)

    def test_alpha_full_tail_max_env(self):
        engine = load_prproj2ffmpeg()
        with patch.dict(os.environ, {"PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX": "60"}):
            self.assertEqual(engine._alpha_full_tail_max(), 60.0)
        with patch.dict(os.environ, {"PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX": "invalide"}):
            self.assertEqual(engine._alpha_full_tail_max(), engine._ALPHA_FULL_TAIL_MAX)
        with patch.dict(os.environ, {"PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX": ""}):
            self.assertEqual(engine._alpha_full_tail_max(), engine._ALPHA_FULL_TAIL_MAX)

    def test_purge_supprime_sidecar_orphelin(self):
        engine = load_prproj2ffmpeg()
        cache_root = self.base_folder / "cache"
        cache_root.mkdir()
        engine.CACHE_ROOT = str(cache_root)
        orphan = cache_root / "orphelin.mov.qtrle.json"
        orphan.write_text("{}")
        kept_mov = cache_root / "garde.mov"
        kept_mov.write_bytes(b"x")
        kept_meta = cache_root / "garde.mov.qtrle.json"
        kept_meta.write_text(json.dumps({"file": "garde.mov"}))

        with patch.dict(os.environ, {"PRPROJ2FFMPEG_CACHE_MAX_MB": "1"}):
            engine._prune_cache()

        self.assertFalse(orphan.exists())
        self.assertTrue(kept_meta.exists())

    def test_cache_keys_reconnait_une_copie(self):
        engine = load_prproj2ffmpeg()
        first = self.base_folder / "first.mov"
        second = self.base_folder / "second.mov"
        first.write_bytes(b"identical-media" * 1024)
        second.write_bytes(b"identical-media" * 1024)
        os.utime(second, (time.time() - 500, time.time() - 500))

        keys_first = engine._cache_keys(str(first), "qtrle8")
        keys_second = engine._cache_keys(str(second), "qtrle8")

        self.assertEqual(len(keys_first), 2)
        self.assertEqual(len(keys_second), 2)
        self.assertNotEqual(keys_first[0], keys_second[0])   # chemin + mtime distincts
        self.assertEqual(keys_first[-1], keys_second[-1])    # empreinte de contenu identique

    def test_purge_cache_lru(self):
        engine = load_prproj2ffmpeg()
        cache_root = self.base_folder / "cache"
        cache_root.mkdir()
        engine.CACHE_ROOT = str(cache_root)
        old = cache_root / "ancien.mov"
        new = cache_root / "recent.mov"
        old.write_bytes(b"o" * 800_000)
        new.write_bytes(b"n" * 800_000)
        now = time.time()
        os.utime(old, (now - 1000, now - 1000))
        os.utime(new, (now, now))

        with patch.dict(os.environ, {"PRPROJ2FFMPEG_CACHE_MAX_MB": "1"}):
            engine._prune_cache()

        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_probe_ffmpeg_avec_nvenc(self):
        fake_ffmpeg = self.base_folder / "fake-ffmpeg"
        fake_ffmpeg.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if '-version' in sys.argv:\n"
            "    print('ffmpeg version 6.1.1-fake Copyright (c) 2000-2023 the FFmpeg developers')\n"
            "elif '-encoders' in sys.argv:\n"
            "    print(' V..... h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)')\n"
            "    print(' V..... libx264              H.264 / AVC / MPEG-4 AVC (codec h264)')\n"
            "elif any('nullsrc' in arg for arg in sys.argv):\n"
            "    print('', end='')\n"
            "else:\n"
            "    sys.exit(2)\n",
            encoding="utf-8",
        )
        fake_ffmpeg.chmod(0o755)

        with patch("prproj_engine.resolve_ffmpeg_binaries", return_value=(str(fake_ffmpeg), str(fake_ffmpeg))):
            probe = probe_ffmpeg()

        self.assertTrue(probe.ready)
        self.assertTrue(probe.has_nvenc)
        self.assertEqual(probe.version, "6.1.1-fake")
        self.assertIn("h264_nvenc disponible", probe.describe())

    def test_probe_ffmpeg_sans_nvenc_et_absent(self):
        fake_ffmpeg = self.base_folder / "fake-ffmpeg-no-nvenc"
        fake_ffmpeg.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if '-version' in sys.argv:\n"
            "    print('ffmpeg version 5.0-only-libx264')\n"
            "else:\n"
            "    print(' V..... libx264              H.264 / AVC / MPEG-4 AVC (codec h264)')\n",
            encoding="utf-8",
        )
        fake_ffmpeg.chmod(0o755)

        with patch("prproj_engine.resolve_ffmpeg_binaries", return_value=(str(fake_ffmpeg), str(fake_ffmpeg))):
            probe = probe_ffmpeg()
        self.assertTrue(probe.ready)
        self.assertFalse(probe.has_nvenc)
        self.assertIn("repli libx264", probe.describe())

        with patch("prproj_engine.resolve_ffmpeg_binaries", return_value=(None, None)):
            absent = probe_ffmpeg()
        self.assertFalse(absent.ready)

    def test_probe_ffmpeg_nvenc_runtime(self):
        """Encodeur compilé mais session NVENC indisponible : repli annoncé à l'UI."""
        fake_ffmpeg = self.base_folder / "fake-ffmpeg-nvenc-ko"
        fake_ffmpeg.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if '-version' in sys.argv:\n"
            "    print('ffmpeg version 6.1.1-fake')\n"
            "elif '-encoders' in sys.argv:\n"
            "    print(' V..... h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)')\n"
            "elif any('nullsrc' in arg for arg in sys.argv):\n"
            "    print('Cannot load libnvidia-encode.so.1', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "else:\n"
            "    sys.exit(2)\n",
            encoding="utf-8",
        )
        fake_ffmpeg.chmod(0o755)

        with patch("prproj_engine.resolve_ffmpeg_binaries",
                   return_value=(str(fake_ffmpeg), str(fake_ffmpeg))):
            probe = probe_ffmpeg()

        self.assertTrue(probe.ready)
        self.assertFalse(probe.has_nvenc)
        self.assertIn("repli libx264", probe.describe())

    def test_read_project_info(self):
        fake_script = self.base_folder / "fake-prproj2ffmpeg.py"
        fake_script.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "print(json.dumps({'sequence': 'Ma Séquence', 'duration': 12.5, 'fps': 25.0,\n"
            "                  'sequence_size': [1080, 1920]}))\n",
            encoding="utf-8",
        )
        info = read_project_info(Path("projet.prproj"), fake_script)
        self.assertIsNotNone(info)
        self.assertEqual(info.sequence_name, "Ma Séquence")
        self.assertEqual(info.duration_sec, 12.5)
        self.assertEqual(info.fps, 25.0)
        self.assertEqual((info.width, info.height), (1080, 1920))

        # Script absent : repli silencieux (jamais bloquant)
        self.assertIsNone(read_project_info(Path("projet.prproj"), self.base_folder / "absent.py"))

        # Le script échoue (média introuvable…) : None également
        failing_script = self.base_folder / "failing-prproj2ffmpeg.py"
        failing_script.write_text(
            "import sys\nprint('error: media not found for X (use --base)', file=sys.stderr)\nsys.exit(1)\n",
            encoding="utf-8",
        )
        self.assertIsNone(read_project_info(Path("projet.prproj"), failing_script))

    # --- Statuts et déplacement ---

    def test_matchable_status(self):
        for status in (STATUS_SUBMITTED, STATUS_ENCODING, STATUS_DONE, STATUS_DONE_WITH_WARNINGS):
            self.assertTrue(is_matchable_status(status), status)
        for status in (STATUS_MOVED, STATUS_FAILED, STATUS_ABORTED, STATUS_SUPERSEDED):
            self.assertFalse(is_matchable_status(status), status)

    def test_move_to_final_verified_meme_systeme(self):
        source = self.base_folder / "source.mp4"
        source.write_bytes(b"x" * 128)
        destination = self.base_folder / "dest.mp4"

        move_to_final_verified(source, destination)

        self.assertFalse(source.exists())
        self.assertTrue(destination.exists())
        self.assertEqual(destination.stat().st_size, 128)

    def test_move_to_final_verified_copie_inter_systemes(self):
        source = self.base_folder / "source.mp4"
        source.write_bytes(b"y" * 256)
        destination = self.base_folder / "dest.mp4"

        with patch("monitor_exports_and_move._is_same_filesystem", return_value=False):
            move_to_final_verified(source, destination)

        self.assertFalse(source.exists())
        self.assertEqual(destination.read_bytes(), b"y" * 256)
        # Aucun fichier .part résiduel
        self.assertEqual(list(self.base_folder.glob(".dest.mp4.part")), [])

    def test_move_to_final_verified_echec_nettoie_le_part(self):
        source = self.base_folder / "source.mp4"
        source.write_bytes(b"z" * 64)
        destination = self.base_folder / "dest.mp4"

        with patch("monitor_exports_and_move._is_same_filesystem", return_value=False), \
             patch("monitor_exports_and_move.shutil.copy2", side_effect=OSError("disque plein")):
            with self.assertRaises(OSError):
                move_to_final_verified(source, destination)

        self.assertTrue(source.exists())
        self.assertEqual(list(self.base_folder.glob(".dest.mp4.part")), [])

    def test_is_same_filesystem(self):
        file_a = self.base_folder / "a.bin"
        file_a.write_bytes(b"a")
        self.assertTrue(_is_same_filesystem(file_a, self.base_folder))
        self.assertTrue(_is_same_filesystem(file_a, file_a))

    def test_publication_encodeur_ignore_la_stabilisation(self):
        """Un MP4 publié par l'encodeur (statut EXPORT_DONE*) est complet : la stabilisation est sautée."""
        from db_helper import add_submission, init_db
        from monitor_exports_and_move import ExportHandler

        db_path = self.base_folder / "jobs.db"
        init_db(db_path)
        add_submission(db_path, "2026-10-10 16:49:33",
                       "/mnt/cache/117 Camille/projets/x.prproj",
                       "x_20261010_164933_7675c7.prproj",
                       "M6 - LMCR - La potée lorraine_9x16",
                       "20261010_164933_7675c7", status=STATUS_DONE)
        add_submission(db_path, "2026-10-10 16:49:34", "/x.prproj", "y_20261010_164933_aaaaaa.prproj",
                       "Test", "20261010_164933_aaaaaa", status=STATUS_ENCODING)

        handler = ExportHandler()
        try:
            with patch("monitor_exports_and_move.JOBS_DB_FILE", db_path):
                self.assertTrue(handler._is_published_by_encoder(Path("x_20261010_164933_7675c7.mp4")))
                # Job encore en cours d'encodage : chemin normal (stabilisation conservée)
                self.assertFalse(handler._is_published_by_encoder(Path("y_20261010_164933_aaaaaa.mp4")))
            # Fichier sans job_id (dépôt manuel) : chemin normal également
            self.assertFalse(handler._is_published_by_encoder(Path("export_manuel.mp4")))
        finally:
            handler.executor.shutdown(wait=False)

    def test_monitor_retire_suffixe_assist_du_nom_final(self):
        """Même une ancienne ligne dont ExpectedExportName contient _assist livre X.mp4."""
        from db_helper import add_submission, init_db
        from monitor_exports_and_move import ExportHandler

        db_path = self.base_folder / "jobs.db"
        init_db(db_path)
        project_dir = self.base_folder / "project1"
        projets_dir = project_dir / "projets"
        projets_dir.mkdir(parents=True, exist_ok=True)
        out_dir = self.base_folder / "WATCH_OUT"
        out_dir.mkdir(parents=True, exist_ok=True)

        job_id = "20261013_120000_aaaaaa"
        exported = out_dir / f"X_9x16_assist_{job_id}.mp4"
        exported.write_bytes(b"x" * 32)
        add_submission(db_path, "2026-10-13 12:00:00",
                       str(projets_dir / "X_9x16_assist.prproj"),
                       f"X_9x16_assist_{job_id}.prproj",
                       "X_9x16_assist", job_id, status=STATUS_DONE)

        handler = ExportHandler()
        try:
            with patch("monitor_exports_and_move.JOBS_DB_FILE", db_path), \
                 patch("monitor_exports_and_move.MONITOR_LOG_FILE", self.base_folder / "monitor_log.txt"), \
                 patch("monitor_exports_and_move.resolve_project_root", return_value=project_dir):
                handler.process_file(str(exported))
        finally:
            handler.executor.shutdown(wait=False)

        self.assertFalse(exported.exists())
        self.assertTrue((project_dir / "X_9x16.mp4").exists())

    def test_cleanup_output_preserve_quarantaine_et_fichiers_recents(self):
        """« Nettoyer les sorties » ne supprime que les MP4 publiés et anciens."""
        out = self.base_folder / "WATCH_OUT"
        (out / "quarantine").mkdir(parents=True)
        (out / ".staging").mkdir(parents=True)

        vieux = out / "vieux.mp4"
        vieux.write_bytes(b"v" * 10)
        recent = out / "recent.mp4"
        recent.write_bytes(b"r" * 10)
        quarantaine = out / "quarantine" / "inconnu.mp4"
        quarantaine.write_bytes(b"q" * 10)
        staging = out / ".staging" / "en-cours.mp4"
        staging.write_bytes(b"s" * 10)

        # Le fichier « vieux » dépasse le délai de protection (les autres non)
        old_time = time.time() - (CLEAN_OUTPUT_GUARD_SECONDS + 10)
        os.utime(vieux, (old_time, old_time))

        with patch("submit_to_watchfolder.SUBMISSION_TEXT_LOG_FILE", self.base_folder / "log.txt"):
            stw.cleanup_output_folder(out)

        self.assertFalse(vieux.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(quarantaine.exists())
        self.assertTrue(staging.exists())

    # --- Correctifs d'audit (2026-10-23) ---

    def test_project_parse_en_memoire(self):
        """Le .prproj gzip est décompressé et parsé en mémoire, sans XML temporaire."""
        engine = load_prproj2ffmpeg()
        raw = FIXTURE_PRPROJ.read_bytes()
        self.assertEqual(raw[:2], b'\x1f\x8b')
        tmp_name = '.prproj2ffmpeg-tmp.xml'
        self.assertFalse((FIXTURES / tmp_name).exists())

        parsed = engine.Project(str(FIXTURE_PRPROJ), xml_bytes=gzip.decompress(raw))
        self.assertTrue(parsed.sequences)
        self.assertFalse((FIXTURES / tmp_name).exists())

        # XML brut (déjà décompressé) accepté par le même chemin.
        raw_xml = gzip.decompress(raw)
        parsed_raw = engine.Project(str(FIXTURE_PRPROJ), xml_bytes=raw_xml)
        self.assertEqual([s['name'] for s in parsed_raw.sequences],
                         [s['name'] for s in parsed.sequences])

    def test_resolve_media_profondeur_et_cache(self):
        engine = load_prproj2ffmpeg()
        engine._resolve_cache.clear()
        deep = self.base_folder / "134 Camille" / "rush"
        deep.mkdir(parents=True)
        target = deep / "video.mp4"
        target.write_bytes(b"x")

        win_path = r"F:\Clients\2026\134 Camille\rush\video.mp4"
        bases = [str(self.base_folder)]
        self.assertEqual(engine.resolve_media(win_path, bases), str(target))

        # Le second appel est servi par le cache (aucun accès disque).
        with patch.object(engine.os.path, "isfile",
                          side_effect=AssertionError("cache de résolution ignoré")):
            self.assertEqual(engine.resolve_media(win_path, bases), str(target))

        # Cache négatif : un chemin absent n'est pas retesté.
        absent = r"F:\Clients\2026\absent.mp4"
        self.assertIsNone(engine.resolve_media(absent, bases))
        with patch.object(engine.os.path, "isfile",
                          side_effect=AssertionError("cache négatif ignoré")):
            self.assertIsNone(engine.resolve_media(absent, bases))

    def test_merge_loop_groups_duree_pleine(self):
        engine = load_prproj2ffmpeg()
        with patch.object(engine, "resolve_media", return_value="/tmp/clip.mp4"), \
             patch.object(engine, "probe_duration", return_value=10.0):
            first = self._clip_item(engine, 0.0, 10.0)
            second = self._clip_item(engine, 10.0, 20.0)
            groups = engine.merge_loop_groups([first, second], [])
            self.assertEqual(len(groups), 1)
            self.assertEqual(len(groups[0][0]), 2)
            self.assertAlmostEqual(groups[0][2], 20.0)

            # Extrait partiel (out < durée réelle) : -stream_loop lirait la suite.
            partial_a = self._clip_item(engine, 0.0, 5.0, out_s=5.0)
            partial_b = self._clip_item(engine, 5.0, 10.0, out_s=5.0)
            self.assertEqual(len(engine.merge_loop_groups([partial_a, partial_b], [])), 2)

            # Masque, flou ou keyframes : jamais fusionnés.
            masked = self._clip_item(engine, 10.0, 20.0)
            masked.masks = [engine.Mask()]
            self.assertEqual(len(engine.merge_loop_groups([first, masked], [])), 2)

            blurred = self._clip_item(engine, 10.0, 20.0)
            blurred.blur = engine.BlurFx()
            self.assertEqual(len(engine.merge_loop_groups([first, blurred], [])), 2)

            animated = self._clip_item(engine, 10.0, 20.0)
            animated.position = types.SimpleNamespace(
                kind='point', static=(0.5, 0.5),
                kfs=[engine.KeyFrame(1.0, 0.5, 0.5)])
            self.assertEqual(len(engine.merge_loop_groups([first, animated], [])), 2)

            still = self._clip_item(engine, 0.0, 10.0, still=True)
            self.assertEqual(len(engine.merge_loop_groups([still], [])), 1)

    def test_pull_table_collisions(self):
        engine = load_prproj2ffmpeg()

        # Deux usages vidéo de la même clé -> deux pulls.
        table = engine.PullTable()
        self.assertEqual(table.get("/m.mp4", 1.0, 2.0, stream='v'), 0)
        self.assertEqual(table.get("/m.mp4", 1.0, 2.0, stream='v'), 1)
        self.assertEqual(len(table.pulls), 2)

        # Un usage vidéo + audio partagent la même pull (pads distincts).
        table = engine.PullTable()
        self.assertEqual(table.get("/m.mp4", 1.0, 2.0, stream='v'), 0)
        self.assertEqual(table.get("/m.mp4", 1.0, 2.0, stream='a'), 0)
        self.assertEqual(len(table.pulls), 1)

        # Deux boucles -> deux pulls, toujours bouclées.
        table = engine.PullTable()
        self.assertEqual(table.get("/m.mp4", 0.0, 5.0, loop_span=5.0, stream='v'), 0)
        self.assertEqual(table.get("/m.mp4", 0.0, 5.0, loop_span=5.0, stream='v'), 1)
        self.assertEqual(len(table.pulls), 2)
        self.assertTrue(all(pull[3] for pull in table.pulls))
        self.assertEqual(table.pulls[1][1:3], (0.0, 5.0))

        # unique : pulls distincts même pour des bornes identiques.
        table = engine.PullTable()
        self.assertEqual(table.get("/m.mp4", 1.0, 2.0, unique=True, stream='v'), 0)
        self.assertEqual(table.get("/m.mp4", 1.0, 2.0, unique=True, stream='v'), 1)
        self.assertEqual(len(table.pulls), 2)

    def test_blur_chain_dimensions(self):
        engine = load_prproj2ffmpeg()
        blur = engine.BlurFx()

        blur.amount = 10.0   # sigma 30 -> décimation 1/4
        chain = engine.blur_chain(blur, 1119, 673)
        self.assertIn("trunc(iw/8)*2", chain)
        self.assertIn("trunc(ih/8)*2", chain)
        self.assertIn("gblur=sigma=7.500", chain)
        self.assertTrue(chain.endswith("scale=1119:673:flags=bilinear"), chain)

        blur.amount = 3.0    # sigma 9 -> décimation 1/2
        chain = engine.blur_chain(blur, 444, 267)
        self.assertIn("trunc(iw/4)*2", chain)
        self.assertIn("trunc(ih/4)*2", chain)
        self.assertTrue(chain.endswith("scale=444:267:flags=bilinear"), chain)

        blur.amount = 1.0    # sigma 3 -> gblur direct
        chain = engine.blur_chain(blur, 444, 267)
        self.assertNotIn("scale=", chain)
        self.assertTrue(chain.startswith("gblur=sigma=3.000"))

        # Dimensions inconnues (zoom animé) : jamais de décimation/restauration.
        blur.amount = 10.0
        self.assertNotIn("scale=", engine.blur_chain(blur))

    def test_uniform_scale_axes(self):
        engine = load_prproj2ffmpeg()
        item = engine.TrackItem()
        item.start, item.end = 0.0, 2.0
        item.scale_w = item.scale_h = 100.0
        kfs = [engine.KeyFrame(0.0, 100.0, 0.0), engine.KeyFrame(2.0, 200.0, 0.0)]
        item.scale = types.SimpleNamespace(kind='scalar', static=100.0, kfs=kfs)
        item.scale2 = types.SimpleNamespace(kind='scalar', static=100.0, kfs=[])
        item.uniform_scale = True

        w_expr, h_expr, anim = engine.scale_expr_pair(item, 1920, 1080, 0.0, 2.0)
        self.assertTrue(anim)
        self.assertIn("*t", w_expr)
        self.assertIn("*t", h_expr)
        self.assertNotEqual(w_expr, h_expr)  # src_w/100 vs src_h/100

        item.uniform_scale = False
        _w_expr, h_expr, anim = engine.scale_expr_pair(item, 1920, 1080, 0.0, 2.0)
        self.assertTrue(anim)
        self.assertEqual(h_expr, "1080.000000")

    def test_transform_filters_rotation_rgba(self):
        engine = load_prproj2ffmpeg()
        item = engine.TrackItem()
        item.scale_w = item.scale_h = 100.0
        item.rotation = types.SimpleNamespace(kfs=[], static=15.0)

        tf = engine.transform_filters(item, 1920, 1080, 0.0, 10.0)
        self.assertIn("format=rgba,rotate=a=", tf)
        self.assertLess(tf.index("format=rgba"), tf.index("rotate="))
        self.assertIn(":c=none", tf)

        # Sans rotation : aucun format=rgba ajouté par le transform.
        item.rotation = None
        item.scale_w = item.scale_h = 110.0
        tf = engine.transform_filters(item, 1920, 1080, 0.0, 10.0)
        self.assertNotIn("format=rgba", tf)
        self.assertIn("scale=", tf)

    def test_axis_expr_equilibre(self):
        engine = load_prproj2ffmpeg()
        intervals = [(float(i), float(i + 1), (float(i), 0.0, 0.0, 0.0))
                     for i in range(16)]
        expr = engine.axis_expr(intervals, 1.0, 0.0, -1e12, 1e12)

        python_expr = expr.replace("if(", "iff(")
        scope = {"iff": lambda condition, left, right: left if condition else right,
                 "lt": lambda a, b: a < b}

        def value(t):
            return eval(python_expr, dict(scope, t=t))

        for i in range(16):
            self.assertEqual(value(i + 0.5), float(i), (i, expr))
        for i in range(15):
            # t == borne bascule à droite, comme la chaîne linéaire d'origine.
            self.assertEqual(value(i + 1.0), float(i + 1))
        self.assertEqual(value(16.0), 15.0)

        # Profondeur de l'arbre : ceil(log2(16)) = 4 (au lieu de 15).
        stack, max_depth = [], 0
        for index, char in enumerate(expr):
            if char == '(':
                is_if = expr[max(0, index - 2):index] == 'if'
                stack.append(is_if)
                max_depth = max(max_depth, sum(stack))
            elif char == ')' and stack:
                stack.pop()
        self.assertEqual(max_depth, 4)

        self.assertEqual(engine.axis_expr([], 1.0, 0.0, 0.0, 1.0), "0.0")

    def test_bbox_cache_write_atomique(self):
        engine = load_prproj2ffmpeg()
        cache_root = self.base_folder / "cache"
        cache_root.mkdir()
        engine.CACHE_ROOT = str(cache_root)
        source = self.base_folder / "source.mov"
        source.write_bytes(b"media")

        engine._bbox_cache_write(str(source), "test", [1, 2, 3, 4])
        self.assertEqual(list(cache_root.glob("*.tmp")), [])
        written = list(cache_root.glob("*.bbox.json"))
        self.assertEqual(len(written), 1)
        self.assertEqual(json.loads(written[0].read_text()), [1, 2, 3, 4])

        # Échec du remplacement : le .tmp est supprimé, aucun fichier corrompu.
        with patch.object(engine.os, "replace", side_effect=OSError("disque plein")):
            engine._bbox_cache_write(str(source), "test2", [5, 6, 7, 8])
        self.assertEqual(list(cache_root.glob("*.tmp")), [])

    def test_prune_cache_protege_les_pulls(self):
        engine = load_prproj2ffmpeg()
        cache_root = self.base_folder / "cache"
        cache_root.mkdir()
        engine.CACHE_ROOT = str(cache_root)
        old = cache_root / "ancien.mov"
        new = cache_root / "recent.mov"
        old.write_bytes(b"o" * 800_000)
        new.write_bytes(b"n" * 800_000)
        now = time.time()
        os.utime(old, (now - 1000, now - 1000))
        os.utime(new, (now, now))
        orphan = cache_root / "analyse.bbox.json.tmp"
        orphan.write_bytes(b"tmp")
        os.utime(orphan, (now - 90000, now - 90000))

        with patch.dict(os.environ, {"PRPROJ2FFMPEG_CACHE_MAX_MB": "1"}):
            engine._prune_cache(protect=[str(old)])

        self.assertTrue(old.exists())
        self.assertFalse(new.exists())
        self.assertFalse(orphan.exists())

    def test_ellipse_geometry_hors_cadre(self):
        engine = load_prproj2ffmpeg()
        mask = engine.Mask()
        mask.shape = 'ellipse'
        mask.bbox = (0.8, 0.8, 1.2, 1.2)
        mask.expansion = 0.0

        cx, cy, rx, ry = engine._ellipse_geometry(mask, 100, 100)
        self.assertAlmostEqual(cx, 100.0)
        self.assertAlmostEqual(cy, 100.0)
        self.assertAlmostEqual(rx, 20.0)
        self.assertAlmostEqual(ry, 20.0)

        chain = engine._single_mask_chain(mask, 100, 100, 25.0, 0.04, "[m]")
        self.assertIn("X-100.000", chain)
        self.assertIn("20.000", chain)
        # Le centre n'a pas été borné à 90 (déformation de l'ellipse).
        self.assertNotIn("X-90.000", chain)

    def test_item_needs_canvas_opacite(self):
        engine = load_prproj2ffmpeg()
        item = engine.TrackItem()
        item.clip = engine.Clip()
        item.clip.source_path = "video.mp4"
        item.scale_w = item.scale_h = 100.0

        item.opacity = types.SimpleNamespace(static=50.0, kfs=[])
        self.assertTrue(engine.item_needs_canvas(item))
        item.opacity = types.SimpleNamespace(static=100.0, kfs=[])
        self.assertFalse(engine.item_needs_canvas(item))
        item.opacity = None
        self.assertFalse(engine.item_needs_canvas(item))

    def test_select_encoder(self):
        engine = load_prproj2ffmpeg()
        no_nvenc = subprocess.CompletedProcess([], 0, stdout=(
            " V..... libx264              H.264 / AVC / MPEG-4 AVC (codec h264)\n"), stderr='')
        with_nvenc = subprocess.CompletedProcess([], 0, stdout=(
            " V..... h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)\n"
            " V..... libx264              H.264 / AVC / MPEG-4 AVC (codec h264)\n"), stderr='')

        with patch.object(engine.subprocess, "run", return_value=no_nvenc):
            self.assertEqual(engine._select_encoder(), 'libx264')

        with patch.object(engine.subprocess, "run", return_value=with_nvenc), \
             patch.object(engine, "_encoder_works", return_value=True):
            self.assertEqual(engine._select_encoder(), 'h264_nvenc')

        stderr = io.StringIO()
        with patch.object(engine.subprocess, "run", return_value=with_nvenc), \
             patch.object(engine, "_encoder_works", return_value=False), \
             redirect_stderr(stderr):
            self.assertEqual(engine._select_encoder(), 'libx264')
        self.assertIn("[encoder] h264_nvenc indisponible", stderr.getvalue())
        self.assertIn("repli libx264", stderr.getvalue())

        # Encodeur forcé : jamais replié, même si NVENC échoue.
        with patch.object(engine.subprocess, "run",
                          side_effect=AssertionError("aucune détection attendue")):
            self.assertEqual(engine._select_encoder('h264_nvenc'), 'h264_nvenc')

    def test_read_components_param_anime(self):
        engine = load_prproj2ffmpeg()

        # Paramètre animé sans StartKeyframe : static None mais keyframes présentes.
        scalar = ET.fromstring(
            "<ScalarComponentParam><Keyframes>0,100,5,0,0,0,0,0</Keyframes>"
            "</ScalarComponentParam>")
        param = engine.Param(scalar)
        self.assertIsNone(param.static)
        self.assertTrue(param.kfs)

        point = ET.fromstring(
            "<PointComponentParam><Keyframes>0,0.5:0.5,5,0,0,0,0,0</Keyframes>"
            "</PointComponentParam>")
        param = engine.Param(point)
        self.assertIsNone(param.static)
        self.assertTrue(param.kfs)

        true_param = engine.Param(ET.fromstring(
            "<ScalarComponentParam><StartKeyframe>0,true,0,0</StartKeyframe>"
            "</ScalarComponentParam>"))
        false_param = engine.Param(ET.fromstring(
            "<ScalarComponentParam><StartKeyframe>0,false,0,0</StartKeyframe>"
            "</ScalarComponentParam>"))
        missing = engine.Param(ET.fromstring("<ScalarComponentParam/>"))
        self.assertIs(true_param.static_bool, True)
        self.assertIs(false_param.static_bool, False)
        self.assertIsNone(missing.static_bool)

    def test_collect_overlay_layers(self):
        engine = load_prproj2ffmpeg()
        v1_still = self._clip_item(engine, 0.0, 10.0, path="still.png", still=True)
        v1_video = self._clip_item(engine, 0.0, 10.0, path="base.mp4")
        v2_item = self._clip_item(engine, 0.0, 10.0, path="v2.png")
        v3_item = self._clip_item(engine, 0.0, 10.0, path="v3.png")
        no_source = self._clip_item(engine, 0.0, 10.0, path=None)
        tracks = ["V1", "V2", "V3"]
        mapping = {"V1": [v1_still, v1_video], "V2": [v2_item, no_source],
                   "V3": [v3_item]}
        parsed = types.SimpleNamespace(items_of_track=lambda track: mapping[track])

        layers = engine.collect_overlay_layers(parsed, tracks, [v1_still, v1_video])
        self.assertEqual(layers, [[v1_still], [v2_item], [v3_item]])

    def test_collect_audio_tracks(self):
        engine = load_prproj2ffmpeg()
        a1 = self._clip_item(engine, 0.0, 10.0, path="a1.wav")
        a2_valid = self._clip_item(engine, 0.0, 10.0, path="a2.wav")
        no_source = self._clip_item(engine, 0.0, 10.0, path=None)
        tracks = ["A1", "A2", "A3"]
        mapping = {"A1": [a1], "A2": [], "A3": [a2_valid, no_source]}
        parsed = types.SimpleNamespace(items_of_track=lambda track: mapping[track])

        collected = engine.collect_audio_tracks(parsed, tracks)
        self.assertEqual(collected, [[a1], [a2_valid]])


class TestFlaskSmoke(unittest.TestCase):
    """Smoke test de l'application Flask (contrats API, CSP, auth, statut de l'encodeur)."""

    @classmethod
    def setUpClass(cls):
        import app as app_module
        import config as config_module
        cls.app_module = app_module
        cls.config_module = config_module
        app_module.app.config['TESTING'] = True
        cls.client = app_module.app.test_client()

    def tearDown(self):
        self.client.get('/logout')

    def _csrf_token(self, path: str = '/login') -> str:
        page = self.client.get(path)
        match = re.search(r'name="csrf-token" content="([0-9a-f]+)"', page.get_data(as_text=True))
        self.assertIsNotNone(match, f"Jeton CSRF absent de '{path}'")
        return match.group(1)

    def _form_token(self, html: str) -> str:
        match = re.search(r'name="csrf_token" value="([0-9a-f]+)"', html)
        self.assertIsNotNone(match, "Jeton CSRF absent du formulaire de connexion")
        return match.group(1)

    def test_health_et_entetes_de_securite(self):
        response = self.client.get('/api/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'status': 'ok'})
        self.assertIn("default-src 'self'", response.headers.get('Content-Security-Policy', ''))
        self.assertEqual(response.headers.get('X-Frame-Options'), 'DENY')

    def test_index_protege_par_authentification(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers.get('Location', ''))

    def test_login_sans_csrf_refuse(self):
        response = self.client.post('/login', data={'username': 'admin', 'password': 'x'})
        self.assertEqual(response.status_code, 403)
        # La page réaffichée doit contenir un nouveau jeton exploitable sans rechargement manuel
        self.assertTrue(self._form_token(response.get_data(as_text=True)))

    def test_login_identifiants_invalides_puis_reessai(self):
        """Régression : après un échec d'identifiants, le formulaire doit rester utilisable (pas de 403 en boucle)."""
        token = self._form_token(self.client.get('/login').get_data(as_text=True))

        response = self.client.post('/login', data={
            'username': self.config_module.API_USER,
            'password': 'mauvais-mot-de-passe',
            'csrf_token': token,
        })
        self.assertEqual(response.status_code, 401)
        retry_token = self._form_token(response.get_data(as_text=True))
        self.assertTrue(retry_token, "le formulaire réaffiché doit porter un jeton CSRF valide")

        response = self.client.post('/login', data={
            'username': self.config_module.API_USER,
            'password': self.config_module.API_PASSWORD,
            'csrf_token': retry_token,
        })
        self.assertEqual(response.status_code, 302)
        self.client.get('/logout')

    def test_page_html_non_mise_en_cache(self):
        response = self.client.get('/login')
        self.assertEqual(response.headers.get('Cache-Control'), 'no-store')

    def test_login_puis_statut_encodeur(self):
        token = self._csrf_token()
        response = self.client.post('/login', data={
            'username': self.config_module.API_USER,
            'password': self.config_module.API_PASSWORD,
            'csrf_token': token,
        })
        self.assertEqual(response.status_code, 302)

        status = self.client.get('/api/status')
        self.assertEqual(status.status_code, 200)
        payload = status.get_json()
        self.assertIn('running', payload)
        self.assertIn('monitor_running', payload)
        self.assertIn('encoder', payload)
        for key in ('running', 'paused', 'queue_pending', 'current_job', 'encoder_available', 'encoder_engine'):
            self.assertIn(key, payload['encoder'])

        # L'interface déclare son icône (favicon servi par /static, autorisé par la CSP img-src 'self')
        page = self.client.get('/')
        self.assertIn('static/icon.svg', page.get_data(as_text=True))

        # Nettoyage de la session du client de test
        self.client.get('/logout')

    def test_start_valide_les_types(self):
        token = self._csrf_token()
        self.client.post('/login', data={
            'username': self.config_module.API_USER,
            'password': self.config_module.API_PASSWORD,
            'csrf_token': token,
        })
        # La rotation de session à la connexion invalide le jeton précédent : on en récupère un nouveau
        token = self._csrf_token('/')
        response = self.client.post('/api/start', json={'force': 'oui'}, headers={'X-CSRF-Token': token})
        self.assertEqual(response.status_code, 400)
        self.assertIn('booléen', response.get_json()['message'])
        self.client.get('/logout')


if __name__ == '__main__':
    unittest.main()
