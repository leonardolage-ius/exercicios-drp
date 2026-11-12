#!/usr/bin/env python3
"""Gera ius-lmb-exr-sql.zip (handler + pymssql para Lambda python3.12 x86_64).

Uso:  python empacotar.py
Funciona em Windows, Linux ou macOS: o pip baixa o pacote pymssql já compilado para Linux.
"""

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PYMSSQL = "pymssql==2.4.6"
AQUI = Path(__file__).resolve().parent
SAIDA = AQUI / "ius-lmb-exr-sql.zip"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        destino = Path(tmp) / "pacote"
        subprocess.run([sys.executable, "-m", "pip", "install", PYMSSQL, "--target", str(destino),
                        "--platform", "manylinux_2_28_x86_64", "--only-binary=:all:",
                        "--python-version", "3.12", "--implementation", "cp", "--quiet"], check=True)
        shutil.copy(AQUI / "sql_nativo" / "handler.py", destino / "handler.py")
        if SAIDA.exists():
            SAIDA.unlink()
        with zipfile.ZipFile(SAIDA, "w", zipfile.ZIP_DEFLATED) as z:
            for arq in sorted(destino.rglob("*")):
                if arq.is_file() and "__pycache__" not in arq.parts:
                    z.write(arq, arq.relative_to(destino))
    print(f"Pacote gerado: {SAIDA} ({SAIDA.stat().st_size / 1024 ** 2:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
