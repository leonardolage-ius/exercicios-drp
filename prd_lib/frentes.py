"""Fluxos das frentes A, B e C dos exercícios de recuperação (PG 13/03, seções 7 e 8).

Frente A — PITR do cofre primário em instância temporária (+ medição da renomeação, item 7.4).
Frente B — tipo 'bak' (CAL®, Portal): instância nova + rds_restore_database do .bak do dia anterior
           + teste de imutabilidade (Object Lock) da versão do .bak (item 7.6).
           tipo 'cliente' (Onegreen): PITR em temporária → backup nativo do banco do cliente →
           restore em outra instância isolada (item 7.7).
Frente C — cópia de retorno do cofre isolado para o cofre primário + restore do snapshot copiado
           + conferência da chave KMS da conta de destino (item 7.5).
"""

from __future__ import annotations

from datetime import timedelta

from botocore.exceptions import ClientError

from . import aws, integridade, lambda_sql, tempo
from .config import exigir_conta_backup, prefixos_do_dia
from .contexto import Abortado, Execucao


# ====================================================================== utilidades
def _cli(ex: Execucao, servico: str):
    """Clientes da conta do serviço (rds, s3, backup, ...), criados uma vez por execução."""
    if "sess" not in ex.rt:
        ex.rt["sess"] = aws.sessao(ex.servico["perfil"], ex.cfg["regiao"])
        aws.conferir_conta(ex.rt["sess"], ex.servico["conta_id"], f"do serviço ({ex.servico['nome']})")
    chave = f"cli_{servico}"
    if chave not in ex.rt:
        ex.rt[chave] = ex.rt["sess"].client(servico)
    return ex.rt[chave]


def _cli_backup_isolada(ex: Execucao):
    if "sess_iso" not in ex.rt:
        cb = exigir_conta_backup(ex.cfg)
        ex.rt["sess_iso"] = aws.sessao(cb["perfil"], ex.cfg["regiao"])
        aws.conferir_conta(ex.rt["sess_iso"], cb["conta_id"], "de backup isolada")
        ex.rt["cli_backup_iso"] = ex.rt["sess_iso"].client("backup")
    return ex.rt["cli_backup_iso"]


def _producao(ex: Execucao) -> dict:
    rds = _cli(ex, "rds")
    prod = aws.descrever(rds, ex.servico["instancia_producao"])
    if prod is None:
        raise Abortado(f"Instância de produção {ex.servico['instancia_producao']} não encontrada.")
    return prod


def _resumo_producao(prod: dict) -> dict:
    return {
        "identificador": prod["DBInstanceIdentifier"],
        "arn": prod["DBInstanceArn"],
        "engine": prod["Engine"],
        "versao": prod["EngineVersion"],
        "classe": prod["DBInstanceClass"],
        "armazenamento_gb": prod["AllocatedStorage"],
        "tipo_armazenamento": prod.get("StorageType"),
        "criptografia": prod.get("StorageEncrypted"),
        "kms_key_id": prod.get("KmsKeyId"),
        "grupo_opcoes": prod["OptionGroupMemberships"][0]["OptionGroupName"],
        "grupo_parametros": prod["DBParameterGroups"][0]["DBParameterGroupName"],
        "latest_restorable_time": tempo.iso(prod.get("LatestRestorableTime")),
    }


def _preparar_comum(ex: Execucao) -> dict:
    """Contas, produção e rede de teste. Comum a todas as frentes."""
    rds = _cli(ex, "rds")
    prod = _producao(ex)
    ex.dados["producao"] = _resumo_producao(prod)
    print(f"  Produção: {prod['DBInstanceIdentifier']} ({prod['Engine']} {prod['EngineVersion']}, {prod['DBInstanceClass']})")
    if not prod.get("StorageEncrypted"):
        ex.lacuna("Instância de produção sem criptografia em repouso", "alta")
    t = ex.servico["teste"]
    rede = aws.verificar_rede_teste(ex.rt["sess"], t["db_subnet_group"], t["security_group_ids"])
    ex.dados["rede_teste"] = rede
    print(f"  Rede de teste: subnet group {rede['subnet_group']} (VPC {rede['vpc']}), SG {', '.join(rede['security_groups'])}")
    if rede["problemas"]:
        for p in rede["problemas"]:
            print(f"    PROBLEMA: {p}")
        raise Abortado("A rede de teste não atende à seção 8 (sub-rede privada e sem entrada da internet). Corrija o config.")
    print("    sub-redes privadas e sem regra de entrada a partir da internet: OK")
    ex.salvar()
    _ = rds
    return prod


def _definir_instante(ex: Execucao, prod: dict, instante_arg: str | None):
    """Frente A / B-cliente: instante de recuperação definido ANTES do início (não por conveniência)."""
    latest = tempo.para_local(prod["LatestRestorableTime"])
    if instante_arg:
        instante = tempo.de_iso(instante_arg)
        if instante > latest:
            raise Abortado(f"Instante {tempo.br(instante)} é posterior ao último restaurável ({tempo.br(latest)}).")
        if instante < tempo.agora() - timedelta(days=30):
            raise Abortado("Instante fora da janela de 30 dias do backup contínuo.")
        origem = "informado pelo executante (--instante)"
    else:
        instante = latest
        origem = "último instante restaurável (LatestRestorableTime) no momento da preparação"
    try:
        backups = _cli(ex, "rds").describe_db_instance_automated_backups(
            DBInstanceIdentifier=prod["DBInstanceIdentifier"])["DBInstanceAutomatedBackups"]
        janela = backups[0].get("RestoreWindow", {}) if backups else {}
        ex.dados["janela_restauracao"] = {k: tempo.iso(v) for k, v in janela.items()}
    except ClientError:
        pass
    ex.dados["instante_recuperacao"] = tempo.iso(instante)
    ex.dados["origem_instante"] = origem
    ex.estado["pontos_recuperacao"].append({
        "tipo": "PITR — backup contínuo do cofre primário",
        "origem": prod["DBInstanceArn"],
        "instante": tempo.iso(instante),
        "observacao": origem,
    })
    print(f"  Instante de recuperação: {tempo.br(instante)} ({origem})")
    print(f"  Último instante restaurável agora: {tempo.br(latest)}")
    ex.salvar()
    return instante


