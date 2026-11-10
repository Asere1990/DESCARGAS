"""Piloto privado: admin existente y acceso.txt; nunca modifica sesiones."""
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent.parent
ACCESS = ROOT / "acceso.txt"


def file_ids(path):
    try:
        return {
            int(row.split("#", 1)[0].strip())
            for row in path.read_text(encoding="utf-8").splitlines()
            if row.split("#", 1)[0].strip().isdigit()
        }
    except OSError:
        return set()


def is_admin(uid):
    # Reutiliza exactamente el archivo de administradores de DESCARGAS.
    return int(uid) in file_ids(ROOT / "admins.txt")


def is_tester(uid):
    return int(uid) in file_ids(ACCESS)


def pilot_allowed(uid):
    return is_admin(uid) or is_tester(uid)


def button_visible(uid):
    return pilot_allowed(uid) or os.getenv("GALERIAS_PUBLIC", "0") == "1"
