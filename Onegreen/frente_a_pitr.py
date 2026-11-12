#!/usr/bin/env python3
"""E2 — Onegreen · Frente A — PITR do cofre primário (PG 13/03, item 7.4)

Restaura o backup contínuo da produção, no instante definido antes do início, em instância
temporária privada (marcos M1 e M2); conduz a verificação de integridade manual (M3); mede o
tempo de renomeação na temporária (opcional, --medir-renomeacao); exclui a temporária. A produção nunca é alterada.

Uso (nesta pasta):
    python frente_a_pitr.py [--ciclo 2027-1] [--executante "Nome"] [--instante 2027-03-15T10:00:00-03:00] [--medir-renomeacao]

A execução pede confirmação antes de criar, renomear ou excluir recursos e retoma do último
passo concluído se for interrompida. Evidências em ./evidencias/E2_<ciclo>/frente_A/.
Configuração: config_onegreen.yaml (copie de config_onegreen.exemplo.yaml).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # biblioteca comum prd_lib

from prd_lib.cli import rodar_frente  # noqa: E402

if __name__ == "__main__":
    sys.exit(rodar_frente("E2", "A", __file__))
