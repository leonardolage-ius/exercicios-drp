#!/usr/bin/env python3
"""E2 — Onegreen: tarefas do sistema nos exercícios de recuperação do PG 13/03.

Uso (nesta pasta):
    python onegreen.py verificar  [--ciclo 2027-1]   prontidão, somente leitura (rodar antes das frentes)
    python onegreen.py situacao   [--ciclo 2027-1]   situação das frentes no ciclo
    python onegreen.py limpar     [--ciclo 2027-1]   exclui instâncias temporárias pendentes
    python onegreen.py relatorio  [--ciclo 2027-1]   gera o registro .docx do exercício

Frentes (um script por cenário):
    frente_a_pitr.py             Frente A — PITR do cofre primário (PG 13/03, item 7.4)
    frente_b_banco_cliente.py    Frente B — banco de um cliente (PG 13/03, item 7.7)
    frente_c_cofre_isolado.py    Frente C — cópia do cofre isolado (PG 13/03, item 7.5)
Configuração: config_onegreen.yaml (copie de config_onegreen.exemplo.yaml).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # biblioteca comum prd_lib

from prd_lib.cli import rodar_sistema  # noqa: E402

if __name__ == "__main__":
    sys.exit(rodar_sistema("E2", __file__))
