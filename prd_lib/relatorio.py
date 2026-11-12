"""Gera o registro do exercício (.docx, identidade visual Ius) a partir dos registros JSON das frentes.

Campos do formulário: seção 7 da Referência dos Exercícios de Recuperação (PG 13/03).
Informações que não vêm da AWS (ações, Não Conformidade, correção de RTO/RPO, Coordenador do PCN)
são lidas do arquivo opcional notas_exercicio.yaml na pasta do exercício; o modelo é criado na
primeira geração.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from . import __version__, tempo

AZUL = RGBColor(0x00, 0x39, 0x75)
VERDE = RGBColor(0x83, 0xB8, 0x1A)
TEXTO = RGBColor(0x44, 0x44, 0x44)
LEGENDA = RGBColor(0x71, 0x71, 0x7A)
F_TITULO = "Trebuchet MS"   # substituto de General Sans
F_CORPO = "Calibri"         # substituto de Inter
LOGO = Path(__file__).parent / "assets" / "logo-ius.png"

MECANISMO = {
    "A": "PITR do cofre primário",
    "B_bak": "Arquivo .bak do dia anterior",
    "B_cliente": "Banco de um cliente (PITR → backup nativo → restore isolado)",
    "C": "Cópia mais recente do cofre isolado",
}

GRAVIDADE = {"baixa": "baixa", "media": "média", "alta": "alta", "critica": "crítica"}

MODELO_NOTAS = """# Notas do exercício — preencha e gere o relatório novamente.
coordenador_pcn: ""            # nome do Coordenador do PCN que assina
acoes:                         # lacunas e ações (complementa as lacunas registradas pelo script)
  - descricao: ""
    responsavel: ""
    prazo: ""
