"""Comando `limpar`: exclui instâncias temporárias que ficaram pendentes e atualiza o registro da frente."""

from __future__ import annotations

from . import aws, tempo
from .contexto import Abortado, Execucao
from .frentes import calcular_resultado


def limpar(cfg: dict, exercicio: str, ciclo: str) -> None:
    s = cfg["servicos"][exercicio]
    sess = aws.sessao(s["perfil"], cfg["regiao"])
    aws.conferir_conta(sess, s["conta_id"], f"do serviço ({s['nome']})")
    rds = sess.client("rds")
    prefixo_tag = f"{exercicio}-{ciclo}-"
    encontradas = []
    for pg in rds.get_paginator("describe_db_instances").paginate():
        for inst in pg["DBInstances"]:
            tags = aws.tags_da_instancia(inst)
            valor = tags.get(aws.TAG_EXERCICIO, "")
            if valor.startswith(prefixo_tag) and inst["DBInstanceIdentifier"].startswith(cfg["prefixo_temporaria"] + "-"):
                encontradas.append((inst["DBInstanceIdentifier"], valor.rsplit("-", 1)[-1], inst["DBInstanceStatus"]))
    if not encontradas:
        print(f"Nenhuma instância temporária do {exercicio} ciclo {ciclo} encontrada na conta {s['conta_id']}.")
        return
    print("Instâncias temporárias encontradas:")
    for ident, frente, status in encontradas:
        print(f"  {ident}  (Frente {frente}, {status})")
    if not Execucao.confirmar("Excluir todas?"):
        return
    for ident, frente, _ in encontradas:
        ex = Execucao(cfg, exercicio, frente, ciclo, executante="")
        if ex.arq_registro.exists():
            ex.carregar_existente()
        else:
            ex.pasta.mkdir(parents=True, exist_ok=True)
            ex.estado = {"instancias": {}, "dados": {}, "marcos": {}, "meta": s["metas"][frente], "lacunas": []}
        try:
            aws.excluir_temporaria(ex, rds, ident)
        except Abortado as e:
            print(f"  {ident}: {e}")
            continue
        ex.evento("limpeza", f"{ident} excluída pelo comando limpar em {tempo.br(tempo.agora())}")
        ex.estado["lacunas"] = [l for l in ex.estado.get("lacunas", [])
                                if not (ident in l["descricao"] and "ainda não excluída" in l["descricao"])]
        if "meta" in ex.estado and ex.estado.get("marcos"):
            calcular_resultado(ex)
        ex.salvar()
