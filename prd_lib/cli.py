"""Pontos de entrada usados pelos scripts de cada sistema e de cada cenário (frente)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__, tempo
from .config import ErroConfig, carregar
from .contexto import Abortado, Execucao, preparar_console

SISTEMAS = {"E1": ("cal", "CAL®"), "E2": ("onegreen", "Onegreen"), "E3": ("portal", "Portal")}
NOMES_FRENTE = {
    ("A", None): "Frente A — PITR do cofre primário (item 7.4)",
    ("B", "bak"): "Frente B — arquivo .bak do dia anterior (item 7.6)",
    ("B", "cliente"): "Frente B — banco de um cliente (item 7.7)",
    ("C", None): "Frente C — cópia do cofre isolado (item 7.5)",
}


def ciclo_atual() -> str:
    hoje = tempo.agora()
    return f"{hoje.year}-{1 if hoje.month <= 6 else 2}"


def _config_padrao(exercicio: str, arquivo_script: str) -> str:
    return str(Path(arquivo_script).resolve().parent / f"config_{SISTEMAS[exercicio][0]}.yaml")


def _comuns(p: argparse.ArgumentParser, exercicio: str, arquivo_script: str) -> None:
    p.add_argument("--config", default=_config_padrao(exercicio, arquivo_script),
                   help=f"arquivo de configuração (padrão: config_{SISTEMAS[exercicio][0]}.yaml na pasta do script)")
    p.add_argument("--ciclo", default=ciclo_atual(), help="semestre, ex.: 2027-1 (padrão: semestre atual)")
    p.add_argument("--version", action="version", version=f"prd_lib {__version__}")


def _executar_protegido(funcao) -> int:
    try:
        funcao()
    except ErroConfig as e:
        print(f"\nErro de configuração: {e}")
        return 2
    except Abortado as e:
        print(f"\nInterrompido: {e}\nO estado foi salvo; rode o mesmo comando para retomar.")
        return 3
    except KeyboardInterrupt:
        print("\nInterrompido (Ctrl+C). O estado foi salvo; rode o mesmo comando para retomar.")
        return 130
    return 0


# ---------------------------------------------------------------------- scripts de cenário (frente)
def rodar_frente(exercicio: str, frente: str, arquivo_script: str, tipo_b: str | None = None, argv=None) -> int:
    preparar_console()
    sigla, nome = SISTEMAS[exercicio]
    titulo = f"{exercicio} — {nome} · {NOMES_FRENTE[(frente, tipo_b)]}"
    p = argparse.ArgumentParser(description=f"Exercício de recuperação do PG 13/03: {titulo}")
    _comuns(p, exercicio, arquivo_script)
    p.add_argument("--executante", help="nome de quem executa (padrão: pergunta, sugerindo o titular)")
    p.set_defaults(instante=None, medir_renomeacao=False, marco2_manual=False, data_bak=None, banco_cliente=None)
    if frente == "A" or tipo_b == "cliente":
        p.add_argument("--instante", help="instante de recuperação em ISO 8601, definido antes do início "
                                          "(padrão: último instante restaurável no momento da preparação)")
    if frente == "A":
        p.add_argument("--medir-renomeacao", action="store_true",
                       help="mede também o tempo de renomeação (item 7.4), renomeando a instância TEMPORÁRIA "
                            "após a integridade; a produção nunca é renomeada")
    if frente == "B":
        p.add_argument("--marco2-manual", action="store_true",
                       help="não usar a Lambda: gera o .sql para executar no SSMS e pede os horários")
    if tipo_b == "bak":
        p.add_argument("--data-bak", help="data do arquivo .bak, AAAA-MM-DD (padrão: dia anterior)")
    if tipo_b == "cliente":
        p.add_argument("--banco-cliente", help="banco do cliente a recuperar (padrão: pergunta)")
    args = p.parse_args(argv)

    def corpo():
        cfg = carregar(args.config, exercicio)
        s = cfg["servicos"][exercicio]
        if frente == "B" and s["tipo_frente_b"] != tipo_b:
            raise ErroConfig(f"tipo_frente_b do arquivo é '{s['tipo_frente_b']}', mas este script é da Frente B '{tipo_b}'.")
        from .frentes import executar
        ex = Execucao(cfg, exercicio, frente, args.ciclo, executante="")
        print(f"\n{titulo} · ciclo {args.ciclo}")
        print(f"Evidências: {ex.pasta}")
        if not ex.arq_registro.exists():
            titular = (s.get("responsavel") or {}).get("titular")
            ex._executante = args.executante or Execucao.perguntar("Executante", padrao=titular)
        ex.iniciar_ou_retomar()
        executar(ex, args)
        print(f"\nGere o registro com:  python {sigla}.py relatorio --ciclo {args.ciclo}")

    return _executar_protegido(corpo)


# ---------------------------------------------------------------------- script do sistema
def _situacao(cfg: dict, exercicio: str, ciclo: str) -> None:
    s = cfg["servicos"][exercicio]
    pasta = Path(cfg["pasta_evidencias"]) / f"{exercicio}_{ciclo}"
    print(f"\n{exercicio} — {s['nome']} · ciclo {ciclo}  ({pasta})\n")
    for f in ("A", "B", "C"):
        arq = pasta / f"frente_{f}" / "registro.json"
        if not arq.exists():
            print(f"  Frente {f}: não iniciada")
            continue
        r = json.loads(arq.read_text(encoding="utf-8"))
        res = r.get("resultado") or {}
        if r.get("concluida"):
            linha = (f"concluída em {tempo.br(r.get('concluida_em'))} — {res.get('resultado')} "
                     f"(RTO {tempo.duracao(res.get('rto_seg'))} / meta {r['meta']['rto_min']} min)")
        else:
            linha = f"em andamento — último passo concluído: {(r.get('passos_concluidos') or ['nenhum'])[-1]}"
        print(f"  Frente {f}: {linha}")
        pend = [i for i, x in r.get("instancias", {}).items() if not x.get("excluida_em")]
        if pend:
            print(f"           instâncias temporárias não excluídas: {', '.join(pend)}")


def rodar_sistema(exercicio: str, arquivo_script: str, argv=None) -> int:
    preparar_console()
    sigla, nome = SISTEMAS[exercicio]
    p = argparse.ArgumentParser(
        description=f"Exercícios de recuperação do PG 13/03 — {exercicio} {nome}: tarefas do sistema. "
                    f"As frentes têm scripts próprios nesta pasta (frente_*.py).")
    sub = p.add_subparsers(dest="comando", required=True)
    for nome_cmd, ajuda in (("verificar", "prontidão para o exercício, somente leitura"),
                            ("situacao", "situação das frentes no ciclo"),
                            ("limpar", "exclui instâncias temporárias pendentes do ciclo"),
                            ("relatorio", "gera o registro .docx do exercício")):
        _comuns(sub.add_parser(nome_cmd, help=ajuda), exercicio, arquivo_script)
    args = p.parse_args(argv)

    def corpo():
        cfg = carregar(args.config, exercicio)
        if args.comando == "verificar":
            from .prontidao import verificar
            verificar(cfg, exercicio, args.ciclo)
        elif args.comando == "situacao":
            _situacao(cfg, exercicio, args.ciclo)
        elif args.comando == "limpar":
            from .limpeza import limpar
            limpar(cfg, exercicio, args.ciclo)
        elif args.comando == "relatorio":
            from .relatorio import gerar
            print(f"Registro gerado: {gerar(cfg, exercicio, args.ciclo)}")

    return _executar_protegido(corpo)