def _iniciar_pitr(ex: Execucao, prod: dict, alvo: str, papel: str, marcar_m1: bool) -> None:
    rds = _cli(ex, "rds")
    aws.garantir_nao_producao(ex, alvo)
    if aws.descrever(rds, alvo) is not None:
        raise Abortado(f"Já existe uma instância {alvo}.")
    instante = tempo.de_iso(ex.dados["instante_recuperacao"])
    params = aws.parametros_restauracao(ex, prod)
    req = dict(SourceDBInstanceIdentifier=prod["DBInstanceIdentifier"], TargetDBInstanceIdentifier=alvo,
               RestoreTime=instante, Tags=aws.tags_exercicio(ex, alvo), **params)
    print(f"""  Restauração PITR planejada:
    origem ............ {prod['DBInstanceIdentifier']} (produção — NÃO será alterada)
    destino ........... {alvo}
    instante .......... {tempo.br(instante)}
    classe ............ {params['DBInstanceClass']}
    subnet group ...... {params['DBSubnetGroupName']}
    SG ................ {', '.join(params['VpcSecurityGroupIds'])}
    acesso público .... não   | Multi-AZ: não | criptografia: herdada da produção
    grupo de opções ... {params['OptionGroupName']}""")
    ex.exigir(f"Iniciar a restauração PITR em {alvo}?")
    if marcar_m1:
        ex.marco("m1", "Início da restauração (PITR)")
        # RPO do PITR: atraso do backup contínuo no M1 (quanto dado se perderia num evento naquele momento),
        # independente do instante escolhido para o exercício.
        lrt = tempo.para_local(_producao(ex)["LatestRestorableTime"])
        ex.dados["latest_restorable_no_m1"] = tempo.iso(lrt)
        ex.dados["referencia_rpo"] = tempo.iso(lrt)
    else:
        ex.marco_extra(f"Início da restauração PITR em {alvo}")
    resp = rds.restore_db_instance_to_point_in_time(**req)
    ex.evento("aws", "restore_db_instance_to_point_in_time", {"pedido": {k: v for k, v in req.items() if k != "Tags"},
                                                               "status": resp["DBInstance"]["DBInstanceStatus"]})
    aws.registrar_instancia(ex, alvo, papel)


def _criar_instancia_vazia(ex: Execucao, prod: dict, alvo: str, papel: str, grupo_opcoes: str, marcar_m1: bool) -> None:
    """Instância RDS for SQL Server nova (item 7.6), para receber restore nativo."""
    rds = _cli(ex, "rds")
    t = ex.servico["teste"]
    aws.garantir_nao_producao(ex, alvo)
    if aws.descrever(rds, alvo) is not None:
        raise Abortado(f"Já existe uma instância {alvo}.")
    params = aws.parametros_restauracao(ex, prod)
    params["OptionGroupName"] = grupo_opcoes
    req = dict(
        DBInstanceIdentifier=alvo,
        Engine=prod["Engine"],
        EngineVersion=prod["EngineVersion"],
        LicenseModel=prod.get("LicenseModel", "license-included"),
        AllocatedStorage=prod["AllocatedStorage"],
        MasterUsername=t.get("usuario_mestre") or prod["MasterUsername"],
        ManageMasterUserPassword=True,
        StorageEncrypted=True,
        KmsKeyId=t["kms_key_id"],
        BackupRetentionPeriod=int(t.get("retencao_backup_dias", 0)),
        Tags=aws.tags_exercicio(ex, alvo),
        **params,
    )
    if prod.get("StorageType") in ("io1", "io2") and prod.get("Iops"):
        req["Iops"] = prod["Iops"]
    if prod.get("CharacterSetName"):
        req["CharacterSetName"] = prod["CharacterSetName"]
    if prod.get("Timezone"):
        req["Timezone"] = prod["Timezone"]
    print(f"""  Criação de instância planejada:
    identificador ..... {alvo}
    engine ............ {req['Engine']} {req['EngineVersion']} ({req['LicenseModel']})
    classe ............ {req['DBInstanceClass']} | armazenamento: {req['AllocatedStorage']} GB {req['StorageType']}
    subnet group ...... {req['DBSubnetGroupName']}
    SG ................ {', '.join(req['VpcSecurityGroupIds'])}
    acesso público .... não   | Multi-AZ: não
    criptografia ...... sim — {req['KmsKeyId']}
    grupo de opções ... {req['OptionGroupName']} (SQLSERVER_BACKUP_RESTORE)
    senha do mestre ... gerenciada pelo Secrets Manager (não aparece no console nem no registro)""")
    ex.exigir(f"Criar a instância {alvo}?")
    if marcar_m1:
        ex.marco("m1", "Início da restauração (criação da instância)")
    else:
        ex.marco_extra(f"Início da criação de {alvo}")
    resp = rds.create_db_instance(**req)
    ex.evento("aws", "create_db_instance", {"pedido": {k: v for k, v in req.items() if k != "Tags"},
                                            "status": resp["DBInstance"]["DBInstanceStatus"]})
    aws.registrar_instancia(ex, alvo, papel)


def _aguardar_disponivel(ex: Execucao, alvo: str, chave_marco: str | None, descricao: str) -> dict:
    rds = _cli(ex, "rds")
    inst = aws.aguardar_status(ex, rds, alvo)
    if chave_marco:
        ex.marco(chave_marco, descricao)
    else:
        ex.marco_extra(descricao)
    aws.verificar_controles(ex, rds, inst)
    ex.dados.setdefault("endpoints", {})[alvo] = (inst.get("Endpoint") or {}).get("Address")
    segredo = (inst.get("MasterUserSecret") or {}).get("SecretArn")
    print(f"  Endpoint: {ex.dados['endpoints'][alvo]}")
    if segredo:
        print(f"  Senha do usuário mestre: aws secretsmanager get-secret-value --secret-id \"{segredo}\" "
              f"--profile {ex.servico['perfil']} --query SecretString --output text")
    else:
        print("  Credenciais: as mesmas da produção (instância restaurada).")
    return inst


def _limpar_instancias(ex: Execucao) -> None:
    rds = _cli(ex, "rds")
    pendentes = [i for i, r in ex.estado["instancias"].items() if not r.get("excluida_em")]
    if not pendentes:
        print("  Nenhuma instância temporária pendente.")
        return
    print("  Instâncias temporárias a excluir: " + ", ".join(pendentes))
    if not ex.confirmar("Excluir agora? (se responder 'n', use depois o comando 'limpar')"):
        ex.evento("limpeza_adiada", "Exclusão das temporárias adiada pelo executante")
        return
    for ident in pendentes:
        aws.excluir_temporaria(ex, rds, ident)


def _confirmar_horario_manual(ex: Execucao, pergunta: str, chave: str | None, descricao: str):
    quando = ex.perguntar_horario(pergunta)
    if chave:
        ex.marco(chave, descricao, quando)
    else:
        ex.marco_extra(descricao, quando)
    return quando


