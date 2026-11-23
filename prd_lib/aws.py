"""Operações AWS comuns, com as salvaguardas do PG 13/03, seção 8.

Salvaguardas aplicadas em todo o código:
  * o perfil AWS precisa estar na conta configurada (evita operar na conta errada);
  * nenhuma operação de alteração ou exclusão aceita identificador de produção;
  * instância temporária só é excluída se tiver o prefixo configurado e a tag PRD-Exercicio;
  * a instância temporária é criada com a configuração de rede do sistema (teste.acesso_publico; padrão: sem
    acesso público), entrada restrita pelo grupo de segurança de teste, com criptografia, na sub-rede de teste
    e sem a tag IUS-BKP-RES (fica fora da política de backup do PG 13/09).
"""

from __future__ import annotations

import time

import boto3
from botocore.exceptions import ClientError

from . import tempo
from .config import identificadores_producao
from .contexto import Abortado, Execucao

TAG_EXERCICIO = "PRD-Exercicio"
TAG_BACKUP = "IUS-BKP-RES"


# ---------------------------------------------------------------------- sessões
def sessao(perfil: str, regiao: str) -> boto3.Session:
    return boto3.Session(profile_name=perfil, region_name=regiao)


def conferir_conta(sess: boto3.Session, conta_esperada: str, rotulo: str) -> dict:
    ident = sess.client("sts").get_caller_identity()
    if str(ident["Account"]) != str(conta_esperada):
        raise Abortado(
            f"O perfil de {rotulo} está na conta {ident['Account']}, mas a configuração indica {conta_esperada}."
        )
    print(f"  Conta {rotulo}: {ident['Account']} — {ident['Arn']}")
    return {"conta": ident["Account"], "arn": ident["Arn"]}


# ---------------------------------------------------------------------- identificação
def id_temporaria(ex: Execucao, sufixo: str = "") -> str:
    data = tempo.agora().strftime("%d%m%Y")
    base = f"{ex.cfg['prefixo_temporaria']}-{ex.exercicio.lower()}{ex.frente.lower()}{sufixo}-{data}"
    return base[:63].rstrip("-")


def tags_exercicio(ex: Execucao, identificador: str) -> list[dict]:
    return [
        {"Key": "Name", "Value": identificador},
        {"Key": TAG_EXERCICIO, "Value": f"{ex.exercicio}-{ex.ciclo}-{ex.frente}"},
        {"Key": "PRD-Temporaria", "Value": "sim"},
        {"Key": "PRD-Executante", "Value": ex.estado.get("executante", "")[:250]},
    ]


def garantir_nao_producao(ex: Execucao, identificador: str) -> None:
    if identificador.lower() in identificadores_producao(ex.cfg):
        raise Abortado(f"SALVAGUARDA: '{identificador}' é identificador de produção. Operação bloqueada.")
    if not identificador.lower().startswith(ex.cfg["prefixo_temporaria"].lower() + "-"):
        raise Abortado(
            f"SALVAGUARDA: '{identificador}' não tem o prefixo de instância temporária "
            f"'{ex.cfg['prefixo_temporaria']}-'. Operação bloqueada."
        )


# ---------------------------------------------------------------------- RDS
def descrever(rds, identificador: str) -> dict | None:
    try:
        return rds.describe_db_instances(DBInstanceIdentifier=identificador)["DBInstances"][0]
    except ClientError as e:
        if e.response["Error"]["Code"] == "DBInstanceNotFound":
            return None
        raise


def tags_da_instancia(inst: dict) -> dict:
    return {t["Key"]: t["Value"] for t in inst.get("TagList", [])}


def aguardar_status(ex: Execucao, rds, identificador: str, alvo: str = "available", id_alternativo: str | None = None) -> dict:
    """Consulta o status até atingir o alvo. id_alternativo é usado durante a renomeação."""
    intervalo = ex.cfg["intervalo_consulta_seg"]
    limite = time.monotonic() + ex.cfg["tempo_maximo_espera_min"] * 60
    ultimo = None
    print(f"  Aguardando '{identificador}' chegar a '{alvo}' (consulta a cada {intervalo}s; Ctrl+C interrompe e permite retomar)...")
    while True:
        inst = descrever(rds, identificador)
        if inst is None and id_alternativo:
            inst = descrever(rds, id_alternativo)
        status = inst["DBInstanceStatus"] if inst else "inexistente"
        if status != ultimo:
            print(f"    {tempo.br(tempo.agora())}  status: {status}")
            ex.evento("status_rds", f"{identificador}: {status}")
            ultimo = status
        if inst and inst["DBInstanceIdentifier"] == identificador and status == alvo:
            return inst
        if status in ("failed", "incompatible-restore", "incompatible-parameters", "incompatible-network", "storage-full"):
            raise Abortado(f"Instância {identificador} entrou em estado de falha: {status}")
        if time.monotonic() > limite:
            raise Abortado(f"Tempo máximo de espera excedido para {identificador} (último status: {status}).")
        time.sleep(intervalo)


