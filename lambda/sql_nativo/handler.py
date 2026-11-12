"""Lambda dos exercícios de recuperação (PG 13/03): executa o backup/restore nativo do RDS for SQL Server
na instância TEMPORÁRIA do exercício e consulta o status da tarefa (Marco 2).

Ações (campo "acao" do evento):
  ping             verifica se a função está no ar (não conecta em banco)
  iniciar_restore  msdb.dbo.rds_restore_database do .bak no S3 para um banco que ainda não existe
  iniciar_backup   msdb.dbo.rds_backup_database de um banco para o S3 (Frente B do Onegreen)
  status           msdb.dbo.rds_task_status de uma tarefa

Salvaguardas (além do grupo de segurança, que só permite saída para o SG de teste na porta 1433):
  * o identificador da instância precisa ter o prefixo das temporárias (variável PREFIXO);
  * o endereço precisa ser o endpoint dessa instância (<identificador>.xxxx.rds.amazonaws.com);
  * a instância precisa estar no ar há no máximo `horas_max` horas (a do exercício acabou de ser criada);
  * no restore, o banco não pode existir na instância;
  * a senha recebida nunca é registrada em log nem devolvida.
"""

from __future__ import annotations

import os
import re
from datetime import datetime

import pymssql

PREFIXO = os.environ.get("PREFIXO", "ius-rds-exr").lower()
RE_BANCO = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
RE_S3 = re.compile(r"^arn:aws:s3:::[a-z0-9.\-]{3,63}/.+\.bak$", re.IGNORECASE)


class Recusado(Exception):
    """Pedido recusado por uma salvaguarda."""


def _texto(v):
    return v.isoformat() if isinstance(v, datetime) else v


def _validar(ev: dict) -> None:
    ident = str(ev.get("identificador", "")).lower()
    if not ident.startswith(PREFIXO + "-"):
        raise Recusado(f"identificador '{ident}' sem o prefixo de instância temporária '{PREFIXO}-'")
    host = str(ev.get("host", "")).lower()
    if not host.startswith(ident + ".") or not host.endswith(".rds.amazonaws.com"):
        raise Recusado("o endereço informado não é o endpoint da instância temporária indicada")
    if "banco" in ev and not RE_BANCO.match(str(ev["banco"])):
        raise Recusado("nome de banco inválido")
    if "s3_arn" in ev and not RE_S3.match(str(ev["s3_arn"])):
        raise Recusado("ARN do arquivo .bak inválido")
    horas = int(ev.get("horas_max", 0))
    if not 1 <= horas <= 96:
        raise Recusado("horas_max deve estar entre 1 e 96")


def _conectar(ev: dict):
    return pymssql.connect(server=ev["host"], port=int(ev.get("porta") or 1433), user=ev["usuario"],
                           password=ev["senha"], database="master", login_timeout=20, timeout=90,
                           autocommit=True, as_dict=True)


def _relogio_e_trava(cur, horas_max: int) -> dict:
    cur.execute("SELECT SYSDATETIME() AS agora_local, SYSUTCDATETIME() AS agora_utc, "
                "DATEDIFF(HOUR, (SELECT sqlserver_start_time FROM sys.dm_os_sys_info), GETDATE()) AS horas_no_ar")
    r = cur.fetchone()
    if r["horas_no_ar"] is None or r["horas_no_ar"] > horas_max:
        raise Recusado(f"PARADO: a instância está no ar há {r['horas_no_ar']} h (máximo {horas_max} h); "
                       "a instância do exercício acabou de ser criada. Conferir se não é a produção.")
    return {"agora_servidor": _texto(r["agora_local"]), "agora_utc": _texto(r["agora_utc"]),
            "offset_seg": (r["agora_local"] - r["agora_utc"]).total_seconds(), "horas_no_ar": r["horas_no_ar"]}


def _consumir(cur) -> None:
    """Descarta os conjuntos de resultado devolvidos pelos procedimentos do RDS."""
    try:
        while True:
            try:
                cur.fetchall()
            except pymssql.OperationalError:
                pass
            if not cur.nextset():
                break
    except Exception:  # noqa: BLE001 — procedimento sem resultado
        pass


def _ultima_tarefa(cur, banco: str, tipo: str) -> dict:
    cur.execute("EXEC msdb.dbo.rds_task_status @db_name = %s", (banco,))
    tarefas = [t for t in cur.fetchall() if str(t.get("task_type", "")).upper().startswith(tipo)]
    if not tarefas:
        raise RuntimeError(f"tarefa {tipo} de {banco} não encontrada em rds_task_status")
    return max(tarefas, key=lambda t: t["task_id"])


def _resumo(t: dict) -> dict:
    return {
        "task_id": t.get("task_id"),
        "task_type": t.get("task_type"),
        "banco": t.get("database_name"),
        "percentual": t.get("% complete"),
        "lifecycle": t.get("lifecycle"),
        "task_info": t.get("task_info"),
        "created_at": _texto(t.get("created_at")),
        "last_updated": _texto(t.get("last_updated")),
        "s3_arn": t.get("S3_object_arn"),
    }


def lambda_handler(ev, _ctx=None):
    acao = ev.get("acao")
    if acao == "ping":
        return {"ok": True, "pymssql": pymssql.__version__, "prefixo": PREFIXO}
    if acao not in ("iniciar_restore", "iniciar_backup", "status"):
        raise Recusado(f"ação inválida: {acao}")
    _validar(ev)
    with _conectar(ev) as conn:
        cur = conn.cursor()
        relogio = _relogio_e_trava(cur, int(ev["horas_max"]))
        if acao == "status":
            cur.execute("EXEC msdb.dbo.rds_task_status @task_id = %d", (int(ev["task_id"]),))
            linhas = cur.fetchall()
            if not linhas:
                raise RuntimeError(f"tarefa {ev['task_id']} não encontrada")
            return {**_resumo(linhas[0]), **relogio}
        banco = ev["banco"]
        if acao == "iniciar_restore":
            cur.execute("SELECT DB_ID(%s) AS id", (banco,))
            if cur.fetchone()["id"] is not None:
                raise Recusado(f"PARADO: o banco {banco} já existe nesta instância. Conferir se não é a produção.")
            cur.execute("EXEC msdb.dbo.rds_restore_database @restore_db_name = %s, @s3_arn_to_restore_from = %s",
                        (banco, ev["s3_arn"]))
            _consumir(cur)
            tarefa = _ultima_tarefa(cur, banco, "RESTORE")
        else:
            cur.execute("EXEC msdb.dbo.rds_backup_database @source_db_name = %s, @s3_arn_to_backup_to = %s, "
                        "@overwrite_s3_backup_file = 1, @type = 'FULL'", (banco, ev["s3_arn"]))
            _consumir(cur)
            tarefa = _ultima_tarefa(cur, banco, "BACKUP")
        return {**_resumo(tarefa), **relogio}