nao_conformidade: ""           # nº da Ocorrência no CAL®, se houver
correcao_rto_rpo: ""           # valor proposto para o PCN, se houver
observacoes: ""
"""


# ---------------------------------------------------------------------- estilo
def _sombrear(celula, cor_hex: str) -> None:
    tcPr = celula._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), cor_hex)
    tcPr.append(shd)


def _bordas_tabela(tabela, cor="D4D4D8") -> None:
    tblPr = tabela._tbl.tblPr
    bordas = OxmlElement("w:tblBorders")
    for lado in ("top", "left", "bottom", "right", "insideH", "insideV"):
        b = OxmlElement(f"w:{lado}")
        b.set(qn("w:val"), "single")
        b.set(qn("w:sz"), "4")
        b.set(qn("w:color"), cor)
        bordas.append(b)
    tblPr.append(bordas)


def _linha_verde(paragrafo) -> None:
    pPr = paragrafo._p.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    b = OxmlElement("w:bottom")
    b.set(qn("w:val"), "single")
    b.set(qn("w:sz"), "18")  # 2,25 pt ≈ 3 px
    b.set(qn("w:color"), "83B81A")
    b.set(qn("w:space"), "4")
    pbdr.append(b)
    pPr.append(pbdr)


def _fonte(run, nome=F_CORPO, tamanho=10, cor=TEXTO, negrito=False, italico=False):
    run.font.name = nome
    run._element.rPr.rFonts.set(qn("w:eastAsia"), nome)
    run.font.size = Pt(tamanho)
    run.font.color.rgb = cor
    run.font.bold = negrito
    run.font.italic = italico
    return run


def _estilos(doc: Document) -> None:
    st = doc.styles["Normal"]
    st.font.name = F_CORPO
    st.element.rPr.rFonts.set(qn("w:eastAsia"), F_CORPO)
    st.font.size = Pt(10)
    st.font.color.rgb = TEXTO
    st.paragraph_format.space_after = Pt(4)
    for nome, tam, antes in (("Heading 1", 14, 14), ("Heading 2", 12, 10), ("Heading 3", 10.5, 8)):
        h = doc.styles[nome]
        h.font.name = F_TITULO
        h.element.rPr.rFonts.set(qn("w:ascii"), F_TITULO)
        h.element.rPr.rFonts.set(qn("w:hAnsi"), F_TITULO)
        h.font.size = Pt(tam)
        h.font.bold = True
        h.font.color.rgb = AZUL
        h.paragraph_format.space_before = Pt(antes)
        h.paragraph_format.space_after = Pt(4)
        h.paragraph_format.keep_with_next = True


def _campo(paragrafo, instrucao: str) -> None:
    for tipo, texto in (("begin", None), (None, instrucao), ("separate", None), (None, "1"), ("end", None)):
        r = paragrafo.add_run()
        _fonte(r, tamanho=8, cor=LEGENDA)
        if tipo:
            fc = OxmlElement("w:fldChar")
            fc.set(qn("w:fldCharType"), tipo)
            r._r.append(fc)
        elif texto == instrucao:
            it = OxmlElement("w:instrText")
            it.set(qn("xml:space"), "preserve")
            it.text = instrucao
            r._r.append(it)
        else:
            r.text = texto


def _cabecalho_rodape(doc: Document, pasta: str) -> None:
    sec = doc.sections[0]
    sec.page_height, sec.page_width = Cm(29.7), Cm(21.0)
    sec.left_margin = sec.right_margin = Cm(2.0)
    sec.top_margin, sec.bottom_margin = Cm(2.0), Cm(1.8)
    sec.header_distance = sec.footer_distance = Cm(1.0)
    cab = sec.header
    t = cab.add_table(rows=1, cols=2, width=Cm(17))
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    c0, c1 = t.rows[0].cells
    _larguras(t, [4, 13])
    if LOGO.exists():
        c0.paragraphs[0].add_run().add_picture(str(LOGO), width=Cm(2.6))
    p = c1.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _fonte(p.add_run("REGISTRO DE EXERCÍCIO DE RECUPERAÇÃO"), F_TITULO, 11, AZUL, True)
    p2 = c1.add_paragraph()
    p2.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _fonte(p2.add_run("PG 13/03 — Recuperação de Desastres em Ambientes de TI · seção 8"), tamanho=8.5, cor=LEGENDA)
    _linha_verde(cab.add_paragraph())
    cab.paragraphs[0].paragraph_format.space_after = Pt(0)

    rod = sec.footer.paragraphs[0]
    rod.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _fonte(rod.add_run(f"Gerado pelos scripts dos exercícios (prd_lib v{__version__}) em {tempo.br(tempo.agora())} a partir das evidências em {pasta}.   Página "),
           tamanho=8, cor=LEGENDA)
    _campo(rod, "PAGE")
    _fonte(rod.add_run(" de "), tamanho=8, cor=LEGENDA)
    _campo(rod, "NUMPAGES")


def _larguras(tabela, larguras: list[float]) -> None:
    """Larguras fixas (o LibreOffice usa tblGrid; o Word, tcW)."""
    tabela.autofit = False
    grade = tabela._tbl.tblGrid
    for gc, w in zip(grade.findall(qn("w:gridCol")), larguras):
        gc.set(qn("w:w"), str(int(Cm(w).twips)))
    for row in tabela.rows:
        for c, w in zip(row.cells, larguras):
            c.width = Cm(w)


def _kms_curto(arn: str | None) -> str:
    if not arn:
        return "—"
    partes = arn.split(":")
    conta = partes[4] if len(partes) > 4 else "?"
    return f"conta {conta}, chave {arn.rsplit('/', 1)[-1][:8]}…"


def _tabela(doc, cabecalho: list[str], linhas: list[list], larguras: list[float] | None = None, chave_valor=False):
    t = doc.add_table(rows=0, cols=len(cabecalho) if cabecalho else 2)
    _bordas_tabela(t)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    if cabecalho:
        cel = t.add_row().cells
        for c, txt in zip(cel, cabecalho):
            _sombrear(c, "003975")
            _fonte(c.paragraphs[0].add_run(str(txt)), tamanho=9, cor=RGBColor(0xFF, 0xFF, 0xFF), negrito=True)
        tr = t.rows[0]._tr
        trPr = tr.get_or_add_trPr()
        h = OxmlElement("w:tblHeader")
        h.set(qn("w:val"), "true")
        trPr.append(h)
    for i, linha in enumerate(linhas):
        cel = t.add_row().cells
        for j, (c, valor) in enumerate(zip(cel, linha)):
            cor_fundo = None
            if isinstance(valor, tuple):  # (texto, cor de fundo)
                valor, cor_fundo = valor
            if chave_valor and j == 0:
                _sombrear(c, "F5F5F5")
                _fonte(c.paragraphs[0].add_run(str(valor)), tamanho=9, cor=AZUL, negrito=True)
            else:
                if cor_fundo:
                    _sombrear(c, cor_fundo)
                elif i % 2 == 1 and not chave_valor:
                    _sombrear(c, "F5F5F5")
                _fonte(c.paragraphs[0].add_run("—" if valor in (None, "") else str(valor)), tamanho=9)
    for row in t.rows:  # linha não se divide entre páginas
        row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
    if larguras or chave_valor:
        _larguras(t, larguras or [4.5, 12.5])
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return t


def _p(doc, texto, negrito=False, cor=TEXTO, tamanho=10, italico=False):
    par = doc.add_paragraph()
    _fonte(par.add_run(texto), tamanho=tamanho, cor=cor, negrito=negrito, italico=italico)
    return par


# ---------------------------------------------------------------------- conteúdo
def _chave_mecanismo(r: dict) -> str:
    return f"B_{r.get('tipo_frente_b') or 'bak'}" if r["frente"] == "B" else r["frente"]


def _sim_nao(v) -> str:
    return "sim" if v else ("não" if v is not None else "—")


def _ok(cond: bool) -> tuple[str, str]:
    return ("dentro da meta", "F8FDE8") if cond else ("EXCEDIDO", "FDECEC")


def _secao_frente(doc, r: dict) -> None:
    res = r.get("resultado") or {}
    meta = r["meta"]
    doc.add_page_break()
    doc.add_heading(f"Frente {r['frente']} — {MECANISMO[_chave_mecanismo(r)]}", level=1)
    _tabela(doc, [], [
        ["Executante", r.get("executante")],
        ["Período", f"{tempo.br(r.get('criado_em'))} a {tempo.br(r.get('concluida_em'))}"],
        ["Situação do registro", "concluído" if r.get("concluida") else "INCOMPLETO — execução não finalizada"],
        ["Resultado", (res.get("resultado", "—"), "F8FDE8" if res.get("resultado") == "Bem-sucedida" else "FDECEC")],
    ], [4.5, 12.5], chave_valor=True)

    doc.add_heading("Pontos de recuperação", level=2)
    linhas = []
    for pr in r.get("pontos_recuperacao", []):
        detalhe = pr.get("version_id") and f"VersionId {pr['version_id']}" or pr.get("observacao") or ""
        linhas.append([pr["tipo"], pr["origem"], tempo.br(pr.get("instante")), detalhe])
    _tabela(doc, ["Tipo", "Origem (ID/ARN)", "Instante / gravação", "Observação"], linhas, [3.2, 7.3, 2.8, 3.7])

    doc.add_heading("Instâncias temporárias", level=2)
    linhas = []
    for ident, i in r.get("instancias", {}).items():
        c = i.get("controles") or {}
        linhas.append([ident + (f" (antes {i['identificador_anterior']})" if i.get("identificador_anterior") else ""),
                       i.get("papel"), c.get("subnet_group"), _sim_nao(c.get("acesso_publico")),
                       f"{_sim_nao(c.get('criptografia'))} — {_kms_curto(c.get('kms_key_id'))}",
                       tempo.br(i.get("criada_em")), tempo.br(i.get("excluida_em"))])
    _tabela(doc, ["Identificador", "Papel", "Subnet group", "Público", "Criptografia (chave KMS)", "Criada", "Excluída"],
            linhas, [3.0, 2.3, 1.9, 1.6, 4.1, 2.05, 2.05])

    doc.add_heading("Cronometragem", level=2)
    m = r.get("marcos", {})
    m1 = tempo.de_iso((m.get("m1") or {}).get("horario"))
    eventos = [(k.upper(), v["descricao"], v["horario"]) for k, v in m.items()]
    eventos += [("—", x["descricao"], x["horario"]) for x in r.get("marcos_extras", [])]
    eventos.sort(key=lambda e: e[2])
    linhas = [[k, d, tempo.br(h), tempo.duracao((tempo.de_iso(h) - m1).total_seconds()) if m1 else "—"] for k, d, h in eventos]
    _tabela(doc, ["Marco", "Evento", "Horário", "Decorrido desde M1"], linhas, [1.5, 8.5, 3.5, 3.5])
    linhas = [
        ["RTO medido (M1 → M3)", tempo.duracao(res.get("rto_seg")), f"{meta['rto_min']} min", _ok(res.get("dentro_rto"))],
        ["RPO observado", tempo.duracao(res.get("rpo_seg")), f"{meta['rpo_min']} min", _ok(res.get("dentro_rpo"))],
    ]
    if "rto_projetado_com_renomeacao_seg" in res:
        linhas.append(["RTO com renomeação (item 7.4)", tempo.duracao(res["rto_projetado_com_renomeacao_seg"]),
                       f"{meta['rto_min']} min", _ok(res.get("dentro_rto_com_renomeacao"))])
    _tabela(doc, ["Indicador", "Medido", "Meta (PCN, seção 7)", "Situação"], linhas, [6, 3.5, 3.5, 4])
    _p(doc, "RPO observado: diferença entre o início da restauração (M1) e o ponto mais recente recuperável "
            "(PITR: último instante restaurável da produção no M1; .bak: gravação do arquivo; cofre isolado: "
            "criação do ponto copiado).", cor=LEGENDA, tamanho=8.5)

    d = r.get("dados", {})
    tarefas = [(k, v) for k, v in d.items() if k.startswith("tarefas_") and v.get("itens")]
    if tarefas:
        doc.add_heading("Tarefas nativas do SQL Server (pela Lambda)", level=2)
        linhas = [[f"{v.get('funcao')} → {v.get('instancia')}", i.get("banco"), i.get("task_id"), i.get("lifecycle"),
                   i.get("created_at_servidor"), i.get("last_updated_servidor")] for _, v in tarefas for i in v["itens"]]
        _tabela(doc, ["Lambda → instância", "Banco", "task_id", "lifecycle", "created_at (servidor)", "last_updated (servidor)"],
                linhas, [4.6, 2.2, 1.4, 1.9, 3.45, 3.45])
    for p in d.get("passos_exclusivos_exercicio", []):
        _p(doc, f"Passo exclusivo do exercício, incluído no RTO medido: {p['descricao']} ({tempo.duracao(p['duracao_seg'])}).",
           cor=LEGENDA, tamanho=8.5)
    if d.get("renomeacao"):
        ren = d["renomeacao"]
        _p(doc, f"Renomeação medida na instância temporária ({ren['de']} → {ren['para']}): "
                f"{tempo.duracao(ren['duracao_seg'])}. A produção não foi renomeada.")
    if d.get("copia"):
        cp, kms = d["copia"], d.get("kms_frente_c", {})
        doc.add_heading("Cópia de retorno do cofre isolado e chave KMS", level=2)
        _tabela(doc, [], [
            ["Copy job", cp.get("copy_job_id")],
            ["Duração da cópia", tempo.duracao(cp.get("duracao_seg"))],
            ["Ponto copiado (destino)", cp.get("ponto_destino")],
            ["Chave KMS do snapshot", kms.get("chave_snapshot")],
            ["Chave da conta de destino", _sim_nao(kms.get("chave_da_conta_destino"))],
            ["Estado da chave", kms.get("estado_chave")],
        ], [4.5, 12.5], chave_valor=True)
    if d.get("exportacao"):
        e = d["exportacao"]
        _p(doc, f"Banco do cliente: {d.get('banco_cliente')}. Arquivo exportado: {e['arn']} — "
                f"{'excluído em ' + tempo.br(d['exportacao_excluida']) if d.get('exportacao_excluida') else 'NÃO excluído'}.")

    doc.add_heading("Verificação de integridade", level=2)
    it = r.get("integridade") or {}
    linhas = [[t["tabela"], t.get("contagem_restaurada"), t.get("contagem_producao"),
               tempo.br(t.get("horario_restaurada")), tempo.br(t.get("horario_producao")), t.get("avaliacao")]
              for t in it.get("tabelas", [])]
    _tabela(doc, ["Tabela", "Restaurada", "Produção", "Horário (rest.)", "Horário (prod.)", "Avaliação"],
            linhas, [3.6, 2.0, 2.0, 2.8, 2.8, 3.8])
    _tabela(doc, [], [
        ["Banco", it.get("banco")],
        ["Consulta funcional", it.get("consulta_funcional")],
        ["Resultado da consulta", it.get("resultado_consulta_funcional")],
        ["Integridade confirmada", (_sim_nao(it.get("confirmada")), "F8FDE8" if it.get("confirmada") else "FDECEC")],
        ["Concluída em", tempo.br(it.get("concluida_em"))],
    ], [4.5, 12.5], chave_valor=True)

    if d.get("imutabilidade"):
        im = d["imutabilidade"]
        doc.add_heading("Verificação de imutabilidade (Object Lock)", level=2)
        _tabela(doc, [], [
            ["Objeto", im.get("objeto")],
            ["VersionId testado", im.get("version_id")],
            ["Retenção", f"{im.get('modo') or '—'} até {tempo.br(im.get('reter_ate'))}"],
            ["Permissão de exclusão (IAM)", im.get("simulacao_iam")],
            ["Horário do teste", tempo.br(im.get("horario"))],
            ["Resposta da AWS", f"{im.get('resposta') or 'não executado'} {im.get('mensagem') or ''}".strip()],
            ["Request ID", im.get("request_id")],
            ["Conclusão", im.get("conclusao")],
        ], [4.5, 12.5], chave_valor=True)

    doc.add_heading("Exclusão das instâncias e custo", level=2)
    linhas = [[i, x.get("evidencia_exclusao") or "PENDENTE"] for i, x in r.get("instancias", {}).items()]
    _tabela(doc, ["Instância", "Evidência da exclusão"], linhas, [5, 12])
    custo = res.get("custo_estimado_usd")
    _p(doc, f"Horas de instância: {res.get('horas_instancia', '—')}. Custo estimado: "
            f"{'USD ' + format(custo, '.2f') if custo is not None else 'não estimado (custo_hora_usd não configurado)'}.")

    if r.get("lacunas"):
        doc.add_heading("Lacunas registradas durante a execução", level=2)
        _tabela(doc, ["Gravidade", "Descrição", "Horário"],
                [[GRAVIDADE.get(l["gravidade"], l["gravidade"]), l["descricao"], tempo.br(l["horario"])] for l in r["lacunas"]], [2.2, 11.5, 3.3])


def gerar(cfg: dict, exercicio: str, ciclo: str) -> Path:
    s = cfg["servicos"][exercicio]
    pasta = Path(cfg["pasta_evidencias"]) / f"{exercicio}_{ciclo}"
    registros = []
    for f in sorted(pasta.glob("frente_*/registro.json")):
        registros.append(json.loads(f.read_text(encoding="utf-8")))
    if not registros:
        raise FileNotFoundError(f"Nenhum registro de frente em {pasta}")
    arq_notas = pasta / "notas_exercicio.yaml"
    if not arq_notas.exists():
        arq_notas.write_text(MODELO_NOTAS, encoding="utf-8")
        print(f"  Modelo de notas criado: {arq_notas} (preencha e gere de novo para completar o registro)")
    notas = yaml.safe_load(arq_notas.read_text(encoding="utf-8")) or {}

    doc = Document()
    _estilos(doc)
    _cabecalho_rodape(doc, str(pasta))

    t = doc.add_paragraph()
    _fonte(t.add_run(f"{exercicio} — Banco de dados do {s['nome']}"), F_TITULO, 20, AZUL, True)
    t.paragraph_format.space_after = Pt(0)
    st = doc.add_paragraph()
    _fonte(st.add_run(f"Ciclo {ciclo} · exercício semestral de recuperação"), F_TITULO, 12, VERDE, True)

    doc.add_heading("1. Identificação", level=1)
    resp = s.get("responsavel") or {}
    executantes = sorted({r.get("executante") for r in registros if r.get("executante")})
    _tabela(doc, [], [
        ["Exercício e ciclo", f"{exercicio} — {s['nome']} · {ciclo}"],
        ["Frentes executadas", "; ".join(f"{r['frente']} ({tempo.br(r.get('criado_em'))[:10]})" for r in registros)],
        ["Executante(s)", ", ".join(executantes)],
        ["Recuperação técnica", f"{resp.get('titular', '—')} (titular); {resp.get('substituto', '—')} (substituto)"],
        ["Conta AWS do serviço", s["conta_id"]],
        ["Instância de produção", s["instancia_producao"]],
    ], [4.5, 12.5], chave_valor=True)

    doc.add_heading("2. Resultado por frente", level=1)
    linhas = []
    faltando = [f for f in ("A", "B", "C") if f not in {r["frente"] for r in registros}]
    for r in registros:
        res = r.get("resultado") or {}
        meta = r["meta"]
        sucesso = res.get("resultado") == "Bem-sucedida"
        linhas.append([r["frente"], MECANISMO[_chave_mecanismo(r)],
                       f"{tempo.duracao(res.get('rto_seg'))} / {meta['rto_min']} min",
                       f"{tempo.duracao(res.get('rpo_seg'))} / {meta['rpo_min']} min",
                       _sim_nao(res.get("integridade_confirmada")),
                       ("Bem-sucedida" if sucesso else "Falha", "F8FDE8" if sucesso else "FDECEC")])
    for f in faltando:
        linhas.append([f, "não executada neste ciclo", "—", "—", "—", ("Pendente", "F5F5F5")])
    _tabela(doc, ["Frente", "Mecanismo", "RTO medido / meta", "RPO observado / meta", "Integridade", "Resultado"],
            linhas, [1.4, 5.0, 2.9, 2.9, 2.3, 2.5])
    _p(doc, "Critério (PG 13/03, seção 8): cada frente dentro do RTO e do RPO do componente no PCN, com integridade "
            "confirmada. A falha de uma frente gera Não Conformidade no CAL® (Ocorrências) e a correção do objetivo no PCN.",
       cor=LEGENDA, tamanho=8.5)

    for r in registros:
        _secao_frente(doc, r)

    doc.add_page_break()
    doc.add_heading("Lacunas e ações", level=1)
    linhas = [[f"Frente {r['frente']}", l["descricao"], GRAVIDADE.get(l["gravidade"], l["gravidade"]), "", ""] for r in registros for l in r.get("lacunas", [])]
    linhas += [["Exercício", a.get("descricao"), "", a.get("responsavel"), a.get("prazo")]
               for a in (notas.get("acoes") or []) if a.get("descricao")]
    _tabela(doc, ["Origem", "Descrição", "Gravidade", "Responsável", "Prazo"], linhas or [["—", "Nenhuma", "", "", ""]],
            [2.2, 8.6, 1.9, 2.4, 1.9])
    _tabela(doc, [], [
        ["Não Conformidade", notas.get("nao_conformidade") or "—"],
        ["Correção de RTO/RPO proposta ao PCN", notas.get("correcao_rto_rpo") or "—"],
        ["Observações", notas.get("observacoes") or "—"],
    ], [5.5, 11.5], chave_valor=True)

    doc.add_heading("Assinaturas", level=1)
    a = doc.add_table(rows=2, cols=2)
    a.alignment = WD_TABLE_ALIGNMENT.CENTER
    for j, (papel, nome) in enumerate((("Executante", ", ".join(executantes)),
                                       ("Coordenador do PCN", notas.get("coordenador_pcn") or ""))):
        c = a.rows[0].cells[j]
        c.paragraphs[0].paragraph_format.space_before = Pt(36)
        _linha_verde(c.paragraphs[0])
        p = a.rows[1].cells[j].paragraphs[0]
        _fonte(p.add_run(papel), tamanho=9, cor=AZUL, negrito=True)
        _fonte(a.rows[1].cells[j].add_paragraph().add_run(nome or " "), tamanho=9)
        _fonte(a.rows[1].cells[j].add_paragraph().add_run("Data: ____/____/______"), tamanho=9, cor=LEGENDA)

    saida = pasta / f"{exercicio}_{ciclo} - Registro do Exercício de Recuperação.docx"
    doc.save(saida)
    return saida