def aguardar_exclusao(ex: Execucao, rds, identificador: str) -> None:
    intervalo = ex.cfg["intervalo_consulta_seg"]
    limite = time.monotonic() + ex.cfg["tempo_maximo_espera_min"] * 60
    print(f"  Aguardando a exclusão de '{identificador}'...")
    while descrever(rds, identificador) is not None:
        if time.monotonic() > limite:
            raise Abortado(f"Tempo máximo de espera excedido na exclusão de {identificador}.")
        time.sleep(intervalo)


def grupo_opcoes_tem_backup_nativo(rds, nome: str) -> bool:
    grupos = rds.describe_option_groups(OptionGroupName=nome)["OptionGroupsList"]
    return any(o["OptionName"] == "SQLSERVER_BACKUP_RESTORE" for g in grupos for o in g.get("Options", []))


def parametros_restauracao(ex: Execucao, prod: dict) -> dict:
    """Parâmetros comuns de rede, classe e grupos para qualquer instância temporária."""
    t = ex.servico["teste"]
    grupo_opcoes = t.get("option_group") or prod["OptionGroupMemberships"][0]["OptionGroupName"]
    grupo_param = t.get("parameter_group") or prod["DBParameterGroups"][0]["DBParameterGroupName"]
    return {
        "DBSubnetGroupName": t["db_subnet_group"],
        "VpcSecurityGroupIds": list(t["security_group_ids"]),
        "PubliclyAccessible": bool(t.get("acesso_publico")),
        "MultiAZ": False,
        "DBInstanceClass": t.get("classe_instancia") or prod["DBInstanceClass"],
        "OptionGroupName": grupo_opcoes,
        "DBParameterGroupName": grupo_param,
        "StorageType": prod.get("StorageType", "gp3"),
        "DeletionProtection": False,
        "CopyTagsToSnapshot": False,
        "AutoMinorVersionUpgrade": False,
    }


def registrar_instancia(ex: Execucao, identificador: str, papel: str, criada_em=None) -> None:
    ex.estado["instancias"][identificador] = {
        "papel": papel,
        "criada_em": tempo.iso(criada_em or tempo.agora()),
        "excluida_em": None,
        "controles": None,
    }
    ex.salvar()


def verificar_controles(ex: Execucao, rds, inst: dict) -> dict:
    """Confere os controles da seção 8 na instância temporária e registra o resultado."""
    t = ex.servico["teste"]
    tags = tags_da_instancia(inst)
    sgs = sorted(g["VpcSecurityGroupId"] for g in inst.get("VpcSecurityGroups", []))
    controles = {
        "identificador": inst["DBInstanceIdentifier"],
        "arn": inst["DBInstanceArn"],
        "classe": inst["DBInstanceClass"],
        "endpoint": (inst.get("Endpoint") or {}).get("Address"),
        "subnet_group": (inst.get("DBSubnetGroup") or {}).get("DBSubnetGroupName"),
        "vpc": (inst.get("DBSubnetGroup") or {}).get("VpcId"),
        "security_groups": sgs,
        "acesso_publico": inst.get("PubliclyAccessible"),
        "criptografia": inst.get("StorageEncrypted"),
        "kms_key_id": inst.get("KmsKeyId"),
        "grupo_opcoes": [g["OptionGroupName"] for g in inst.get("OptionGroupMemberships", [])],
        "protecao_exclusao": inst.get("DeletionProtection"),
        "tag_ius_bkp_res": tags.get(TAG_BACKUP),
        "segredo_usuario_mestre": (inst.get("MasterUserSecret") or {}).get("SecretArn"),
        "verificado_em": tempo.iso(tempo.agora()),
    }
    problemas = []
    if bool(controles["acesso_publico"]) != bool(t.get("acesso_publico")):
        problemas.append(f"acesso público = {controles['acesso_publico']}, diferente do configurado "
                         f"(teste.acesso_publico = {bool(t.get('acesso_publico'))})")
    if not controles["criptografia"]:
        problemas.append("instância sem criptografia em repouso")
    if controles["subnet_group"] != t["db_subnet_group"]:
        problemas.append(f"subnet group {controles['subnet_group']} diferente do de teste {t['db_subnet_group']}")
    if sgs != sorted(t["security_group_ids"]):
        problemas.append(f"grupos de segurança {sgs} diferentes dos de teste")
    controles["problemas"] = problemas
    ident = inst["DBInstanceIdentifier"]
    if ident in ex.estado["instancias"]:
        ex.estado["instancias"][ident]["controles"] = controles
    print(f"  Controles de {ident}: público={controles['acesso_publico']}  criptografia={controles['criptografia']}  "
          f"subnet={controles['subnet_group']}  SG={','.join(sgs)}")
    print(f"    KMS: {controles['kms_key_id']}")
    for p in problemas:
        ex.lacuna(f"{ident}: {p}", "alta")

    # Tag IUS-BKP-RES: a temporária não pode entrar na política de backup.
    if controles["tag_ius_bkp_res"] is not None:
        ex.lacuna(f"{ident} herdou a tag {TAG_BACKUP}={controles['tag_ius_bkp_res']} — na recuperação real, "
                  "conferir se a tag é herdada ou precisa ser aplicada manualmente", "baixa")
        garantir_nao_producao(ex, ident)
        rds.remove_tags_from_resource(ResourceName=inst["DBInstanceArn"], TagKeys=[TAG_BACKUP])
        ex.evento("aws", f"Tag {TAG_BACKUP} removida da instância temporária {ident}")
        print(f"  Tag {TAG_BACKUP} removida da temporária para mantê-la fora da política de backup.")
    else:
        print(f"  {ident} não herdou a tag {TAG_BACKUP} (na recuperação real ela é aplicada manualmente, valor RDS).")
    ex.salvar()
    return controles


