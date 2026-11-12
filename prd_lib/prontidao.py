"""Verificação de prontidão (somente leitura): confere tudo o que o exercício vai usar, sem criar nada.

Rodar antes de programar o exercício e novamente no dia, antes de `executar`.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from botocore.exceptions import ClientError, ProfileNotFound

from . import aws, tempo
from .config import prefixos_do_dia, regiao_conta_backup
from .contexto import Abortado


class Lista:
    def __init__(self):
        self.itens = []

    def add(self, situacao: str, item: str, detalhe: str = "") -> None:
        self.itens.append({"situacao": situacao, "item": item, "detalhe": detalhe})
        print(f"  [{situacao:<6}] {item}" + (f" — {detalhe}" if detalhe else ""))

    def ok(self, item, detalhe=""):
        self.add("OK", item, detalhe)

    def falha(self, item, detalhe=""):
        self.add("FALHA", item, detalhe)

    def aviso(self, item, detalhe=""):
        self.add("AVISO", item, detalhe)


class _Falhou:
    def __bool__(self):
        return False


FALHOU = _Falhou()


def _tentar(lista: Lista, item: str, funcao):
    try:
        return funcao()
    except (ClientError, ProfileNotFound, Abortado) as e:
        lista.falha(item, str(e))
    except Exception as e:  # noqa: BLE001
        lista.falha(item, f"{type(e).__name__}: {e}")
    return FALHOU


def _verificar_lambda(L: Lista, sess, t: dict, prod: dict | None) -> None:
    """Lambda do Marco 2: ativa, na sub-rede de teste, saída só para o SG de teste e sem acesso à produção."""
    nome = t["lambda_sql"]
    lam, ec2 = sess.client("lambda"), sess.client("ec2")
    conf = _tentar(L, f"Lambda {nome}", lambda: lam.get_function_configuration(FunctionName=nome))
    if not conf:
        return
    (L.ok if conf.get("State", "Active") == "Active" else L.falha)(f"Lambda {nome}", f"estado {conf.get('State')}, {conf.get('Runtime')}")
    sgs_lambda = (conf.get("VpcConfig") or {}).get("SecurityGroupIds") or []
    if not sgs_lambda:
        L.falha("Lambda na VPC", "função sem VpcConfig — não alcança a sub-rede de teste")
        return
    teste = set(t["security_group_ids"])
    problemas, saida_para = [], set()
    for sg in ec2.describe_security_groups(GroupIds=sgs_lambda)["SecurityGroups"]:
        for regra in sg.get("IpPermissionsEgress", []):
            destinos = {g["GroupId"] for g in regra.get("UserIdGroupPairs", [])}
            saida_para |= destinos
            abertos = regra.get("IpRanges") or regra.get("Ipv6Ranges") or regra.get("PrefixListIds")
            if abertos or regra.get("IpProtocol") == "-1" or regra.get("FromPort") != 1433 or not destinos <= teste:
                problemas.append(f"{sg['GroupId']} tem saída além de TCP 1433 para o SG de teste")
    if not saida_para & teste:
        problemas.append(f"nenhuma saída 1433 para o SG de teste {', '.join(sorted(teste))} "
                         f"(saída hoje para: {', '.join(sorted(saida_para)) or 'nenhum SG'}) — atualizar a pilha com SgTesteId")
    (L.falha if problemas else L.ok)("Saída da Lambda restrita ao SG de teste (1433)",
                                     "; ".join(problemas) or f"SG da Lambda {', '.join(sgs_lambda)} → {', '.join(sorted(saida_para))}")
    entrada, origens = False, set()
    for sg in ec2.describe_security_groups(GroupIds=list(teste))["SecurityGroups"]:
        for regra in sg.get("IpPermissions", []):
            grupos = {g["GroupId"] for g in regra.get("UserIdGroupPairs", [])}
            origens |= grupos
            if grupos & set(sgs_lambda) and regra.get("FromPort") in (1433, None) and regra.get("IpProtocol") in ("tcp", "-1"):
                entrada = True
    (L.ok if entrada else L.falha)(
        "SG de teste aceita a Lambda na porta 1433",
        f"{', '.join(sorted(teste))} ← {', '.join(sgs_lambda)}" if entrada else
        f"{', '.join(sorted(teste))} não tem entrada 1433 vinda do SG da Lambda {', '.join(sgs_lambda)} "
        f"(origens por SG hoje: {', '.join(sorted(origens)) or 'nenhuma'}) — atualizar a pilha com SgTesteId")
    if prod:
        sgs_prod = [g["VpcSecurityGroupId"] for g in prod.get("VpcSecurityGroups", [])]
        abre = [sg["GroupId"] for sg in ec2.describe_security_groups(GroupIds=sgs_prod)["SecurityGroups"]
                for r in sg.get("IpPermissions", []) if {g["GroupId"] for g in r.get("UserIdGroupPairs", [])} & set(sgs_lambda)]
        (L.falha if abre else L.ok)("Produção não aceita conexões da Lambda", ", ".join(abre))
    r = _tentar(L, "Lambda responde (ping)", lambda: json.loads(lam.invoke(FunctionName=nome, Payload=b'{"acao": "ping"}')["Payload"].read()))
    if r:
        (L.ok if r.get("ok") else L.falha)("Lambda responde (ping)", f"pymssql {r.get('pymssql')}, prefixo {r.get('prefixo')}"
                                           if r.get("ok") else str(r))


def verificar(cfg: dict, exercicio: str, ciclo: str) -> Path:
    s = cfg["servicos"][exercicio]
    regiao = cfg["regiao"]
    L = Lista()
    print(f"\nVerificação de prontidão — {exercicio} ({s['nome']}), ciclo {ciclo}\n")

    sess = aws.sessao(s["perfil"], regiao)
    if _tentar(L, f"Perfil '{s['perfil']}' na conta {s['conta_id']}", lambda: aws.conferir_conta(sess, s["conta_id"], "do serviço")):
        L.ok(f"Perfil '{s['perfil']}' na conta {s['conta_id']}")
    rds = sess.client("rds")

    prod = _tentar(L, "Instância de produção", lambda: aws.descrever(rds, s["instancia_producao"]))
    if prod:
        L.ok("Instância de produção", f"{prod['DBInstanceIdentifier']} {prod['DBInstanceStatus']} {prod['DBInstanceClass']}")
        (L.ok if prod.get("StorageEncrypted") else L.falha)("Produção criptografada", prod.get("KmsKeyId") or "sem criptografia")
        lrt = prod.get("LatestRestorableTime")
        if lrt:
            atraso = (tempo.agora() - tempo.para_local(lrt)).total_seconds()
            (L.ok if atraso <= s["metas"]["A"]["rpo_min"] * 60 else L.aviso)(
                "Backup contínuo (PITR)", f"último restaurável {tempo.br(lrt)} — atraso {tempo.duracao(atraso)}")
        else:
            L.falha("Backup contínuo (PITR)", "LatestRestorableTime ausente")
        tags = aws.tags_da_instancia(prod)
        (L.ok if aws.TAG_BACKUP in tags else L.aviso)(f"Tag {aws.TAG_BACKUP} na produção", tags.get(aws.TAG_BACKUP, "ausente"))
    elif prod is None:
        L.falha("Instância de produção", f"{s['instancia_producao']} não encontrada")

    t = s["teste"]
    rede = _tentar(L, "Rede de teste", lambda: aws.verificar_rede_teste(sess, t["db_subnet_group"], t["security_group_ids"]))
    if rede:
        if rede["problemas"]:
            for p in rede["problemas"]:
                L.falha("Rede de teste", p)
        else:
            L.ok("Rede de teste privada e sem entrada da internet", f"{rede['subnet_group']} / {', '.join(rede['security_groups'])}")

    if t.get("lambda_sql"):
        _verificar_lambda(L, sess, t, prod if prod else None)
    else:
        L.aviso("Lambda do Marco 2 não configurada", "teste.lambda_sql vazio — restore/backup nativos serão manuais (SSMS)")

    kms = _tentar(L, "Chave KMS de teste", lambda: aws.verificar_kms(sess, t["kms_key_id"], s["conta_id"]))
    if kms:
        (L.falha if kms["problemas"] else L.ok)("Chave KMS de teste", "; ".join(kms["problemas"]) or kms["arn"])

    if prod:
        grupo = t.get("option_group_bak") or t.get("option_group") or prod["OptionGroupMemberships"][0]["OptionGroupName"]
        tem = _tentar(L, "Grupo de opções", lambda: aws.grupo_opcoes_tem_backup_nativo(rds, grupo))
        if tem is not FALHOU:
            (L.ok if tem else L.falha)(f"SQLSERVER_BACKUP_RESTORE no grupo {grupo}", "" if tem else "ausente (Frente B)")

    # Frente B
    s3 = sess.client("s3")
    if s["tipo_frente_b"] == "bak":
        ontem = (tempo.agora() - timedelta(days=1)).date()
        for b in s["bak"]["bancos"]:
            def ultimo_bak(b=b):
                ultimos = []
                for pref in prefixos_do_dia(b.get("prefixo"), ontem):
                    for pg in s3.get_paginator("list_objects_v2").paginate(Bucket=s["bak"]["bucket"], Prefix=pref):
                        ultimos += [o for o in pg.get("Contents", []) if o["Key"].lower().endswith(".bak")]
                return max(ultimos, key=lambda o: o["LastModified"]) if ultimos else None
            o = _tentar(L, f".bak de {b['nome']}", ultimo_bak)
            if o:
                data = tempo.para_local(o["LastModified"])
                (L.ok if data.date() >= ontem else L.aviso)(f".bak mais recente de {b['nome']}", f"{o['Key']} ({tempo.br(data)})")
                h = _tentar(L, f"Object Lock do .bak de {b['nome']}", lambda o=o: s3.head_object(Bucket=s["bak"]["bucket"], Key=o["Key"]))
                if h:
                    ate = h.get("ObjectLockRetainUntilDate")
                    ativo = h.get("ObjectLockMode") and ate and tempo.para_local(ate) > tempo.agora()
                    (L.ok if ativo else L.falha)(f"Object Lock do .bak de {b['nome']}",
                                                 f"{h.get('ObjectLockMode') or 'ausente'} até {tempo.br(ate) if ate else '—'}")
            elif o is None:
                L.falha(f".bak de {b['nome']}", "nenhum arquivo encontrado")
        perfil_exc = s["bak"].get("perfil_exclusao")
        if perfil_exc:
            sx = aws.sessao(perfil_exc, regiao)
            if _tentar(L, f"Perfil de teste de exclusão '{perfil_exc}'", lambda: aws.conferir_conta(sx, s["conta_id"], "de exclusão")):
                L.ok(f"Perfil de teste de exclusão '{perfil_exc}'")
    else:
        exp = s["exportacao"]
        if _tentar(L, "Bucket de exportação", lambda: s3.head_bucket(Bucket=exp["bucket"])):
            L.ok("Bucket de exportação do banco do cliente", exp["bucket"])

    # Frente C
    cb = cfg.get("conta_backup") or {}
    siso = aws.sessao(cb["perfil"], regiao_conta_backup(cfg)) if cb.get("perfil") else None
    if not siso:
        L.aviso("conta_backup não configurada", "Frente C indisponível neste arquivo (executada pelo Custodiante)")
    elif _tentar(L, f"Perfil da conta de backup '{cb['perfil']}'", lambda: aws.conferir_conta(siso, cb["conta_id"], "de backup isolada")):
        L.ok(f"Perfil da conta de backup '{cb['perfil']}' na conta {cb['conta_id']}", f"cofre isolado em {regiao_conta_backup(cfg)}")
        if prod:
            def ultimo_ponto():
                pts = []
                for pg in siso.client("backup").get_paginator("list_recovery_points_by_backup_vault").paginate(
                        BackupVaultName=cb["cofre"], ByResourceArn=prod["DBInstanceArn"]):
                    pts += [p for p in pg["RecoveryPoints"] if p.get("Status") == "COMPLETED"]
                return max(pts, key=lambda p: p["CreationDate"]) if pts else None
            rp = _tentar(L, "Ponto no cofre isolado", ultimo_ponto)
            if rp:
                idade = (tempo.agora() - tempo.para_local(rp["CreationDate"])).total_seconds()
                (L.ok if idade <= s["metas"]["C"]["rpo_min"] * 60 else L.aviso)(
                    "Ponto mais recente no cofre isolado", f"{tempo.br(rp['CreationDate'])} — idade {tempo.duracao(idade)}")
            elif rp is None:
                L.falha("Ponto no cofre isolado", f"nenhum ponto COMPLETED de {s['instancia_producao']} em {cb['cofre']}")
    cofre = _tentar(L, "Cofre primário (destino da cópia)", lambda: sess.client("backup").describe_backup_vault(BackupVaultName=s["cofre_primario"]))
    if cofre:
        L.ok("Cofre primário (destino da cópia)", cofre["BackupVaultArn"])

    if not s.get("custo_hora_usd"):
        L.aviso("custo_hora_usd não definido", "o custo do exercício não será estimado")
    if not (s.get("integridade") or {}).get("tabelas"):
        L.aviso("integridade.tabelas vazio", "defina as tabelas de referência")

    falhas = sum(1 for i in L.itens if i["situacao"] == "FALHA")
    print(f"\n  {falhas} falha(s), {sum(1 for i in L.itens if i['situacao'] == 'AVISO')} aviso(s).")
    pasta = Path(cfg["pasta_evidencias"]) / f"{exercicio}_{ciclo}"
    pasta.mkdir(parents=True, exist_ok=True)
    arq = pasta / f"verificacao_{tempo.agora():%Y%m%d_%H%M%S}.json"
    arq.write_text(json.dumps({"exercicio": exercicio, "ciclo": ciclo, "horario": tempo.iso(tempo.agora()), "itens": L.itens},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Resultado gravado em {arq}")
    return arq
