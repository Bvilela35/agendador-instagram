#!/usr/bin/env python3
"""Partida no Railway: baixa a versão mais recente do repositório (planilha,
vídeos e código) para o volume e sobe o servidor a partir dela.

O envio direto pelo `railway up` não aceita os vídeos (passa de 200 MB), então
o contêiner leva só este arquivo e busca o resto no GitHub ao iniciar. Se o
GitHub estiver fora do ar, usa a última cópia baixada.
"""
import os
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO = os.environ.get("REPOSITORIO", "Bvilela35/agendador-instagram")
RAMO = os.environ.get("RAMO", "main")
BASE = Path(os.environ.get("PASTA_DADOS", "/data"))
ATUAL, NOVO, PACOTE = BASE / "repo", BASE / "repo-novo", BASE / "repo.tar.gz"


def baixar() -> None:
    url = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{RAMO}"
    shutil.rmtree(NOVO, ignore_errors=True)
    NOVO.mkdir(parents=True)
    with urllib.request.urlopen(url, timeout=120) as resp, open(PACOTE, "wb") as f:
        shutil.copyfileobj(resp, f, 1024 * 1024)
    raiz = NOVO.resolve()
    with tarfile.open(PACOTE) as tar:
        for item in tar:
            partes = Path(item.name).parts[1:]  # tira a pasta "repo-ramo/" do começo
            if not partes or not (item.isfile() or item.isdir()):
                continue
            destino = (NOVO / Path(*partes)).resolve()
            if raiz != destino and raiz not in destino.parents:
                continue
            if item.isdir():
                destino.mkdir(parents=True, exist_ok=True)
                continue
            destino.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(item) as origem, open(destino, "wb") as saida:
                shutil.copyfileobj(origem, saida, 1024 * 1024)
    PACOTE.unlink(missing_ok=True)
    if not (NOVO / "servidor.py").is_file():
        raise RuntimeError("o pacote baixado não tem servidor.py")
    shutil.rmtree(ATUAL, ignore_errors=True)
    NOVO.rename(ATUAL)


try:
    baixar()
    print(f"Repositório {REPO}@{RAMO} atualizado.", flush=True)
except Exception as e:
    print(f"Não consegui atualizar o repositório ({type(e).__name__}: {e}).", flush=True)
    if not (ATUAL / "servidor.py").is_file():
        sys.exit("Sem cópia anterior para usar. Encerrando.")
    print("Usando a última cópia baixada.", flush=True)

os.chdir(ATUAL)
os.execv(sys.executable, [sys.executable, str(ATUAL / "servidor.py")])
