"""Verificação de integridade manual (PG 13/03, item 7.1).

O script não se conecta ao SQL Server. Ele gera:
  integridade_<rotulo>.sql — consultas a executar no SSMS na instância restaurada e na produção;
  integridade_<rotulo>.csv — planilha para o executante preencher com as contagens e os horários.
Depois lê a planilha, confere o preenchimento e grava o resultado no registro.
"""

from __future__ import annotations

import csv
from pathlib import Path

from . import tempo
from .contexto import Abortado, Execucao

CABECALHO = ["tabela", "contagem_restaurada", "horario_restaurada", "contagem_producao", "horario_producao"]


def _objetos(tabelas: list[str]) -> str:
    return ", ".join(f"OBJECT_ID(N'{t}')" for t in tabelas)


def gerar_sql(pasta: Path, rotulo: str, banco: str, tabelas: list[str], consulta_funcional: str | None,
              endpoint: str | None, producao: str) -> tuple[Path, Path]:
    """Gera dois scripts: um para a instância restaurada (contagem exata) e um para a produção.

    Na produção não há varredura de tabelas: as contagens vêm dos metadados
    (sys.partitions), sem leitura das tabelas, sem bloqueio e sem carga perceptível. Basta permissão de
    leitura nas tabelas (não exige VIEW DATABASE STATE, como sys.dm_db_partition_stats).
    """
    cab = ["-- Verificação de integridade — exercício de recuperação (PG 13/03, seção 8)", ""]
    horario = ["SELECT SYSDATETIMEOFFSET() AS horario_consulta, @@SERVERNAME AS servidor, DB_NAME() AS banco;", ""]
    metadados = (
        "-- Se vier vazio para alguma tabela, o usuário não tem permissão de leitura nela.\n"
        "SELECT OBJECT_SCHEMA_NAME(p.object_id) + '.' + OBJECT_NAME(p.object_id) AS tabela,\n"
        "       SUM(p.rows) AS registros_metadados\n"
        "FROM sys.partitions p\n"
        f"WHERE p.index_id IN (0, 1) AND p.object_id IN ({_objetos(tabelas)})\n"
        "GROUP BY p.object_id;"
    ) if tabelas else "-- (definir integridade.tabelas no config)"

    rest = pasta / f"integridade_{rotulo}_restaurada.sql"
    linhas = cab + [f"-- Executar SOMENTE na instância RESTAURADA: {endpoint or '(ver registro)'}",
                    "-- Contagem exata (varre as tabelas — sem impacto, a instância é temporária).", "",
                    f"USE [{banco}];", "GO"] + horario
    if tabelas:
        partes = [f"SELECT '{t}' AS tabela, COUNT_BIG(*) AS registros FROM {t} WITH (NOLOCK)" for t in tabelas]
        linhas += ["\nUNION ALL\n".join(partes) + ";", "",
                   "-- Mesma consulta por metadados, para comparar com a produção na mesma base", metadados]
    linhas += ["", "-- Consulta funcional de amostra", consulta_funcional or "-- (definir integridade.consulta_funcional no config)", ""]
    rest.write_text("\n".join(linhas), encoding="utf-8-sig")

    prod = pasta / f"integridade_{rotulo}_producao.sql"
    linhas = cab + [f"-- Executar na PRODUÇÃO ({producao}).",
                    "-- Somente leitura de metadados: não varre tabelas, não bloqueia e não gera carga perceptível.",
                    "-- Não use COUNT(*) na produção durante o horário comercial.", "",
                    f"USE [{banco}];", "GO"] + horario + [metadados, "",
                    "-- Consulta funcional: rode na produção apenas se for leve (TOP N por índice).",
                    "-- Na dúvida, rode fora do horário comercial ou só na restaurada.",
                    "-- " + (consulta_funcional or "(não definida)").replace("\n", "\n-- "), ""]
    prod.write_text("\n".join(linhas), encoding="utf-8-sig")
    return rest, prod


def gerar_planilha(caminho: Path, tabelas: list[str]) -> None:
    if caminho.exists():
        return
    with caminho.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(CABECALHO)
        for t in tabelas:
            w.writerow([t, "", "", "", ""])


def _abrir_planilha(caminho: Path):
    """Aceita a planilha salva pelo Excel como CSV UTF-8, CSV comum (ANSI) ou 'Texto Unicode' (UTF-16, tabulação)."""
    import io
    bruto = caminho.read_bytes()
    if bruto[:2] in (b"\xff\xfe", b"\xfe\xff"):
        texto = bruto.decode("utf-16")
    else:
        try:
            texto = bruto.decode("utf-8-sig")
        except UnicodeDecodeError:
            texto = bruto.decode("cp1252")
    cabecalho = texto.splitlines()[0] if texto else ""
    sep = max((";", "\t", ","), key=cabecalho.count)
    return csv.DictReader(io.StringIO(texto, newline=""), delimiter=sep)


