#!/usr/bin/env python3
"""E2 — Onegreen · Frente B — banco de um cliente (PG 13/03, item 7.7)

PITR em instância temporária (M1) e, em paralelo, uma instância isolada no papel da produção;
gera o T-SQL do backup nativo do banco do cliente e do restore na instância isolada (M2);
integridade manual do banco do cliente (M3); exclui as duas instâncias e o .bak exportado.

Uso (nesta pasta):
    python frente_b_banco_cliente.py [--ciclo 2027-1] [--executante "Nome"] [--banco-cliente NOME] [--instante 2027-03-15T10:00:00-03:00]

A execução pede confirmação antes de criar, renomear ou excluir recursos e retoma do último
passo concluído se for interrompida. Evidências em ./evidencias/E2_<ciclo>/frente_B/.
Configuração: config_onegreen.yaml (copie de config_onegreen.exemplo.yaml).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # biblioteca comum prd_lib

from prd_lib.cli import rodar_frente  # noqa: E402

if __name__ == "__main__":
    sys.exit(rodar_frente("E2", "B", __file__, tipo_b="cliente"))
