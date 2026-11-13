#!/usr/bin/env python3
"""Agendador de posts do Instagram usando a API oficial (login do Instagram).

Lê posts.csv, publica os posts cujo horário já chegou e registra o resultado
em publicados.json. Feito para rodar a cada 15 minutos (GitHub Actions), mas
funciona igual na sua máquina.

Comandos:
  verificar        confere posts.csv e as mídias, sem publicar nada
  publicar         publica o que estiver na hora (use --simular para só mostrar)
  conta            mostra a conta conectada e a cota de publicação
  renovar-token    gera um token novo (imprime só o token no stdout)

Só usa a biblioteca padrão do Python (3.9+).
"""
import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """Carrega .env (CHAVE=valor) para uso local. Variáveis já definidas vencem."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

POSTS_CSV = Path(os.environ.get("POSTS_CSV", ROOT / "posts.csv"))
STATE_FILE = Path(os.environ.get("ESTADO_JSON", ROOT / "publicados.json"))
MEDIA_DIR = Path(os.environ.get("MIDIA_DIR", ROOT / "midia"))
# Token do login do Instagram (começa com "IG") usa graph.instagram.com.
# Token do login unificado/Facebook (começa com "EAA") usa graph.facebook.com.
_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "").strip()
API_BASE = (
    os.environ.get("IG_API_BASE")
    or ("https://graph.instagram.com" if _TOKEN.startswith("IG") else "https://graph.facebook.com")
).rstrip("/")
FACEBOOK_GRAPH = "graph.facebook.com" in API_BASE
# Vazio = usa a versão padrão do app na Meta.
API_VERSION = os.environ.get("IG_API_VERSION", "").strip()
TZ = ZoneInfo(os.environ.get("FUSO", "America/Sao_Paulo"))
MAX_DELAY = timedelta(hours=float(os.environ.get("MAX_ATRASO_HORAS", "12")))
MAX_ATTEMPTS = int(os.environ.get("MAX_TENTATIVAS", "3"))
POLL_SECONDS = float(os.environ.get("INTERVALO_STATUS", "5"))

TYPES = {
    "foto": "foto", "imagem": "foto", "image": "foto", "post": "foto",
    "carrossel": "carrossel", "carousel": "carrossel",
    "reels": "reels", "reel": "reels", "video": "reels", "vídeo": "reels",
    "story": "story", "stories": "story", "storie": "story",
}
IMAGE_EXT = {".jpg", ".jpeg"}
VIDEO_EXT = {".mp4", ".mov"}
DATE_FORMATS = ("%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M")


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    pass


def api(method: str, path: str, params: Optional[dict] = None, versioned: bool = True) -> dict:
    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not token:
        raise SystemExit("Falta a variável IG_ACCESS_TOKEN (secret no GitHub ou arquivo .env).")
    params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    params["access_token"] = token
    prefix = f"{API_BASE}/{API_VERSION}" if versioned and API_VERSION else API_BASE
    url = f"{prefix}/{path.lstrip('/')}"
    query = urllib.parse.urlencode(params)
    data = None
    if method == "GET":
        url = f"{url}?{query}"
    else:
        data = query.encode()
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            err = json.loads(body)["error"]
            detail = err.get("error_user_msg") or err.get("message")
            msg = f"{detail} (code {err.get('code')}, subcode {err.get('error_subcode')})"
        except (ValueError, KeyError, TypeError):
            msg = body[:300]
        raise ApiError(f"HTTP {e.code}: {msg}") from None
    except urllib.error.URLError as e:
        raise ApiError(f"Erro de rede: {e.reason}") from None


_ig_user_id: Optional[str] = None


def ig_user_id() -> str:
    """ID da conta do Instagram que recebe os posts."""
    global _ig_user_id
    if _ig_user_id:
        return _ig_user_id
    if os.environ.get("IG_USER_ID", "").strip():
        _ig_user_id = os.environ["IG_USER_ID"].strip()
    elif FACEBOOK_GRAPH:
        # Token do login unificado: descobre a conta pelas permissões do próprio token.
        info = api("GET", "debug_token", {"input_token": os.environ.get("IG_ACCESS_TOKEN", "").strip()})
        ids = []
        for scope in (info.get("data") or {}).get("granular_scopes") or []:
            if scope.get("scope") == "instagram_content_publish":
                ids = scope.get("target_ids") or []
        if len(ids) != 1:
            raise ApiError(
                "não consegui descobrir qual conta do Instagram o token publica "
                f"({len(ids)} encontradas). Defina IG_USER_ID com o ID da conta."
            )
        _ig_user_id = str(ids[0])
    else:
        me = api("GET", "me", {"fields": "user_id,username"})
        _ig_user_id = str(me.get("user_id") or me["id"])
    return _ig_user_id


# --------------------------------------------------------------------------- #
# Posts
# --------------------------------------------------------------------------- #
@dataclass
class Post:
    line: int
    when: datetime
    kind: Optional[str]
    kind_raw: str
    files: List[str]
    caption: str

    @property
    def key(self) -> str:
        return f"{self.when.strftime('%Y-%m-%d %H:%M')} | {' | '.join(self.files)}"

    def label(self) -> str:
        return f"linha {self.line} ({self.when.strftime('%d/%m %H:%M')}, {self.kind or self.kind_raw})"


def load_posts() -> Tuple[List[Post], List[str]]:
    if not POSTS_CSV.exists():
        raise SystemExit(f"Não encontrei {POSTS_CSV.name}. Copie posts.exemplo.csv para começar.")
    raw = POSTS_CSV.read_text(encoding="utf-8-sig")
    header = raw.splitlines()[0] if raw.strip() else ""
    delimiter = ";" if header.count(";") > header.count(",") else ","
    reader = csv.DictReader(io.StringIO(raw), delimiter=delimiter)
    posts, errors = [], []
    for row in reader:
        line = reader.line_num
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k}
        if not any(row.values()):
            continue
        stamp = f"{row.get('data', '')} {row.get('hora', '')}"
        when = None
        for fmt in DATE_FORMATS:
            try:
                when = datetime.strptime(stamp, fmt).replace(tzinfo=TZ)
                break
            except ValueError:
                pass
        if when is None:
            errors.append(f"linha {line}: data/hora inválida '{stamp.strip()}' (use 2026-12-01 e 18:30)")
            continue
        kind_raw = row.get("tipo", "").lower()
        files = [f.strip() for f in row.get("arquivos", "").split("|") if f.strip()]
        posts.append(Post(line, when, TYPES.get(kind_raw), kind_raw, files, row.get("legenda", "")))
    posts.sort(key=lambda p: p.when)
    return posts, errors


def is_url(name: str) -> bool:
    return name.startswith(("http://", "https://"))


def is_video(name: str) -> bool:
    path = urllib.parse.urlparse(name).path if is_url(name) else name
    return Path(path).suffix.lower() in VIDEO_EXT


def jpeg_size(path: Path):
    """Retorna (largura, altura), None se não achar, ou False se não for JPEG."""
    with open(path, "rb") as f:
        if f.read(2) != b"\xff\xd8":
            return False
        while True:
            b = f.read(1)
            while b and b != b"\xff":
                b = f.read(1)
            while b == b"\xff":
                b = f.read(1)
            if not b:
                return None
            marker = b[0]
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:
                continue
            seg_len = int.from_bytes(f.read(2), "big")
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                f.read(1)
                h = int.from_bytes(f.read(2), "big")
                w = int.from_bytes(f.read(2), "big")
                return w, h
            if seg_len < 2:
                return None
            f.seek(seg_len - 2, 1)


def validate(post: Post) -> Tuple[List[str], List[str]]:
    """Retorna (erros, avisos)."""
    errors, warnings = [], []
    if post.kind is None:
        errors.append(f"tipo '{post.kind_raw}' desconhecido (use foto, carrossel, reels ou story)")
        return errors, warnings
    n = len(post.files)
    if n == 0:
        errors.append("coluna 'arquivos' vazia")
    elif post.kind == "foto" and (n != 1 or is_video(post.files[0])):
        errors.append("foto precisa de exatamente 1 arquivo .jpg")
    elif post.kind == "reels" and (n != 1 or not is_video(post.files[0])):
        errors.append("reels precisa de exatamente 1 arquivo .mp4 ou .mov")
    elif post.kind == "story" and n != 1:
        errors.append("story precisa de exatamente 1 arquivo (.jpg, .mp4 ou .mov)")
    elif post.kind == "carrossel" and not 2 <= n <= 20:
        errors.append(f"carrossel precisa de 2 a 20 arquivos (tem {n})")

    for name in post.files:
        if is_url(name):
            continue
        path = MEDIA_DIR / name
        if not path.is_file():
            errors.append(f"arquivo não encontrado: midia/{name}")
            continue
        ext = path.suffix.lower()
        if ext in VIDEO_EXT:
            if path.stat().st_size > 300 * 1024 * 1024:
                warnings.append(f"{name}: vídeo acima de 300 MB, o Instagram pode recusar")
            continue
        if ext not in IMAGE_EXT:
            errors.append(f"{name}: formato não aceito (a API só aceita JPEG, .mp4 e .mov)")
            continue
        size = jpeg_size(path)
        if size is False:
            errors.append(f"{name}: tem extensão .jpg mas não é JPEG de verdade")
        elif size:
            ratio = size[0] / size[1]
            if not 0.8 - 0.005 <= ratio <= 1.91 + 0.005:
                errors.append(
                    f"{name}: proporção {size[0]}x{size[1]} fora do aceito "
                    "(entre 4:5 vertical e 1,91:1 horizontal)"
                )
            if size[0] > 1440:
                warnings.append(f"{name}: {size[0]}px de largura, o Instagram reduz para 1440px")

    if len(post.caption) > 2200:
        errors.append(f"legenda com {len(post.caption)} caracteres (máximo 2.200)")
    if post.caption.count("#") > 30:
        errors.append("mais de 30 hashtags")
    if post.caption.count("@") > 20:
        errors.append("mais de 20 menções")
    return errors, warnings


# --------------------------------------------------------------------------- #
# Estado
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_FILE.exists() and STATE_FILE.read_text(encoding="utf-8").strip():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(STATE_FILE)


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Publicação
# --------------------------------------------------------------------------- #
def media_url(name: str) -> str:
    if is_url(name):
        return name
    base = os.environ.get("MEDIA_BASE_URL", "").strip()
    if not base:
        raise SystemExit(
            "Falta MEDIA_BASE_URL: o endereço público da pasta midia/ "
            "(no GitHub Actions o workflow já define isso sozinho)."
        )
    return base.rstrip("/") + "/" + urllib.parse.quote(name)


def create_container(params: dict) -> str:
    return api("POST", f"{ig_user_id()}/media", params)["id"]


def wait_ready(container_id: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while True:
        info = api("GET", container_id, {"fields": "status_code,status"})
        code = info.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise ApiError(f"o Instagram não processou a mídia ({code}): {info.get('status', 'sem detalhes')}")
        if time.time() > deadline:
            raise ApiError(f"a mídia demorou mais de {int(timeout)}s para processar")
        time.sleep(POLL_SECONDS)


def build_container(post: Post) -> str:
    urls = [media_url(f) for f in post.files]
    if post.kind == "foto":
        cid = create_container({"image_url": urls[0], "caption": post.caption})
        wait_ready(cid, 180)
        return cid
    if post.kind == "story":
        # A API não aceita legenda, figurinhas nem link para o Reels em stories.
        if is_video(urls[0]):
            cid = create_container({"media_type": "STORIES", "video_url": urls[0]})
            wait_ready(cid, 900)
        else:
            cid = create_container({"media_type": "STORIES", "image_url": urls[0]})
            wait_ready(cid, 180)
        return cid
    if post.kind == "reels":
        cid = create_container({"media_type": "REELS", "video_url": urls[0], "caption": post.caption})
        wait_ready(cid, 900)
        return cid
    children = []
    for url in urls:
        params = {"is_carousel_item": "true"}
        if is_video(url):
            params.update(media_type="VIDEO", video_url=url)
        else:
            params["image_url"] = url
        children.append((create_container(params), is_video(url)))
    for child_id, video in children:
        wait_ready(child_id, 900 if video else 180)
    cid = create_container({
        "media_type": "CAROUSEL",
        "children": ",".join(c for c, _ in children),
        "caption": post.caption,
    })
    wait_ready(cid, 180)
    return cid


def publish(post: Post, state: dict) -> dict:
    container_id = build_container(post)
    # Guarda o container antes de publicar: se a execução cair no meio, a próxima
    # consulta o status e não publica de novo.
    entry = state.setdefault(post.key, {})
    entry.update(status="publicando", container=container_id, atualizado=now_iso())
    save_state(state)
    media_id = api("POST", f"{ig_user_id()}/media_publish", {"creation_id": container_id})["id"]
    permalink = None
    try:
        permalink = api("GET", media_id, {"fields": "permalink"}).get("permalink")
    except ApiError:
        pass
    return {"media_id": media_id, "permalink": permalink}


def already_published(entry: dict) -> bool:
    """Confere se um post que ficou em 'publicando' chegou a sair."""
    container = entry.get("container")
    if not container:
        return False
    try:
        return api("GET", container, {"fields": "status_code"}).get("status_code") == "PUBLISHED"
    except ApiError:
        return False


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #
def cmd_verificar(_args) -> int:
    posts, parse_errors = load_posts()
    state = load_state()
    now = datetime.now(TZ)
    problems = len(parse_errors)
    for err in parse_errors:
        print(f"ERRO  {err}")

    per_day: dict = {}
    print(f"{len(posts)} posts em {POSTS_CSV.name} (fuso {TZ.key})\n")
    for post in posts:
        errors, warnings = validate(post)
        problems += len(errors)
        status = state.get(post.key, {}).get("status")
        if status:
            tag = status
        elif post.when <= now - MAX_DELAY:
            tag = "passou"
        elif post.when <= now:
            tag = "na fila"
        else:
            tag = "agendado"
        per_day[post.when.date()] = per_day.get(post.when.date(), 0) + 1
        mark = "ERRO" if errors else "ok  "
        print(f"{mark}  {post.when.strftime('%d/%m/%Y %H:%M')}  {post.kind or post.kind_raw:<9} "
              f"{tag:<10} {', '.join(post.files)[:60]}")
        for e in errors:
            print(f"        -> {e}")
        for w in warnings:
            print(f"        aviso: {w}")

    busy = {d: n for d, n in per_day.items() if n > 25}
    for day, n in sorted(busy.items()):
        print(f"\naviso: {n} posts em {day.strftime('%d/%m')}. A API aceita até 100 por 24h, "
              "mas tanto post num dia só costuma derrubar o alcance.")
    print(f"\n{'Tudo certo.' if not problems else f'{problems} problema(s) para corrigir.'}")
    return 1 if problems else 0


def cmd_publicar(args) -> int:
    posts, parse_errors = load_posts()
    # Linha ilegível não derruba a rodada (senão o GitHub mandaria e-mail a cada 15 min);
    # o comando verificar acusa esses erros.
    for err in parse_errors:
        print(f"aviso: {err} (linha ignorada)")
    state = load_state()
    now = datetime.now(TZ)
    failed = False
    did_something = False

    for post in posts:
        if post.when > now:
            continue
        entry = state.get(post.key, {})
        status = entry.get("status")
        if status in ("publicado", "ignorado"):
            continue
        if status == "erro" and entry.get("tentativas", 0) >= MAX_ATTEMPTS:
            continue
        if now - post.when > MAX_DELAY and status is None:
            print(f"IGNORADO  {post.label()}: passou mais de {MAX_DELAY.total_seconds() / 3600:g}h do horário. "
                  "Mude a data/hora no CSV se ainda quiser publicar.")
            if not args.simular:
                state[post.key] = {"status": "ignorado", "linha": post.line, "atualizado": now_iso()}
                save_state(state)
            did_something = True
            continue

        did_something = True
        errors, _ = validate(post)
        if args.simular:
            result = "não publicaria: " + "; ".join(errors) if errors else "publicaria agora"
            print(f"SIMULAÇÃO  {post.label()}: {result}")
            continue

        if status == "publicando" and already_published(entry):
            entry.update(status="publicado", atualizado=now_iso())
            state[post.key] = entry
            save_state(state)
            print(f"OK  {post.label()}: já tinha saído na execução anterior")
            continue

        try:
            if errors:
                raise ApiError("; ".join(errors))
            print(f"Publicando {post.label()}...")
            result = publish(post, state)
            state[post.key] = {
                "status": "publicado", "linha": post.line, "atualizado": now_iso(), **result,
            }
            print(f"OK  {post.label()}: {result.get('permalink') or result['media_id']}")
        except ApiError as e:
            attempts = entry.get("tentativas", 0) + 1
            state[post.key] = {
                "status": "erro", "linha": post.line, "tentativas": attempts,
                "ultimo_erro": str(e), "atualizado": now_iso(),
            }
            final = " Não vou tentar de novo." if attempts >= MAX_ATTEMPTS else " Tento de novo na próxima rodada."
            print(f"FALHOU  {post.label()} (tentativa {attempts}/{MAX_ATTEMPTS}): {e}.{final}")
            failed = True
        save_state(state)
        time.sleep(2)

    if not did_something:
        upcoming = next((p for p in posts if p.when > now), None)
        when = upcoming.when.strftime("%d/%m %H:%M") if upcoming else "nenhum agendado"
        print(f"Nada para publicar agora. Próximo post: {when}.")
    return 1 if failed else 0


def cmd_conta(_args) -> int:
    if FACEBOOK_GRAPH:
        info = api("GET", ig_user_id(), {"fields": "username,name,followers_count"})
        print(f"Conta: @{info.get('username')} ({info.get('name', '')}, {info.get('followers_count', '?')} seguidores)")
        print(f"ID: {ig_user_id()}")
    else:
        me = api("GET", "me", {"fields": "user_id,username,account_type"})
        print(f"Conta: @{me.get('username')} ({me.get('account_type', 'tipo desconhecido')})")
        print(f"ID: {me.get('user_id') or me.get('id')}")
    try:
        limit = api("GET", f"{ig_user_id()}/content_publishing_limit", {"fields": "quota_usage,config"})
        item = (limit.get("data") or [{}])[0]
        total = (item.get("config") or {}).get("quota_total", "?")
        print(f"Publicações nas últimas 24h: {item.get('quota_usage', '?')} de {total}")
    except ApiError as e:
        print(f"Não consegui ler a cota: {e}")
    return 0


def cmd_renovar_token(_args) -> int:
    if FACEBOOK_GRAPH:
        print("Token do login unificado (usuário do sistema) não expira. Nada a renovar.", file=sys.stderr)
        return 0
    res = api("GET", "refresh_access_token", {"grant_type": "ig_refresh_token"}, versioned=False)
    days = int(res.get("expires_in", 0)) // 86400
    print(f"Token renovado, vale por mais {days} dias.", file=sys.stderr)
    print(res["access_token"])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Agendador de posts do Instagram")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verificar", help="confere posts.csv e as mídias")
    p = sub.add_parser("publicar", help="publica os posts que estão na hora")
    p.add_argument("--simular", action="store_true", help="só mostra o que faria")
    sub.add_parser("conta", help="mostra a conta conectada e a cota")
    sub.add_parser("renovar-token", help="gera um token novo")
    args = parser.parse_args()
    commands = {
        "verificar": cmd_verificar, "publicar": cmd_publicar,
        "conta": cmd_conta, "renovar-token": cmd_renovar_token,
    }
    try:
        return commands[args.cmd](args)
    except ApiError as e:
        print(f"Erro da API: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
