#!/usr/bin/env python3
"""Servidor do agendador: painel de agendamentos + publicação automática.

Feito para rodar num serviço sempre ligado (Railway). Faz três coisas:

  1. Mostra o painel (/) com todos os posts de posts.csv e o estado de cada um.
  2. Serve a pasta midia/ em /midia/, que é de onde o Instagram baixa os vídeos.
  3. Se PUBLICAR=1 e houver IG_ACCESS_TOKEN, roda `scheduler.py publicar` a cada
     minuto. Sem isso fica só como painel, lendo o registro de publicações do
     GitHub (ESTADO_REMOTO), para não publicar em dobro com o GitHub Actions.

Só usa a biblioteca padrão do Python.
"""
import argparse
import contextlib
import io
import json
import mimetypes
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DOMINIO = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
if DOMINIO and not os.environ.get("MEDIA_BASE_URL"):
    os.environ["MEDIA_BASE_URL"] = f"https://{DOMINIO}/midia/"

import scheduler  # noqa: E402  (lê as variáveis de ambiente ao importar)

ROOT = Path(__file__).resolve().parent
PORTA = int(os.environ.get("PORT", "8080"))
INTERVALO = float(os.environ.get("INTERVALO_SEGUNDOS", "60"))
ESTADO_REMOTO = os.environ.get(
    "ESTADO_REMOTO",
    "https://raw.githubusercontent.com/Bvilela35/agendador-instagram/main/publicados.json",
).strip()
PUBLICA = os.environ.get("PUBLICAR", "").strip() == "1" and bool(
    os.environ.get("IG_ACCESS_TOKEN", "").strip()
)
PERFIL = os.environ.get("PERFIL", "favo.colmeia").strip().lstrip("@")

_trava = threading.Lock()
_estado_remoto: dict = {}
_rodadas: list = []  # últimas rodadas de publicação (texto), mais nova primeiro
_ultima_checagem = None


def _baixar_estado_remoto() -> dict:
    if not ESTADO_REMOTO:
        return {}
    url = f"{ESTADO_REMOTO}?t={int(time.time())}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.load(resp)


def _semear_estado() -> None:
    """Na primeira subida em modo de publicação, copia para o volume o que já
    foi publicado antes (pelo GitHub Actions), para não repetir post."""
    conhecido = {}
    empacotado = ROOT / "publicados.json"
    if empacotado.exists() and empacotado.resolve() != scheduler.STATE_FILE.resolve():
        with contextlib.suppress(ValueError, OSError):
            conhecido.update(json.loads(empacotado.read_text(encoding="utf-8") or "{}"))
    with contextlib.suppress(Exception):
        conhecido.update(_baixar_estado_remoto())
    scheduler.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    estado = scheduler.load_state()
    novos = {k: v for k, v in conhecido.items() if k not in estado}
    if novos:
        estado.update(novos)
        scheduler.save_state(estado)
        print(f"Registro inicial: {len(novos)} publicação(ões) anteriores importadas.", flush=True)


def _rodada() -> None:
    global _estado_remoto, _ultima_checagem
    if PUBLICA:
        saida = io.StringIO()
        try:
            with contextlib.redirect_stdout(saida), contextlib.redirect_stderr(saida):
                scheduler.cmd_publicar(argparse.Namespace(simular=False))
        except SystemExit as e:
            saida.write(f"\nParou: {e}")
        except Exception as e:  # nunca derruba o laço
            saida.write(f"\nErro inesperado: {type(e).__name__}: {e}")
        texto = saida.getvalue().strip()
        if texto and not texto.startswith("Nada para publicar"):
            print(texto, flush=True)
            with _trava:
                _rodadas.insert(0, {"quando": scheduler.now_iso(), "texto": texto})
                del _rodadas[20:]
    else:
        with contextlib.suppress(Exception):
            remoto = _baixar_estado_remoto()
            with _trava:
                _estado_remoto = remoto
    _ultima_checagem = scheduler.now_iso()


def _laco() -> None:
    if PUBLICA:
        _semear_estado()
    while True:
        _rodada()
        time.sleep(INTERVALO)


def _estado_atual() -> dict:
    if PUBLICA:
        with contextlib.suppress(Exception):
            return scheduler.load_state()
        return {}
    with _trava:
        return dict(_estado_remoto)