def excluir_temporaria(ex: Execucao, rds, identificador: str) -> None:
    garantir_nao_producao(ex, identificador)
    inst = descrever(rds, identificador)
    if inst is None:
        print(f"  {identificador} já não existe.")
        registro = ex.estado["instancias"].get(identificador)
        if registro and not registro.get("excluida_em"):
            registro["excluida_em"] = tempo.iso(tempo.agora())
            registro["evidencia_exclusao"] = "DBInstanceNotFound na consulta"
        ex.salvar()
        return
    tags = tags_da_instancia(inst)
    if TAG_EXERCICIO not in tags:
        raise Abortado(f"SALVAGUARDA: {identificador} não tem a tag {TAG_EXERCICIO}; exclusão bloqueada.")
    if inst.get("DeletionProtection"):
        raise Abortado(f"{identificador} tem proteção contra exclusão ativa; verifique manualmente.")
    resp = rds.delete_db_instance(DBInstanceIdentifier=identificador, SkipFinalSnapshot=True, DeleteAutomatedBackups=True)
    ex.evento("aws", f"delete_db_instance {identificador}", resp.get("DBInstance", {}).get("DBInstanceStatus"))
    solicitado = tempo.agora()
    aguardar_exclusao(ex, rds, identificador)
    concluido = tempo.agora()
    registro = ex.estado["instancias"].setdefault(identificador, {"papel": "temporária"})
    registro["exclusao_solicitada_em"] = tempo.iso(solicitado)
    registro["excluida_em"] = tempo.iso(concluido)
    registro["evidencia_exclusao"] = (
        f"delete_db_instance aceito em {tempo.br(solicitado)}; describe_db_instances retornou "
        f"DBInstanceNotFound em {tempo.br(concluido)}"
    )
    print(f"  {identificador} excluída em {tempo.br(concluido)}.")
    ex.salvar()