# ====================================================================== resultado
def calcular_resultado(ex: Execucao) -> dict:
    """Calcula RTO, RPO, custo e o resultado da frente a partir do estado gravado."""
    e = ex.estado
    m = e["marcos"]
    meta = e["meta"]
    m1, m2, m3 = (tempo.de_iso(m.get(k, {}).get("horario")) for k in ("m1", "m2", "m3"))
    rto = (m3 - m1).total_seconds() if m1 and m3 else None
    ref_rpo = tempo.de_iso(e["dados"].get("referencia_rpo"))
    rpo = (m1 - ref_rpo).total_seconds() if m1 and ref_rpo else None
    res = {
        "rto_seg": rto,
        "rto_meta_seg": meta["rto_min"] * 60,
        "dentro_rto": rto is not None and rto <= meta["rto_min"] * 60,
        "rpo_seg": rpo,
        "rpo_meta_seg": meta["rpo_min"] * 60,
        "dentro_rpo": rpo is not None and rpo <= meta["rpo_min"] * 60,
        "ate_disponivel_seg": (m2 - m1).total_seconds() if m1 and m2 else None,
        "integridade_confirmada": bool((e.get("integridade") or {}).get("confirmada")),
    }
    ren = e["dados"].get("renomeacao")
    if ren and ren.get("duracao_seg") is not None and rto is not None:
        res["rto_projetado_com_renomeacao_seg"] = rto + ren["duracao_seg"]
        res["dentro_rto_com_renomeacao"] = res["rto_projetado_com_renomeacao_seg"] <= meta["rto_min"] * 60
    # custo aproximado: horas de cada temporária × custo por hora
    preco = ex.servico.get("custo_hora_usd")
    horas_total = 0.0
    for r in e["instancias"].values():
        ini = tempo.de_iso(r.get("criada_em"))
        fim = tempo.de_iso(r.get("excluida_em")) or tempo.agora()
        if ini:
            horas_total += max((fim - ini).total_seconds(), 0) / 3600
    res["horas_instancia"] = round(horas_total, 2)
    res["custo_estimado_usd"] = round(horas_total * float(preco), 2) if preco else None
    res["instancias_pendentes"] = [i for i, r in e["instancias"].items() if not r.get("excluida_em")]
    sucesso = res["dentro_rto"] and res["dentro_rpo"] and res["integridade_confirmada"]
    res["resultado"] = "Bem-sucedida" if sucesso else "Falha — abrir Não Conformidade no CAL® (Ocorrências)"
    e["resultado"] = res
    return res


def _finalizar(ex: Execucao) -> None:
    res = calcular_resultado(ex)
    for i in res["instancias_pendentes"]:
        ex.lacuna(f"Instância temporária {i} ainda não excluída — executar o comando 'limpar'", "alta")
    obs = ex.perguntar("Observações/lacunas adicionais (Enter para nenhuma)", padrao="", obrigatorio=False)
    if obs:
        ex.lacuna(obs, "media")
    ex.estado["concluida"] = True
    ex.estado["concluida_em"] = tempo.iso(tempo.agora())
    calcular_resultado(ex)
    ex.salvar()
    meta = ex.estado["meta"]
    print(f"""
  RESULTADO — {ex.exercicio} Frente {ex.frente} ({ex.servico['nome']})
    RTO medido ....... {tempo.duracao(res['rto_seg'])}  (meta {meta['rto_min']} min)  {'OK' if res['dentro_rto'] else 'EXCEDIDO'}
    RPO observado .... {tempo.duracao(res['rpo_seg'])}  (meta {meta['rpo_min']} min)  {'OK' if res['dentro_rpo'] else 'EXCEDIDO'}
    Integridade ...... {'confirmada' if res['integridade_confirmada'] else 'NÃO confirmada'}""")
    if "rto_projetado_com_renomeacao_seg" in res:
        print(f"    RTO com renomeação {tempo.duracao(res['rto_projetado_com_renomeacao_seg'])}  "
              f"{'OK' if res['dentro_rto_com_renomeacao'] else 'EXCEDE a meta do componente'}")
    print(f"    Custo estimado ... {res['custo_estimado_usd'] if res['custo_estimado_usd'] is not None else '(defina custo_hora_usd)'} USD")
    print(f"    {res['resultado']}")
    print(f"\n  Registro: {ex.arq_registro}")


# ====================================================================== Frente A
def frente_a(ex: Execucao, args) -> None:
    alvo = ex.dados.get("alvo") or aws.id_temporaria(ex)

    def preparar():
        prod = _preparar_comum(ex)
        _definir_instante(ex, prod, args.instante)
        ex.dados["alvo"] = alvo
        ex.dados["referencia_rpo"] = ex.dados["instante_recuperacao"]
        ex.dados["medir_renomeacao"] = bool(args.medir_renomeacao)
        ex.exigir("Parâmetros conferidos. Prosseguir?")

    def restaurar():
        _iniciar_pitr(ex, _producao(ex), alvo, "PITR (Frente A)", marcar_m1=True)
        # RPO: diferença entre o instante restaurado e o início da restauração (m1)

    def aguardar():
        _aguardar_disponivel(ex, alvo, "m2", "Instância disponível (status available)")

    def verificar():
        integridade.executar(ex, "frente_A", ex.dados["endpoints"].get(alvo))

    def renomear():
        if not ex.dados.get("medir_renomeacao"):
            print("  Medição da renomeação não solicitada (use --medir-renomeacao para medir).")
            return
        rds = _cli(ex, "rds")
        novo = (alvo + "-ren")[:63]
        aws.garantir_nao_producao(ex, alvo)
        aws.garantir_nao_producao(ex, novo)
        print(f"  Mede o tempo da renomeação (item 7.4) na TEMPORÁRIA: {alvo} -> {novo}. A produção não é tocada.")
        if alvo in ex.estado["instancias"]:  # ainda não renomeada (permite retomar com segurança)
            ex.exigir("Renomear a instância temporária?")
            inicio = tempo.agora()
            ex.marco_extra("Início da renomeação da temporária", inicio)
            rds.modify_db_instance(DBInstanceIdentifier=alvo, NewDBInstanceIdentifier=novo, ApplyImmediately=True)
            ex.evento("aws", f"modify_db_instance {alvo} -> {novo}")
            ex.estado["instancias"][novo] = ex.estado["instancias"].pop(alvo)
            ex.estado["instancias"][novo]["identificador_anterior"] = alvo
            ex.dados["renomeacao_inicio"] = tempo.iso(inicio)
            ex.salvar()
        inicio = tempo.de_iso(ex.dados["renomeacao_inicio"])
        aws.aguardar_status(ex, rds, novo, id_alternativo=alvo)
        fim = tempo.agora()
        ex.marco_extra("Renomeação concluída (status available)", fim)
        ex.dados["renomeacao"] = {"de": alvo, "para": novo, "inicio": tempo.iso(inicio), "fim": tempo.iso(fim),
                                  "duracao_seg": (fim - inicio).total_seconds()}
        print(f"  Renomeação: {tempo.duracao((fim - inicio).total_seconds())}")

    ex.passo("preparar", "Preparação — contas, produção, rede de teste e instante de recuperação", preparar)
    ex.passo("restaurar", "Marco 1 — início da restauração PITR", restaurar)
    ex.passo("aguardar", "Marco 2 — aguardando a instância ficar disponível", aguardar)
    ex.passo("integridade", "Marco 3 — verificação de integridade", verificar)
    ex.passo("renomear", "Medição do tempo de renomeação (item 7.4)", renomear)
    ex.passo("limpeza", "Exclusão das instâncias temporárias", lambda: _limpar_instancias(ex))
    ex.passo("finalizar", "Resultado", lambda: _finalizar(ex))