def ler_planilha(caminho: Path) -> tuple[list[dict], list[str]]:
    linhas, erros = [], []
    if True:
        for i, r in enumerate(_abrir_planilha(caminho), start=2):
            if not (r.get("tabela") or "").strip():
                continue
            item = {"tabela": r["tabela"].strip()}
            for lado in ("restaurada", "producao"):
                bruto = (r.get(f"contagem_{lado}") or "").strip().replace(".", "").replace(" ", "")
                try:
                    item[f"contagem_{lado}"] = int(bruto)
                except ValueError:
                    erros.append(f"linha {i}: contagem_{lado} vazia ou inválida ('{bruto}')")
                    item[f"contagem_{lado}"] = None
                h = (r.get(f"horario_{lado}") or "").strip()
                try:
                    item[f"horario_{lado}"] = tempo.iso(tempo.interpretar_horario(h)) if h else None
                except ValueError:
                    erros.append(f"linha {i}: horario_{lado} inválido ('{h}')")
                    item[f"horario_{lado}"] = None
                if not h:
                    erros.append(f"linha {i}: horario_{lado} não preenchido")
            rest, prod = item["contagem_restaurada"], item["contagem_producao"]
            if rest is not None and prod is not None:
                if rest == 0:
                    item["avaliacao"] = "verificar: tabela vazia na restaurada"
                elif rest > prod:
                    item["avaliacao"] = "verificar: restaurada maior que produção"
                else:
                    item["avaliacao"] = "coerente" if rest == prod else f"coerente (produção +{prod - rest} após o ponto restaurado)"
            linhas.append(item)
    if not linhas:
        erros.append("nenhuma tabela preenchida")
    return linhas, erros


def executar(ex: Execucao, rotulo: str, endpoint: str | None, banco: str | None = None) -> dict:
    """Conduz a verificação manual e grava o marco m3 ao final."""
    cfg_int = ex.servico.get("integridade") or {}
    banco = banco or cfg_int.get("banco") or "<banco>"
    tabelas = cfg_int.get("tabelas") or []
    if not tabelas:
        print("  AVISO: nenhuma tabela de referência em integridade.tabelas; preencha a planilha manualmente.")
    arq_csv = ex.pasta / f"integridade_{rotulo}.csv"
    sql_rest, sql_prod = gerar_sql(ex.pasta, rotulo, banco, tabelas, cfg_int.get("consulta_funcional"), endpoint,
                                   ex.servico["instancia_producao"])
    gerar_planilha(arq_csv, tabelas)
    print(f"""
  Verificação de integridade (manual)
  1. Instância restaurada ({endpoint}):
     execute {sql_rest.name}
  2. Produção ({ex.servico['instancia_producao']}):
     execute {sql_prod.name} — só metadados, sem varrer tabelas
  3. Preencha {arq_csv.name}: contagem_restaurada = registros_metadados da restaurada
     (a contagem exata fica como conferência); contagem_producao = registros_metadados da produção.
     Contagens sem separador de milhar; horário como HH:MM:SS ou copiado de horario_consulta.
  Arquivos em {ex.pasta}
""")
    while True:
        input("  Pressione Enter quando a planilha estiver preenchida e salva...")
        try:
            linhas, erros = ler_planilha(arq_csv)
        except PermissionError:
            print("  A planilha está aberta em outro programa; feche-a e tente de novo.")
            continue
        if not erros:
            break
        print("  Planilha incompleta:")
        for e in erros:
            print(f"    - {e}")
        if ex.confirmar("  Seguir mesmo assim (as falhas ficam registradas como lacuna)?"):
            ex.lacuna("Verificação de integridade com planilha incompleta: " + "; ".join(erros), "media")
            break

    for l in linhas:
        print(f"    {l['tabela']:<40} restaurada={l['contagem_restaurada']}  produção={l['contagem_producao']}  {l.get('avaliacao', '')}")
    funcional = ex.perguntar("  Consulta funcional — descreva o resultado (ex.: 10 registros do cliente X conferidos)")
    confirmada = ex.confirmar("  A integridade dos dados restaurados está CONFIRMADA?")
    fim = ex.perguntar_horario("Horário de conclusão da verificação de integridade (marco 3):")
    resultado = {
        "banco": banco,
        "arquivo_sql": [sql_rest.name, sql_prod.name],
        "metodo_contagem": "metadados (sys.partitions) nos dois lados; contagem exata só na restaurada",
        "arquivo_planilha": arq_csv.name,
        "tabelas": linhas,
        "consulta_funcional": cfg_int.get("consulta_funcional"),
        "resultado_consulta_funcional": funcional,
        "confirmada": confirmada,
        "concluida_em": tempo.iso(fim),
    }
    ex.estado["integridade"] = resultado
    if not confirmada:
        ex.lacuna("Integridade NÃO confirmada pelo executante", "alta")
    ex.marco("m3", "Conclusão da verificação de integridade", fim)
    return resultado


def exigir_integridade_previa(ex: Execucao) -> None:
    if not ex.estado.get("integridade"):
        raise Abortado("Verificação de integridade não registrada.")