# ---------------------------------------------------------------------- rede de teste
def verificar_rede_teste(sess: boto3.Session, subnet_group: str, security_groups: list[str],
                         cidrs_autorizados: list[str] | None = None, acesso_publico: bool = False) -> dict:
    """Confere a rede de teste.

    Padrão (PG 13/03, seção 8): sub-redes privadas e SGs sem entrada da internet.
    Com cidrs_autorizados (teste.acesso_publico = true): as sub-redes precisam ser públicas para a
    instância ser alcançável, e toda entrada por IP nos SGs tem de estar dentro dos CIDRs autorizados.
    """
    import ipaddress
    publico = acesso_publico or bool(cidrs_autorizados)
    ips_entrada = []
    redes_ok = [ipaddress.ip_network(c, strict=False) for c in (cidrs_autorizados or [])]
    rds = sess.client("rds")
    ec2 = sess.client("ec2")
    problemas = []
    grupo = rds.describe_db_subnet_groups(DBSubnetGroupName=subnet_group)["DBSubnetGroups"][0]
    vpc = grupo["VpcId"]
    subnets = [s["SubnetIdentifier"] for s in grupo["Subnets"]]
    tabelas = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc]}])["RouteTables"]
    principal = next((t for t in tabelas if any(a.get("Main") for a in t.get("Associations", []))), None)
    publicas = []
    for sn in subnets:
        tabela = next((t for t in tabelas if any(a.get("SubnetId") == sn for a in t.get("Associations", []))), principal)
        rotas = tabela.get("Routes", []) if tabela else []
        if any((r.get("GatewayId") or "").startswith("igw-") for r in rotas):
            publicas.append(sn)
            if not publico:
                problemas.append(f"sub-rede {sn} tem rota para internet gateway (sub-rede pública)")
    if publico and not publicas:
        problemas.append("acesso_publico = true, mas nenhuma sub-rede do subnet group é pública: a instância não seria alcançável")
    for sg in ec2.describe_security_groups(GroupIds=list(security_groups))["SecurityGroups"]:
        if sg["VpcId"] != vpc:
            problemas.append(f"grupo de segurança {sg['GroupId']} é de outra VPC ({sg['VpcId']})")
        for regra in sg.get("IpPermissions", []):
            abertos = [r["CidrIp"] for r in regra.get("IpRanges", []) if r.get("CidrIp") == "0.0.0.0/0"]
            abertos += [r["CidrIpv6"] for r in regra.get("Ipv6Ranges", []) if r.get("CidrIpv6") == "::/0"]
            if abertos:
                problemas.append(f"grupo de segurança {sg['GroupId']} aceita entrada de {', '.join(abertos)}")
            ips_entrada += [r["CidrIp"] for r in regra.get("IpRanges", [])] + [r["CidrIpv6"] for r in regra.get("Ipv6Ranges", [])]
            if publico and redes_ok:
                fora = [r["CidrIp"] for r in regra.get("IpRanges", [])
                        if not any(ipaddress.ip_network(r["CidrIp"], strict=False).subnet_of(n) for n in redes_ok
                                   if n.version == 4)]
                fora += [r["CidrIpv6"] for r in regra.get("Ipv6Ranges", [])
                         if not any(ipaddress.ip_network(r["CidrIpv6"], strict=False).subnet_of(n) for n in redes_ok
                                    if n.version == 6)]
                if fora:
                    problemas.append(f"grupo de segurança {sg['GroupId']} aceita entrada de {', '.join(fora)}, "
                                     "fora de teste.cidrs_autorizados")
    return {"subnet_group": subnet_group, "vpc": vpc, "subnets": subnets, "subnets_publicas": publicas,
            "security_groups": list(security_groups), "acesso_publico": publico,
            "cidrs_autorizados": list(cidrs_autorizados or []), "ips_entrada": sorted(set(ips_entrada)),
            "problemas": problemas}


def verificar_kms(sess: boto3.Session, chave: str, conta: str) -> dict:
    meta = sess.client("kms").describe_key(KeyId=chave)["KeyMetadata"]
    problemas = []
    if meta["KeyState"] != "Enabled":
        problemas.append(f"chave KMS em estado {meta['KeyState']}")
    if str(meta["AWSAccountId"]) != str(conta):
        problemas.append(f"chave KMS pertence à conta {meta['AWSAccountId']}, não à {conta}")
    return {"arn": meta["Arn"], "estado": meta["KeyState"], "conta": meta["AWSAccountId"], "problemas": problemas}


def iam_permite(sess: boto3.Session, acao: str, recurso: str) -> tuple[bool | None, str]:
    """Simula, pelo IAM, se a identidade do perfil pode executar a ação (melhor esforço).

    Retorna (True/False, detalhe) ou (None, motivo) quando a simulação não é possível.
    """
    try:
        arn = sess.client("sts").get_caller_identity()["Arn"]
        iam = sess.client("iam")
        if ":assumed-role/" in arn:
            nome_papel = arn.split(":assumed-role/", 1)[1].split("/", 1)[0]
            arn = iam.get_role(RoleName=nome_papel)["Role"]["Arn"]
        r = iam.simulate_principal_policy(PolicySourceArn=arn, ActionNames=[acao], ResourceArns=[recurso])
        decisao = r["EvaluationResults"][0]["EvalDecision"]
        return decisao == "allowed", f"{arn}: {acao} = {decisao}"
    except ClientError as e:
        return None, f"simulação IAM indisponível ({codigo_erro(e)})"
    except Exception as e:  # noqa: BLE001
        return None, f"simulação IAM indisponível ({type(e).__name__})"


def conta_do_arn(arn: str | None) -> str | None:
    if not arn or not arn.startswith("arn:"):
        return None
    partes = arn.split(":")
    return partes[4] if len(partes) > 4 else None


def codigo_erro(e: ClientError) -> str:
    return e.response.get("Error", {}).get("Code", "")
