"""Leitura e validação do arquivo de configuração de um sistema (YAML).

Cada sistema tem o seu arquivo (CAL/config_cal.yaml, Onegreen/config_onegreen.yaml,
Portal/config_portal.yaml) com a chave `exercicio` (E1, E2 ou E3), a seção `servico` e,
para a Frente C, a seção `conta_backup`. Internamente o arquivo é normalizado para
cfg["servicos"][exercicio], que é o formato usado pela biblioteca.
"""

from __future__ import annotations

from pathlib import Path

import yaml

FRENTES = ("A", "B", "C")
OBRIGATORIOS_SERVICO = ("nome", "perfil", "conta_id", "instancia_producao", "cofre_primario", "teste", "metas", "tipo_frente_b")

# Salvaguarda fixa: identificadores de produção dos três sistemas, mesmo que não estejam no arquivo carregado.
IDS_PRODUCAO_CONHECIDOS = {"ius-mssql-cal", "medium-onegreen-rds", "db-portal-fornecedores"}


class ErroConfig(Exception):
    pass


def carregar(caminho: str | Path, exercicio_esperado: str | None = None) -> dict:
    caminho = Path(caminho)
    if not caminho.exists():
        raise ErroConfig(
            f"Arquivo de configuração não encontrado: {caminho}\n"
            f"Copie o arquivo .exemplo.yaml da mesma pasta para {caminho.name} e preencha."
        )
    with caminho.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    exercicio = str(cfg.get("exercicio", "")).upper()
    if not exercicio or not cfg.get("servico"):
        raise ErroConfig(f"{caminho.name}: informe 'exercicio' (E1, E2 ou E3) e a seção 'servico'.")
    if exercicio_esperado and exercicio != exercicio_esperado.upper():
        raise ErroConfig(f"{caminho.name} é do {exercicio}, mas este script é do {exercicio_esperado}.")
    cfg["exercicio"] = exercicio
    cfg["servicos"] = {exercicio: cfg.pop("servico")}
    cfg.setdefault("regiao", "us-east-1")
    cfg.setdefault("prefixo_temporaria", "ius-rds-exr")
    cfg.setdefault("intervalo_consulta_seg", 30)
    cfg.setdefault("tempo_maximo_espera_min", 240)
    # evidências relativas à pasta do arquivo de configuração (a pasta do sistema)
    pasta = Path(cfg.get("pasta_evidencias") or "evidencias")
    if not pasta.is_absolute():
        pasta = caminho.resolve().parent / pasta
    cfg["pasta_evidencias"] = str(pasta)
    cfg["_arquivo"] = str(caminho.resolve())
    validar(cfg)
    return cfg


def validar(cfg: dict) -> None:
    prefixo = str(cfg["prefixo_temporaria"]).lower()
    if len(prefixo) < 8 or any(i.startswith(prefixo) for i in identificadores_producao(cfg)):
        raise ErroConfig(f"prefixo_temporaria '{prefixo}' é genérico demais ou coincide com uma instância de produção.")
    for chave, s in cfg["servicos"].items():
        faltando = [c for c in OBRIGATORIOS_SERVICO if not s.get(c)]
        if faltando:
            raise ErroConfig(f"{chave}: campos obrigatórios ausentes em 'servico': {', '.join(faltando)}")
        if s["tipo_frente_b"] not in ("bak", "cliente"):
            raise ErroConfig(f"{chave}: tipo_frente_b deve ser 'bak' ou 'cliente'.")
        t = s["teste"]
        for c in ("db_subnet_group", "security_group_ids", "kms_key_id"):
            if not t.get(c):
                raise ErroConfig(f"{chave}: servico.teste.{c} é obrigatório.")
        for f in FRENTES:
            meta = (s["metas"] or {}).get(f)
            if not meta or "rto_min" not in meta or "rpo_min" not in meta:
                raise ErroConfig(f"{chave}: servico.metas.{f} precisa de rto_min e rpo_min (PCN, seção 7).")
        if s["tipo_frente_b"] == "bak" and not (s.get("bak") or {}).get("bucket"):
            raise ErroConfig(f"{chave}: Frente B do tipo 'bak' exige servico.bak.bucket.")
        if s["tipo_frente_b"] == "cliente" and not (s.get("exportacao") or {}).get("bucket"):
            raise ErroConfig(f"{chave}: Frente B do tipo 'cliente' exige servico.exportacao.bucket.")
        if t.get("acesso_publico"):
            cidrs = t.get("cidrs_autorizados") or []
            if any(c in ("0.0.0.0/0", "::/0") for c in cidrs):
                raise ErroConfig(f"{chave}: teste.cidrs_autorizados não pode conter 0.0.0.0/0 nem ::/0.")


def exigir_conta_backup(cfg: dict) -> dict:
    """Seção conta_backup: obrigatória só para a Frente C (executada pelo Custodiante)."""
    cb = cfg.get("conta_backup") or {}
    faltando = [c for c in ("perfil", "conta_id", "cofre", "papel_copia_arn") if not cb.get(c)]
    if faltando:
        raise ErroConfig("Frente C: preencha conta_backup." + ", conta_backup.".join(faltando) + " no arquivo de configuração.")
    return cb


def regiao_conta_backup(cfg: dict) -> str:
    """Região do cofre isolado (pode diferir da região do sistema, ex.: Portal em us-east-2, cofre em us-east-1)."""
    return (cfg.get("conta_backup") or {}).get("regiao") or cfg["regiao"]


def resolver_prefixo(prefixo: str | None, data) -> str:
    """Prefixo do .bak com variáveis da data do arquivo procurado: {ano}, {mes}, {dia}.

    Ex.: "{ano}_{mes}/BACKUP_AUDIT_PORTAL_" -> "2026_11/BACKUP_AUDIT_PORTAL_" para um .bak de 12/11/2026.
    A data é a do arquivo (dia anterior, por padrão), não a de hoje: no dia 1º, busca a pasta do mês anterior.
    """
    try:
        return (prefixo or "").format(ano=f"{data.year:04d}", mes=f"{data.month:02d}", dia=f"{data.day:02d}")
    except (KeyError, IndexError, ValueError) as e:
        raise ErroConfig(f"prefixo '{prefixo}' inválido: use apenas {{ano}}, {{mes}} e {{dia}} ({e})") from e


def prefixos_do_dia(prefixo: str | None, data) -> list[str]:
    """Prefixos a pesquisar para o .bak gravado em `data` (horário de Brasília).

    Inclui também o prefixo do dia seguinte: um backup gravado à noite em Brasília pode ter nome e pasta
    do dia seguinte (ex.: job às 00:00 UTC = 21:00 em Brasília), inclusive na virada do mês.
    """
    from datetime import timedelta
    vistos = []
    for d in (data, data + timedelta(days=1)):
        p = resolver_prefixo(prefixo, d)
        if p not in vistos:
            vistos.append(p)
    return vistos


def servico(cfg: dict, exercicio: str) -> dict:
    exercicio = exercicio.upper()
    if exercicio not in cfg["servicos"]:
        raise ErroConfig(f"Exercício {exercicio} não configurado neste arquivo.")
    return cfg["servicos"][exercicio]


def identificadores_producao(cfg: dict) -> set[str]:
    """Identificadores de produção: nunca podem ser alterados nem excluídos."""
    ids = set(IDS_PRODUCAO_CONHECIDOS)
    ids |= {s["instancia_producao"].lower() for s in cfg["servicos"].values()}
    ids |= {i.lower() for i in cfg.get("identificadores_protegidos", [])}
    return ids
