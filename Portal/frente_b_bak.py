#!/usr/bin/env python3
"""E3 — Portal · Frente B — arquivo .bak do dia anterior (PG 13/03, item 7.6)

Localiza o .bak do dia anterior no S3 (versão e Object Lock); cria instância nova criptografada
com SQLSERVER_BACKUP_RESTORE (M1); gera o T-SQL do rds_restore_database e registra início e
conclusão informados (M2); integridade manual (M3); testa a imutabilidade do .bak tentando
excluir a versão (espera AccessDenied); exclui a instância.

Uso (nesta pasta):
    python frente_b_bak.py [--ciclo 2027-1] [--executante "Nome"] [--data-bak 2027-03-14]

A execução pede confirmação antes de criar, renomear ou excluir recursos e retoma do último
passo concluído se for interrompida. Evidências em ./evidencias/E3_<ciclo>/frente_B/.
Configuração: config_portal.yaml (copie de config_portal.exemplo.yaml).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # biblioteca comum prd_lib

from prd_lib.cli import rodar_frente  # noqa: E402

if __name__ == "__main__":
    sys.exit(rodar_frente("E3", "B", __file__, tipo_b="bak"))