# ====================================================================== Frente B — .bak (CAL®, Portal)
def _localizar_bak(ex: Execucao, s3, bucket: str, banco: dict, data_alvo) -> dict:
    prefixos = prefixos_do_dia(banco.get("prefixo"), data_alvo)
    prefixo = " | ".join(prefixos)
    print(f"  Procurando em s3://{bucket}/{prefixo}")
    candidatos = []
    for pref in prefixos:
        for pagina in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=pref):
            for o in pagina.get("Contents", []):
                if o["Key"].lower().endswith(".bak") and o["Key"] not in {c["Key"] for c in candidatos}:
                    candidatos.append(o)
    if not candidatos:
        raise Abortado(f"Nenhum .bak em s3://{bucket}/{prefixo}")
    do_dia = [o for o in candidatos if tempo.para_local(o["LastModified"]).date() == data_alvo]
    if not do_dia:
        recentes = sorted(candidatos, key=lambda o: o["LastModified"], reverse=True)[:5]
        for o in recentes:
            print(f"    {tempo.br(o['LastModified'])}  {o['Key']}")
        raise Abortado(f"Nenhum .bak de {data_alvo:%d/%m/%Y} para {banco['nome']} (acima, os mais recentes).")
    escolhido = max(do_dia, key=lambda o: o["LastModified"])
    head = s3.head_object(Bucket=bucket, Key=escolhido["Key"])
    info = {
        "banco": banco["nome"],
        "bucket": bucket,
        "chave": escolhido["Key"],
        "arn": f"arn:aws:s3:::{bucket}/{escolhido['Key']}",
        "version_id": head.get("VersionId"),
        "gravado_em": tempo.iso(head["LastModified"]),
        "tamanho_bytes": head["ContentLength"],
        "object_lock_modo": head.get("ObjectLockMode"),
        "object_lock_reter_ate": tempo.iso(head.get("ObjectLockRetainUntilDate")),
        "legal_hold": head.get("ObjectLockLegalHoldStatus"),
        "criptografia": head.get("ServerSideEncryption"),
    }
    print(f"  {banco['nome']}: {info['chave']}  ({tempo.br(info['gravado_em'])}, {info['tamanho_bytes'] / 1024 ** 3:.2f} GiB)")
    print(f"    VersionId {info['version_id']} | Object Lock {info['object_lock_modo'] or 'AUSENTE'} até {tempo.br(info['object_lock_reter_ate'])}")
    return info


