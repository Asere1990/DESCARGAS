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


def payment_test_user(uid):
    """Reversible, strictly scoped payment-gate test; no file changes needed.

    Configured on the CLOUDFLARE service only, not on DESCARGAS. A listed
    tester remains visible in /start but does not get free gallery access.
    """
    value=str(int(uid))
    return value in {
        part.strip() for part in
        os.getenv("GALERIAS_PAYMENT_TEST_UIDS","").split(",")
        if part.strip().isdigit()
    }


def pilot_allowed(uid):
    return is_admin(uid) or (is_tester(uid) and not payment_test_user(uid))


def button_visible(uid):
    # Keep the existing /start button even if this tester must purchase.
    return (is_admin(uid) or is_tester(uid) or payment_test_user(uid)
            or os.getenv("GALERIAS_PUBLIC", "0") == "1")
