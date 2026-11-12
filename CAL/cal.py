#!/usr/bin/env python3
"""E1 — CAL®: tarefas do sistema nos exercícios de recuperação do PG 13/03.

Uso (nesta pasta):
    python cal.py verificar  [--ciclo 2027-1]   prontidão, somente leitura (rodar antes das frentes)
    python cal.py situacao   [--ciclo 2027-1]   situação das frentes no ciclo
    python cal.py limpar     [--ciclo 2027-1]   exclui instâncias temporárias pendentes
    python cal.py relatorio  [--ciclo 2027-1]   gera o registro .docx do exercício

Frentes (um script por cenário):
    frente_a_pitr.py             Frente A — PITR do cofre primário (PG 13/03, item 7.4)
    frente_b_bak.py              Frente B — arquivo .bak do dia anterior (PG 13/03, item 7.6)
    frente_c_cofre_isolado.py    Frente C — cópia do cofre isolado (PG 13/03, item 7.5)
Configuração: config_cal.yaml (copie de config_cal.exemplo.yaml).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # biblioteca comum prd_lib

from prd_lib.cli import rodar_sistema  # noqa: E402

if __name__ == "__main__":
    sys.exit(rodar_sistema("E1", __file__))
