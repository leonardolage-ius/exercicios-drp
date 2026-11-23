"""Marco 2 automático: backup/restore nativo do SQL Server pela Lambda do exercício (lambda/sql_nativo).

O script lê a senha do usuário mestre da instância TEMPORÁRIA no Secrets Manager (segredo gerenciado
pelo RDS) e a envia à Lambda na chamada. A senha não é gravada em registro, eventos nem console.
A Lambda só alcança o grupo de segurança de teste (porta 1433) e recusa instâncias sem o prefixo das
temporárias, endpoints que não sejam dessa instância, instâncias no ar há mais de `horas_max` horas e,
no restore, bancos que já existam.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from . import aws, tempo
from .contexto import Abortado, Execucao

FINAIS_ERRO = ("ERROR", "CANCELLED", "CANCEL_REQUESTED")


class ErroLambda(Exception):
    pass


def nome_funcao(ex: Execucao) -> str | None:
    return (ex.servico["teste"].get("lambda_sql") or None) if not ex.rt.get("marco2_manual") else None


def _cliente(ex: Execucao, servico: str):
    if "sess" not in ex.rt:  # retomada: a sessão ainda não foi criada nesta execução
        ex.rt["sess"] = aws.sessao(ex.servico["perfil"], ex.cfg["regiao"])
        aws.conferir_conta(ex.rt["sess"], ex.servico["conta_id"], f"do serviço ({ex.servico['nome']})")
    chave = f"cli_{servico}"
    if chave not in ex.rt:
        ex.rt[chave] = ex.rt["sess"].client(servico)
    return ex.rt[chave]


def _credenciais(ex: Execucao, identificador: str) -> dict:
    """Endpoint e credenciais da temporária (cache em memória; nunca persistido)."""
    cache = ex.rt.setdefault("credenciais", {})
    if identificador not in cache:
        aws.garantir_nao_producao(ex, identificador)
        inst = aws.descrever(_cliente(ex, "rds"), identificador)
        if inst is None:
            raise Abortado(f"Instância {identificador} não encontrada.")
        segredo = (inst.get("MasterUserSecret") or {}).get("SecretArn")
        if not segredo:
            raise Abortado(f"{identificador} não tem senha gerenciada pelo Secrets Manager.")
        valor = json.loads(_cliente(ex, "secretsmanager").get_secret_value(SecretId=segredo)["SecretString"])
        cache[identificador] = {"host": inst["Endpoint"]["Address"], "porta": inst["Endpoint"]["Port"],
                                "usuario": valor["username"], "senha": valor["password"]}
    return cache[identificador]


def invocar(ex: Execucao, payload: dict) -> dict:
    nome = ex.servico["teste"]["lambda_sql"]
    registro = {k: v for k, v in payload.items() if k != "senha"}
    try:
        resp = _cliente(ex, "lambda").invoke(FunctionName=nome, Payload=json.dumps(payload).encode("utf-8"))
    except ClientError as e:
        ex.evento("lambda_erro", f"invoke {payload.get('acao')}: {aws.codigo_erro(e)}", registro)
        raise ErroLambda(f"falha ao chamar a Lambda {nome}: {aws.codigo_erro(e)} {e}") from e
    corpo = json.loads(resp["Payload"].read() or b"{}")
    if resp.get("FunctionError"):
        msg = corpo.get("errorMessage") or str(corpo)
        ex.evento("lambda_erro", f"{payload.get('acao')}: {msg}", {"pedido": registro, "tipo": corpo.get("errorType")})
        raise ErroLambda(f"{corpo.get('errorType', 'Erro')}: {msg}")
    ex.evento("lambda", payload.get("acao"), {"pedido": registro, "resposta": corpo})
    return corpo


def hora_servidor(texto: str | None, offset_seg: float):
    """Converte um horário sem fuso do SQL Server para Brasília, dado o deslocamento do relógio em relação a UTC.

    Atenção: o rds_task_status devolve created_at/last_updated em UTC mesmo quando a instância tem fuso
    configurado (constatado em 22/11/2026 no Onegreen: servidor em UTC-3, created_at em UTC). Para esses
    campos, use offset_seg = 0.
    """
    if not texto:
        return None
    ingenuo = datetime.fromisoformat(texto)
    return tempo.para_local((ingenuo - timedelta(seconds=offset_seg)).replace(tzinfo=timezone.utc))


def garantir_senha_gerenciada(ex: Execucao, identificador: str) -> None:
    """Instância restaurada por PITR herda a senha da produção. Para a Lambda nunca usar a credencial da
    produção, a senha da TEMPORÁRIA passa a ser gerenciada pelo Secrets Manager (só a temporária muda)."""
    rds = _cliente(ex, "rds")
    aws.garantir_nao_producao(ex, identificador)
    inst = aws.descrever(rds, identificador)
    if (inst.get("MasterUserSecret") or {}).get("SecretArn"):
        return
    print(f"  {identificador} usa a senha herdada da produção; passando a senha DA TEMPORÁRIA ao Secrets Manager.")
    ex.exigir(f"Trocar a senha do usuário mestre de {identificador} por uma gerenciada pelo Secrets Manager?")
    inicio = tempo.agora()
    rds.modify_db_instance(DBInstanceIdentifier=identificador, ManageMasterUserPassword=True, ApplyImmediately=True)
    ex.evento("aws", f"modify_db_instance {identificador} ManageMasterUserPassword=True")
    limite = time.monotonic() + 30 * 60
    while True:
        time.sleep(ex.cfg["intervalo_consulta_seg"])
        inst = aws.descrever(rds, identificador)
        seg = inst.get("MasterUserSecret") or {}
        if inst["DBInstanceStatus"] == "available" and seg.get("SecretArn") and seg.get("SecretStatus", "active") == "active":
            break
        if time.monotonic() > limite:
            raise Abortado("Tempo excedido aguardando a senha gerenciada da temporária.")
    fim = tempo.agora()
    ex.marco_extra(f"Senha da temporária {identificador} gerenciada pelo Secrets Manager "
                   f"({tempo.duracao((fim - inicio).total_seconds())}; passo exclusivo do exercício)", fim)
    ex.dados.setdefault("passos_exclusivos_exercicio", []).append(
        {"descricao": "troca da senha da temporária PITR para o Secrets Manager", "duracao_seg": (fim - inicio).total_seconds()})


def executar_tarefas(ex: Execucao, acao: str, identificador: str, tarefas: list[dict], horas_max: int,
                     rotulo: str, desc_inicio: str, chave_fim: str | None, desc_fim: str) -> None:
    """Inicia (ou retoma) as tarefas nativas na temporária e acompanha até SUCCESS.

    tarefas: [{"banco": ..., "s3_arn": ...}]. Grava task_id, created_at e last_updated no registro;
    o início é o created_at da primeira tarefa e o fim é o last_updated da última.
    """
    estado = ex.dados.setdefault(f"tarefas_{rotulo}", {"metodo": "lambda", "funcao": ex.servico["teste"]["lambda_sql"],
                                                        "instancia": identificador, "itens": []})
    cred = _credenciais(ex, identificador)
    base = {"identificador": identificador, "horas_max": horas_max, **cred}

    if not estado["itens"]:
        print(f"  Lambda {estado['funcao']} → {identificador} ({cred['host']})")
        for t in tarefas:
            print(f"    {acao}: banco {t['banco']}  ←→  {t['s3_arn']}")
        ex.exigir(f"Iniciar {'o restore' if acao == 'iniciar_restore' else 'o backup'} pela Lambda?")
        for t in tarefas:
            solicitado = tempo.agora()
            r = invocar(ex, {**base, "acao": acao, "banco": t["banco"], "s3_arn": t["s3_arn"]})
            estado["itens"].append({"banco": t["banco"], "s3_arn": t["s3_arn"], "task_id": r["task_id"],
                                    "offset_seg": r["offset_seg"], "solicitado_em": tempo.iso(solicitado),
                                    "created_at_servidor": r["created_at"], "lifecycle": r["lifecycle"]})
            ex.salvar()
            print(f"    tarefa {r['task_id']} criada ({t['banco']}): {r['lifecycle']}")
        primeiro = estado["itens"][0]
        inicio = hora_servidor(primeiro["created_at_servidor"], 0) or tempo.de_iso(primeiro["solicitado_em"])
        ex.marco_extra(desc_inicio, inicio)
    else:
        # corrige registros gravados pela versão que convertia created_at pelo fuso do servidor
        primeiro = estado["itens"][0]
        correto = hora_servidor(primeiro["created_at_servidor"], 0)
        for m in ex.estado.get("marcos_extras", []):
            if m["descricao"] == desc_inicio and correto and m["horario"] != tempo.iso(correto):
                ex.evento("correcao", f"{desc_inicio}: {m['horario']} -> {tempo.iso(correto)} (created_at em UTC)")
                m["horario"] = tempo.iso(correto)
        ex.salvar()

    intervalo = ex.cfg["intervalo_consulta_seg"]
    limite = time.monotonic() + ex.cfg["tempo_maximo_espera_min"] * 60
    ultimo = {}
    while True:
        pendentes = [i for i in estado["itens"] if i.get("lifecycle") != "SUCCESS"]
        if not pendentes:
            break
        for item in pendentes:
            try:
                r = invocar(ex, {**base, "acao": "status", "task_id": item["task_id"]})
            except ErroLambda as e:
                print(f"    {tempo.br(tempo.agora())}  falha ao consultar a tarefa {item['task_id']}: {e} (nova tentativa)")
                continue
            item.update(lifecycle=r["lifecycle"], percentual=r.get("percentual"), task_info=r.get("task_info"),
                        last_updated_servidor=r.get("last_updated"), offset_seg=r["offset_seg"])
            chave = (r["lifecycle"], r.get("percentual"))
            if ultimo.get(item["task_id"]) != chave:
                print(f"    {tempo.br(tempo.agora())}  tarefa {item['task_id']} ({item['banco']}): "
                      f"{r['lifecycle']} {r.get('percentual') or 0}%")
                ultimo[item["task_id"]] = chave
            if r["lifecycle"] == "SUCCESS":
                item["observado_em"] = tempo.iso(tempo.agora())
            elif r["lifecycle"] in FINAIS_ERRO:
                ex.salvar()
                ex.lacuna(f"Tarefa {item['task_id']} ({item['banco']}) terminou com {r['lifecycle']}: {r.get('task_info')}", "alta")
                raise Abortado(f"A tarefa nativa {item['task_id']} falhou ({r['lifecycle']}). Ver task_info no registro.")
        ex.salvar()
        if any(i.get("lifecycle") != "SUCCESS" for i in estado["itens"]):
            if time.monotonic() > limite:
                raise Abortado("Tempo máximo de espera da tarefa nativa excedido.")
            time.sleep(intervalo)

    ultimo_item = max(estado["itens"], key=lambda i: i.get("last_updated_servidor") or "")
    fim = hora_servidor(ultimo_item.get("last_updated_servidor"), 0)
    observado = tempo.de_iso(ultimo_item["observado_em"])
    if fim is None:
        fim = observado
    elif abs((observado - fim).total_seconds()) > intervalo + 120:
        ex.lacuna(f"Horário de conclusão do rds_task_status ({tempo.br(fim)}) difere do observado pelo script "
                  f"({tempo.br(observado)}); conferir o fuso da instância", "baixa")
    if chave_fim:
        ex.marco(chave_fim, desc_fim, fim)
    else:
        ex.marco_extra(desc_fim, fim)
    ex.salvar()
