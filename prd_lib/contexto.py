"""Estado da execução, registro de evidências, marcos de cronometragem e interação com o executante.

Cada frente grava em evidencias/<Exercício>_<ciclo>/frente_<X>/:
  registro.json  — estado e resultado (base do relatório .docx)
  eventos.jsonl  — trilha cronológica de tudo o que foi feito (chamadas AWS, confirmações)
O estado é salvo após cada passo: se o script for interrompido, `executar` retoma do passo seguinte.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Callable

from . import tempo


class Abortado(Exception):
    """O executante recusou uma confirmação ou uma salvaguarda impediu a continuação."""


def _json_padrao(o):
    if hasattr(o, "isoformat"):
        return tempo.iso(o) if hasattr(o, "tzinfo") else o.isoformat()
    if isinstance(o, (set, tuple)):
        return list(o)
    if isinstance(o, bytes):
        return o.decode("utf-8", "replace")
    return str(o)


def limpar_resposta(r):
    """Remove metadados irrelevantes das respostas do boto3 antes de gravar."""
    if isinstance(r, dict):
        return {k: limpar_resposta(v) for k, v in r.items() if k != "ResponseMetadata"}
    if isinstance(r, list):
        return [limpar_resposta(v) for v in r]
    return r


class Execucao:
    def __init__(self, cfg: dict, exercicio: str, frente: str, ciclo: str, executante: str):
        self.cfg = cfg
        self.exercicio = exercicio.upper()
        self.frente = frente.upper()
        self.ciclo = ciclo
        self.servico = cfg["servicos"][self.exercicio]
        self.pasta_exercicio = Path(cfg["pasta_evidencias"]) / f"{self.exercicio}_{ciclo}"
        self.pasta = self.pasta_exercicio / f"frente_{self.frente}"
        self.arq_registro = self.pasta / "registro.json"
        self.arq_eventos = self.pasta / "eventos.jsonl"
        self.estado: dict = {}
        self._executante = executante
        self.rt: dict = {}  # objetos de execução (sessões, clientes AWS); não persistidos

    # ------------------------------------------------------------------ estado
    def iniciar_ou_retomar(self) -> bool:
        """Retorna True se retomou uma execução anterior."""
        if self.arq_registro.exists():
            self.estado = json.loads(self.arq_registro.read_text(encoding="utf-8"))
            if self.estado.get("concluida"):
                raise Abortado(
                    f"A Frente {self.frente} do {self.exercicio} no ciclo {self.ciclo} já foi concluída "
                    f"({self.arq_registro}). Use outro ciclo ou arquive a pasta antes de repetir."
                )
            print(f"\nExecução anterior encontrada em {self.pasta}.")
            print(f"Passos concluídos: {', '.join(self.estado.get('passos_concluidos', [])) or 'nenhum'}")
            if not self.confirmar("Retomar a partir do próximo passo?"):
                raise Abortado("Retomada recusada. Arquive a pasta se quiser recomeçar do zero.")
            self.evento("retomada", "Execução retomada")
            return True
        self.pasta.mkdir(parents=True, exist_ok=True)
        s = self.servico
        self.estado = {
            "versao_ferramenta": __import__("prd_lib").__version__,
            "exercicio": self.exercicio,
            "servico": s["nome"],
            "ciclo": self.ciclo,
            "frente": self.frente,
            "tipo_frente_b": s["tipo_frente_b"] if self.frente == "B" else None,
            "executante": self._executante,
            "responsavel": s.get("responsavel", {}),
            "conta_servico": s["conta_id"],
            "instancia_producao": s["instancia_producao"],
            "meta": s["metas"][self.frente],
            "criado_em": tempo.iso(tempo.agora()),
            "concluida": False,
            "passos_concluidos": [],
            "marcos": {},
            "marcos_extras": [],
            "instancias": {},
            "pontos_recuperacao": [],
            "integridade": None,
            "lacunas": [],
            "dados": {},
        }
        self.salvar()
        self.evento("inicio", f"Início da Frente {self.frente} do {self.exercicio} ({s['nome']}), ciclo {self.ciclo}")
        return False

    def carregar_existente(self) -> None:
        """Carrega o registro de uma frente já iniciada (usado por limpar e relatorio)."""
        self.estado = json.loads(self.arq_registro.read_text(encoding="utf-8"))

    def salvar(self) -> None:
        self.estado["atualizado_em"] = tempo.iso(tempo.agora())
        tmp = self.arq_registro.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.estado, ensure_ascii=False, indent=2, default=_json_padrao), encoding="utf-8")
        tmp.replace(self.arq_registro)

    def evento(self, tipo: str, mensagem: str, dados=None) -> None:
        linha = {"horario": tempo.iso(tempo.agora()), "tipo": tipo, "mensagem": mensagem}
        if dados is not None:
            linha["dados"] = limpar_resposta(dados)
        with self.arq_eventos.open("a", encoding="utf-8") as f:
            f.write(json.dumps(linha, ensure_ascii=False, default=_json_padrao) + "\n")

    @property
    def dados(self) -> dict:
        return self.estado["dados"]

    # ------------------------------------------------------------------ passos
    def passo(self, nome: str, titulo: str, funcao: Callable[[], None]) -> None:
        if nome in self.estado["passos_concluidos"]:
            print(f"  [já concluído] {titulo}")
            return
        print(f"\n=== {titulo} ===")
        self.evento("passo_inicio", titulo)
        try:
            funcao()
        except Abortado:
            self.evento("abortado", f"Interrompido no passo: {titulo}")
            self.salvar()
            raise
        except Exception as e:  # registra e propaga
            self.evento("erro", f"Erro no passo {titulo}: {e}", {"traceback": traceback.format_exc()})
            self.salvar()
            raise
        self.estado["passos_concluidos"].append(nome)
        self.evento("passo_fim", titulo)
        self.salvar()

    # ------------------------------------------------------------------ marcos
    def marco(self, chave: str, descricao: str, quando=None) -> None:
        """Marcos principais: m1 (início da restauração), m2 (instância disponível / restore concluído),
        m3 (conclusão da verificação de integridade)."""
        quando = quando or tempo.agora()
        self.estado["marcos"][chave] = {"descricao": descricao, "horario": tempo.iso(quando)}
        self.evento("marco", f"{chave} — {descricao}", {"horario": tempo.iso(quando)})
        print(f"  >> Marco {chave}: {descricao} — {tempo.br(quando)}")
        self.salvar()

    def marco_extra(self, descricao: str, quando=None) -> None:
        quando = quando or tempo.agora()
        self.estado["marcos_extras"].append({"descricao": descricao, "horario": tempo.iso(quando)})
        self.evento("marco_extra", descricao, {"horario": tempo.iso(quando)})
        print(f"  >> {descricao} — {tempo.br(quando)}")
        self.salvar()

    def lacuna(self, descricao: str, gravidade: str = "media") -> None:
        self.estado["lacunas"].append({"descricao": descricao, "gravidade": gravidade, "horario": tempo.iso(tempo.agora())})
        self.evento("lacuna", descricao, {"gravidade": gravidade})
        print(f"  !! LACUNA ({gravidade}): {descricao}")
        self.salvar()

    # ------------------------------------------------------------------ interação
    @staticmethod
    def confirmar(pergunta: str) -> bool:
        while True:
            r = input(f"{pergunta} [s/n]: ").strip().lower()
            if r in ("s", "sim"):
                return True
            if r in ("n", "nao", "não"):
                return False

    def exigir(self, pergunta: str) -> None:
        if not self.confirmar(pergunta):
            raise Abortado(f"Recusado pelo executante: {pergunta}")
        self.evento("confirmacao", pergunta)

    def exigir_digitando(self, texto_esperado: str, aviso: str) -> None:
        print(f"\n  ATENÇÃO: {aviso}")
        r = input(f"  Para prosseguir, digite exatamente: {texto_esperado}\n  > ").strip()
        if r != texto_esperado:
            raise Abortado("Texto de confirmação não confere.")
        self.evento("confirmacao_digitada", aviso, {"texto": texto_esperado})

    def perguntar_horario(self, pergunta: str):
        print(f"\n{pergunta}")
        print("  (Enter = agora; ou HH:MM[:SS] de hoje, horário de Brasília; ou ISO com Z para UTC, ex.: 2027-03-15T13:45:00Z)")
        m1 = tempo.de_iso((self.estado.get("marcos", {}).get("m1") or {}).get("horario"))
        while True:
            try:
                dt = tempo.interpretar_horario(input("  horário: "))
                if m1 and dt < m1:
                    print(f"  {tempo.br(dt)} é anterior ao Marco 1 ({tempo.br(m1)}). Confira (horário de Brasília) e digite de novo.")
                    continue
                print(f"  registrado: {tempo.br(dt)}")
                return dt
            except ValueError as e:
                print(f"  {e}; tente novamente.")

    @staticmethod
    def perguntar(pergunta: str, padrao: str | None = None, obrigatorio: bool = True) -> str:
        sufixo = f" [{padrao}]" if padrao else ""
        while True:
            r = input(f"{pergunta}{sufixo}: ").strip()
            if not r and padrao is not None:
                return padrao
            if r or not obrigatorio:
                return r


def preparar_console() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8")
        except Exception:
            pass