def _horas_desde_m1(ex: Execucao) -> int:
    """Limite de tempo no ar aceito para a instância do exercício (criada a partir de M1), com folga de 2 h."""
    m1 = tempo.de_iso(ex.estado["marcos"]["m1"]["horario"])
    return int((tempo.agora() - m1).total_seconds() // 3600) + 2


def _trava_producao(horas_max: int) -> list[str]:
    """Condição T-SQL que impede executar o comando numa instância que não acabou de ser criada (a produção)."""
    return [
        f"IF (SELECT DATEDIFF(HOUR, sqlserver_start_time, GETDATE()) FROM sys.dm_os_sys_info) > {horas_max}",
        f"    RAISERROR(N'PARADO: esta instância está no ar há mais de {horas_max} h; a do exercício acabou de ser criada. "
        "Confira se a conexão NÃO é a produção.', 16, 1);",
    ]


CABECALHO_TRAVA = [
    "-- TRAVAS: o comando só roda se a instância estiver no ar há poucas horas (a do exercício acabou de ser",
    "-- criada) e, no restore, se o banco ainda não existir. Na produção, as duas condições impedem a execução.",
    "-- Se a primeira consulta der erro de permissão, confira a conexão antes de executar o comando à mão.",
    "",
]


def _sql_restore(caminho, arquivos: list[dict], destino: str | None, horas_max: int) -> None:
    linhas = ["-- Restore nativo — exercício de recuperação (PG 13/03, item 7.6 / 7.7)",
              f"-- Executar SOMENTE na instância do exercício: {destino or '(ver registro)'}", ""] + CABECALHO_TRAVA
    for a in arquivos:
        linhas += _trava_producao(horas_max) + [
            f"ELSE IF DB_ID(N'{a['banco']}') IS NOT NULL",
            f"    RAISERROR(N'PARADO: o banco {a['banco']} já existe nesta instância. Confira se a conexão NÃO é a produção.', 16, 1);",
            "ELSE",
            "    EXEC msdb.dbo.rds_restore_database",
            f"        @restore_db_name = N'{a['banco']}',",
            f"        @s3_arn_to_restore_from = N'{a['arn']}';",
            "GO",
            "",
        ]
    linhas += ["-- Acompanhar até lifecycle = SUCCESS (created_at / last_updated no relógio do servidor: UTC, salvo fuso configurado)"]
    linhas += [f"exec msdb.dbo.rds_task_status @db_name = '{a['banco']}';" for a in arquivos]
    caminho.write_text("\n".join(linhas) + "\n", encoding="utf-8-sig")


def _restore_manual(ex: Execucao, arquivos: list[dict], destino: str, rotulo: str) -> None:
    arq = ex.pasta / f"restore_{rotulo}.sql"
    _sql_restore(arq, arquivos, ex.dados["endpoints"].get(destino), _horas_desde_m1(ex))
    print(f"""
  Restore nativo (manual, no SSMS)
  1. Conecte à instância {destino}: {ex.dados['endpoints'].get(destino)}
  2. Execute {arq}
  3. Acompanhe com rds_task_status até lifecycle = SUCCESS.""")
    _confirmar_horario_manual(ex, "Horário em que o restore foi INICIADO (created_at da tarefa, ou Enter ao executar):",
                              None, "Restore nativo iniciado")
    tarefa = ex.perguntar("  task_id da tarefa de restore (rds_task_status)", padrao="", obrigatorio=False)
    _confirmar_horario_manual(ex, "Horário em que o restore foi CONCLUÍDO (last_updated com lifecycle SUCCESS):",
                              "m2", "Restore concluído (rds_task_status = SUCCESS)")
    status = ex.perguntar("  lifecycle final informado pelo rds_task_status", padrao="SUCCESS")
    ex.dados[f"tarefa_{rotulo}"] = {"task_id": tarefa, "lifecycle": status}
    if status.upper() != "SUCCESS":
        ex.lacuna(f"Tarefa de restore terminou com lifecycle {status}", "alta")


def _restore(ex: Execucao, arquivos: list[dict], destino: str, rotulo: str) -> None:
    """Marco 2: restore nativo pela Lambda (se configurada) ou manual pelo SSMS."""
    if lambda_sql.nome_funcao(ex):
        _cli(ex, "rds")
        try:
            lambda_sql.executar_tarefas(
                ex, "iniciar_restore", destino, [{"banco": a["banco"], "s3_arn": a["arn"]} for a in arquivos],
                _horas_desde_m1(ex), rotulo, "Restore nativo iniciado (rds_task_status.created_at)",
                "m2", "Restore concluído (rds_task_status = SUCCESS)")
            return
        except lambda_sql.ErroLambda as e:
            if (ex.dados.get(f"tarefas_{rotulo}") or {}).get("itens"):
                raise Abortado(f"Falha na Lambda com a tarefa já iniciada: {e}. Rode de novo para retomar o acompanhamento.")
            print(f"\n  A Lambda recusou ou falhou: {e}")
            ex.lacuna(f"Marco 2 pela Lambda não iniciado: {e}", "media")
            if not ex.confirmar("Seguir no modo manual (SSMS)?"):
                raise Abortado("Marco 2 não executado.")
            ex.dados.pop(f"tarefas_{rotulo}", None)
    _restore_manual(ex, arquivos, destino, rotulo)


def frente_b_bak(ex: Execucao, args) -> None:
    alvo = ex.dados.get("alvo") or aws.id_temporaria(ex)
    cfg_bak = ex.servico["bak"]

    def preparar():
        prod = _preparar_comum(ex)
        rds, s3 = _cli(ex, "rds"), _cli(ex, "s3")
        t = ex.servico["teste"]
        grupo = t.get("option_group_bak") or t.get("option_group") or prod["OptionGroupMemberships"][0]["OptionGroupName"]
        if not aws.grupo_opcoes_tem_backup_nativo(rds, grupo):
            raise Abortado(f"O grupo de opções {grupo} não tem SQLSERVER_BACKUP_RESTORE (item 7.6). Ajuste teste.option_group_bak.")
        print(f"  Grupo de opções {grupo}: SQLSERVER_BACKUP_RESTORE presente")
        kms = aws.verificar_kms(ex.rt["sess"], t["kms_key_id"], ex.servico["conta_id"])
        if kms["problemas"]:
            raise Abortado("Chave KMS de teste inválida: " + "; ".join(kms["problemas"]))
        print(f"  Chave KMS de teste: {kms['arn']} ({kms['estado']})")
        data_alvo = (tempo.de_iso(args.data_bak + "T12:00:00").date() if args.data_bak
                     else (tempo.agora() - timedelta(days=1)).date())
        print(f"  Arquivos .bak de {data_alvo:%d/%m/%Y} (dia anterior):")
        arquivos = [_localizar_bak(ex, s3, cfg_bak["bucket"], b, data_alvo) for b in cfg_bak["bancos"]]
        for a in arquivos:
            ex.estado["pontos_recuperacao"].append({"tipo": "Arquivo .bak (S3)", "origem": a["arn"],
                                                     "instante": a["gravado_em"], "version_id": a["version_id"]})
        ex.dados.update(alvo=alvo, grupo_opcoes=grupo, kms_teste=kms, arquivos_bak=arquivos,
                        referencia_rpo=min(a["gravado_em"] for a in arquivos))
        ex.exigir("Parâmetros conferidos. Prosseguir?")

    def criar():
        _criar_instancia_vazia(ex, _producao(ex), alvo, ".bak (Frente B)", ex.dados["grupo_opcoes"], marcar_m1=True)

    def aguardar():
        _aguardar_disponivel(ex, alvo, None, "Instância criada e disponível")

    def restore():
        _restore(ex, ex.dados["arquivos_bak"], alvo, "frente_B")

    def verificar():
        integridade.executar(ex, "frente_B", ex.dados["endpoints"].get(alvo))

    def imutabilidade():
        _teste_imutabilidade(ex, ex.dados["arquivos_bak"][0])

    ex.passo("preparar", "Preparação — contas, produção, rede, grupo de opções, KMS e .bak do dia anterior", preparar)
    ex.passo("criar", "Marco 1 — criação da instância para o restore do .bak", criar)
    ex.passo("aguardar", "Aguardando a instância ficar disponível", aguardar)
    ex.passo("restore", "Marco 2 — restore do .bak (rds_restore_database)", restore)
    ex.passo("integridade", "Marco 3 — verificação de integridade", verificar)
    ex.passo("imutabilidade", "Verificação de imutabilidade do .bak (Object Lock)", imutabilidade)
    ex.passo("limpeza", "Exclusão das instâncias temporárias", lambda: _limpar_instancias(ex))
    ex.passo("finalizar", "Resultado", lambda: _finalizar(ex))


def _teste_imutabilidade(ex: Execucao, arq: dict) -> None:
    """Tenta excluir uma VERSÃO específica do .bak e espera AccessDenied por Object Lock.

    Salvaguarda: só tenta se o objeto reportar retenção ativa (COMPLIANCE/GOVERNANCE com data futura).
    Sem retenção, a exclusão apagaria o backup — o teste é registrado como falha e não é executado.
    """
    resultado = {"objeto": arq["arn"], "version_id": arq["version_id"], "modo": arq["object_lock_modo"],
                 "reter_ate": arq["object_lock_reter_ate"]}
    reter_ate = tempo.de_iso(arq["object_lock_reter_ate"])
    if not arq["version_id"] or arq["version_id"] == "null":
        resultado.update(executado=False, conclusao="bucket sem versionamento — Object Lock não está ativo")
        ex.lacuna("Bucket do .bak sem versionamento: Object Lock inexistente", "critica")
    elif arq["object_lock_modo"] not in ("COMPLIANCE", "GOVERNANCE") or not reter_ate or reter_ate <= tempo.agora():
        resultado.update(executado=False, conclusao="objeto sem retenção ativa — exclusão NÃO tentada para não apagar o backup")
        ex.lacuna(f"Arquivo .bak {arq['chave']} sem retenção Object Lock ativa", "critica")
    else:
        perfil = ex.servico["bak"].get("perfil_exclusao") or ex.servico["perfil"]
        sess = aws.sessao(perfil, ex.cfg["regiao"])
        aws.conferir_conta(sess, ex.servico["conta_id"], f"de teste de exclusão (perfil {perfil})")
        s3 = sess.client("s3")
        permitido, detalhe_iam = aws.iam_permite(sess, "s3:DeleteObjectVersion", arq["arn"])
        resultado["simulacao_iam"] = detalhe_iam
        print(f"  Objeto: {arq['arn']}\n  VersionId: {arq['version_id']}\n  Retenção: {arq['object_lock_modo']} até {tempo.br(reter_ate)}")
        print(f"  Permissão do perfil (simulação IAM): {detalhe_iam}")
        if permitido is False:
            print("  AVISO: o perfil não tem s3:DeleteObjectVersion; a recusa viria do IAM e não comprovaria o Object Lock.")
            if not ex.confirmar("Executar o teste mesmo assim?"):
                resultado.update(executado=False, conclusao="não executado: perfil sem s3:DeleteObjectVersion")
                ex.lacuna("Teste de imutabilidade não executado: configurar bak.perfil_exclusao com s3:DeleteObjectVersion", "media")
                ex.dados["imutabilidade"] = resultado
                return
        ex.exigir_digitando("TESTAR IMUTABILIDADE",
                            "será enviada uma exclusão da VERSÃO do .bak; com Object Lock ativo a AWS recusa (AccessDenied).")
        horario = tempo.agora()
        try:
            resp = s3.delete_object(Bucket=arq["bucket"], Key=arq["chave"], VersionId=arq["version_id"])
            resultado.update(executado=True, horario=tempo.iso(horario), resposta="EXCLUSÃO ACEITA",
                             request_id=resp.get("ResponseMetadata", {}).get("RequestId"),
                             conclusao="FALHA CRÍTICA: a versão foi excluída — Object Lock não protegeu o backup")
            ex.lacuna("A exclusão da versão do .bak foi ACEITA — Object Lock não protegeu o arquivo", "critica")
        except ClientError as e:
            codigo = aws.codigo_erro(e)
            msg = e.response.get("Error", {}).get("Message", "")
            resultado.update(executado=True, horario=tempo.iso(horario), resposta=codigo, mensagem=msg,
                             request_id=e.response.get("ResponseMetadata", {}).get("RequestId"))
            if codigo == "AccessDenied" and "object lock" in msg.lower():
                resultado["conclusao"] = "AccessDenied por Object Lock — imutabilidade comprovada"
            elif codigo == "AccessDenied" and permitido:
                resultado["conclusao"] = ("AccessDenied com permissão IAM de exclusão confirmada pela simulação — "
                                          "a recusa decorre do Object Lock; imutabilidade comprovada")
            elif codigo == "AccessDenied":
                resultado["conclusao"] = ("AccessDenied sem menção a Object Lock — pode ser falta de permissão IAM do "
                                          "perfil de teste; não comprova a imutabilidade")
                ex.lacuna("Teste de imutabilidade inconclusivo: usar perfil com s3:DeleteObjectVersion", "media")
            else:
                resultado["conclusao"] = f"resposta inesperada ({codigo}) — avaliar"
                ex.lacuna(f"Teste de imutabilidade com resposta inesperada: {codigo} {msg}", "media")
        try:
            s3.head_object(Bucket=arq["bucket"], Key=arq["chave"], VersionId=arq["version_id"])
            resultado["versao_existe_apos_teste"] = True
        except ClientError:
            resultado["versao_existe_apos_teste"] = False
    print(f"  Imutabilidade: {resultado['conclusao']}")
    ex.dados["imutabilidade"] = resultado
    ex.evento("imutabilidade", resultado["conclusao"], resultado)


# ====================================================================== Frente B — banco de um cliente (Onegreen)
def frente_b_cliente(ex: Execucao, args) -> None:
    alvo_pitr = ex.dados.get("alvo") or aws.id_temporaria(ex, "1")
    alvo_dest = ex.dados.get("alvo_destino") or aws.id_temporaria(ex, "2")
    exp = ex.servico["exportacao"]

    def preparar():
        prod = _preparar_comum(ex)
        rds, s3 = _cli(ex, "rds"), _cli(ex, "s3")
        grupo = prod["OptionGroupMemberships"][0]["OptionGroupName"]
        if ex.servico["teste"].get("option_group"):
            grupo = ex.servico["teste"]["option_group"]
        if not aws.grupo_opcoes_tem_backup_nativo(rds, grupo):
            raise Abortado(f"O grupo de opções {grupo} não tem SQLSERVER_BACKUP_RESTORE (item 7.7).")
        print(f"  Grupo de opções {grupo}: SQLSERVER_BACKUP_RESTORE presente")
        kms = aws.verificar_kms(ex.rt["sess"], ex.servico["teste"]["kms_key_id"], ex.servico["conta_id"])
        if kms["problemas"]:
            raise Abortado("Chave KMS de teste inválida: " + "; ".join(kms["problemas"]))
        s3.head_bucket(Bucket=exp["bucket"])
        banco = args.banco_cliente or ex.perguntar("Nome do banco do cliente a recuperar")
        chave = f"{exp.get('prefixo', '')}{ex.exercicio}_{ex.ciclo}/{banco}_{tempo.agora():%Y%m%d_%H%M}.bak"
        _definir_instante(ex, prod, args.instante)
        ex.dados.update(alvo=alvo_pitr, alvo_destino=alvo_dest, grupo_opcoes=grupo, kms_teste=kms, banco_cliente=banco,
                        exportacao={"bucket": exp["bucket"], "chave": chave, "arn": f"arn:aws:s3:::{exp['bucket']}/{chave}"},
                        referencia_rpo=ex.dados["instante_recuperacao"])
        print(f"  Banco do cliente: {banco}\n  Exportação: s3://{exp['bucket']}/{chave}")
        print(f"  Instâncias: {alvo_pitr} (PITR) e {alvo_dest} (destino isolado, faz o papel da produção)")
        ex.exigir("Parâmetros conferidos. Prosseguir?")

    def restaurar():
        prod = _producao(ex)
        _iniciar_pitr(ex, prod, alvo_pitr, "PITR temporária (Frente B — cliente)", marcar_m1=True)
        print("\n  A instância de destino é criada em paralelo: na recuperação real o destino é a produção, que já existe.")
        _criar_instancia_vazia(ex, prod, alvo_dest, "Destino isolado (Frente B — cliente)", ex.dados["grupo_opcoes"], marcar_m1=False)

    def aguardar():
        _aguardar_disponivel(ex, alvo_pitr, None, "Instância PITR disponível")
        _aguardar_disponivel(ex, alvo_dest, None, "Instância de destino disponível")

    def backup_cliente():
        banco, e = ex.dados["banco_cliente"], ex.dados["exportacao"]
        if lambda_sql.nome_funcao(ex):
            try:
                lambda_sql.garantir_senha_gerenciada(ex, alvo_pitr)
                lambda_sql.executar_tarefas(
                    ex, "iniciar_backup", alvo_pitr, [{"banco": banco, "s3_arn": e["arn"]}], _horas_desde_m1(ex),
                    "backup_cliente", "Backup nativo do banco do cliente iniciado (rds_task_status.created_at)",
                    None, "Backup nativo do banco do cliente concluído (rds_task_status = SUCCESS)")
                return
            except lambda_sql.ErroLambda as erro:
                if (ex.dados.get("tarefas_backup_cliente") or {}).get("itens"):
                    raise Abortado(f"Falha na Lambda com o backup já iniciado: {erro}. Rode de novo para retomar.")
                print(f"\n  A Lambda recusou ou falhou: {erro}")
                ex.lacuna(f"Backup do cliente pela Lambda não iniciado: {erro}", "media")
                if not ex.confirmar("Seguir no modo manual (SSMS)?"):
                    raise Abortado("Backup do banco do cliente não executado.")
                ex.dados.pop("tarefas_backup_cliente", None)
        arq = ex.pasta / "backup_cliente.sql"
        arq.write_text("\n".join([
            "-- Backup nativo apenas do banco do cliente (PG 13/03, item 7.7, passo 2)",
            f"-- Executar SOMENTE na instância PITR do exercício: {ex.dados['endpoints'].get(alvo_pitr)}", ""]
            + CABECALHO_TRAVA + _trava_producao(_horas_desde_m1(ex)) + [
            "ELSE",
            "    EXEC msdb.dbo.rds_backup_database",
            f"        @source_db_name = N'{banco}',",
            f"        @s3_arn_to_backup_to = N'{e['arn']}',",
            "        @overwrite_s3_backup_file = 1,",
            "        @type = 'FULL';",
            "GO", "",
            f"exec msdb.dbo.rds_task_status @db_name = '{banco}';", ""]), encoding="utf-8-sig")
        print(f"\n  Execute {arq} na instância PITR ({ex.dados['endpoints'].get(alvo_pitr)}) e acompanhe até SUCCESS.")
        _confirmar_horario_manual(ex, "Horário de INÍCIO do backup nativo:", None, "Backup nativo do banco do cliente iniciado")
        _confirmar_horario_manual(ex, "Horário de CONCLUSÃO do backup nativo (SUCCESS):", None, "Backup nativo do banco do cliente concluído")

    def restore():
        e = ex.dados["exportacao"]
        _restore(ex, [{"banco": ex.dados["banco_cliente"], "arn": e["arn"]}], alvo_dest, "frente_B_cliente")

    def verificar():
        integridade.executar(ex, "frente_B", ex.dados["endpoints"].get(alvo_dest), banco=ex.dados["banco_cliente"])

    def limpeza():
        _limpar_instancias(ex)
        _excluir_exportacao(ex)

    ex.passo("preparar", "Preparação — contas, produção, rede, grupo de opções e banco do cliente", preparar)
    ex.passo("restaurar", "Marco 1 — PITR em temporária e criação do destino isolado", restaurar)
    ex.passo("aguardar", "Aguardando as instâncias ficarem disponíveis", aguardar)
    ex.passo("backup_cliente", "Backup nativo do banco do cliente para o S3", backup_cliente)
    ex.passo("restore", "Marco 2 — restore do banco do cliente no destino isolado", restore)
    ex.passo("integridade", "Marco 3 — verificação de integridade do banco do cliente", verificar)
    ex.passo("limpeza", "Exclusão das instâncias temporárias e do .bak exportado", limpeza)
    ex.passo("finalizar", "Resultado", lambda: _finalizar(ex))


def _excluir_exportacao(ex: Execucao) -> None:
    """O .bak exportado contém dados de cliente (LGPD): excluir todas as versões ao final."""
    e = ex.dados.get("exportacao")
    if not e or ex.dados.get("exportacao_excluida"):
        return
    s3 = _cli(ex, "s3")
    print(f"  O arquivo exportado s3://{e['bucket']}/{e['chave']} contém dados do cliente.")
    if not ex.confirmar("Excluir todas as versões do arquivo exportado?"):
        ex.lacuna(f"Cópia do banco do cliente mantida em s3://{e['bucket']}/{e['chave']}", "alta")
        return
    versoes = s3.list_object_versions(Bucket=e["bucket"], Prefix=e["chave"])
    itens = [v for v in versoes.get("Versions", []) + versoes.get("DeleteMarkers", []) if v["Key"] == e["chave"]]
    falhas = []
    for v in itens:
        try:
            s3.delete_object(Bucket=e["bucket"], Key=e["chave"], VersionId=v["VersionId"])
        except ClientError as erro:
            falhas.append(f"{v['VersionId']}: {aws.codigo_erro(erro)}")
    if not itens:
        try:
            s3.delete_object(Bucket=e["bucket"], Key=e["chave"])
        except ClientError as erro:
            falhas.append(aws.codigo_erro(erro))
    if falhas:
        ex.lacuna("Não foi possível excluir o .bak exportado do cliente: " + "; ".join(falhas), "alta")
    else:
        ex.dados["exportacao_excluida"] = tempo.iso(tempo.agora())
        print("  Arquivo exportado excluído.")
    ex.salvar()


# ====================================================================== Frente C — cofre isolado
def frente_c(ex: Execucao, args) -> None:
    alvo = ex.dados.get("alvo") or aws.id_temporaria(ex)
    cb = exigir_conta_backup(ex.cfg)

    def preparar():
        prod = _preparar_comum(ex)
        bkp_iso = _cli_backup_isolada(ex)
        bkp = _cli(ex, "backup")
        pontos = []
        for pg in bkp_iso.get_paginator("list_recovery_points_by_backup_vault").paginate(
                BackupVaultName=cb["cofre"], ByResourceArn=prod["DBInstanceArn"]):
            pontos += [p for p in pg["RecoveryPoints"] if p.get("Status") == "COMPLETED"]
        if not pontos:
            raise Abortado(f"Nenhum ponto de recuperação COMPLETED de {prod['DBInstanceArn']} no cofre isolado {cb['cofre']}.")
        rp = max(pontos, key=lambda p: p["CreationDate"])
        cofre_dest = bkp.describe_backup_vault(BackupVaultName=ex.servico["cofre_primario"])
        ponto = {
            "tipo": "Cópia mais recente do cofre isolado",
            "origem": rp["RecoveryPointArn"],
            "instante": tempo.iso(rp["CreationDate"]),
            "concluido_em": tempo.iso(rp.get("CompletionDate")),
            "chave_kms_origem": rp.get("EncryptionKeyArn"),
            "cofre_origem": cb["cofre"],
            "conta_origem": cb["conta_id"],
        }
        ex.estado["pontos_recuperacao"].append(ponto)
        ex.dados.update(alvo=alvo, ponto_isolado=ponto, referencia_rpo=ponto["instante"],
                        cofre_destino={"nome": cofre_dest["BackupVaultName"], "arn": cofre_dest["BackupVaultArn"],
                                       "kms": cofre_dest.get("EncryptionKeyArn")})
        print(f"  Ponto de recuperação no cofre isolado: {rp['RecoveryPointArn']}")
        print(f"    criado em {tempo.br(rp['CreationDate'])} — idade {tempo.duracao((tempo.agora() - tempo.para_local(rp['CreationDate'])).total_seconds())}")
        print(f"  Cofre de destino: {cofre_dest['BackupVaultArn']}")
        print(f"  Executante da cópia: Custodiante da Conta de Backup ({cb.get('custodiante', 'ver PG 13/09')})")
        ex.exigir("Parâmetros conferidos. Prosseguir?")

    def copiar():
        bkp_iso = _cli_backup_isolada(ex)
        req = dict(RecoveryPointArn=ex.dados["ponto_isolado"]["origem"], SourceBackupVaultName=cb["cofre"],
                   DestinationBackupVaultArn=ex.dados["cofre_destino"]["arn"], IamRoleArn=cb["papel_copia_arn"],
                   IdempotencyToken=f"{ex.exercicio}-{ex.ciclo}-C")
        if cb.get("ciclo_vida_copia_dias"):
            req["Lifecycle"] = {"DeleteAfterDays": int(cb["ciclo_vida_copia_dias"])}
        print(f"  Cópia de retorno: {cb['cofre']} (conta {cb['conta_id']}) -> {ex.dados['cofre_destino']['nome']} (conta {ex.servico['conta_id']})")
        ex.exigir("Iniciar a cópia de retorno (item 7.5)?")
        ex.marco("m1", "Início da restauração (cópia de retorno do cofre isolado)")
        resp = bkp_iso.start_copy_job(**req)
        ex.dados["copia"] = {"copy_job_id": resp["CopyJobId"], "iniciada_em": tempo.iso(tempo.agora())}
        ex.evento("aws", "start_copy_job", {"pedido": req, "resposta": resp})

    def aguardar_copia():
        import time as _t
        bkp_iso = _cli_backup_isolada(ex)
        job_id = ex.dados["copia"]["copy_job_id"]
        ultimo = None
        limite = _t.monotonic() + ex.cfg["tempo_maximo_espera_min"] * 60
        while True:
            job = bkp_iso.describe_copy_job(CopyJobId=job_id)["CopyJob"]
            if job["State"] != ultimo:
                print(f"    {tempo.br(tempo.agora())}  cópia: {job['State']}")
                ex.evento("status_copia", job["State"])
                ultimo = job["State"]
            if job["State"] == "COMPLETED":
                break
            if job["State"] in ("FAILED", "ABORTED"):
                ex.dados["copia"]["falha"] = job.get("StatusMessage")
                ex.lacuna(f"Cópia de retorno falhou: {job.get('StatusMessage')}", "critica")
                ex.salvar()
                raise Abortado("A cópia de retorno do cofre isolado falhou — registrar Não Conformidade.")
            if _t.monotonic() > limite:
                raise Abortado("Tempo máximo de espera da cópia excedido.")
            _t.sleep(ex.cfg["intervalo_consulta_seg"])
        fim = tempo.agora()
        ex.marco_extra("Cópia de retorno concluída", fim)
        ini = tempo.de_iso(ex.dados["copia"]["iniciada_em"])
        ex.dados["copia"].update(concluida_em=tempo.iso(fim), duracao_seg=(fim - ini).total_seconds(),
                                 ponto_destino=job["DestinationRecoveryPointArn"])
        print(f"  Tempo da cópia de retorno: {tempo.duracao((fim - ini).total_seconds())}")

    def verificar_kms():
        bkp, rds = _cli(ex, "backup"), _cli(ex, "rds")
        arn_dest = ex.dados["copia"]["ponto_destino"]
        rp = bkp.describe_recovery_point(BackupVaultName=ex.dados["cofre_destino"]["nome"], RecoveryPointArn=arn_dest)
        id_snap = arn_dest.split(":snapshot:", 1)[-1]
        try:
            snap = rds.describe_db_snapshots(DBSnapshotIdentifier=id_snap)["DBSnapshots"][0]
        except ClientError:
            snaps = rds.describe_db_snapshots(SnapshotType="awsbackup",
                                              DBInstanceIdentifier=ex.servico["instancia_producao"])["DBSnapshots"]
            snap = next((s for s in snaps if s["DBSnapshotArn"] == arn_dest), {})
        chave_rp, chave_snap = rp.get("EncryptionKeyArn"), snap.get("KmsKeyId")
        kms = {
            "ponto_destino": arn_dest,
            "snapshot": id_snap,
            "chave_ponto_recuperacao": chave_rp,
            "chave_snapshot": chave_snap,
            "conta_chave": aws.conta_do_arn(chave_snap or chave_rp),
            "conta_destino": ex.servico["conta_id"],
        }
        kms["chave_da_conta_destino"] = str(kms["conta_chave"]) == str(ex.servico["conta_id"])
        try:
            if chave_snap:
                meta = ex.rt["sess"].client("kms").describe_key(KeyId=chave_snap)["KeyMetadata"]
                kms["estado_chave"] = meta["KeyState"]
                kms["chave_utilizavel_pela_conta_destino"] = meta["KeyState"] == "Enabled"
        except ClientError as e:
            kms["estado_chave"] = f"erro: {aws.codigo_erro(e)}"
            kms["chave_utilizavel_pela_conta_destino"] = False
        ex.dados["kms_frente_c"] = kms
        ex.dados["snapshot_copiado"] = id_snap
        print(f"  Chave KMS do snapshot copiado: {chave_snap} (conta {kms['conta_chave']})")
        if not kms["chave_da_conta_destino"]:
            ex.lacuna("O snapshot copiado não está criptografado com chave da conta de destino", "alta")
        else:
            print("  Chave pertence à conta de destino: OK")

    def restaurar():
        rds = _cli(ex, "rds")
        prod = _producao(ex)
        aws.garantir_nao_producao(ex, alvo)
        if aws.descrever(rds, alvo) is not None:
            raise Abortado(f"Já existe uma instância {alvo}.")
        params = aws.parametros_restauracao(ex, prod)
        params.pop("StorageType", None)
        req = dict(DBInstanceIdentifier=alvo, DBSnapshotIdentifier=ex.dados["snapshot_copiado"],
                   Tags=aws.tags_exercicio(ex, alvo), **params)
        print(f"  Restore do snapshot {ex.dados['snapshot_copiado']} em {alvo} "
              f"(subnet {params['DBSubnetGroupName']}, sem acesso público)")
        ex.exigir("Iniciar o restore do snapshot copiado?")
        ex.marco_extra("Início do restore do snapshot copiado")
        resp = rds.restore_db_instance_from_db_snapshot(**req)
        ex.evento("aws", "restore_db_instance_from_db_snapshot", {"pedido": {k: v for k, v in req.items() if k != "Tags"},
                                                                  "status": resp["DBInstance"]["DBInstanceStatus"]})
        aws.registrar_instancia(ex, alvo, "Restore da cópia do cofre isolado (Frente C)")

    def aguardar():
        _aguardar_disponivel(ex, alvo, "m2", "Instância disponível (status available)")

    def verificar():
        integridade.executar(ex, "frente_C", ex.dados["endpoints"].get(alvo))

    def limpeza():
        _limpar_instancias(ex)
        arn = (ex.dados.get("copia") or {}).get("ponto_destino")
        if arn and not ex.dados.get("copia_excluida") and ex.confirmar(
                "Excluir do cofre primário o ponto de recuperação copiado no exercício?"):
            try:
                _cli(ex, "backup").delete_recovery_point(BackupVaultName=ex.dados["cofre_destino"]["nome"], RecoveryPointArn=arn)
                ex.dados["copia_excluida"] = tempo.iso(tempo.agora())
                print("  Ponto de recuperação copiado excluído.")
            except ClientError as e:
                ex.lacuna(f"Ponto copiado não excluído ({aws.codigo_erro(e)}); expira pelo ciclo de vida do cofre", "baixa")

    ex.passo("preparar", "Preparação — contas, produção, rede, ponto do cofre isolado e cofre de destino", preparar)
    ex.passo("copiar", "Marco 1 — início da cópia de retorno do cofre isolado", copiar)
    ex.passo("aguardar_copia", "Aguardando a cópia de retorno", aguardar_copia)
    ex.passo("verificar_kms", "Conferência da chave KMS na conta de destino", verificar_kms)
    ex.passo("restaurar", "Restore do snapshot copiado em instância temporária", restaurar)
    ex.passo("aguardar", "Marco 2 — aguardando a instância ficar disponível", aguardar)
    ex.passo("integridade", "Marco 3 — verificação de integridade", verificar)
    ex.passo("limpeza", "Exclusão das instâncias temporárias", limpeza)
    ex.passo("finalizar", "Resultado", lambda: _finalizar(ex))


def executar(ex: Execucao, args) -> None:
    ex.rt["marco2_manual"] = bool(getattr(args, "marco2_manual", False))
    if ex.frente == "A":
        frente_a(ex, args)
    elif ex.frente == "B":
        (frente_b_cliente if ex.servico["tipo_frente_b"] == "cliente" else frente_b_bak)(ex, args)
    elif ex.frente == "C":
        frente_c(ex, args)
    else:
        raise Abortado(f"Frente inválida: {ex.frente}")
