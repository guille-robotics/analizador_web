"""Exporta un analisis (o un informe combinado) a PDF con reportlab."""
import io
import re
from datetime import datetime
from pathlib import Path

GREEN = "#1f7a4d"
REPLACE = {"→": "->", "←": "<-", "≥": ">=", "≤": "<=", "≈": "~", "✓": "-", "✔": "-",
           " ": " ", "‑": "-", "​": "", "−": "-"}


def clean(text):
    """Las fuentes estandar del PDF solo cubren Latin-1/Windows-1252 (espanol incluido):
    se reemplazan los simbolos habituales y se quitan los que no existen (emojis, etc.)."""
    out = []
    for ch in str(text or ""):
        for c in REPLACE.get(ch, ch):
            try:
                c.encode("cp1252")
                out.append(c)
            except UnicodeEncodeError:
                pass
    return "".join(out)


def inline(text):
    s = clean(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', s)
    return s


def fmt_date(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return str(iso or "")


def build_pdf(meta, frames_dir, include_frames=True, include_chat=False):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (HRFlowable, Image, KeepTogether, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)

    accent = colors.HexColor(GREEN)
    body = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=14.5, alignment=TA_LEFT, spaceAfter=4)
    st = {
        "body": body,
        "title": ParagraphStyle("title", parent=body, fontName="Helvetica-Bold", fontSize=21, leading=25,
                                textColor=colors.HexColor("#14352a"), spaceAfter=2),
        "sub": ParagraphStyle("sub", parent=body, fontSize=10, textColor=colors.HexColor("#5f6b63"), spaceAfter=10),
        "h2": ParagraphStyle("h2", parent=body, fontName="Helvetica-Bold", fontSize=14, leading=18, textColor=accent,
                             spaceBefore=12, spaceAfter=4, keepWithNext=1),
        "h3": ParagraphStyle("h3", parent=body, fontName="Helvetica-Bold", fontSize=12, leading=16,
                             textColor=colors.HexColor("#14352a"), spaceBefore=8, spaceAfter=3, keepWithNext=1),
        "h4": ParagraphStyle("h4", parent=body, fontName="Helvetica-Bold", fontSize=10.5, spaceBefore=6,
                             spaceAfter=2, keepWithNext=1),
        "bullet": ParagraphStyle("bullet", parent=body, leftIndent=15, bulletIndent=3, spaceAfter=2.5),
        "cell": ParagraphStyle("cell", parent=body, fontSize=9, leading=12, spaceAfter=0),
        "cellb": ParagraphStyle("cellb", parent=body, fontName="Helvetica-Bold", fontSize=9, leading=12,
                                spaceAfter=0, textColor=colors.HexColor("#14352a")),
        "cap": ParagraphStyle("cap", parent=body, fontSize=8.5, leading=11, spaceAfter=0,
                              textColor=colors.HexColor("#3c4640")),
        "q": ParagraphStyle("q", parent=body, backColor=colors.HexColor("#e5f2ea"), borderPadding=(5, 6, 5, 6),
                            spaceBefore=8, spaceAfter=8),
    }

    def md_flowables(text):
        flows = []
        for raw in (text or "").split("\n"):
            line = raw.rstrip()
            m = re.match(r"^(#{1,4})\s+(.*)$", line)
            if m:
                level = max(2, min(len(m.group(1)), 4))   # "#" y "##" -> h2, "###" -> h3, "####" -> h4
                flows.append(Paragraph(inline(m.group(2)), st[f"h{level}"]))
                continue
            m = re.match(r"^\s*[-*•]\s+(.*)$", line)
            if m:
                flows.append(Paragraph(inline(m.group(1)), st["bullet"], bulletText="•"))
                continue
            m = re.match(r"^\s*(\d+)[.)]\s+(.*)$", line)
            if m:
                flows.append(Paragraph(inline(m.group(2)), st["bullet"], bulletText=f"{m.group(1)}."))
                continue
            if re.match(r"^\s*(-{3,}|_{3,})\s*$", line):
                flows.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#c8d2cb"),
                                        spaceBefore=6, spaceAfter=6))
                continue
            if not line.strip():
                continue
            flows.append(Paragraph(inline(line), st["body"]))
        return flows

    def kv_table(rows):
        data = [[Paragraph(inline(k), st["cellb"]), Paragraph(v, st["cell"])] for k, v in rows]
        t = Table(data, colWidths=[3.6 * cm, 13.4 * cm])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.HexColor("#f2f6f3"), colors.white]),
            ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#dfe5e0")),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    def link(url):
        u = clean(url).replace("&", "&amp;")
        return f'<link href="{u}" color="{GREEN}">{u}</link>'

    def thumb(path, width_cm=8.2):
        import cv2
        img = cv2.imread(str(path))
        if img is None:
            return None
        h, w = img.shape[:2]
        if w > 640:
            img = cv2.resize(img, (640, int(h * 640 / w)))
            h, w = img.shape[:2]
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 78])
        if not ok:
            return None
        wp = width_cm * cm
        return Image(io.BytesIO(buf.tobytes()), width=wp, height=wp * h / w)

    combined = meta.get("kind") == "combined"
    pos = "Local" if meta.get("position") == "local" else "Visitante"
    story = [Paragraph(inline(f"Informe táctico: {meta['team']}"), st["title"]),
             Paragraph(clean(f"Combinado de {len(meta.get('sources', []))} análisis" if combined
                             else "Análisis de rival a partir de video") +
                       clean(f" · generado el {datetime.now():%d/%m/%Y %H:%M}"), st["sub"])]

    if combined:
        matches = {}
        rows = [["Equipo", inline(meta["team"])]]
        src_lines = []
        for i, s in enumerate(meta["sources"], 1):
            p = matches.setdefault(s["clean_url"], len(matches) + 1)
            end = s["start_minute"] + s["duration_minutes"]
            spos = "local" if s["position"] == "local" else "visitante"
            src_lines.append(f"<b>F{i}</b> · partido P{p} · min {s['start_minute']}-{end} · {spos}, "
                             f"camiseta {inline(s['color'])} · {s['n_useful']} frames útiles · "
                             f"analizado el {fmt_date(s['created'])}")
        rows.append(["Fuentes", "<br/>".join(src_lines)])
        rows.append(["Partidos", "<br/>".join(f"<b>P{n}</b> · {link(u)}" for u, n in matches.items())])
        rows.append(["Modelo", inline(meta.get("model", ""))])
    else:
        end = meta["start_minute"] + meta["duration_minutes"]
        f = meta.get("filter") or {}
        useful = sum(1 for x in meta["frames"] if x.get("ok"))
        frames_txt = f"{useful} útiles de {len(meta['frames'])} enviados a Claude"
        if f:
            frames_txt += f" ({f.get('reviewed', 0)} tomas revisadas, {f.get('wide', 0)} planos generales)"
        camiseta = inline(meta["color"]) + (f" (rival: {inline(meta['rival_color'])})" if meta.get("rival_color") else "")
        rows = [["Equipo", inline(meta["team"])], ["Juega de", pos], ["Camiseta", camiseta],
                ["Video", link(meta["clean_url"])], ["Tramo", f"minutos {meta['start_minute']} a {end} del video"],
                ["Frames", clean(frames_txt)], ["Modelo", inline(meta.get("model", ""))],
                ["Analizado el", fmt_date(meta.get("created"))]]
    story += [kv_table(rows), Spacer(1, 6)]
    story += md_flowables(meta.get("report", ""))

    if include_frames and not combined:
        shown = sorted((x for x in meta["frames"] if x.get("ok")), key=lambda x: x["seconds"])
        cells = []
        for fr in shown:
            im = thumb(Path(frames_dir) / fr["file"])
            if im is None:
                continue
            m = re.search(r"Fase del juego:\s*(.+)", fr.get("analysis", ""))
            cap = f"<b>min {inline(fr['time'])}</b>"
            if m:
                phase = clean(m.group(1)).strip()
                cap += " · " + inline(phase[:110] + ("…" if len(phase) > 110 else ""))
            cells.append([im, Paragraph(cap, st["cap"])])
        if cells:
            story += [Paragraph("Fotogramas analizados", st["h2"]),
                      Paragraph("Planos generales que se enviaron a Claude, para verificar que miró al equipo correcto.",
                                st["sub"])]
            grid = [cells[i:i + 2] for i in range(0, len(cells), 2)]
            for row in grid:
                while len(row) < 2:
                    row.append("")
            t = Table(grid, colWidths=[8.5 * cm, 8.5 * cm])
            t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 9), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
            story.append(t)

    chat = meta.get("chat") or []
    if include_chat and chat:
        story.append(Paragraph("Conversación", st["h2"]))
        for h in chat:
            if h["role"] == "user":
                story.append(Paragraph("<b>Pregunta:</b> " + inline(h["content"]), st["q"]))
            else:
                story += md_flowables(h["content"])
                story.append(Spacer(1, 4))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#5f6b63"))
        canvas.drawString(2 * cm, 1.1 * cm, clean("Analizador de rivales · basado en fotogramas sueltos: tratar como hipótesis"))
        canvas.drawRightString(A4[0] - 2 * cm, 1.1 * cm, clean(f"Página {doc.page}"))
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.8 * cm,
                            bottomMargin=2 * cm, title=clean(f"Informe táctico: {meta['team']}"),
                            author="Analizador de rivales")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
