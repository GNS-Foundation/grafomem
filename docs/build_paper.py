import pathlib
import re
import subprocess

SRC = pathlib.Path("/mnt/user-data/outputs/grafomem-paper.md").read_text()
BUILD = pathlib.Path("/tmp/paperbuild")
BUILD.mkdir(parents=True, exist_ok=True)

# --- preprocess markdown ---------------------------------------------------
text = re.sub(r"<!--.*?-->", "", SRC, flags=re.DOTALL)
out, kt, ka, kf = [], False, False, False
for ln in text.splitlines():
    s = ln.strip()
    if not kt and s.startswith("# GRAFOMEM"):
        kt = True; continue
    if not ka and s.startswith("**Camilo"):
        ka = True; continue
    if not kf and s.startswith("*GNS Foundation"):
        kf = True; continue
    if s == "---":
        continue
    out.append(ln)
body = "\n".join(out).lstrip("\n")

YAML = (
    "---\n"
    'title: "GRAFOMEM: A Reproducible Benchmark for Agent Memory, and What It Says '
    'a Memory Protocol Must Specify"\n'
    'author: "Camilo Ayerbe Posada — GNS Foundation · ULISSY s.r.l. · grafomem.com"\n'
    'date: "May 2026"\n'
    "---\n\n"
)
(BUILD / "paper.md").write_text(YAML + body)

(BUILD / "header.tex").write_text(
    r"""\usepackage{amssymb}
\usepackage{etoolbox}
\AtBeginEnvironment{longtable}{\footnotesize}
\usepackage{newunicodechar}
\newunicodechar{✓}{\ensuremath{\checkmark}}
\newunicodechar{✗}{\ensuremath{\times}}
\newunicodechar{≠}{\ensuremath{\neq}}
\newunicodechar{≤}{\ensuremath{\leq}}
\newunicodechar{≥}{\ensuremath{\geq}}
\newunicodechar{→}{\ensuremath{\rightarrow}}
\newunicodechar{×}{\ensuremath{\times}}
\newunicodechar{·}{\textperiodcentered}
\newunicodechar{∩}{\ensuremath{\cap}}
\newunicodechar{∪}{\ensuremath{\cup}}
\newunicodechar{∅}{\ensuremath{\varnothing}}
\newunicodechar{𝟙}{\ensuremath{\mathbb{1}}}
"""
)

# --- step 1: pandoc -> .tex ------------------------------------------------
r = subprocess.run(
    ["pandoc", str(BUILD / "paper.md"), "--standalone",
     "-H", str(BUILD / "header.tex"), "--toc", "-V", "toc-depth=2",
     "-V", "geometry:margin=1in", "-V", "fontsize=11pt",
     "-V", "colorlinks=true", "-V", "linkcolor=NavyBlue",
     "-o", str(BUILD / "paper.tex")],
    capture_output=True, text=True,
)
print("pandoc rc:", r.returncode, r.stderr[-800:])

# --- step 2: drop lmodern (minimal texlive lacks it; xelatex doesn't need it)
tex = (BUILD / "paper.tex").read_text()
tex = re.sub(r"^\\usepackage\{lmodern\}\n", "", tex, flags=re.MULTILINE)
(BUILD / "paper.tex").write_text(tex)
print("lmodern present after strip:", "lmodern" in tex)

# --- step 3: xelatex x2 (TOC needs two passes; no citations -> no bibtex) ---
for i in (1, 2):
    x = subprocess.run(
        ["xelatex", "-interaction=nonstopmode", "-halt-on-error", "paper.tex"],
        cwd=str(BUILD), capture_output=True, text=True,
    )
    print(f"xelatex pass {i} rc:", x.returncode)
    if x.returncode != 0:
        print(x.stdout[-2500:])
        break

pdf = BUILD / "paper.pdf"
if pdf.exists():
    subprocess.run(["cp", str(pdf), "/mnt/user-data/outputs/grafomem-paper.pdf"])
    print("copied PDF to outputs")
