#!/usr/bin/env python3
"""Benchmark reproductible d'un export prproj2ffmpeg.

Le script vendorisé est exécuté en sous-processus (jamais importé) : on lui demande
d'abord la commande ffmpeg via `--dry-run`, puis on l'exécute sur N secondes avec le
muxer `null` et `-benchmark`. Aucun fichier vidéo n'est écrit sauf avec `--out-dir`.

Exemples :
    python3 tools/benchmark_export.py --prproj projet.prproj --base /mnt/cache
    python3 tools/benchmark_export.py --prproj projet.prproj --seconds 30 --runs 5
    python3 tools/benchmark_export.py --prproj projet.prproj --out-dir /tmp/ab \
        --script /tmp/prproj2ffmpeg_baseline.py

Le temps du `--dry-run` (préparation alpha, analyse bbox) est mesuré séparément :
le cache est chaud pour les passages suivants.
"""
import argparse
import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_SCRIPT = Path(__file__).parent / "prproj-unpacker" / "prproj2ffmpeg.py"


def build_dry_run(script, prproj, bases, bitrate, encoder, sequence):
    cmd = [sys.executable, str(script), str(prproj)]
    for base in bases:
        cmd += ["--base", str(base)]
    cmd += ["--bitrate", str(bitrate), "--dry-run"]
    if encoder:
        cmd += ["--encoder", str(encoder)]
    if sequence:
        cmd += ["--sequence", str(sequence)]
    return cmd


def normalize_for_null(command, seconds):
    """Transforme la commande d'export en rendu null de `seconds` secondes."""
    cmd = list(command)
    while "-movflags" in cmd:
        i = cmd.index("-movflags")
        del cmd[i:i + 2]
    t_indexes = [i for i, token in enumerate(cmd) if token == "-t"]
    if not t_indexes:
        raise ValueError("la commande ffmpeg ne contient aucune option -t")
    cmd[t_indexes[-1] + 1] = str(seconds)
    cmd[-1:] = ["-f", "null", "-"]
    cmd.insert(1, "-benchmark")
    return cmd


def run_command(cmd, timeout):
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    elapsed = time.monotonic() - started
    return proc, elapsed


def last_bench(stderr):
    lines = [line.strip() for line in stderr.splitlines() if line.startswith("bench:")]
    for line in reversed(lines):
        if "rtime=" in line:
            return line
    return lines[-1] if lines else ""


def last_stats(stderr):
    for line in reversed(stderr.splitlines()):
        if "frame=" in line and "speed=" in line:
            return line.strip()
    return ""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prproj", required=True, type=Path, help="projet .prproj à mesurer")
    parser.add_argument("--base", action="append", default=[], type=Path,
                        help="racine média supplémentaire (répétable)")
    parser.add_argument("--seconds", type=float, default=15.0, help="durée mesurée (défaut 15)")
    parser.add_argument("--runs", type=int, default=3, help="nombre de passages (défaut 3)")
    parser.add_argument("--bitrate", default="10M", help="débit vidéo passé au script (défaut 10M)")
    parser.add_argument("--encoder", default=None, help="encodeur forcé (défaut : choix du script)")
    parser.add_argument("--sequence", default=None, help="nom de séquence à exporter")
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT,
                        help="script prproj2ffmpeg.py à mesurer (défaut : copie vendorisée)")
    parser.add_argument("--timeout", type=float, default=600.0, help="timeout d'un passage (secondes)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="écrit aussi un extrait MP4 de --seconds secondes dans ce dossier")
    parser.add_argument("--print-command", action="store_true", help="affiche la commande ffmpeg mesurée")
    args = parser.parse_args()

    if args.runs < 1:
        parser.error("--runs doit être >= 1")
    if not args.script.is_file():
        parser.error(f"script introuvable : {args.script}")

    dry_cmd = build_dry_run(args.script, args.prproj, args.base, args.bitrate,
                            args.encoder, args.sequence)
    started = time.monotonic()
    dry = subprocess.run(dry_cmd, capture_output=True, text=True, timeout=args.timeout)
    prep = time.monotonic() - started
    if dry.returncode != 0:
        print(dry.stderr.strip(), file=sys.stderr)
        return dry.returncode
    line = next((l for l in dry.stdout.splitlines() if l.startswith("ffmpeg ")), "")
    if not line:
        print("commande ffmpeg introuvable dans la sortie de --dry-run", file=sys.stderr)
        return 1
    command = shlex.split(line)
    null_cmd = normalize_for_null(command, args.seconds)
    if args.print_command:
        print("commande mesurée :", " ".join(shlex.quote(c) for c in null_cmd))

    print(f"préparation (dry-run) : {prep:.2f} s")
    timings = []
    for run in range(1, args.runs + 1):
        proc, elapsed = run_command(null_cmd, args.timeout)
        if proc.returncode != 0:
            print(f"passage {run} : échec (code {proc.returncode})", file=sys.stderr)
            print(proc.stderr.strip()[-2000:], file=sys.stderr)
            return proc.returncode
        timings.append(elapsed)
        print(f"passage {run} : {elapsed:.2f} s | {last_bench(proc.stderr)}")
        print(f"  {last_stats(proc.stderr)}")

    median = statistics.median(timings)
    print(f"\n{args.seconds:.1f} s de rendu : médiane {median:.2f} s "
          f"(min {min(timings):.2f}, max {max(timings):.2f}) "
          f"→ {args.seconds / median:.2f}x")

    if args.out_dir is not None:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        out_path = args.out_dir / f"benchmark_{args.seconds:g}s.mp4"
        t_indexes = [i for i, token in enumerate(command) if token == "-t"]
        command[t_indexes[-1] + 1] = str(args.seconds)
        command[-1] = str(out_path)
        proc, elapsed = run_command(command, args.timeout)
        if proc.returncode != 0:
            print(proc.stderr.strip()[-2000:], file=sys.stderr)
            return proc.returncode
        print(f"extrait écrit : {out_path} ({elapsed:.2f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
