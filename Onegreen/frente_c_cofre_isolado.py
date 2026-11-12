#!/usr/bin/env python3
"""E2 — Onegreen · Frente C — cópia do cofre isolado (PG 13/03, item 7.5)

Executada pelo Custodiante da Conta de Backup. Copia o ponto mais recente do cofre isolado para
o cofre primário (M1; tempo da cópia registrado à parte); confere a chave KMS da conta de destino;
restaura o snapshot copiado em instância temporária (M2); integridade manual (M3); exclui a
instância e, se confirmado, o ponto copiado.

Uso (nesta pasta):
    python frente_c_cofre_isolado.py [--ciclo 2027-1] [--executante "Nome"]

A execução pede confirmação antes de criar, renomear ou excluir recursos e retoma do último
passo concluído se for interrompida. Evidências em ./evidencias/E2_<ciclo>/frente_C/.
Configuração: config_onegreen.yaml (copie de config_onegreen.exemplo.yaml).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # biblioteca comum prd_lib

from prd_lib.cli import rodar_frente  # noqa: E402

if __name__ == "__main__":
    sys.exit(rodar_frente("E2", "C", __file__))