def dados_do_painel() -> dict:
    posts, erros = scheduler.load_posts()
    estado = _estado_atual()
    agora = datetime.now(scheduler.TZ)
    itens = []
    for post in posts:
        entrada = estado.get(post.key, {})
        status = entrada.get("status")
        if not status:
            if post.when <= agora - scheduler.MAX_DELAY:
                status = "passou"
            elif post.when <= agora:
                status = "na fila"
            else:
                status = "agendado"
        arquivo = post.files[0] if post.files else ""
        local = bool(arquivo) and not scheduler.is_url(arquivo)
        miniatura = Path(arquivo).with_suffix(".jpg").name if local else ""
        itens.append({
            "quando": post.when.isoformat(timespec="minutes"),
            "tipo": post.kind or post.kind_raw,
            "arquivos": post.files,
            "midia": f"/midia/{urllib.parse.quote(arquivo)}" if local else arquivo,
            "miniatura": (
                f"/miniaturas/{urllib.parse.quote(miniatura)}"
                if miniatura and (ROOT / "miniaturas" / miniatura).is_file() else ""
            ),
            "legenda": post.caption,
            "status": status,
            "link": entrada.get("permalink"),
            "erro": entrada.get("ultimo_erro"),
            "tentativas": entrada.get("tentativas"),
            "atualizado": entrada.get("atualizado"),
        })
    with _trava:
        rodadas = list(_rodadas[:5])
    return {
        "agora": agora.isoformat(timespec="seconds"),
        "fuso": scheduler.TZ.key,
        "perfil": PERFIL,
        "modo": "publicando" if PUBLICA else "painel",
        "ultima_checagem": _ultima_checagem,
        "erros_planilha": erros,
        "rodadas": rodadas,
        "posts": itens,
    }


class Tratador(BaseHTTPRequestHandler):
    server_version = "agendador"
    protocol_version = "HTTP/1.1"

    def log_message(self, formato, *args):  # silencia o log de cada requisição
        pass

    def _enviar(self, codigo: int, corpo: bytes, tipo: str, extras: dict = None) -> None:
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        for chave, valor in (extras or {}).items():
            self.send_header(chave, valor)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(corpo)

    def _arquivo(self, pasta: Path, nome: str) -> None:
        caminho = (pasta / nome).resolve()
        if pasta.resolve() not in caminho.parents or not caminho.is_file():
            return self._enviar(404, b"nao encontrado", "text/plain; charset=utf-8")
        tamanho = caminho.stat().st_size
        tipo = mimetypes.guess_type(caminho.name)[0] or "application/octet-stream"
        inicio, fim, codigo = 0, tamanho - 1, 200
        faixa = self.headers.get("Range", "")
        if faixa.startswith("bytes=") and "," not in faixa:
            a, _, b = faixa[6:].partition("-")
            try:
                if a:
                    inicio = int(a)
                    fim = min(int(b), tamanho - 1) if b else tamanho - 1
                else:
                    inicio = max(tamanho - int(b), 0)
                if inicio > fim or inicio >= tamanho:
                    raise ValueError
                codigo = 206
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{tamanho}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(fim - inicio + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "public, max-age=3600")
        if codigo == 206:
            self.send_header("Content-Range", f"bytes {inicio}-{fim}/{tamanho}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(caminho, "rb") as f:
            f.seek(inicio)
            falta = fim - inicio + 1
            while falta > 0:
                bloco = f.read(min(256 * 1024, falta))
                if not bloco:
                    break
                try:
                    self.wfile.write(bloco)
                except (BrokenPipeError, ConnectionResetError):
                    return
                falta -= len(bloco)

    def do_GET(self):
        caminho = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        if caminho in ("/", "/index.html"):
            corpo = (ROOT / "painel.html").read_bytes()
            return self._enviar(200, corpo, "text/html; charset=utf-8", {"Cache-Control": "no-cache"})
        if caminho == "/api/posts":
            try:
                corpo = json.dumps(dados_do_painel(), ensure_ascii=False).encode()
            except SystemExit as e:
                corpo = json.dumps({"erro": str(e)}, ensure_ascii=False).encode()
                return self._enviar(500, corpo, "application/json; charset=utf-8")
            return self._enviar(200, corpo, "application/json; charset=utf-8", {"Cache-Control": "no-store"})
        if caminho == "/saude":
            return self._enviar(200, b"ok", "text/plain; charset=utf-8")
        if caminho.startswith("/midia/"):
            return self._arquivo(scheduler.MEDIA_DIR, caminho[len("/midia/"):])
        if caminho.startswith("/miniaturas/"):
            return self._arquivo(ROOT / "miniaturas", caminho[len("/miniaturas/"):])
        return self._enviar(404, b"nao encontrado", "text/plain; charset=utf-8")

    do_HEAD = do_GET


def main() -> None:
    threading.Thread(target=_laco, daemon=True).start()
    modo = "publicando a cada %gs" % INTERVALO if PUBLICA else "somente painel"
    print(f"Agendador no ar na porta {PORTA} ({modo}).", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORTA), Tratador).serve_forever()


if __name__ == "__main__":
    main()
